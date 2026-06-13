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
        import langfuse

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
    *,
    usage: dict[str, int] | None = None,
    cost_usd: float | None = None,
    agent_name: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Record a completed LLM generation to Langfuse with full prompt/output/timing.

    ``usage`` is a provider-agnostic token dict (keys: input_tokens,
    output_tokens, cache_read_tokens, cache_creation_tokens). When provided it
    is attached to the Langfuse generation so per-agent / per-skill token cost
    is queryable; ``cost_usd`` (computed from list prices upstream) is recorded
    as the generation's total cost, and ``agent_name`` + skill (``name``) land
    in metadata for attribution. ``metadata`` adds extra key/values to the
    generation metadata (Phase 20 routing attaches tier + escalation reason
    here, so per-tier volume and escalation rate are queryable).

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
        trace_name = f"{agent_name}:{name}" if agent_name else name
        trace = lf.trace(name=trace_name)

        gen_kwargs: dict[str, object] = {
            "name": name,
            "model": model,
            "input": messages,
            "output": output,
            "start_time": start_time,
            "end_time": end_time,
            "metadata": {
                "agent": agent_name,
                "skill": name,
                **(metadata or {}),
                **(
                    {
                        "cache_read_tokens": usage.get("cache_read_tokens", 0),
                        "cache_creation_tokens": usage.get("cache_creation_tokens", 0),
                    }
                    if usage
                    else {}
                ),
            },
        }
        if usage is not None:
            # Langfuse v2 Usage dict. "input"/"output"/"total" are token counts;
            # cache reads/writes are billed input, folded into the input count so
            # the cost the dashboard shows matches our list-price computation.
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_tokens", 0)
                + usage.get("cache_creation_tokens", 0)
            )
            output_tokens = usage.get("output_tokens", 0)
            usage_payload: dict[str, object] = {
                "input": input_tokens,
                "output": output_tokens,
                "total": input_tokens + output_tokens,
                "unit": "TOKENS",
            }
            if cost_usd is not None:
                usage_payload["total_cost"] = cost_usd
            gen_kwargs["usage"] = usage_payload

        trace.generation(**gen_kwargs)
        lf.flush()
    except ImportError:
        pass
    except Exception:
        pass
