from __future__ import annotations

import logging
import os
import time
from typing import Any, TypeVar, overload

import anthropic
from anthropic.types import MessageParam, TextBlockParam, ToolUseBlock
from mesh_tracing.tracing import trace_generation
from pydantic import BaseModel
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from mesh_llm.batch import BatchItemResult, BatchRequestItem
from mesh_llm.client import LLMProviderNotReadyError, LLMResponseError
from mesh_llm.pricing import estimate_cost, is_priced
from mesh_llm.usage import LLMUsage

T = TypeVar("T", bound=BaseModel)

_DEFAULT_MODEL = "claude-haiku-4-5"
_DEFAULT_MAX_TOKENS = 16000

logger = logging.getLogger(__name__)


class AnthropicNotReadyError(LLMProviderNotReadyError):
    """Raised when the Anthropic API is unreachable or credentials are invalid."""


def _is_connection_error(exc: BaseException) -> bool:
    return isinstance(exc, anthropic.APIConnectionError)


def _should_retry(exc: BaseException) -> bool:
    return _is_connection_error(exc)


class AnthropicClient:
    """Anthropic-API-backed LLM client conforming to mesh_llm.protocol.LLMClient.

    Uses `messages.parse()` for Pydantic-typed structured output. The system
    prompt is sent with `cache_control={"type": "ephemeral"}` so that repeated
    calls within a pipeline run reuse the cached prefix.

    Caveat: prompt caching has a model-specific minimum cacheable prefix
    (4096 tokens on Haiku 4.5). The marker is harmless when the system prompt
    is shorter than that — caching simply will not fire. Verify with
    `usage.cache_read_input_tokens` if you grow the prompt past the threshold.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        agent_name: str | None = None,
    ) -> None:
        self.agent_name = agent_name
        if model is not None:
            self.model = model
        else:
            from mesh_llm.factory import resolve_model
            self.model = resolve_model(agent_name, _DEFAULT_MODEL)
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise AnthropicNotReadyError(
                "ANTHROPIC_API_KEY is not set. Get one at https://console.anthropic.com "
                "and put it in .env, or switch providers with MESH_LLM_PROVIDER=ollama."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._max_tokens = max_tokens

    def health_check(self) -> None:
        """Verify the API key and that the configured model exists."""
        try:
            self._client.models.retrieve(self.model)
        except anthropic.AuthenticationError as exc:
            raise AnthropicNotReadyError(
                "Anthropic API key was rejected. Check ANTHROPIC_API_KEY."
            ) from exc
        except anthropic.NotFoundError as exc:
            raise AnthropicNotReadyError(
                f"Anthropic model '{self.model}' was not found. "
                "Check MESH_LLM_MODEL — see docs.claude.com for valid IDs."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AnthropicNotReadyError(
                f"Cannot reach Anthropic API: {exc}"
            ) from exc

    @overload
    def complete(
        self,
        name: str,
        system: str,
        user: str,
        response_model: None = None,
        options: dict[str, Any] | None = None,
    ) -> str: ...

    @overload
    def complete(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T],
        options: dict[str, Any] | None = None,
    ) -> T: ...

    def complete(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T] | None = None,
        options: dict[str, Any] | None = None,
    ) -> str | T:
        result, _, _ = self._complete(name, system, user, response_model, options)
        return result

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str | T, int]:
        result, latency_ms, _ = self._complete(
            name, system, user, response_model, options
        )
        return result, latency_ms

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str | T, int, LLMUsage]:
        return self._complete(name, system, user, response_model, options)

    def _complete(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str | T, int, LLMUsage]:
        max_tokens = (options or {}).get("max_tokens", self._max_tokens)
        # Routing decision metadata (Phase 20), if a RoutedLLMClient wrapped this
        # call. Reserved, namespaced key; harmless/absent on direct calls.
        route_meta = (options or {}).get("_route")

        # System prompt is cached on the assumption it's identical across calls
        # within a 5-minute window. Below the model's minimum cacheable prefix
        # the marker is a no-op (see class docstring).
        system_blocks: list[TextBlockParam] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        start = time.monotonic()
        try:
            message = _call_with_retry(
                self._client,
                self.model,
                max_tokens,
                system_blocks,
                user,
                response_model,
            )
        except RetryError as exc:
            raise AnthropicNotReadyError(
                "Anthropic API did not respond after 3 attempts (connection error)."
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise AnthropicNotReadyError(
                "Anthropic API key was rejected. Check ANTHROPIC_API_KEY."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AnthropicNotReadyError(
                "Anthropic rate limit exceeded; back off and retry later."
            ) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        if response_model is not None:
            parsed = message.parsed_output
            if parsed is None:
                # parse() returns None on refusals or schema validation failure.
                stop_reason = getattr(message, "stop_reason", None)
                refusal = "; ".join(
                    block.refusal for block in message.content
                    if getattr(block, "type", None) == "refusal"
                )
                raise LLMResponseError(
                    f"Anthropic returned no parsed output for '{name}' "
                    f"(stop_reason={stop_reason}, refusal={refusal or '<none>'})."
                )
            raw = parsed.model_dump_json()
        else:
            raw = "".join(
                block.text for block in message.content
                if getattr(block, "type", None) == "text"
            )

        raw_usage = getattr(message, "usage", None)
        usage = LLMUsage(
            model=self.model,
            input_tokens=int(getattr(raw_usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(raw_usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(
                getattr(raw_usage, "cache_read_input_tokens", 0) or 0
            ),
            cache_creation_tokens=int(
                getattr(raw_usage, "cache_creation_input_tokens", 0) or 0
            ),
        )
        if raw_usage is not None:
            logger.debug(
                "anthropic_usage",
                extra={
                    "name": name,
                    "model": self.model,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_creation_input_tokens": usage.cache_creation_tokens,
                    "cache_read_input_tokens": usage.cache_read_tokens,
                },
            )

        cost = estimate_cost(self.model, usage)
        trace_generation(
            name=name,
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            output=raw,
            latency_ms=latency_ms,
            usage=usage.model_dump(),
            cost_usd=cost.total_cost if is_priced(self.model) else None,
            agent_name=self.agent_name,
            metadata=route_meta if isinstance(route_meta, dict) else None,
        )

        if response_model is not None:
            return parsed, latency_ms, usage
        return raw, latency_ms, usage

    # ── Message Batches (Phase 11d) ────────────────────────────────────────

    def submit_batch(
        self, items: list[BatchRequestItem], response_model: type[T]
    ) -> str:
        """Submit one Message Batch and return its id. Structured output is
        enforced per request via a forced tool built from ``response_model``."""
        tool = _tool_for(response_model)
        requests: list[dict[str, Any]] = [
            {
                "custom_id": item.custom_id,
                "params": {
                    "model": self.model,
                    "max_tokens": item.max_tokens,
                    "system": [
                        {
                            "type": "text",
                            "text": item.system,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": [{"role": "user", "content": item.user}],
                    "tools": [tool],
                    "tool_choice": {"type": "tool", "name": tool["name"]},
                },
            }
            for item in items
        ]
        batch = self._client.messages.batches.create(requests=requests)  # type: ignore[arg-type]
        return batch.id

    def batch_status(self, batch_id: str) -> str:
        """Return the batch processing_status: in_progress | ended | canceling."""
        return self._client.messages.batches.retrieve(batch_id).processing_status

    def collect_batch(
        self, batch_id: str, response_model: type[T]
    ) -> dict[str, BatchItemResult]:
        """Download results and validate each against ``response_model``,
        keyed by custom_id. Failed/errored/expired items get parsed=None."""
        out: dict[str, BatchItemResult] = {}
        tool_name = _tool_name(response_model)
        for resp in self._client.messages.batches.results(batch_id):
            cid = resp.custom_id
            result = resp.result
            if result.type != "succeeded":
                out[cid] = BatchItemResult(
                    custom_id=cid, parsed=None, model=self.model,
                    error=f"batch result type={result.type}",
                )
                continue
            msg = result.message
            raw_usage = msg.usage
            usage = LLMUsage(
                model=self.model,
                input_tokens=int(getattr(raw_usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(raw_usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0),
                cache_creation_tokens=int(
                    getattr(raw_usage, "cache_creation_input_tokens", 0) or 0
                ),
            )
            parsed: Any | None = None
            error: str | None = None
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.name == tool_name:
                    try:
                        parsed = response_model.model_validate(block.input)
                    except Exception as exc:  # schema drift — record, don't raise
                        error = f"schema validation failed: {exc}"
                    break
            if parsed is None and error is None:
                error = "no matching tool_use block in batch response"
            out[cid] = BatchItemResult(
                custom_id=cid, parsed=parsed, usage=usage, model=self.model, error=error
            )
        return out


def _tool_name(response_model: type[BaseModel]) -> str:
    return f"emit_{response_model.__name__.lower()}"


def _tool_for(response_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "name": _tool_name(response_model),
        "description": f"Emit a single {response_model.__name__} object.",
        "input_schema": response_model.model_json_schema(),
    }


@retry(
    retry=retry_if_exception(_should_retry),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=False,
)
def _call_with_retry(
    client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    system_blocks: list[TextBlockParam],
    user: str,
    response_model: type[BaseModel] | None,
) -> Any:
    messages: list[MessageParam] = [{"role": "user", "content": user}]
    if response_model is not None:
        return client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
            output_format=response_model,
        )
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=messages,
    )
