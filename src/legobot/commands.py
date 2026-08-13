"""Telegram command handling.

Kept separate from the app loop so each command is a small function that takes
text and returns a reply string. The only stateful dependencies are the Store and
the BrickBorrow client.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .brickborrow import BrickBorrowClient, BrickBorrowError, extract_slugs, slug_from_url
from .store import Store
from .telegram import esc

log = logging.getLogger(__name__)

COMMAND_MENU = [
    ("add", "Track a set — paste its Brick Borrow link"),
    ("search", "Find a set by name or number, e.g. /search 10349"),
    ("list", "Show everything you're tracking"),
    ("remove", "Stop tracking — /remove 3 or paste the link"),
    ("check", "Check all tracked sets right now"),
    ("pause", "Stop notifications (e.g. while you have a set)"),
    ("resume", "Start notifying again"),
    ("status", "Is the bot running, paused, when's the next check"),
    ("help", "Show all commands"),
]

HELP_TEXT = """<b>Brick Borrow watcher</b>

I check your tracked sets every <b>{interval} minutes</b> between \
<b>{start}–{end}</b> ({tz}) and message you the moment one becomes available.

<b>Adding sets</b>
/add &lt;link&gt; — paste one or more Brick Borrow product links
/search &lt;text&gt; — find by name or set number, then /add the link I give you

⚠️ <b>Heads up:</b> Brick Borrow's URLs often name the wrong set — the page at \
<code>…/lego-10349-icons-happy-plants</code> is actually the Bonsai Tree. I always \
reply with the <i>real</i> set name so you can check. When in doubt use /search.

<b>Managing</b>
/list — what I'm watching, with current status
/remove &lt;number&gt; — remove by its number in /list (or paste the link)
/check — force a check right now

<b>Pausing</b>
/pause — stop notifying (I keep watching quietly)
/resume — start again, and I'll tell you what's available right now
/status — current state

