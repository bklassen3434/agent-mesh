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
