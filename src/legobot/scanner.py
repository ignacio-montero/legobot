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
    # Subset of became_unavailable that we had actually announced. Filled by the
    # app, which is the only layer that knows what was communicated.
    gone_announced: list[tuple[TrackedSet, Product]] = field(default_factory=list)
    # Product ids currently available. Stated explicitly rather than re-derived
    # from the display objects, because in a partial scan the display object for
    # an unavailable set is the TrackedSet itself, which has no `.available`.
    available_ids: set = field(default_factory=set)

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
        if now:
            outcome.available_ids.add(item.product_id)
        if now and was is not True:
            outcome.became_available.append((item, product))
        elif not now and was is True:
            outcome.became_unavailable.append((item, product))

    return outcome


def evaluate_availability(
    tracked: list[TrackedSet], in_stock: list[Product]
) -> ScanOutcome:
    """Fast-tier diff against an in-stock-only snapshot.

    **This function can only ever turn a set ON, never off.** It reports sets
    that have just become available and ignores everything absent from the
    snapshot. That asymmetry is deliberate and load-bearing:

    The in-stock filter is not a perfect complement of the full sweep — five
    variant-managed merchandise products (jewellery, the builder's mat) read as
    available in a full sweep yet never appear in the filtered list. If absence
    here meant "unavailable", any such product would flap: the fast tier would
    fire 🔴, the full sweep 5 minutes later would fire 🟢, forever.

    So the fast tier only accelerates the *urgent* edge — "it's free, go now" —
    and the authoritative full sweep owns going unavailable, which is not
    latency-critical. Absence in a partial snapshot means **no new information**.
    """
    outcome = ScanOutcome()
    present = {p.id: p for p in in_stock}

    for item in tracked:
        product = present.get(item.product_id)
        if product is None or not product.available:
            continue  # no information — leave it to the full sweep

        outcome.seen.append((item, product))
        outcome.available_ids.add(item.product_id)
        if item.last_available is not True:
            outcome.became_available.append((item, product))

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


def render_gone_alert(pairs: list[tuple[TrackedSet, Product]]) -> str:
    """Closes the loop on an alert you didn't act on.

    Only sent for sets we actually announced as available (see TrackedSet
    .announced) — telling you something ended that you were never told had
    started is just noise.
    """
    from .telegram import esc

    if len(pairs) == 1:
        _, product = pairs[0]
        detail = f"\n{product.pieces_label}" if product.pieces_label else ""
        return (
            "🔴 <b>Gone again</b>\n\n"
            f"<b>{esc(product.name)}</b>{detail}\n"
            "Someone else took it. Still tracking — I'll tell you if it returns."
        )

    lines = [f"🔴 <b>{len(pairs)} sets are no longer available</b>", ""]
    for _, product in pairs:
        suffix = f" · {product.pieces_label}" if product.pieces_label else ""
        lines.append(f"• {esc(product.name)}{suffix}")
    lines.append("")
    lines.append("Still tracking them — I'll tell you if they return.")
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