I only notify you when a set <i>changes</i> from unavailable to available, so \
you won't get the same alert twice.
"""


class CommandHandler:
    def __init__(
        self,
        store: Store,
        client: BrickBorrowClient,
        *,
        max_tracked: int,
        interval_minutes: int,
        active_start: str,
        active_end: str,
        tz_name: str,
        trigger_scan: Callable[[], Awaitable[str]],
        status_report: Callable[[], Awaitable[str]],
    ) -> None:
        self.store = store
        self.client = client
        self.max_tracked = max_tracked
        self.interval_minutes = interval_minutes
        self.active_start = active_start
        self.active_end = active_end
        self.tz_name = tz_name
        self.trigger_scan = trigger_scan
        self.status_report = status_report

    async def handle(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""

        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            command = head.lstrip("/").split("@", 1)[0].lower()
            arg = rest.strip()
        else:
            # A bare message containing links is treated as /add — the common case
            # is forwarding a link from the browser, and making that work without
            # a command is the single biggest usability win here.
            if extract_slugs(text):
                command, arg = "add", text
            else:
                return (
                    "I didn't find a Brick Borrow link in that.\n\n"
                    "Try /search &lt;set name or number&gt;, or /help."
                )

        handler = {
            "start": self._help,
            "help": self._help,
            "add": self._add,
            "track": self._add,
            "search": self._search,
            "find": self._search,
            "list": self._list,
            "remove": self._remove,
            "delete": self._remove,
            "rm": self._remove,
            "check": self._check,
            "pause": self._pause,
            "resume": self._resume,
            "unpause": self._resume,
            "status": self._status,
            "clear": self._clear,
        }.get(command)

        if handler is None:
            return f"Unknown command <code>/{esc(command)}</code>. Try /help."
        return await handler(arg)

    # ---------------- individual commands ----------------

    async def _help(self, _arg: str) -> str:
        return HELP_TEXT.format(
            interval=self.interval_minutes,
            start=self.active_start,
            end=self.active_end,
            tz=self.tz_name,
        )

    async def _add(self, arg: str) -> str:
        slugs = extract_slugs(arg)
        if not slugs:
            # An explicit `/add <slug>` with no URL around it is still valid;
            # free-text scanning above deliberately won't accept a bare word.
            single = slug_from_url(arg) if arg and " " not in arg.strip() else None
            slugs = [single] if single else []
        if not slugs:
            return (
                "Give me a Brick Borrow product link, e.g.\n"
                "<code>/add https://www.brickborrow.com/product-page/…</code>\n\n"
                "Or find one with /search &lt;name or number&gt;."
            )

        room = self.max_tracked - self.store.count_tracked()
        if room <= 0:
            return (
                f"You're already tracking the maximum of {self.max_tracked} sets. "
                "Remove one first with /remove."
            )

        lines: list[str] = []
        added = 0
        for slug in slugs[:room]:
            try:
                product = await self.client.product_by_slug(slug)
            except BrickBorrowError as exc:
                log.warning("lookup failed for %s: %s", slug, exc)
                lines.append(f"⚠️ <code>{esc(slug)}</code> — couldn't reach the store, try again")
                continue

            if product is None:
                lines.append(
                    f"❌ <code>{esc(slug)}</code> — no such set. "
                    "Check the link, or use /search."
                )
                continue

            if self.store.add(
                product.id, product.url_part, product.name, product.pieces
            ):
                added += 1
                mark = "🟢 available right now" if product.available else "🔴 not available yet"
                detail = f"{product.pieces_label} · {mark}" if product.pieces_label else mark
                lines.append(f"✅ <b>{esc(product.name)}</b>\n   {detail}")
                # Seed the state so the first scan doesn't re-announce something
                # we just told them about.
                self.store.record_scan(
                    product.id, available=product.available, pieces=product.pieces
                )
            else:
                lines.append(f"• <b>{esc(product.name)}</b> — already tracking")

        if len(slugs) > room:
            lines.append(f"\n⚠️ Skipped {len(slugs) - room} — that would exceed your limit.")

        header = f"Now tracking {self.store.count_tracked()} set(s).\n\n" if added else ""
        footer = (
            "\n\n<i>Names come from the store, not the link — Brick Borrow's URLs "
            "often name the wrong set, so check that's what you meant.</i>"
            if added
            else ""
        )
        return header + "\n".join(lines) + footer

    async def _search(self, arg: str) -> str:
        if not arg:
            return "What should I look for? e.g. <code>/search munchlax</code> or <code>/search 10349</code>"

        try:
            results = await self.client.search(arg, limit=10)
        except BrickBorrowError as exc:
            log.warning("search failed: %s", exc)
            return "Couldn't reach the store just now — try again in a minute."

        if not results:
            return f"Nothing matched <b>{esc(arg)}</b>."

        lines = [f"Found {len(results)} match(es) for <b>{esc(arg)}</b>:", ""]
        for product in results:
            mark = "🟢" if product.available else "🔴"
            tracked = " · <i>tracked</i>" if self.store.get(product.id) else ""
            suffix = f" · {product.pieces_label}" if product.pieces_label else ""
            lines.append(
                f'{mark} <a href="{esc(product.url)}">{esc(product.name)}</a>{suffix}{tracked}'
            )
            lines.append(f"   <code>/add {esc(product.url)}</code>")
        return "\n".join(lines)

    async def _list(self, _arg: str) -> str:
        tracked = self.store.list_tracked()
        if not tracked:
            return "You're not tracking anything yet. Paste a Brick Borrow link to start."

        lines = [f"<b>Tracking {len(tracked)} set(s)</b>", ""]
        for index, item in enumerate(tracked, start=1):
            if item.last_available is None:
                mark = "❔"
            elif item.last_available:
                mark = "🟢"
            else:
                mark = "🔴"
            lines.append(f'{index}. {mark} <a href="{esc(item.url)}">{esc(item.name)}</a>')
            if item.pieces_label:
                lines.append(f"    <i>{item.pieces_label}</i>")

        known = [i.pieces for i in tracked if i.pieces is not None]
        if known:
            lines.append("")
            lines.append(f"<i>{sum(known):,} pieces across {len(known)} set(s).</i>")
        lines.append("")
        lines.append("<i>Status is from the last check.</i> /check to refresh now.")
        if self.store.paused:
            lines.append("\n⏸ <b>Notifications are paused</b> — /resume to turn them back on.")
        return "\n".join(lines)

    async def _remove(self, arg: str) -> str:
        if not arg:
            return "Which one? <code>/remove 3</code> (the number from /list) or paste its link."

        tracked = self.store.list_tracked()

        # By index from /list — the ergonomic path on a phone.
        if arg.isdigit():
            index = int(arg)
            if not 1 <= index <= len(tracked):
                return f"There's no #{index}. You're tracking {len(tracked)} set(s) — see /list."
            item = tracked[index - 1]
            self.store.remove(item.product_id)
            return f"Removed <b>{esc(item.name)}</b>. Now tracking {self.store.count_tracked()}."

        # By link.
        slug = slug_from_url(arg)
        if slug:
            for item in tracked:
                if item.url_part == slug:
                    self.store.remove(item.product_id)
                    return (
                        f"Removed <b>{esc(item.name)}</b>. "
                        f"Now tracking {self.store.count_tracked()}."
                    )
            return "That link isn't in your tracking list. /list to see what is."

        # By name fragment.
        matches = [i for i in tracked if arg.lower() in i.name.lower()]
        if len(matches) == 1:
            self.store.remove(matches[0].product_id)
            return (
                f"Removed <b>{esc(matches[0].name)}</b>. "
                f"Now tracking {self.store.count_tracked()}."
            )
        if len(matches) > 1:
            names = "\n".join(f"• {esc(m.name)}" for m in matches)
            return f"That matches {len(matches)} sets:\n{names}\n\nUse the number from /list."
        return "Nothing matched. /list to see what you're tracking."

    async def _check(self, _arg: str) -> str:
        if not self.store.count_tracked():
            return "Nothing to check — you're not tracking any sets yet."
        return await self.trigger_scan()

    async def _pause(self, _arg: str) -> str:
        if self.store.paused:
            return "Already paused. /resume when you're ready for more."
        self.store.set_paused(True)
        return (
            "⏸ <b>Paused.</b> I'll keep checking quietly but won't message you.\n\n"
            "/resume and I'll tell you what's available right then."
        )

    async def _resume(self, _arg: str) -> str:
        if not self.store.paused:
            return "Not paused — I'm already watching. /status for details."
        self.store.set_paused(False)
        if not self.store.count_tracked():
            return "▶️ <b>Resumed.</b> You're not tracking anything yet though — paste a link."
        return "▶️ <b>Resumed.</b> Checking what's available right now…\n\n" + (
            await self.trigger_scan()
        )

    async def _status(self, _arg: str) -> str:
        return await self.status_report()

    async def _clear(self, arg: str) -> str:
        if arg.strip().lower() != "confirm":
            count = self.store.count_tracked()
            return (
                f"This removes all {count} tracked set(s). "
                "Send <code>/clear confirm</code> if you're sure."
            )
        removed = self.store.clear_tracked()
        return f"Cleared {removed} tracked set(s)."
