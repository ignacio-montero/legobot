"""The watch loop: sweep the catalog, diff against last-known state, notify.

The whole point of the bot lives in `evaluate_scan`, which is deliberately a
*pure function* — it takes the tracked sets and a catalog snapshot and returns
the events that follow. No I/O, no clock, no network. That makes the tricky part
(the edge-triggered notification rules) directly unit-testable, which matters
because the failure modes are "silently never notifies" and "notifies every 30
minutes forever" — both of which are miserable to debug in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from .brickborrow import Product
from .store import TrackedSet

log = logging.getLogger(__name__)


@dataclass
class ScanOutcome:
    """What a scan concluded, ready to be turned into messages and DB writes."""

    became_available: list[tuple[TrackedSet, Product]] = field(default_factory=list)
    became_unavailable: list[tuple[TrackedSet, Product]] = field(default_factory=list)
    renamed: list[tuple[TrackedSet, Product]] = field(default_factory=list)
    vanished: list[TrackedSet] = field(default_factory=list)
    seen: list[tuple[TrackedSet, Product]] = field(default_factory=list)

    @property
    def has_alerts(self) -> bool:
        return bool(self.became_available or self.renamed or self.vanished)


def evaluate_scan(tracked: list[TrackedSet], catalog: dict[str, Product]) -> ScanOutcome:
    """Diff tracked sets against a catalog snapshot.

    Notifications are **edge-triggered**: we alert on the unavailable→available
    transition, not on the available state. Otherwise every 30-minute scan would
    re-announce the same set until it was taken. Going available→unavailable
    silently re-arms the trigger.

    A set whose `last_available` is NULL has never been scanned. We treat a
    first-scan "available" as a notifiable edge, so adding a set that happens to
    be free right now tells you immediately.
    """
    outcome = ScanOutcome()

    for item in tracked:
        product = catalog.get(item.product_id)
        if product is None:
            # Delisted, hidden, or the store dropped it. Don't guess — say so.
            outcome.vanished.append(item)
            continue

        outcome.seen.append((item, product))

        if product.name != item.name:
            # Brick Borrow renames products under a stable URL (RESEARCH.md §5),
            # so a rename can silently change *which set* a link tracks.
            outcome.renamed.append((item, product))

        was = item.last_available
        now = product.available
        if now and was is not True:
            outcome.became_available.append((item, product))
        elif not now and was is True:
            outcome.became_unavailable.append((item, product))

    return outcome


def within_active_hours(now: datetime, start: dtime, end: dtime) -> bool:
    """Is `now` inside the daily polling window?

    Handles a window that wraps past midnight (start > end), which is not the
    default 07:00–19:00 but costs one line to support and avoids a silent
    misconfiguration if the user ever sets ACTIVE_START=22:00.
    """
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def next_scan_delay(
    now: datetime,
    start: dtime,
    end: dtime,
    interval_minutes: int,
) -> float:
    """Seconds to sleep before the next scan.

    Inside the window: the normal interval. Outside it: sleep until the window
    opens, so an overnight bot makes zero requests instead of polling a warehouse
    that nobody is working in.
    """
    if within_active_hours(now, start, end):
        return interval_minutes * 60.0

    opening = now.replace(
        hour=start.hour, minute=start.minute, second=0, microsecond=0
    )
    if opening <= now:
        opening += timedelta(days=1)
    return max(60.0, (opening - now).total_seconds())


def render_available_alert(pairs: list[tuple[TrackedSet, Product]]) -> str:
    from .telegram import esc

    if len(pairs) == 1:
        _, product = pairs[0]
        detail = f"\n{product.pieces_label}" if product.pieces_label else ""
        return (
            "🟢 <b>Available now</b>\n\n"
            f"<b>{esc(product.name)}</b>{detail}\n"
            f'<a href="{esc(product.url)}">Open on Brick Borrow →</a>'
        )

    lines = [f"🟢 <b>{len(pairs)} sets just became available</b>", ""]
    for _, product in pairs:
        suffix = f" · {product.pieces_label}" if product.pieces_label else ""
        lines.append(f'• <a href="{esc(product.url)}">{esc(product.name)}</a>{suffix}')
    return "\n".join(lines)


def render_rename_alert(pairs: list[tuple[TrackedSet, Product]]) -> str:
    from .telegram import esc

    lines = [
        "⚠️ <b>A tracked set was renamed</b>",
        "",
        "Brick Borrow reuses URLs when they swap a set, so this link may now "
        "point at a different set than when you added it:",
        "",
    ]
    for item, product in pairs:
        lines.append(f"• was: <i>{esc(item.name)}</i>")
        lines.append(f'  now: <a href="{esc(product.url)}">{esc(product.name)}</a>')
    lines.append("")
    lines.append("Use /remove if this is no longer the set you wanted.")
    return "\n".join(lines)


def render_vanished_alert(items: list[TrackedSet]) -> str:
    from .telegram import esc

    lines = ["⚠️ <b>Tracked set no longer in the catalogue</b>", ""]
    for item in items:
        lines.append(f'• <a href="{esc(item.url)}">{esc(item.name)}</a>')
    lines.append("")
    lines.append("It may have been hidden or retired. It stays tracked in case it returns.")
    return "\n".join(lines)
