"""Installed system-prompt versions per (field, skill).

The sink of the autonomous prompt-improvement loop and the source the live
extractor consults. Content rows are immutable and never deleted; installing a
new version flips the prior active row's ``is_active`` off and inserts the new one
active. Reading is a single indexed lookup on the hot extraction path.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.prompt_version import PromptVersion

from mesh_db.connection import MeshConnection

_COLS = (
    "id, field_id, skill_key, prompt, is_active, dataset_field, baseline_f1, "
    "best_f1, holdout_baseline_f1, holdout_best_f1, holdout_gain, rationale, "
    "proposer_tokens, created_at"
)


def get_active_prompt(
    conn: MeshConnection, field_id: str, skill_key: str
) -> str | None:
    """The active prompt text for ``(field_id, skill_key)``, or ``None`` when no
    version is installed (caller falls back to the built-in prompt)."""
    row = conn.execute(
        """
        SELECT prompt
        FROM prompt_versions
        WHERE field_id = %s AND skill_key = %s AND is_active
        LIMIT 1
        """,
        [field_id, skill_key],
    ).fetchone()
    return row[0] if row else None


def install_prompt_version(
    conn: MeshConnection, version: PromptVersion
) -> PromptVersion:
    """Install ``version`` as the active prompt for its (field, skill): deactivate
    the prior active row, then insert this one active. Both content rows are kept —
    rollback is re-activating an earlier version.

    (Connections are autocommit, so this is two statements; deactivate runs first
    so the partial-unique-active index is never violated. A crash between them
    leaves no active version, which degrades safely to the built-in prompt.)
    """
    conn.execute(
        """
        UPDATE prompt_versions SET is_active = false
        WHERE field_id = %s AND skill_key = %s AND is_active
        """,
        [version.field_id, version.skill_key],
    )
    version = version.model_copy(update={"is_active": True})
    conn.execute(
        f"""
        INSERT INTO prompt_versions ({_COLS})
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            version.id,
            version.field_id,
            version.skill_key,
            version.prompt,
            version.is_active,
            version.dataset_field,
            version.baseline_f1,
            version.best_f1,
            version.holdout_baseline_f1,
            version.holdout_best_f1,
            version.holdout_gain,
            version.rationale,
            version.proposer_tokens,
            version.created_at,
        ],
    )
    return version


def list_prompt_versions(
    conn: MeshConnection, field_id: str, skill_key: str, *, limit: int = 50
) -> list[PromptVersion]:
    """The version lineage for ``(field_id, skill_key)``, newest first."""
    rows = conn.execute(
        f"""
        SELECT {_COLS}
        FROM prompt_versions
        WHERE field_id = %s AND skill_key = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        [field_id, skill_key, limit],
    ).fetchall()
    return [_row_to_version(r) for r in rows]


def _row_to_version(row: tuple[Any, ...]) -> PromptVersion:
    (
        id_,
        field_id,
        skill_key,
        prompt,
        is_active,
        dataset_field,
        baseline_f1,
        best_f1,
        holdout_baseline_f1,
        holdout_best_f1,
        holdout_gain,
        rationale,
        proposer_tokens,
        created_at,
    ) = row[:14]
    return PromptVersion(
        id=id_,
        field_id=field_id,
        skill_key=skill_key,
        prompt=prompt,
        is_active=bool(is_active),
        dataset_field=dataset_field or "",
        baseline_f1=baseline_f1 or 0.0,
        best_f1=best_f1 or 0.0,
        holdout_baseline_f1=holdout_baseline_f1 or 0.0,
        holdout_best_f1=holdout_best_f1 or 0.0,
        holdout_gain=holdout_gain or 0.0,
        rationale=rationale or "",
        proposer_tokens=proposer_tokens or 0,
        created_at=(
            created_at
            if isinstance(created_at, datetime)
            else datetime.fromisoformat(str(created_at))
        ),
    )
