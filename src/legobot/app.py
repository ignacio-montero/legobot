"""Wires everything together and runs the two concurrent loops.

Two asyncio tasks share one process:

  * `_command_loop`  — long-polls Telegram for your commands (mostly idle).
  * `_scan_loop`     — sweeps the catalog on the interval, inside active hours.

They are concurrent rather than sequential because a 30-second long-poll must
not delay a scheduled scan, and a 2-second scan must not make the bot deaf to
commands. A single process (rather than two containers) keeps one SQLite writer
and satisfies Telegram's rule that only one process may poll a bot token.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime
from typing import Optional

import httpx

from .brickborrow import BrickBorrowClient, BrickBorrowError, index_by_id
from .commands import COMMAND_MENU, CommandHandler
from .config import Config
from .scanner import (
    ScanOutcome,
    evaluate_scan,
    next_scan_delay,
    render_available_alert,
    render_rename_alert,
    render_vanished_alert,
    within_active_hours,
)
from .store import Store
from .telegram import TelegramClient, TelegramError, esc

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class App:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self._stopping = asyncio.Event()
        self._scan_now = asyncio.Event()
        self._last_scan_at: Optional[datetime] = None
        self._last_scan_error: Optional[str] = None
        self._scan_lock = asyncio.Lock()

    # ---------------- helpers ----------------

    def _now(self) -> datetime:
        return datetime.now(self.config.timezone)

    async def apply_catalog(self, products: list, *, notify: bool) -> ScanOutcome:
        """Fold a catalogue snapshot into tracked state, and optionally alert.

        Every code path that obtains a fresh catalogue funnels through here — the
        timed scan, /check, /resume and /available alike. That matters: before
        this existed, /available fetched live data and threw it away, so browsing
        could reveal a set as available while the notifier, still on its 10-minute
        cadence, had not yet reacted. Any fresh catalogue is now acted upon.
        """
        async with self._scan_lock:
            tracked = self.store.list_tracked()
            if not tracked:
                self._last_scan_at = self._now()
                self._last_scan_error = None
                return ScanOutcome()

            outcome = evaluate_scan(tracked, index_by_id(products))

            # Persist first, notify second. If sending fails we'd rather drop a
            # message than re-fire the same alert on every subsequent scan.
            notified_ids = (
                {item.product_id for item, _ in outcome.became_available}
                if notify
                else set()
            )
            for item, product in outcome.seen:
                self.store.record_scan(
                    item.product_id,
                    available=product.available,
                    name=product.name,
                    url_part=product.url_part,
                    pieces=product.pieces,
                    notified=item.product_id in notified_ids,
                )

            self._last_scan_at = self._now()
            self._last_scan_error = None

            if notify:
                await self._send_alerts(outcome)
            return outcome

    async def _run_scan(self, *, notify: bool) -> ScanOutcome:
        """Fetch a fresh catalogue and apply it. The timed scan's entry point."""
        if not self.store.count_tracked():
            self._last_scan_at = self._now()
            return ScanOutcome()
        products = await self.client.fetch_catalog()
        return await self.apply_catalog(products, notify=notify)

    async def _send_alerts(self, outcome: ScanOutcome) -> None:
        if outcome.became_available:
            await self.telegram.send(
                render_available_alert(outcome.became_available), disable_preview=False
            )
        if outcome.renamed:
            await self.telegram.send(render_rename_alert(outcome.renamed))
        if outcome.vanished:
            await self.telegram.send(render_vanished_alert(outcome.vanished))

    async def _scan_and_report(self) -> str:
        """A scan whose result is reported back as a chat reply (for /check, /resume)."""
        try:
            outcome = await self._run_scan(notify=False)
        except BrickBorrowError as exc:
            self._last_scan_error = str(exc)
            log.warning("manual scan failed: %s", exc)
            return "Couldn't reach the store just now — I'll try again on the next check."

        if not outcome.seen and not outcome.vanished:
            return "Nothing to check — you're not tracking any sets yet."

        available = [(i, p) for i, p in outcome.seen if p.available]
        lines = []
        if available:
            lines.append(f"🟢 <b>{len(available)} of your sets are available now</b>")
            lines.append("")
            for _, product in available:
                suffix = f" · {product.pieces_label}" if product.pieces_label else ""
                lines.append(
                    f'• <a href="{esc(product.url)}">{esc(product.name)}</a>{suffix}'
                )
        else:
            lines.append("🔴 None of your tracked sets are available right now.")

        unavailable = [(i, p) for i, p in outcome.seen if not p.available]
        if available and unavailable:
            lines.append("")
            lines.append(f"<i>{len(unavailable)} still unavailable.</i>")

        if outcome.vanished:
            lines.append("")
            lines.append(f"❔ {len(outcome.vanished)} no longer in the catalogue — see /list.")

        # Renames are important enough to surface even on a manual check.
        if outcome.renamed:
            lines.append("")
            lines.append(render_rename_alert(outcome.renamed))

        return "\n".join(lines)

    async def _status_report(self) -> str:
        now = self._now()
        active = within_active_hours(now, self.config.active_start, self.config.active_end)
        tracked = self.store.count_tracked()
        available = sum(1 for t in self.store.list_tracked() if t.last_available)

        lines = [
            "<b>Status</b>",
            "",
            f"Tracking: <b>{tracked}</b> set(s) — {available} available at last check",
            f"Notifications: {'⏸ <b>paused</b>' if self.store.paused else '▶️ on'}",
            f"Window: {self.config.active_start:%H:%M}–{self.config.active_end:%H:%M} "
            f"{self.config.tz_name} ({'inside' if active else 'outside'} it now)",
            f"Interval: every {self.config.poll_interval_minutes} min",
        ]
        if self._last_scan_at:
            lines.append(f"Last check: {self._last_scan_at:%H:%M on %d %b}")
        else:
            lines.append("Last check: not yet")
        if self._last_scan_error:
            lines.append(f"⚠️ Last error: <code>{esc(self._last_scan_error[:200])}</code>")
        return "\n".join(lines)

    # ---------------- loops ----------------

    async def _scan_loop(self) -> None:
        # A short initial delay lets the command loop register and greet first.
        await asyncio.sleep(5)

        while not self._stopping.is_set():
            now = self._now()
            active = within_active_hours(
                now, self.config.active_start, self.config.active_end
            )

            if active and self.store.count_tracked():
                try:
                    outcome = await self._run_scan(notify=not self.store.paused)
                    if outcome.became_available and self.store.paused:
                        log.info(
                            "%d set(s) became available while paused — staying quiet",
                            len(outcome.became_available),
                        )
                except BrickBorrowError as exc:
                    self._last_scan_error = str(exc)
                    log.warning("scan failed: %s", exc)
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("unexpected error during scan")
                    self._last_scan_error = "unexpected internal error (see logs)"

            delay = next_scan_delay(
                self._now(),
                self.config.active_start,
                self.config.active_end,
                self.config.poll_interval_minutes,
            )
            log.info("next scan in %.0f min", delay / 60)

            # Wake early if /check asked for a scan, or if we're shutting down.
            try:
                await asyncio.wait_for(self._scan_now.wait(), timeout=delay)
                self._scan_now.clear()
            except asyncio.TimeoutError:
                pass
            if self._stopping.is_set():
                return

    async def _command_loop(self) -> None:
        offset = self.store.telegram_offset
        consecutive_errors = 0

        while not self._stopping.is_set():
            try:
                updates = await self.telegram.get_updates(offset, poll_seconds=30)
                consecutive_errors = 0
            except TelegramError as exc:
                consecutive_errors += 1
                # Back off, but cap it so a transient outage doesn't leave the bot
                # unresponsive for an hour.
                backoff = min(60, 2 ** min(consecutive_errors, 6))
                log.warning("getUpdates failed (%s); retrying in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                continue
            except Exception:  # noqa: BLE001
                log.exception("unexpected error polling Telegram")
                await asyncio.sleep(10)
                continue

            for update in updates:
                offset = update.update_id + 1
                self.store.set_telegram_offset(offset)

                # Single-user bot: ignore anyone who isn't the owner.
                if update.chat_id != self.config.telegram_chat_id:
                    log.warning("ignoring message from unauthorised chat %s", update.chat_id)
                    continue
                if not update.text:
                    continue

                log.info("command: %s", update.text[:80])
                try:
                    reply = await self.handler.handle(update.text)
                except Exception:  # noqa: BLE001
                    log.exception("command handler blew up")
                    reply = "Something went wrong handling that — check the logs."
                if reply:
                    await self.telegram.send(reply)

    # ---------------- lifecycle ----------------

    async def run(self) -> None:
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
        async with httpx.AsyncClient(
            timeout=self.config.http_timeout_seconds,
            limits=limits,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as http:
            self.client = BrickBorrowClient(http)
            self.telegram = TelegramClient(
                http, self.config.telegram_bot_token, self.config.telegram_chat_id
            )
            self.handler = CommandHandler(
                self.store,
                self.client,
                max_tracked=self.config.max_tracked_sets,
                interval_minutes=self.config.poll_interval_minutes,
                active_start=f"{self.config.active_start:%H:%M}",
                active_end=f"{self.config.active_end:%H:%M}",
                tz_name=self.config.tz_name,
                trigger_scan=self._scan_and_report,
                status_report=self._status_report,
                apply_catalog=self.apply_catalog,
            )

            await self.telegram.set_my_commands(COMMAND_MENU)

            tracked = self.store.count_tracked()
            await self.telegram.send(
                "🤖 <b>Brick Borrow watcher is up.</b>\n\n"
                f"Tracking {tracked} set(s), checking every "
                f"{self.config.poll_interval_minutes} min between "
                f"{self.config.active_start:%H:%M}–{self.config.active_end:%H:%M} "
                f"{self.config.tz_name}."
                + ("\n\n⏸ Notifications are currently paused." if self.store.paused else "")
                + ("\n\nSend /help to get started." if not tracked else "")
            )

            tasks = [
                asyncio.create_task(self._command_loop(), name="commands"),
                asyncio.create_task(self._scan_loop(), name="scanner"),
            ]
            try:
                await self._stopping.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def request_stop(self) -> None:
        log.info("shutdown requested")
        self._stopping.set()


async def main_async(config: Config) -> None:
    store = Store(config.db_path)
    app = App(config, store)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:  # pragma: no cover - not on Linux
            pass

    try:
        await app.run()
    finally:
        store.close()
