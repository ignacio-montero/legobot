"""Persistence tests — the tracking list must survive restarts and scans."""

from __future__ import annotations

import pytest

from legobot.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.sqlite3"))
    yield s
    s.close()


def test_add_is_idempotent(store):
    assert store.add("p1", "slug", "Name") is True
    assert store.add("p1", "slug", "Name") is False
    assert store.count_tracked() == 1


def test_record_scan_updates_state_and_name(store):
    store.add("p1", "slug", "Old Name")
    store.record_scan("p1", available=True, name="New Name", url_part="slug2")
    item = store.get("p1")
    assert item.last_available is True
    assert item.name == "New Name"
    assert item.url_part == "slug2"
    assert item.last_seen_at is not None


def test_notified_at_only_set_when_asked(store):
    store.add("p1", "slug", "N")
    store.record_scan("p1", available=True)
    assert store.get("p1").notified_at is None
    store.record_scan("p1", available=True, notified=True)
    assert store.get("p1").notified_at is not None


def test_last_available_starts_unknown(store):
    store.add("p1", "slug", "N")
    assert store.get("p1").last_available is None


def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "t.sqlite3")
    s1 = Store(path)
    s1.add("p1", "slug", "Name")
    s1.set_paused(True)
    s1.set_telegram_offset(42)
    s1.close()

    s2 = Store(path)
    assert s2.count_tracked() == 1
    assert s2.paused is True
    assert s2.telegram_offset == 42
    s2.close()


def test_ordering_is_stable_for_remove_by_index(store):
    for i in range(5):
        store.add(f"p{i}", f"s{i}", f"N{i}")
    assert [t.product_id for t in store.list_tracked()] == [f"p{i}" for i in range(5)]


def test_remove_returns_whether_it_existed(store):
    store.add("p1", "s", "N")
    assert store.remove("p1") is True
    assert store.remove("p1") is False


def test_pieces_round_trip(store):
    store.add("p1", "s", "N", 757)
    assert store.get("p1").pieces == 757
    assert store.get("p1").pieces_label == "757 pcs"


def test_pieces_default_to_none(store):
    store.add("p1", "s", "N")
    assert store.get("p1").pieces is None
    assert store.get("p1").pieces_label == ""


def test_record_scan_does_not_blank_a_known_count(store):
    """A briefly malformed spec table must not erase a piece count we already had."""
    store.add("p1", "s", "N", 757)
    store.record_scan("p1", available=True, pieces=None)
    assert store.get("p1").pieces == 757
    store.record_scan("p1", available=True, pieces=800)
    assert store.get("p1").pieces == 800


def test_migration_adds_pieces_to_a_pre_existing_database(tmp_path):
    """The deployed bot already has a DB without this column — it must migrate, not crash."""
    import sqlite3

    path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE tracked (
            product_id TEXT PRIMARY KEY, url_part TEXT NOT NULL, name TEXT NOT NULL,
            added_at INTEGER NOT NULL, last_available INTEGER,
            last_seen_at INTEGER, notified_at INTEGER
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO tracked VALUES ('p1','slug','Old Set',1,1,1,NULL);
        """
    )
    old.commit()
    old.close()

    s = Store(path)                      # must migrate on open
    item = s.get("p1")
    assert item is not None              # existing row survives
    assert item.name == "Old Set"
    assert item.pieces is None           # new column, no value yet
    s.record_scan("p1", available=True, pieces=1234)
    assert s.get("p1").pieces == 1234
    s.close()

    Store(path).close()                  # idempotent: re-opening must not fail
