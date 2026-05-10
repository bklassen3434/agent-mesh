from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from starlette.applications import Starlette


class BaseAgent(ABC):
    """Abstract base for all mesh agents.

    Phase 1 subclasses implement run() for in-process use.
    Phase 2 subclasses also implement to_a2a_server() for A2A deployment.
    """

    name: str

    def __init__(
        self,
        llm: Any | None = None,
        db_conn: Any | None = None,
    ) -> None:
        self.llm = llm
        self.db_conn = db_conn

    @abstractmethod
    async def run(self, input: BaseModel) -> BaseModel:
        ...

    def to_a2a_server(self, url: str) -> Starlette:  # pragma: no cover
        raise NotImplementedError(f"{self.__class__.__name__} has no A2A server factory")
