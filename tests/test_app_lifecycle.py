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
