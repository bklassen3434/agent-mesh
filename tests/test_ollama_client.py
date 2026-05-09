from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mesh_llm.client import LLMResponseError, OllamaClient, OllamaNotReadyError


class FakeModel:
    def __init__(self, name: str) -> None:
        self.model = name


class FakeListResponse:
    def __init__(self, model_names: list[str]) -> None:
        self.models = [FakeModel(n) for n in model_names]


class FakeChatResponse:
    def __init__(self, content: str) -> None:
        self.message = MagicMock()
        self.message.content = content


def _make_client(model: str = "qwen3:14b") -> OllamaClient:
    client = OllamaClient(model=model, host="http://localhost:11434")
    return client


class TestHealthCheck:
    def test_success(self) -> None:
        client = _make_client()
        with patch.object(client._client, "list", return_value=FakeListResponse(["qwen3:14b"])):
            client.health_check()  # should not raise

    def test_model_missing_raises(self) -> None:
        client = _make_client()
        with (
            patch.object(client._client, "list", return_value=FakeListResponse(["llama3:8b"])),
            pytest.raises(OllamaNotReadyError, match="ollama pull"),
        ):
            client.health_check()

    def test_connection_refused_raises(self) -> None:
        client = _make_client()
        with (
            patch.object(client._client, "list", side_effect=ConnectionRefusedError),
            pytest.raises(OllamaNotReadyError, match="ollama serve"),
        ):
            client.health_check()

    def test_model_absent_shows_pull_command(self) -> None:
        client = _make_client("qwen3:14b")
        with patch.object(client._client, "list", return_value=FakeListResponse([])):
            with pytest.raises(OllamaNotReadyError) as exc_info:
                client.health_check()
            assert "qwen3:14b" in str(exc_info.value)


class TestComplete:
    def test_text_response(self) -> None:
        client = _make_client()
        with patch.object(
            client._client, "chat", return_value=FakeChatResponse("Hello world")
        ):
            result = client.complete("test", "system", "user")
        assert result == "Hello world"

    def test_structured_output_happy_path(self) -> None:
        from pydantic import BaseModel

        class MyModel(BaseModel):
            value: int

        client = _make_client()
        with patch.object(
            client._client, "chat", return_value=FakeChatResponse('{"value": 42}')
        ):
            result = client.complete("test", "system", "user", response_model=MyModel)
        assert isinstance(result, MyModel)
        assert result.value == 42

    def test_parse_failure_raises_llm_response_error(self) -> None:
        from pydantic import BaseModel

        class MyModel(BaseModel):
            value: int

        client = _make_client()
        with patch.object(
            client._client, "chat", return_value=FakeChatResponse("not valid json at all")
        ), pytest.raises(LLMResponseError):
            client.complete("test", "system", "user", response_model=MyModel)

    def test_connection_error_retries(self) -> None:
        client = _make_client()
        call_count = 0

        def raise_then_succeed(**kwargs: object) -> FakeChatResponse:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionRefusedError("refused")
            return FakeChatResponse("ok")

        with patch.object(client._client, "chat", side_effect=raise_then_succeed):
            result = client.complete("test", "system", "user")
        assert result == "ok"
        assert call_count == 3

    def test_connection_error_exhausted_raises(self) -> None:
        client = _make_client()
        with patch.object(
            client._client, "chat", side_effect=ConnectionRefusedError("always refused")
        ), pytest.raises(OllamaNotReadyError):
            client.complete("test", "system", "user")

    def test_complete_with_latency_returns_tuple(self) -> None:
        client = _make_client()
        with patch.object(
            client._client, "chat", return_value=FakeChatResponse("pong")
        ):
            result, latency = client.complete_with_latency("test", "system", "user")
        assert result == "pong"
        assert isinstance(latency, int)
        assert latency >= 0
