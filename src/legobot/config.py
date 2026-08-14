"""Runtime configuration, read once from the environment at startup.

Everything the bot needs to run lives here so there is exactly one place to look
when a setting misbehaves. Secrets arrive via env vars (an untracked `.env` on
the server) and are never written to disk by the bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    """Raised when the environment is missing something we cannot invent."""


def _parse_hhmm(value: str, label: str) -> dtime:
    try:
        hh, mm = value.strip().split(":")
        return dtime(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001 - want a friendly message, not a traceback
        raise ConfigError(f"{label} must look like HH:MM, got {value!r}") from exc


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if val < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {val}")
    return val


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: int
    db_path: str = "/data/legobot.sqlite3"

    # Polling cadence and the window during which we bother to poll at all.
    poll_interval_minutes: int = 5
    active_start: dtime = field(default_factory=lambda: dtime(7, 0))
    active_end: dtime = field(default_factory=lambda: dtime(19, 0))
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/London"))

    # Fast tier: an in-stock-only sweep (5 requests, ~1s) that drives the
    # availability alerts. Cheap enough to run every 30s, which cuts median
    # time-to-detect from ~150s to ~15s. The slow full sweep above still runs,
    # because only it can tell "out of stock" from "delisted".
    fast_interval_seconds: int = 30
    fast_tier_enabled: bool = True

    # Guard rails.
    max_tracked_sets: int = 100
    http_timeout_seconds: float = 30.0

    @property
    def tz_name(self) -> str:
        return str(self.timezone)

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather on "
                "Telegram and put the token in your .env file."
            )

        raw_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not raw_chat:
            raise ConfigError(
                "TELEGRAM_CHAT_ID is not set. Message your bot, then read the "
                "chat id from https://api.telegram.org/bot<TOKEN>/getUpdates"
            )
        try:
            chat_id = int(raw_chat)
        except ValueError as exc:
            raise ConfigError(f"TELEGRAM_CHAT_ID must be numeric, got {raw_chat!r}") from exc

        tz_name = os.getenv("TZ", "Europe/London").strip() or "Europe/London"
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"TZ {tz_name!r} is not a known timezone") from exc

        return cls(
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            db_path=os.getenv("LEGOBOT_DB_PATH", "/data/legobot.sqlite3"),
            poll_interval_minutes=_env_int("POLL_INTERVAL_MINUTES", 5),
            active_start=_parse_hhmm(os.getenv("ACTIVE_START", "07:00"), "ACTIVE_START"),
            active_end=_parse_hhmm(os.getenv("ACTIVE_END", "19:00"), "ACTIVE_END"),
            timezone=tz,
            fast_interval_seconds=_env_int("FAST_INTERVAL_SECONDS", 30, minimum=10),
            fast_tier_enabled=os.getenv("FAST_TIER_ENABLED", "1").strip() not in ("0", "false", "no"),
            max_tracked_sets=_env_int("MAX_TRACKED_SETS", 100),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
        )
