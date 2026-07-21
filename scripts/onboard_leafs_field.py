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
    extraction_examples=_LEAFS_EXAMPLES,
    topic_label="news",
)

# Connectors: news feeds that need no extra API keys, plus web_search (no-op
# without BRAVE_API_KEY). Feeds are stable WordPress/SB-Nation RSS.
_LEAFS_CONNECTORS: list[tuple[str, dict]] = [
    (
        "blog",
        {
            "feeds": [
                {"name": "Maple Leafs Hotstove", "url": "https://www.mapleleafshotstove.com/feed/"},
                {"name": "Pension Plan Puppets", "url": "https://www.pensionplanpuppets.com/rss/index.xml"},
                {"name": "Editor In Leaf", "url": "https://editorinleaf.com/feed"},
            ],
            "lookback_hours": 48,
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
            enable_connector(conn, field_id, connector_id, config=cfg, enabled=True)
            print(f"  enabled connector {connector_id!r}")

        conn.commit()
        print("done. Set MESH_PIPELINE_FIELD=toronto-maple-leafs and restart the controller.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
