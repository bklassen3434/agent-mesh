"""Groq-hosted open-weight model client.

Speaks Groq's OpenAI-compatible chat-completions API directly over httpx — no
extra provider SDK. Exists as the cheap tier for tiered routing (Phase 20):
open-weight models on Groq (default ``openai/gpt-oss-120b``) serve the bulk of
traffic at a fraction of Anthropic list price, with escalation to an Anthropic
model handled by ``RoutedLLMClient``.

Structured output uses ``response_format={"type": "json_schema", ...}``. When
Groq's server-side schema validation rejects what the model generated it
returns a 400 with code ``json_validate_failed`` — surfaced here as
:class:`~mesh_llm.client.LLMResponseError` so the router's
escalate-on-parse-fail retry (cheap → strong) applies exactly as it does for a
local parse failure.

Groq's prompt caching is provider-side and automatic (cached input bills at
0.5x, not Anthropic's 0.1x). Usage reports the full prompt as ``input_tokens``
and leaves the cache fields zero, so estimated cost is a slight *over*estimate
when Groq serves a cached prefix.
"""
from __future__ import annotations

import os
import time
from typing import Any, TypeVar, overload

import httpx
from mesh_tracing.tracing import trace_generation
from pydantic import BaseModel
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from mesh_llm.client import (
    LLMProviderNotReadyError,
    LLMRateLimitedError,
    LLMResponseError,
)
from mesh_llm.pricing import estimate_cost, is_priced
from mesh_llm.usage import LLMUsage
from mesh_llm.usage_sink import UsageEvent, record_usage

T = TypeVar("T", bound=BaseModel)

_DEFAULT_MODEL = "openai/gpt-oss-120b"
_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
# Groq counts max_completion_tokens against the per-minute token limit (TPM)
# when admitting a request — a 16k reservation per call 413s the free tier
# (8k TPM) outright and burns paid-tier TPM under concurrency. Skill outputs
# (claim lists, critiques, answers) fit comfortably in 4k; callers needing
# more pass options={"max_tokens": ...}.
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TEMPERATURE = 0.1
_TIMEOUT_SECONDS = 120.0


class GroqNotReadyError(LLMProviderNotReadyError):
    """Raised when the Groq API is unreachable or credentials are invalid."""


def _should_retry(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError)


class GroqClient:
    """Groq-backed LLM client conforming to ``mesh_llm.protocol.LLMClient``.

    Same surface as ``AnthropicClient`` / ``OllamaClient``: static ``system``
    prompt + per-call ``user`` content, optional Pydantic ``response_model``
    enforced via Groq's ``json_schema`` response format.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        agent_name: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.agent_name = agent_name
        if model is not None:
            self.model = model
        else:
            from mesh_llm.factory import resolve_model

            self.model = resolve_model(agent_name, _DEFAULT_MODEL)
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise GroqNotReadyError(
                "GROQ_API_KEY is not set. Get one at https://console.groq.com/keys "
                "and put it in .env, or switch providers with MESH_LLM_PROVIDER."
            )
        self.base_url = (
            base_url or os.environ.get("GROQ_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._max_tokens = max_tokens
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT_SECONDS,
            transport=transport,
        )

    def health_check(self) -> None:
        """Verify the API key and that the configured model exists."""
        try:
            response = self._http.get(f"/models/{self.model}")
        except httpx.TransportError as exc:
            raise GroqNotReadyError(
                f"Cannot reach Groq API at {self.base_url}: {exc}"
            ) from exc
        if response.status_code == 401:
            raise GroqNotReadyError(
                "Groq API key was rejected. Check GROQ_API_KEY."
            )
        if response.status_code == 404:
            raise GroqNotReadyError(
                f"Groq model '{self.model}' was not found. Check the configured "
                "model id — see https://console.groq.com/docs/models."
            )
        if response.status_code >= 400:
            raise GroqNotReadyError(
                f"Groq health check failed ({response.status_code}): "
                f"{response.text[:300]}"
            )

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
        # Strip the reserved routing-metadata key (Phase 20) before building the
        # request body; forward it to tracing instead.
        opts = dict(options or {})
        route_meta = opts.pop("_route", None)
        max_tokens = opts.pop("max_tokens", self._max_tokens)
        temperature = opts.pop("temperature", _DEFAULT_TEMPERATURE)

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_model is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": response_model.model_json_schema(),
                },
            }

        start = time.monotonic()
        try:
            response = _post_with_retry(self._http, body)
        except RetryError as exc:
            raise GroqNotReadyError(
                f"Groq API at {self.base_url} did not respond after 3 attempts "
                "(connection error)."
            ) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code == 400 and "json_validate_failed" in response.text:
            # Groq's server-side schema validation rejected the generation —
            # semantically a parse failure, so the router may escalate.
            raise LLMResponseError(
                f"Groq schema-constrained generation failed for '{name}': "
                f"{response.text[:500]}"
            )
        if response.status_code == 401:
            raise GroqNotReadyError(
                "Groq API key was rejected. Check GROQ_API_KEY."
            )
        if response.status_code == 429 or response.status_code == 413:
            # 429 = over RPM/TPM/TPD; 413 = single request larger than the
            # tier's TPM admission budget. Both are capacity, not config —
            # distinguishable so the router can escalate instead of failing.
            raise LLMRateLimitedError(
                f"Groq rate limit exceeded ({response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise GroqNotReadyError(
                f"Groq API error {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        try:
            raw = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                f"Malformed Groq response for '{name}': missing "
                f"choices[0].message.content ({exc})"
            ) from exc

        raw_usage = data.get("usage") or {}
        usage = LLMUsage(
            model=self.model,
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
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
        record_usage(
            UsageEvent(
                name=name,
                model=self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cost_usd=cost.total_cost if is_priced(self.model) else 0.0,
            )
        )

        if response_model is None:
            return raw, latency_ms, usage

        try:
            return response_model.model_validate_json(raw), latency_ms, usage
        except Exception as exc:
            raise LLMResponseError(
                f"Failed to parse LLM response for '{name}' into "
                f"{response_model.__name__}: {exc}\nRaw: {raw[:500]}"
            ) from exc


@retry(
    retry=retry_if_exception(_should_retry),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=False,
)
def _post_with_retry(client: httpx.Client, body: dict[str, Any]) -> httpx.Response:
    return client.post("/chat/completions", json=body)
