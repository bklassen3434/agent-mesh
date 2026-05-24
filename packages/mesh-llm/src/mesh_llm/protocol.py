from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

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
