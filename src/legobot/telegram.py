"""Minimal Telegram Bot API client — long polling, no framework.

Long polling ("getUpdates") means the bot opens an outbound HTTPS request that
the server holds open until a message arrives. The alternative, a webhook, needs
an inbound public HTTPS endpoint — a port, a domain, a certificate. Long polling
needs none of that, which is exactly why this container publishes no ports and
can sit behind the homelab firewall untouched. Same pattern as tennisbot-prefs.

⚠️ Only ONE process may long-poll a given bot token; a second gets HTTP 409.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

# Telegram rejects messages over 4096 chars.
MAX_MESSAGE = 4000


@dataclass(frozen=True)
class Update:
    update_id: int
    chat_id: Optional[int]
    text: str


class TelegramError(RuntimeError):
    pass


def esc(text: str) -> str:
    """Escape text for Telegram's HTML parse mode, in ELEMENT-TEXT position."""
    return html.escape(str(text), quote=False)


def esc_attr(text: str) -> str:
    """Escape text destined for an HTML ATTRIBUTE value, e.g. ``href="..."``.

    Escaping is context-dependent: `esc` leaves `"` alone, which is fine between
    tags but not inside a quoted attribute, where a `"` in the value ends the
    attribute early and lets the rest be read as markup. Anything interpolated
    into `href="{...}"` must go through this instead.
    """
    return html.escape(str(text), quote=True)


def chunk(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split a long message on line boundaries so we never hit the 4096 cap."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current)
            # A single monstrous line still has to be cut somewhere.
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient, token: str, chat_id: int) -> None:
        self._http = http
        self._base = f"{API}/bot{token}"
        self.chat_id = chat_id

    async def _call(self, method: str, *, http_timeout: Optional[float] = None, **params: Any) -> Any:
        """Call a Bot API method.

        `http_timeout` is the socket timeout; anything else in **params is sent to
        Telegram as a method parameter. They are kept distinct because getUpdates
        has its own `timeout` parameter meaning the long-poll window.
        """
        try:
            resp = await self._http.post(
                f"{self._base}/{method}", json=params, timeout=http_timeout
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"network error calling {method}: {exc}") from exc

        if resp.status_code == 409:
            raise TelegramError(
                "Telegram says another process is already polling this bot token "
                "(409). Stop the other instance — only one may run."
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON (HTTP {resp.status_code})") from exc

        if not body.get("ok"):
            raise TelegramError(
                f"{method} failed: {body.get('description', 'unknown error')}"
            )
        return body.get("result")

    async def send(self, text: str, *, disable_preview: bool = True) -> None:
        """Send to the owner. Never raises — a failed notification must not kill the loop."""
        for part in chunk(text):
            try:
                await self._call(
                    "sendMessage",
                    chat_id=self.chat_id,
                    text=part,
                    parse_mode="HTML",
                    disable_web_page_preview=disable_preview,
                )
            except TelegramError as exc:
                log.error("failed to send Telegram message: %s", exc)
                return
            await asyncio.sleep(0.05)  # stay well under Telegram's rate limits

    async def get_updates(self, offset: int, poll_seconds: int = 30) -> list[Update]:
        """Long-poll for new messages. Blocks up to `poll_seconds` server-side."""
        result = await self._call(
            "getUpdates",
            # Socket timeout must exceed the server-side long-poll window, or
            # httpx would abort every single poll.
            http_timeout=poll_seconds + 15,
            offset=offset,
            timeout=poll_seconds,
            allowed_updates=["message"],
        )

        updates: list[Update] = []
        for raw in result or []:
            message = raw.get("message") or {}
            updates.append(
                Update(
                    update_id=int(raw["update_id"]),
                    chat_id=(message.get("chat") or {}).get("id"),
                    text=(message.get("text") or "").strip(),
                )
            )
        return updates

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register the command menu so Telegram offers autocomplete."""
        try:
            await self._call(
                "setMyCommands",
                commands=[{"command": c, "description": d} for c, d in commands],
            )
        except TelegramError as exc:
            log.warning("could not register command menu: %s", exc)
