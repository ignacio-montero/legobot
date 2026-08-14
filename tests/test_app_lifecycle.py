"""End-to-end test of App.apply_catalog — the real wiring, not a fake.

The unit tests pin evaluate_scan; this pins the layer that decides what actually
gets *sent*, because that is where the announced-flag bookkeeping lives.
"""

from __future__ import annotations

from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest

from legobot.app import App
from legobot.brickborrow import Product
from legobot.config import Config
from legobot.store import Store


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send(self, text, disable_preview=True):
        self.sent.append(text)


@pytest.fixture
def app(tmp_path):
    config = Config(
        telegram_bot_token="t", telegram_chat_id=1,
        db_path=str(tmp_path / "db.sqlite3"),
        poll_interval_minutes=5,
        active_start=dtime(7, 0), active_end=dtime(19, 0),
        timezone=ZoneInfo("Europe/London"),
    )
    store = Store(config.db_path)
    a = App(config, store)
    a.telegram = FakeTelegram()
    yield a
    store.close()


def prod(available, name="Mona Lisa"):
    return Product("mona", name, "mona", available, available, 1503)


@pytest.mark.asyncio
async def test_full_cycle_sends_exactly_two_messages(app):
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=False)

    # becomes available -> one alert
    await app.apply_catalog([prod(True)], notify=True)
    assert len(app.telegram.sent) == 1
    assert "Available now" in app.telegram.sent[0]
    assert app.store.get("mona").announced is True

    # stays available across several scans -> silence
    for _ in range(5):
        await app.apply_catalog([prod(True)], notify=True)
    assert len(app.telegram.sent) == 1, "must not repeat while still available"

    # taken -> exactly one more alert
    await app.apply_catalog([prod(False)], notify=True)
    assert len(app.telegram.sent) == 2
    assert "Gone again" in app.telegram.sent[1]
    assert app.store.get("mona").announced is False

    # stays gone -> silence
    for _ in range(3):
        await app.apply_catalog([prod(False)], notify=True)
    assert len(app.telegram.sent) == 2

    # returns -> alerts again, cycle re-armed
    await app.apply_catalog([prod(True)], notify=True)
    assert len(app.telegram.sent) == 3
    assert "Available now" in app.telegram.sent[2]


@pytest.mark.asyncio
async def test_browsing_informs_so_a_later_removal_still_alerts(app):
    """/available tells you without a push; the 'gone' alert must still fire."""
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=False)

    # Simulates /available: no push, but the reply informs.
    await app.apply_catalog([prod(True)], notify=False, inform=True)
    assert app.telegram.sent == []
    assert app.store.get("mona").announced is True

    await app.apply_catalog([prod(False)], notify=True)
    assert len(app.telegram.sent) == 1
    assert "Gone again" in app.telegram.sent[0]


@pytest.mark.asyncio
async def test_paused_scan_neither_announces_nor_reports_removal(app):
    """While paused we stay silent at both ends, and don't fake having told you."""
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=False)

    await app.apply_catalog([prod(True)], notify=False)   # paused
    assert app.telegram.sent == []
    assert app.store.get("mona").announced is False

    await app.apply_catalog([prod(False)], notify=True)
    assert app.telegram.sent == [], "no 'gone' for something never announced"


@pytest.mark.asyncio
async def test_renames_are_still_reported(app):
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=False)
    await app.apply_catalog([prod(False, name="Something Else")], notify=True)
    assert any("renamed" in m.lower() for m in app.telegram.sent)


# ---------------- fast tier (in-stock-only, partial snapshots) ----------------


@pytest.mark.asyncio
async def test_fast_tier_alerts_from_an_in_stock_only_snapshot(app):
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=False)

    # The fast tier is handed ONLY the in-stock products.
    await app.apply_catalog([prod(True)], notify=True, partial=True)
    assert len(app.telegram.sent) == 1
    assert "Available now" in app.telegram.sent[0]
    assert app.store.get("mona").last_available is True


@pytest.mark.asyncio
async def test_fast_tier_absence_means_no_information_not_unavailable(app):
    """The load-bearing rule: the fast tier can turn a set ON, never off.

    The in-stock filter omits five variant-managed merch products that a full
    sweep calls available. If absence meant "unavailable" they would flap 🔴/🟢
    forever between the two tiers.
    """
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=True, announced=True)

    await app.apply_catalog([], notify=True, partial=True)   # empty snapshot

    assert app.store.get("mona").last_available is True, "state must be untouched"
    assert app.telegram.sent == [], "no alert from an absence"


@pytest.mark.asyncio
async def test_full_sweep_still_reports_a_genuinely_delisted_set(app):
    """The slow tier keeps the ability the fast tier gives up."""
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=True, announced=True)

    await app.apply_catalog([], notify=True, partial=False)
    assert any("no longer in the catalogue" in m for m in app.telegram.sent)


@pytest.mark.asyncio
async def test_fast_tier_does_not_fire_spurious_rename_or_vanished_alerts(app):
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=True)

    await app.apply_catalog([], notify=True, partial=True)
    assert app.telegram.sent == []
    assert app.store.get("mona").name == "Mona Lisa"


@pytest.mark.asyncio
async def test_a_merch_style_gap_product_cannot_flap_between_tiers(app):
    """Regression for the 5 products the in-stock filter omits."""
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=False)

    # Full sweep says available -> one alert.
    await app.apply_catalog([prod(True)], notify=True, partial=False)
    assert len(app.telegram.sent) == 1

    # Fast tier never sees it (the filter omits it). Ten cycles, still silent.
    for _ in range(10):
        await app.apply_catalog([], notify=True, partial=True)
    assert len(app.telegram.sent) == 1, "fast tier must not contradict the full sweep"
    assert app.store.get("mona").last_available is True


@pytest.mark.asyncio
async def test_fast_and_slow_tiers_agree_over_a_full_cycle(app):
    """Mixing tiers must not double-alert or lose an edge."""
    app.store.add("mona", "mona", "Mona Lisa", 1503)
    app.store.record_scan("mona", available=False)

    await app.apply_catalog([prod(True)], notify=True, partial=True)   # fast: appears
    await app.apply_catalog([prod(True)], notify=True, partial=False)  # slow: confirms
    assert len(app.telegram.sent) == 1, "slow tier must not re-announce"

    # Taken: the fast tier stays silent (absence = no info); the SLOW tier owns
    # the removal edge, which is not latency-critical.
    await app.apply_catalog([], notify=True, partial=True)
    assert len(app.telegram.sent) == 1, "fast tier must not fire the removal"

    await app.apply_catalog([prod(False)], notify=True, partial=False)
    assert len(app.telegram.sent) == 2
    assert "Gone again" in app.telegram.sent[1]
