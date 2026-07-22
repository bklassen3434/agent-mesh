# ruff: noqa: E501  — example/config data lines are intentionally long
"""Onboard the "Toronto Maple Leafs" field — all-things-Leafs news.

Idempotent operator script (safe to re-run): creates the ``toronto-maple-leafs``
field with a hockey-news extraction profile and enables the news connectors that
need no extra API keys (``blog`` RSS feeds + ``reddit``). ``web_search`` is
enabled too but only produces sources when ``BRAVE_API_KEY`` is set.

Run against a writer DB (the same env the controller uses):

    uv run python scripts/onboard_leafs_field.py

Then point the controller at it by setting ``MESH_PIPELINE_FIELD=toronto-maple-leafs``
(the controller's ``--field`` default now reads that env var) and restarting it.

The seeded ``ai-robotics`` field and its data are left untouched — fields are a
partition, so shifting the controller simply stops it from touching ai-robotics.
"""
from __future__ import annotations

from typing import Any

from mesh_db import create_field, enable_connector, get_connection
from mesh_db.fields import get_field_by_slug
from mesh_models.field import Field, FieldProfile

FIELD_SLUG = "toronto-maple-leafs"

# Hockey-news few-shot block. The predicate vocabulary is universal (score /
# comparison / capability / attribution), so we steer it toward hockey:
#   has_capability  → any notable fact/event about a player, coach, or the team
#   outperforms     → a game result (team beat team on a date)
#   achieves_score  → a countable stat (goals/points/saves in a game/season)
#   developed_by    → roster/affiliation (a player belongs to the Maple Leafs org)
_LEAFS_EXAMPLES = """=== EXAMPLE 1 ===
Title: "Matthews nets hat trick as Leafs down Bruins 5-2"
Content: "...Auston Matthews scored three goals and the Toronto Maple Leafs beat the Boston Bruins 5-2 on Tuesday. Matthews now leads the team with 28 goals..."

Output:
{
  "claims": [
    {
      "predicate": "achieves_score",
      "subject_name": "Auston Matthews",
      "object": {"score": 3, "benchmark": "game vs Boston Bruins", "metric": "goals"},
      "raw_excerpt": "Auston Matthews scored three goals",
      "confidence": 0.95
    },
    {
      "predicate": "outperforms",
      "subject_name": "Toronto Maple Leafs",
      "object": {"compared_to": "Boston Bruins", "on": "game on Tuesday (5-2)"},
      "raw_excerpt": "the Toronto Maple Leafs beat the Boston Bruins 5-2",
      "confidence": 0.95
    },
    {
      "predicate": "has_capability",
      "subject_name": "Auston Matthews",
      "object": {"capability": "leads the team with 28 goals this season"},
      "raw_excerpt": "Matthews now leads the team with 28 goals",
      "confidence": 0.9
    }
  ]
}

=== EXAMPLE 2 ===
Title: "Leafs acquire defenseman in deadline deal"
Content: "...The Toronto Maple Leafs acquired veteran defenseman Chris Tanev from the Calgary Flames ahead of the trade deadline. Tanev is a right-shot defenseman known for shot-blocking..."

Output:
{
  "claims": [
    {
      "predicate": "has_capability",
      "subject_name": "Toronto Maple Leafs",
      "object": {"capability": "acquired Chris Tanev from Calgary at the trade deadline"},
      "raw_excerpt": "The Toronto Maple Leafs acquired veteran defenseman Chris Tanev from the Calgary Flames",
      "confidence": 0.95
    },
    {
      "predicate": "developed_by",
      "subject_name": "Chris Tanev",
      "object": {"lab": "Toronto Maple Leafs"},
      "raw_excerpt": "The Toronto Maple Leafs acquired veteran defenseman Chris Tanev",
      "confidence": 0.9
    },
    {
      "predicate": "has_capability",
      "subject_name": "Chris Tanev",
      "object": {"capability": "right-shot defenseman known for shot-blocking"},
      "raw_excerpt": "a right-shot defenseman known for shot-blocking",
      "confidence": 0.85
    }
  ]
}
"""

LEAFS_PROFILE = FieldProfile(
    slug=FIELD_SLUG,
    name="Toronto Maple Leafs",
    description="a Toronto Maple Leafs NHL hockey news knowledge base",
    # How entities are named in this field: players, coaches/GMs, the club and
    # its opponents, arenas, and league bodies.
    entity_type_hints=[
        "Auston Matthews",
        "Toronto Maple Leafs",
        "Brad Treliving",
        "Boston Bruins",
        "NHL",
    ],
    # Field-agnostic entity vocabulary (rule 4b): hockey types, not the AI
    # default. "concept" stays the universal fallback.
    entity_types=[
        "player",
        "team",
        "coach",
        "executive",
        "game",
        "season",
        "award",
        "arena",
        "league",
        "concept",
    ],
    extraction_examples=_LEAFS_EXAMPLES,
    topic_label="news",
)

