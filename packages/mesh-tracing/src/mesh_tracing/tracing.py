from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class LangfuseConfig:
    public_key: str
    secret_key: str
    host: str

    @classmethod
    def from_env(cls) -> LangfuseConfig | None:
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
        host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
        if not public_key or not secret_key:
            return None
        return cls(public_key=public_key, secret_key=secret_key, host=host)


@contextmanager
def traced(operation_name: str) -> Generator[None, None, None]:
    config = LangfuseConfig.from_env()
    if config is None:
        yield
        return

    try:
        import langfuse  # type: ignore[import-not-found]

        client = langfuse.Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            host=config.host,
        )
        trace = client.trace(name=operation_name)
        try:
            yield
        finally:
            trace.update(status="success")
            client.flush()
    except ImportError:
        yield


def trace_generation(
    name: str,
    model: str,
    messages: list[dict[str, str]],
    output: str,
    latency_ms: int,
) -> None:
    """Record a completed LLM generation to Langfuse with full prompt/output/timing.

    No-ops silently when Langfuse env vars are absent or the package is not installed.
    Never raises — tracing must not break the pipeline.
    """
    config = LangfuseConfig.from_env()
    if config is None:
        return
    try:
        from datetime import UTC, datetime, timedelta

        import langfuse

        lf = langfuse.Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            host=config.host,
        )
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(milliseconds=latency_ms)
        trace = lf.trace(name=name)
        trace.generation(
            name=name,
            model=model,
            input=messages,
            output=output,
            start_time=start_time,
            end_time=end_time,
        )
        lf.flush()
    except ImportError:
        pass
    except Exception:
        pass
