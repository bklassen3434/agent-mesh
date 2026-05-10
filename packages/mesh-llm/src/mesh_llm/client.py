from __future__ import annotations

import os
import time
from typing import Any, TypeVar, overload

import ollama
from mesh_tracing.tracing import trace_generation
from pydantic import BaseModel
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

T = TypeVar("T", bound=BaseModel)

_DEFAULT_MODEL = "qwen3:8b"
_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_OPTIONS: dict[str, Any] = {"temperature": 0.1}


class LLMResponseError(Exception):
    """Raised when the LLM returns output that cannot be parsed into the expected schema."""


class OllamaNotReadyError(Exception):
    """Raised when Ollama is unreachable or the required model is not pulled."""


def _is_connection_error(exc: BaseException) -> bool:
    try:
        import httpx

        if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
            return True
    except ImportError:
        pass
    return isinstance(exc, (ConnectionRefusedError, ConnectionError, TimeoutError))


def _should_retry(exc: BaseException) -> bool:
    return _is_connection_error(exc)


class OllamaClient:
    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        self.model = model or os.environ.get("MESH_LLM_MODEL", _DEFAULT_MODEL)
        self.host = host or os.environ.get("OLLAMA_HOST", _DEFAULT_HOST)
        self._client = ollama.Client(host=self.host)

    def health_check(self) -> None:
        """Verify Ollama is running and the configured model is available."""
        try:
            models_response = self._client.list()
        except Exception as exc:
            if _is_connection_error(exc):
                raise OllamaNotReadyError(
                    f"Cannot connect to Ollama at {self.host}. "
                    "Run: ollama serve"
                ) from exc
            raise OllamaNotReadyError(f"Ollama health check failed: {exc}") from exc

        available = [m.model for m in models_response.models if m.model is not None]
        matched = any(m == self.model or m.startswith(self.model) for m in available)
        if not matched:
            raise OllamaNotReadyError(
                f"Model '{self.model}' is not available in Ollama "
                f"(available: {available or 'none'}). "
                f"Run: ollama pull {self.model}"
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
        """Call the LLM. Returns str or parsed Pydantic model when response_model given."""
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
        """Like complete(), but also returns latency in milliseconds."""
        merged_options = {**_DEFAULT_OPTIONS, **(options or {})}
        schema = response_model.model_json_schema() if response_model is not None else None

        start = time.monotonic()
        try:
            raw = _chat_with_retry(
                self._client, self.model, schema, merged_options, name, system, user
            )
        except RetryError as exc:
            raise OllamaNotReadyError(
                f"Ollama at {self.host} did not respond after 3 attempts. "
                "Is Ollama running? Run: ollama serve"
            ) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

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

        if response_model is None:
            return raw, latency_ms

        try:
            return response_model.model_validate_json(raw), latency_ms
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
def _chat_with_retry(
    client: ollama.Client,
    model: str,
    schema: dict[str, Any] | None,
    options: dict[str, Any],
    name: str,
    system: str,
    user: str,
) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": options,
    }
    if schema is not None:
        kwargs["format"] = schema
    response = client.chat(**kwargs)
    return response.message.content  # type: ignore[no-any-return]
