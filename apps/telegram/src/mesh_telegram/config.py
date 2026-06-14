"""Environment-driven configuration for the Telegram bridge.

Pure stdlib so it imports (and unit-tests) without the ``telegram`` package or
any network access.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, time, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse_chat_ids(raw: str) -> list[int]:
    """Parse a comma-separated allow-list of chat ids, ignoring blanks/junk."""
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


def _parse_bool(raw: str, default: bool) -> bool:
    raw = raw.strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _parse_tz(name: str) -> tzinfo:
    name = name.strip()
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Config:
    """Resolved runtime config for the bridge."""

    token: str
    allowed_chat_ids: list[int] = field(default_factory=list)
    api_url: str = "http://api:8000"
    field_slug: str = "ai-robotics"
    wiki_url: str | None = None
    ask_timeout: float = 130.0
    briefing_enabled: bool = True
    briefing_hour: int = 13
    briefing_minute: int = 0
    tz: tzinfo = UTC
    health_host: str = "0.0.0.0"
    health_port: int = 9110

    @property
    def briefing_time(self) -> time:
        """The tz-aware daily-run time the JobQueue schedules against."""
        return time(hour=self.briefing_hour, minute=self.briefing_minute, tzinfo=self.tz)

    def is_allowed(self, chat_id: int) -> bool:
        """A non-empty allow-list is required — an empty one authorizes no one.

        The bot is discoverable by username, so refusing every chat until an id
        is explicitly allow-listed keeps a stranger from querying your mesh.
        """
        return chat_id in self.allowed_chat_ids

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        return cls(
            token=e.get("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_chat_ids=_parse_chat_ids(e.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            api_url=e.get("MESH_TELEGRAM_API_URL", "http://api:8000").rstrip("/"),
            field_slug=e.get("MESH_TELEGRAM_FIELD", "ai-robotics").strip() or "ai-robotics",
            wiki_url=(e.get("MESH_TELEGRAM_WIKI_URL", "").strip().rstrip("/") or None),
            ask_timeout=float(e.get("MESH_TELEGRAM_ASK_TIMEOUT", "130") or "130"),
            briefing_enabled=_parse_bool(e.get("TELEGRAM_BRIEFING_ENABLED", ""), True),
            briefing_hour=_clamp(int(e.get("TELEGRAM_BRIEFING_HOUR", "13") or "13"), 0, 23),
            briefing_minute=_clamp(int(e.get("TELEGRAM_BRIEFING_MINUTE", "0") or "0"), 0, 59),
            tz=_parse_tz(e.get("TELEGRAM_TZ", "UTC")),
            health_host=e.get("HEALTH_HOST", "0.0.0.0"),
            health_port=int(e.get("HEALTH_PORT", "9110") or "9110"),
        )