# Connectors. Every URL below was verified live (HTTP 200 + fresh items) on
# 2026-07-21. Feeds are grouped so the Leafs FIELD never ingests other teams:
#   blog      → multi-feed RSS, Leafs-specific outlets + a Leafs-scoped Google
#               News query. `blog` has no term filter, so only Leafs-scoped
#               feeds go here.
#   rss       → single feed WITH include/exclude term filters — used for a
#               league-wide feed (Daily Faceoff) narrowed to Leafs items.
#   rest_json → ESPN's free NHL news JSON (prose items). League-wide NHL; the
#               Leafs-specific signal comes from the feeds above. Repoint the
#               endpoint to any JSON source.
#   reddit    → needs free REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in the env;
#               without them the scout logs "creds missing" and returns nothing.
#   web_search→ needs BRAVE_API_KEY; a no-op without it.
_GOOGLE_NEWS_LEAFS = (
    'https://news.google.com/rss/search?q=%22Toronto+Maple+Leafs%22'
    "&hl=en-CA&gl=CA&ceid=CA:en"
)

_LEAFS_CONNECTORS: list[tuple[str, dict[str, Any]]] = [
    (
        "blog",
        {
            "feeds": [
                {"name": "Maple Leafs Hotstove", "url": "https://mapleleafshotstove.com/feed/"},
                {"name": "Pension Plan Puppets", "url": "https://www.pensionplanpuppets.com/feed/"},
                {"name": "Editor In Leaf", "url": "https://editorinleaf.com/feed"},
                {"name": "The Leafs Nation", "url": "https://theleafsnation.com/feed"},
                {"name": "Google News — Toronto Maple Leafs", "url": _GOOGLE_NEWS_LEAFS},
                # r/leafs via Reddit's public .rss — no API app/creds needed
                # (the credentialed `reddit` connector stays off until keys exist).
                {"name": "r/leafs (Reddit)", "url": "https://www.reddit.com/r/leafs/.rss"},
            ],
            "lookback_hours": 48,
        },
    ),
    (
        "rss",
        {
            "feed_url": "https://dailyfaceoff.com/feed/",
            "include_terms": ["Maple Leafs", "Leafs", "Toronto"],
        },
    ),
    (
        "rest_json",
        {
            "endpoint": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news",
            "query_template": "limit=25",
            "items_path": "articles",
            "title_path": "headline",
            "text_path": "description",
            "url_path": "links.web.href",
            "published_path": "published",
        },
    ),
    (
        "reddit",
        {"subreddits": ["leafs", "hockey"], "listing": "day", "min_score": 25},
    ),
    (
        "web_search",
        {"web_seed_queries": ["Toronto Maple Leafs news", "Maple Leafs trade rumors", "Leafs injury update"]},
    ),
]

# Enabled by default unless it needs a credential the host may not have. Disabled
# connectors are still configured (config persisted) — flip them on once the key
# exists. web_search floods the controller with `web_search_no_api_key` and eats a
# dispatch slot every round without a BRAVE_API_KEY, so it ships disabled. The
# credentialed `reddit` connector likewise returns 0 without REDDIT_CLIENT_ID/
# SECRET, so it ships disabled too — r/leafs is covered above via Reddit's keyless
# public .rss in `blog`. Flip either on once its key exists.
_DISABLED_PENDING_CREDS = {"web_search", "reddit"}


def main() -> None:
    conn = get_connection()  # writer
    try:
        existing = get_field_by_slug(conn, FIELD_SLUG)
        if existing is None:
            create_field(
                conn,
                Field(id=FIELD_SLUG, name=LEAFS_PROFILE.name, slug=FIELD_SLUG, profile=LEAFS_PROFILE),
            )
            print(f"created field {FIELD_SLUG!r}")
            field_id = FIELD_SLUG
        else:
            field_id = existing.id
            print(f"field {FIELD_SLUG!r} already exists ({field_id}) — updating connectors")

        for connector_id, cfg in _LEAFS_CONNECTORS:
            on = connector_id not in _DISABLED_PENDING_CREDS
            enable_connector(conn, field_id, connector_id, config=cfg, enabled=on)
            print(f"  {'enabled' if on else 'configured (disabled)'} connector {connector_id!r}")

        conn.commit()
        print("done. Set MESH_PIPELINE_FIELD=toronto-maple-leafs and restart the controller.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
