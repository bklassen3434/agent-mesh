"""Anthropic Message Batches support types (Phase 11d).

The skeptic sweep submits all belief evaluations as one batch (50% cheaper,
async). These dataclasses are the provider-agnostic surface the coordinator
uses; the Anthropic-specific submission/collection lives on ``AnthropicClient``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mesh_llm.usage import LLMUsage


@dataclass
class BatchRequestItem:
    """One request in a batch. ``custom_id`` maps results back to the caller's
    unit of work (e.g. a belief id). ``response_model`` (a Pydantic class) is
    enforced via a forced tool so batch output is schema-validated."""

    custom_id: str
    system: str
    user: str
    max_tokens: int = 4096


@dataclass
class BatchItemResult:
    """Collected result for one ``custom_id``. ``parsed`` is the validated
    response-model instance, or None when the request errored / expired /
    failed schema validation (``error`` explains)."""

    custom_id: str
    parsed: Any | None
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    error: str | None = None
