"""W3C traceparent helpers for distributed tracing across the mesh."""
from __future__ import annotations

import secrets
import uuid


def new_traceparent(trace_id: str | None = None) -> str:
    """Create a W3C traceparent value for a new pipeline run.

    The trace_id is a 32-char hex string (128-bit, matches Langfuse IDs when
    stripped of hyphens). The parent_id is freshly generated each call so every
    span gets a unique parent slot.
    """
    tid = trace_id if trace_id is not None else uuid.uuid4().hex
    # Ensure 32 hex chars (strip hyphens if a UUID string was passed)
    tid = tid.replace("-", "").lower()[:32].ljust(32, "0")
    pid = secrets.token_hex(8)  # 16 hex chars
    return f"00-{tid}-{pid}-01"


def extract_trace_id(traceparent: str) -> str | None:
    """Return the trace_id portion of a W3C traceparent string, or None."""
    parts = traceparent.split("-")
    if len(parts) == 4 and parts[0] == "00":
        return parts[1]
    return None


def extract_parent_id(traceparent: str) -> str | None:
    """Return the parent-id portion of a W3C traceparent string, or None."""
    parts = traceparent.split("-")
    if len(parts) == 4 and parts[0] == "00":
        return parts[2]
    return None


TRACEPARENT_KEY = "traceparent"
