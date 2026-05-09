from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class BaseAgent(ABC):
    """Abstract base for all Phase 1 agents.

    Phase 2 will subclass this to add A2A server scaffolding without
    modifying agent logic.
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
