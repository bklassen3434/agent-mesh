"""The contextvar usage sink the controller uses to attribute LLM spend to a
dispatch. The load-bearing properties: it captures across ``asyncio.to_thread``
(the sync LLM clients run there) and it isolates concurrent dispatches."""
from __future__ import annotations

import asyncio

from mesh_llm.usage_sink import UsageEvent, open_sink, record_usage


def _evt(name: str, cost: float = 0.01) -> UsageEvent:
    return UsageEvent(
        name=name,
        model="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=cost,
    )


def test_record_without_open_sink_is_noop() -> None:
    # No sink in this context → recording must not raise and must drop silently.
    record_usage(_evt("orphan"))


def test_open_sink_captures_direct_record() -> None:
    sink = open_sink()
    record_usage(_evt("a"))
    record_usage(_evt("b"))
    assert [e.name for e in sink] == ["a", "b"]


def test_capture_across_to_thread() -> None:
    """A sink opened in the async context must capture usage recorded inside a
    worker thread — asyncio.to_thread copies the context and the sink list is
    shared by reference, so appends in the thread are visible to the opener."""

    async def go() -> list[UsageEvent]:
        sink = open_sink()

        def sync_llm_call() -> None:  # mimics the sync client inside the skill
            record_usage(_evt("from-thread"))

        await asyncio.to_thread(sync_llm_call)
        return sink

    captured = asyncio.run(go())
    assert [e.name for e in captured] == ["from-thread"]


def test_concurrent_dispatches_have_isolated_sinks() -> None:
    """Each concurrent task opens its own sink; usage never crosses between
    them, even when they run interleaved (the controller fans dispatches out
    with asyncio.gather)."""

    async def dispatch(tag: str) -> list[str]:
        sink = open_sink()

        def work() -> None:
            record_usage(_evt(tag))

        await asyncio.to_thread(work)
        await asyncio.sleep(0)  # force interleaving
        return [e.name for e in sink]

    async def go() -> list[list[str]]:
        return await asyncio.gather(dispatch("x"), dispatch("y"), dispatch("z"))

    results = asyncio.run(go())
    # Each dispatch sees only its own event — no cross-contamination.
    assert sorted(r[0] for r in results) == ["x", "y", "z"]
    assert all(len(r) == 1 for r in results)
