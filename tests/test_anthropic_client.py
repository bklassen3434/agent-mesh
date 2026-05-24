from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import pytest
from mesh_llm import AnthropicClient, AnthropicNotReadyError, LLMResponseError
from pydantic import BaseModel


class _Item(BaseModel):
    name: str
    score: float


def _stub_message(parsed: BaseModel | None, **usage: int) -> MagicMock:
    msg = MagicMock()
    msg.parsed_output = parsed
    msg.content = []
    msg.stop_reason = "end_turn"
    msg.usage = MagicMock(**usage)
    return msg


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> AnthropicClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("MESH_LLM_MODEL", "claude-haiku-4-5")
    return AnthropicClient()


def test_complete_with_latency_parses_pydantic(client: AnthropicClient) -> None:
    expected = _Item(name="GPT-4", score=0.91)
    client._client.messages.parse = MagicMock(  # type: ignore[method-assign]
        return_value=_stub_message(expected, input_tokens=100, output_tokens=20)
    )

    parsed, latency = client.complete_with_latency(
        name="extract", system="sys", user="usr", response_model=_Item
    )

    assert isinstance(parsed, _Item)
    assert parsed.name == "GPT-4"
    assert parsed.score == 0.91
    assert latency >= 0


def test_system_block_carries_cache_control(client: AnthropicClient) -> None:
    expected = _Item(name="X", score=1.0)
    parse = MagicMock(return_value=_stub_message(expected))
    client._client.messages.parse = parse  # type: ignore[method-assign]

    client.complete_with_latency("n", "the system prompt", "u", response_model=_Item)

    kwargs = parse.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["output_format"] is _Item
    system_blocks = kwargs["system"]
    assert isinstance(system_blocks, list) and len(system_blocks) == 1
    assert system_blocks[0]["text"] == "the system prompt"
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_messages_create_path_when_no_schema(client: AnthropicClient) -> None:
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hello world"
    msg = MagicMock()
    msg.content = [text_block]
    msg.usage = MagicMock(input_tokens=10, output_tokens=2)
    client._client.messages.create = MagicMock(return_value=msg)  # type: ignore[method-assign]

    result, _ = client.complete_with_latency("n", "sys", "usr")
    assert result == "hello world"


def test_parse_failure_raises_llmresponseerror(client: AnthropicClient) -> None:
    msg = _stub_message(parsed=None)
    msg.stop_reason = "refusal"
    refusal_block = MagicMock()
    refusal_block.type = "refusal"
    refusal_block.refusal = "Cannot comply"
    msg.content = [refusal_block]
    client._client.messages.parse = MagicMock(return_value=msg)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="no parsed output"):
        client.complete_with_latency("n", "s", "u", response_model=_Item)


def test_auth_error_maps_to_not_ready(client: AnthropicClient) -> None:
    err = anthropic.AuthenticationError(
        "bad key", response=MagicMock(status_code=401), body=None
    )
    client._client.messages.parse = MagicMock(side_effect=err)  # type: ignore[method-assign]

    with pytest.raises(AnthropicNotReadyError, match="rejected"):
        client.complete_with_latency("n", "s", "u", response_model=_Item)


def test_rate_limit_error_maps_to_not_ready(client: AnthropicClient) -> None:
    err = anthropic.RateLimitError(
        "slow down", response=MagicMock(status_code=429), body=None
    )
    client._client.messages.parse = MagicMock(side_effect=err)  # type: ignore[method-assign]

    with pytest.raises(AnthropicNotReadyError, match="rate limit"):
        client.complete_with_latency("n", "s", "u", response_model=_Item)


def test_health_check_returns_on_success(client: AnthropicClient) -> None:
    client._client.models.retrieve = MagicMock(return_value=MagicMock(id="claude-haiku-4-5"))  # type: ignore[method-assign]
    client.health_check()  # no exception


def test_health_check_unknown_model(client: AnthropicClient) -> None:
    err = anthropic.NotFoundError("nope", response=MagicMock(status_code=404), body=None)
    client._client.models.retrieve = MagicMock(side_effect=err)  # type: ignore[method-assign]

    with pytest.raises(AnthropicNotReadyError, match="not found"):
        client.health_check()


def test_max_tokens_override(client: AnthropicClient) -> None:
    expected = _Item(name="Y", score=0.5)
    parse = MagicMock(return_value=_stub_message(expected))
    client._client.messages.parse = parse  # type: ignore[method-assign]

    client.complete_with_latency(
        "n", "s", "u", response_model=_Item, options={"max_tokens": 4096}
    )
    assert parse.call_args.kwargs["max_tokens"] == 4096
