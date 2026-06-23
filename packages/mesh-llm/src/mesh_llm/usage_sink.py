"""Per-dispatch LLM usage capture via a contextvar sink.

The deterministic controller needs to attribute every LLM call a skill makes to
*that* dispatch, so it can persist the cost ledger (``runtime.llm_usage``) and
the per-invocation cost (``agents.agent_invocations``). Skills return only
``Effect``s — they can't hand usage back through their return type. So instead
the LLM clients append a :class:`UsageEvent` to a contextvar sink on every
completion, and the controller opens a fresh sink around each dispatch and drains
it afterwards.

Why a contextvar works across the controller's concurrency:

* The sync clients run inside ``asyncio.to_thread``, which **copies the current
  context**; the sink is a mutable list shared by reference across that copy, so
  appends made inside the thread are visible to the opener.
* Each concurrent dispatch is its own ``asyncio`` task with its own copied
  context, so sinks never cross-contaminate — usage lands in the dispatch that
  opened the sink, even with many dispatches in flight.
* When no sink is open (the default), :func:`record_usage` is a no-op, so direct
  / library callers and tests pay nothing and behave exactly as before.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class UsageEvent:
    """One LLM completion's token + cost usage, as recorded by a client."""

    name: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float


_SINK: contextvars.ContextVar[list[UsageEvent] | None] = contextvars.ContextVar(
    "mesh_llm_usage_sink", default=None
)


def open_sink() -> list[UsageEvent]:
    """Start a fresh usage sink in the current context; return its backing list.

    Call this at the top of a unit of work (one controller dispatch) *before*
    spawning any child tasks, then read the returned list once they finish."""
    events: list[UsageEvent] = []
    _SINK.set(events)
    return events


def record_usage(event: UsageEvent) -> None:
    """Append one completion's usage to the open sink, if any (else no-op)."""
    sink = _SINK.get()
    if sink is not None:
        sink.append(event)
