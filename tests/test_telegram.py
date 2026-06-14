"""Unit tests for the Telegram bridge's pure logic (config + formatting).

No ``telegram`` import and no network — just env parsing and message rendering.
"""
from __future__ import annotations

from datetime import UTC
from zoneinfo import ZoneInfo

from mesh_telegram.config import Config
from mesh_telegram.format import (
    MAX_MESSAGE_CHARS,
    format_answer,
    format_briefing,
    truncate,
)

# ── Config ──────────────────────────────────────────────────────────────────


def test_from_env_defaults() -> None:
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "tok"})
    assert cfg.token == "tok"
    assert cfg.allowed_chat_ids == []
    assert cfg.field_slug == "ai-robotics"
    assert cfg.api_url == "http://api:8000"
    assert cfg.briefing_enabled is True
    assert cfg.briefing_hour == 13
    assert cfg.tz is UTC


def test_chat_id_parsing_ignores_blanks_and_junk() -> None:
    cfg = Config.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_CHAT_IDS": " 1, 2 ,,abc, 3 "}
    )
    assert cfg.allowed_chat_ids == [1, 2, 3]


def test_is_allowed_requires_non_empty_allowlist() -> None:
    empty = Config.from_env({"TELEGRAM_BOT_TOKEN": "t"})
    assert empty.is_allowed(123) is False

    allowed = Config.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}
    )
    assert allowed.is_allowed(123) is True
    assert allowed.is_allowed(999) is False


def test_briefing_disabled_and_clamped_time() -> None:
    cfg = Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_BRIEFING_ENABLED": "false",
            "TELEGRAM_BRIEFING_HOUR": "99",
            "TELEGRAM_BRIEFING_MINUTE": "-5",
        }
    )
    assert cfg.briefing_enabled is False
    assert cfg.briefing_hour == 23
    assert cfg.briefing_minute == 0


def test_named_tz_and_url_normalization() -> None:
    cfg = Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_TZ": "America/Vancouver",
            "MESH_TELEGRAM_API_URL": "http://api:8000/",
            "MESH_TELEGRAM_WIKI_URL": "http://pi:3000/",
        }
    )
    assert cfg.tz == ZoneInfo("America/Vancouver")
    assert cfg.api_url == "http://api:8000"
    assert cfg.wiki_url == "http://pi:3000"
    assert cfg.briefing_time.tzinfo == ZoneInfo("America/Vancouver")


def test_bad_tz_falls_back_to_utc() -> None:
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_TZ": "Not/AZone"})
    assert cfg.tz is UTC


# ── Formatting ────────────────────────────────────────────────────────────────


def test_truncate() -> None:
    assert truncate("short") == "short"
    long = "x" * (MAX_MESSAGE_CHARS + 100)
    out = truncate(long)
    assert len(out) <= MAX_MESSAGE_CHARS
    assert out.endswith("…")


def test_format_answer_full() -> None:
    text = format_answer(
        {
            "answer_markdown": "GPT-5 leads on MMLU.",
            "coverage": "well_supported",
            "caveats": ["Single benchmark."],
            "citations": [{"kind": "belief", "id": "b1"}, {"kind": "claim", "id": "c2"}],
        }
    )
    assert "GPT-5 leads on MMLU." in text
    assert "well supported" in text
    assert "Single benchmark." in text
    assert "belief:b1" in text
    assert "claim:c2" in text


def test_format_answer_minimal() -> None:
    text = format_answer({"answer_markdown": "", "coverage": "uncovered"})
    assert "No answer." in text
    assert "not covered" in text


def test_format_briefing_with_items_and_links() -> None:
    text = format_briefing(
        {
            "date": "2026-06-14",
            "profile_excerpt": "Interested in robotics.",
            "sections": [
                {
                    "name": "New Beliefs",
                    "items": [
                        {
                            "item_type": "belief",
                            "item_id": "b9",
                            "relevance_score": 0.91,
                            "rationale": "New SOTA on your benchmark.",
                        }
                    ],
                }
            ],
        },
        wiki_url="http://pi:3000",
        field_slug="ai-robotics",
    )
    assert "Daily Brief — 2026-06-14" in text
    assert "Interested in robotics." in text
    assert "▸ New Beliefs" in text
    assert "New SOTA on your benchmark." in text
    assert "http://pi:3000/knowledge/beliefs/b9?field=ai-robotics" in text


def test_format_briefing_quiet_day() -> None:
    text = format_briefing(
        {"date": "2026-06-14", "sections": [{"name": "Quiet day", "items": []}]}
    )
    assert "Quiet day" in text
