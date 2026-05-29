from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from mesh_llm.usage import LLMUsage

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMClient(Protocol):
    """Structural contract shared by OllamaClient and AnthropicClient.

    Both clients accept a static `system` prompt plus per-call `user` content, and
    optionally validate the response against a Pydantic model. The factory in
    `mesh_llm.factory` returns one or the other based on `MESH_LLM_PROVIDER`.
    """

    model: str

    def health_check(self) -> None: ...

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int]: ...

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type[T] | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int, LLMUsage]:
        """Like ``complete_with_latency`` but also returns token usage.

        Callers that persist per-call cost (the coordinator / skeptic sweep)
        use this; everything else can stay on ``complete_with_latency``.
        """
        ...
