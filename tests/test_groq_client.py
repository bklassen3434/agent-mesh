"""GroqClient behaviour against a mock HTTP transport — no network, no SDK."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mesh_llm import GroqClient, GroqNotReadyError, LLMResponseError
from pydantic import BaseModel


class _Answer(BaseModel):
    verdict: str
    score: float


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MESH_LLM_MODEL", "MESH_LLM_MODEL_DEFAULT", "GROQ_API_KEY", "GROQ_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def _client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> GroqClient:
    return GroqClient(
        api_key="gsk-test", transport=httpx.MockTransport(handler), **kwargs
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GroqNotReadyError, match="GROQ_API_KEY"):
        GroqClient()


def test_default_model_and_base_url() -> None:
    client = _client(lambda request: _completion("hi"))
    assert client.model == "openai/gpt-oss-120b"
    assert client.base_url == "https://api.groq.com/openai/v1"


def test_plain_completion_returns_text_latency_usage() -> None:
    client = _client(lambda request: _completion("hello world"))
    result, latency_ms, usage = client.complete_with_usage("t", "sys", "user text")
    assert result == "hello world"
    assert latency_ms >= 0
    assert usage.model == "openai/gpt-oss-120b"
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 0


def test_structured_output_parses_and_sends_json_schema() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _completion('{"verdict": "supported", "score": 0.9}')

    client = _client(handler)
    result, _ = client.complete_with_latency("t", "sys", "user", _Answer)
    assert isinstance(result, _Answer)
    assert result.verdict == "supported"

    assert seen["model"] == "openai/gpt-oss-120b"
    assert seen["max_completion_tokens"] == 4096
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["name"] == "_Answer"
    assert "properties" in seen["response_format"]["json_schema"]["schema"]
    assert seen["messages"][0] == {"role": "system", "content": "sys"}


def test_route_meta_is_stripped_from_request_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _completion("ok")

    client = _client(handler)
    client.complete("t", "sys", "user", options={"_route": {"tier": "cheap"}})
    assert "_route" not in seen


def test_unparseable_structured_output_raises_response_error() -> None:
    client = _client(lambda request: _completion("not json at all"))
    with pytest.raises(LLMResponseError, match="Failed to parse"):
        client.complete("t", "sys", "user", _Answer)


def test_json_validate_failed_is_a_parse_failure() -> None:
    # Groq's server-side schema validation failure must surface as
    # LLMResponseError so the router's cheap→strong escalation applies.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "json_validate_failed", "message": "bad"}},
        )

    client = _client(handler)
    with pytest.raises(LLMResponseError, match="schema-constrained"):
        client.complete("t", "sys", "user", _Answer)


def test_rejected_key_raises_not_ready() -> None:
    client = _client(lambda request: httpx.Response(401, json={"error": "nope"}))
    with pytest.raises(GroqNotReadyError, match="rejected"):
        client.complete("t", "sys", "user")


def test_rate_limit_raises_not_ready() -> None:
    client = _client(lambda request: httpx.Response(429, json={"error": "slow down"}))
    with pytest.raises(GroqNotReadyError, match="rate limit"):
        client.complete("t", "sys", "user")


def test_health_check_ok_and_missing_model() -> None:
    ok = _client(lambda request: httpx.Response(200, json={"id": "openai/gpt-oss-120b"}))
    ok.health_check()  # no raise

    missing = _client(lambda request: httpx.Response(404, json={"error": "no model"}))
    with pytest.raises(GroqNotReadyError, match="not found"):
        missing.health_check()
