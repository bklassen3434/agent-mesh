from __future__ import annotations

import logging
import os
import time
from typing import Any, TypeVar, overload

import anthropic
from anthropic.types import MessageParam, TextBlockParam
from mesh_tracing.tracing import trace_generation
from pydantic import BaseModel
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from mesh_llm.client import LLMProviderNotReadyError, LLMResponseError

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
    ) -> None:
        self.model = model or os.environ.get("MESH_LLM_MODEL", _DEFAULT_MODEL)
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
        result, _ = self.complete_with_latency(name, system, user, response_model, options)
        return result

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str | T, int]:
        max_tokens = (options or {}).get("max_tokens", self._max_tokens)

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

        usage = getattr(message, "usage", None)
        if usage is not None:
            logger.debug(
                "anthropic_usage",
                extra={
                    "name": name,
                    "model": self.model,
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "cache_creation_input_tokens": getattr(
                        usage, "cache_creation_input_tokens", None
                    ),
                    "cache_read_input_tokens": getattr(
                        usage, "cache_read_input_tokens", None
                    ),
                },
            )

        trace_generation(
            name=name,
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            output=raw,
            latency_ms=latency_ms,
        )

        if response_model is not None:
            return parsed, latency_ms
        return raw, latency_ms


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
