"""Source-connector catalog + per-field enablement models (Phase 17c).

A **connector** is a reusable *definition* of a source (arXiv, Hacker News,
GitHub, …): its slug, the A2A scout skill that runs it, and a ``config_schema``
describing the fields a field must supply (categories / keywords / topics / …).
The catalog is global. A **field connector** is one field's *enablement +
config* of a catalog connector — the per-field row the coordinator reads to
decide which connectors a run dispatches and with what search terms.

This phase ships built-in connectors only (the eight existing scouts); the
self-serve, user-addable connector layer is Phase 18. The registry below is the
single source of truth: ``init_pg`` seeds the catalog + the ai-robotics field's
enablement from it, and the coordinator maps a connector slug → its scout skill
(``scout_<slug>``).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from mesh_models.field import DEFAULT_FIELD_ID


class ConnectorKind(StrEnum):
    builtin = "builtin"


class Connector(BaseModel):
    """A catalog connector definition (global, reusable across fields)."""

    id: str
    slug: str
    name: str
    description: str
    kind: ConnectorKind = ConnectorKind.builtin
    # JSON-schema-lite: field_name -> {"type": "list[str]"|"int"|"str"|"list[dict]",
    # "required": bool, "description": str}. Validated on enable.
    config_schema: dict[str, Any] = Field(default_factory=dict)

    @property
    def scout_skill_id(self) -> str:
        """The A2A scout skill that runs this connector."""
        return f"scout_{self.slug}"


class FieldConnector(BaseModel):
    """One field's enablement + config of a catalog connector."""

    field_id: str
    connector_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


# ── Built-in connector catalog (single source of truth) ──────────────────────
#
# Each entry: the catalog definition + the seed config for the ai-robotics field
# (equal to today's scout defaults, so the seeded field behaves exactly as
# before Phase 17c).


def _conn(
    slug: str,
    name: str,
    description: str,
    config_schema: dict[str, Any],
    ai_config: dict[str, Any],
) -> tuple[Connector, dict[str, Any]]:
    return (
        Connector(
            id=slug,
            slug=slug,
            name=name,
            description=description,
            config_schema=config_schema,
        ),
        ai_config,
    )


# (connector, ai_robotics_seed_config) pairs.
_BUILTIN: list[tuple[Connector, dict[str, Any]]] = [
    _conn(
        "arxiv",
        "arXiv",
        "Recent arXiv papers by subject category.",
        {"categories": {"type": "list[str]", "required": True,
                        "description": "arXiv subject categories, e.g. cs.AI"}},
        {"categories": ["cs.AI", "cs.RO", "cs.LG"]},
    ),
    _conn(
        "hn",
        "Hacker News",
        "Hacker News stories/comments matching keywords.",
        {"keywords": {"type": "list[str]", "required": True,
                      "description": "Search keywords"},
         "min_points": {"type": "int", "required": False,
                        "description": "Minimum points to include"}},
        {"keywords": ["AI", "LLM", "GPT", "Claude", "robotics", "RAG", "agent"],
         "min_points": 20},
    ),
    _conn(
        "github",
        "GitHub",
        "Trending GitHub repositories by topic (plus an optional watchlist).",
        {"topics": {"type": "list[str]", "required": True,
                    "description": "GitHub topics"},
         "watchlist": {"type": "list[str]", "required": False,
                       "description": "owner/repo entries to always check"}},
        {"topics": ["llm", "agents", "machine-learning", "ai", "robotics"]},
    ),
    _conn(
        "bluesky",
        "Bluesky",
        "Bluesky posts by hashtag (plus an optional handle list).",
        {"hashtags": {"type": "list[str]", "required": True,
                      "description": "Hashtags without the #"},
         "handles": {"type": "list[str]", "required": False,
                     "description": "Handles to always include"}},
        {"hashtags": ["ai", "ml", "llm"]},
    ),
    _conn(
        "reddit",
        "Reddit",
        "Top Reddit posts from a set of subreddits.",
        {"subreddits": {"type": "list[str]", "required": True,
                        "description": "Subreddit names"},
         "listing": {"type": "str", "required": False,
                     "description": "hour|day|week|month|year|all"},
         "min_score": {"type": "int", "required": False,
                       "description": "Minimum score to include"}},
        {"subreddits": ["MachineLearning", "LocalLLaMA", "singularity", "artificial"]},
    ),
    _conn(
        "blog",
        "Blogs (RSS)",
        "New entries from a list of RSS/Atom blog feeds.",
        {"feeds": {"type": "list[dict]", "required": False,
                   "description": "Feed entries {name, url}; empty uses the default file"},
         "lookback_hours": {"type": "int", "required": False,
                            "description": "How far back to look"}},
        {},
    ),
    _conn(
        "leaderboard",
        "Leaderboards",
        "Snapshots from public model leaderboards.",
        {"lanes": {"type": "list[str]", "required": False,
                   "description": "Leaderboard lanes; empty runs all"}},
        {},
    ),
]

# Catalog connectors (global).
BUILTIN_CONNECTORS: list[Connector] = [c for c, _ in _BUILTIN]

# ai-robotics field enablement: every built-in enabled, config = today's defaults.
AI_ROBOTICS_FIELD_CONNECTORS: list[FieldConnector] = [
    FieldConnector(field_id=DEFAULT_FIELD_ID, connector_id=c.id, config=cfg, enabled=True)
    for c, cfg in _BUILTIN
]


_TYPE_CHECKS = {
    "list[str]": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "list[dict]": lambda v: isinstance(v, list) and all(isinstance(x, dict) for x in v),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "str": lambda v: isinstance(v, str),
}


def validate_connector_config(
    config: dict[str, Any], config_schema: dict[str, Any]
) -> None:
    """Validate a per-field connector config against its catalog ``config_schema``.

    Raises ``ValueError`` on an unknown key, a wrong type, or a missing required
    field — so bad config is rejected at write time (on enable), never mid-run."""
    for key, value in config.items():
        spec = config_schema.get(key)
        if spec is None:
            raise ValueError(f"unknown config key '{key}' for this connector")
        type_name = spec.get("type", "str")
        check = _TYPE_CHECKS.get(type_name)
        if check is not None and not check(value):
            raise ValueError(f"config key '{key}' must be of type {type_name}")
    for key, spec in config_schema.items():
        if spec.get("required") and key not in config:
            raise ValueError(f"missing required config key '{key}'")
