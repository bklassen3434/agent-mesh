"""Phase 11d: Anthropic batch submit + collect (custom_id matching)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from anthropic.types import ToolUseBlock
from mesh_llm import AnthropicClient, BatchRequestItem
from pydantic import BaseModel


class _Verdict(BaseModel):
    verdict: str
    score: float


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> AnthropicClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("MESH_LLM_MODEL", "claude-haiku-4-5")
    return AnthropicClient()


def _succeeded(custom_id: str, payload: dict[str, object]) -> SimpleNamespace:
    block = ToolUseBlock(type="tool_use", id="t1", name="emit__verdict", input=payload)
    message = SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(
            input_tokens=120, output_tokens=30,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="succeeded", message=message),
    )


def _errored(custom_id: str) -> SimpleNamespace:
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored"))


def test_submit_batch_builds_forced_tool_requests(client: AnthropicClient) -> None:
    captured: dict[str, object] = {}

    def _create(requests: list[dict[str, Any]]) -> SimpleNamespace:
        captured["requests"] = requests
        return SimpleNamespace(id="batch_abc")

    client._client.messages.batches.create = MagicMock(side_effect=_create)  # type: ignore[method-assign]

    items = [
        BatchRequestItem(custom_id="bel-1", system="SYS", user="U1"),
        BatchRequestItem(custom_id="bel-2", system="SYS", user="U2"),
    ]
    batch_id = client.submit_batch(items, _Verdict)

    assert batch_id == "batch_abc"
    reqs = cast(list[dict[str, Any]], captured["requests"])
    assert [r["custom_id"] for r in reqs] == ["bel-1", "bel-2"]
    params = reqs[0]["params"]
    assert params["tool_choice"] == {"type": "tool", "name": "emit__verdict"}
    assert params["tools"][0]["name"] == "emit__verdict"
    # system carries a cache_control marker (caching stacks with batch)
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_collect_batch_matches_by_custom_id(client: AnthropicClient) -> None:
    # Results come back out of order; collect must key by custom_id.
    client._client.messages.batches.results = MagicMock(  # type: ignore[method-assign]
        return_value=iter([
            _succeeded("bel-2", {"verdict": "weakened", "score": 0.6}),
            _errored("bel-3"),
            _succeeded("bel-1", {"verdict": "supported", "score": 0.9}),
        ])
    )

    out = client.collect_batch("batch_abc", _Verdict)

    assert set(out) == {"bel-1", "bel-2", "bel-3"}
    bel1, bel2 = out["bel-1"].parsed, out["bel-2"].parsed
    assert bel1 is not None and bel2 is not None
    assert bel1.verdict == "supported"
    assert bel1.score == 0.9
    assert bel2.verdict == "weakened"
    assert out["bel-1"].usage.input_tokens == 120
    # errored item: no parse, error recorded
    assert out["bel-3"].parsed is None
    assert "errored" in (out["bel-3"].error or "")


def test_collect_batch_records_schema_failure(client: AnthropicClient) -> None:
    client._client.messages.batches.results = MagicMock(  # type: ignore[method-assign]
        return_value=iter([_succeeded("bel-1", {"verdict": "weakened"})])  # missing 'score'
    )
    out = client.collect_batch("batch_abc", _Verdict)
    assert out["bel-1"].parsed is None
    assert "schema validation failed" in (out["bel-1"].error or "")


def test_batch_status_passthrough(client: AnthropicClient) -> None:
    client._client.messages.batches.retrieve = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(processing_status="ended")
    )
    assert client.batch_status("batch_abc") == "ended"
