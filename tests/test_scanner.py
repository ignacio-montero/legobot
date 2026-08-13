"""Tests for the notification state machine and the scheduling window.

These are the two places where a bug is invisible: a broken edge trigger either
never notifies (you miss the set) or notifies forever (you mute the bot). Both
are pure functions precisely so they can be pinned down here.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from legobot.brickborrow import Product, extract_slugs, slug_from_url
from legobot.scanner import (
    evaluate_scan,
    next_scan_delay,
    within_active_hours,
)
from legobot.store import TrackedSet

LONDON = ZoneInfo("Europe/London")


def make_tracked(product_id="p1", name="Set A", last_available=None) -> TrackedSet:
    return TrackedSet(
        product_id=product_id,
        url_part="lego-1-set-a",
        name=name,
        added_at=0,
        last_available=last_available,
        last_seen_at=None,
        notified_at=None,
    )


def make_product(product_id="p1", name="Set A", available=True) -> Product:
    return Product(
        id=product_id,
        name=name,
        url_part="lego-1-set-a",
        is_in_stock=available,
        is_sellable=available,
    )


# ---------------- availability semantics ----------------


def test_available_requires_both_flags():
    assert make_product(available=True).available is True
    assert Product("p", "n", "u", is_in_stock=True, is_sellable=False).available is False
    assert Product("p", "n", "u", is_in_stock=False, is_sellable=True).available is False


# ---------------- edge-triggered notifications ----------------


def test_notifies_on_unavailable_to_available():
    tracked = [make_tracked(last_available=False)]
    catalog = {"p1": make_product(available=True)}
    outcome = evaluate_scan(tracked, catalog)
    assert len(outcome.became_available) == 1
    assert not outcome.became_unavailable


def test_does_not_renotify_while_still_available():
    """The bug that would make the bot unusable: repeating every 30 minutes."""
    tracked = [make_tracked(last_available=True)]
    catalog = {"p1": make_product(available=True)}
    outcome = evaluate_scan(tracked, catalog)
    assert outcome.became_available == []


def test_first_ever_scan_of_an_available_set_notifies():
    tracked = [make_tracked(last_available=None)]
    catalog = {"p1": make_product(available=True)}
    outcome = evaluate_scan(tracked, catalog)
    assert len(outcome.became_available) == 1


def test_first_ever_scan_of_an_unavailable_set_is_silent():
    tracked = [make_tracked(last_available=None)]
    catalog = {"p1": make_product(available=False)}
    outcome = evaluate_scan(tracked, catalog)
    assert outcome.became_available == []
    assert outcome.became_unavailable == []


def test_trigger_rearms_after_going_unavailable():
    tracked = [make_tracked(last_available=True)]
    outcome = evaluate_scan(tracked, {"p1": make_product(available=False)})
    assert len(outcome.became_unavailable) == 1

    # ...and the next time it comes back, we alert again.
    tracked = [make_tracked(last_available=False)]
    outcome = evaluate_scan(tracked, {"p1": make_product(available=True)})
    assert len(outcome.became_available) == 1


# ---------------- rename + disappearance detection ----------------


def test_detects_rename_under_a_stable_url():
    """Brick Borrow renames products in place — see RESEARCH.md section 5."""
    tracked = [make_tracked(name="LEGO (10349) Happy Plants", last_available=False)]
    catalog = {"p1": make_product(name="LEGO (10348) Bonsai Tree", available=False)}
    outcome = evaluate_scan(tracked, catalog)
    assert len(outcome.renamed) == 1
    old, new = outcome.renamed[0]
    assert old.name == "LEGO (10349) Happy Plants"
    assert new.name == "LEGO (10348) Bonsai Tree"


def test_missing_product_is_reported_not_guessed():
    tracked = [make_tracked(last_available=True)]
    outcome = evaluate_scan(tracked, {})
    assert len(outcome.vanished) == 1
    # Crucially it is NOT reported as "became unavailable" — we don't know that.
    assert outcome.became_unavailable == []


def test_multiple_sets_are_batched():
    tracked = [
        make_tracked("a", "A", last_available=False),
        make_tracked("b", "B", last_available=False),
        make_tracked("c", "C", last_available=True),
    ]
    catalog = {
        "a": make_product("a", "A", available=True),
        "b": make_product("b", "B", available=True),
        "c": make_product("c", "C", available=True),
    }
    outcome = evaluate_scan(tracked, catalog)
    assert len(outcome.became_available) == 2


# ---------------- active hours ----------------


@pytest.mark.parametrize(
    "hour,expected",
    [(6, False), (7, True), (12, True), (19, True), (19.5, False), (23, False), (3, False)],
)
def test_within_active_hours(hour, expected):
    now = datetime(2026, 8, 13, int(hour), 30 if hour % 1 else 0, tzinfo=LONDON)
    assert within_active_hours(now, dtime(7, 0), dtime(19, 0)) is expected


def test_window_can_wrap_past_midnight():
    start, end = dtime(22, 0), dtime(4, 0)
    assert within_active_hours(datetime(2026, 8, 13, 23, 0, tzinfo=LONDON), start, end)
    assert within_active_hours(datetime(2026, 8, 13, 2, 0, tzinfo=LONDON), start, end)
    assert not within_active_hours(datetime(2026, 8, 13, 12, 0, tzinfo=LONDON), start, end)


def test_inside_window_sleeps_one_interval():
    now = datetime(2026, 8, 13, 10, 0, tzinfo=LONDON)
    assert next_scan_delay(now, dtime(7, 0), dtime(19, 0), 30) == 30 * 60


def test_after_hours_sleeps_until_the_window_opens():
    now = datetime(2026, 8, 13, 20, 0, tzinfo=LONDON)
    delay = next_scan_delay(now, dtime(7, 0), dtime(19, 0), 30)
    assert delay == pytest.approx(11 * 3600)  # 20:00 -> 07:00 next day


def test_before_hours_sleeps_until_the_same_morning():
    now = datetime(2026, 8, 13, 5, 0, tzinfo=LONDON)
    delay = next_scan_delay(now, dtime(7, 0), dtime(19, 0), 30)
    assert delay == pytest.approx(2 * 3600)


# ---------------- URL parsing ----------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "https://www.brickborrow.com/product-page/lego-10349-icons-happy-plants",
            "lego-10349-icons-happy-plants",
        ),
        # Percent-encoded accent, as copied from a browser address bar.
        (
            "https://www.brickborrow.com/product-page/lego-72150-pok%C3%A9mon-munchlax",
            "lego-72150-pokémon-munchlax",
        ),
        # Tracking params and a trailing full stop from a chat message.
        (
            "https://www.brickborrow.com/product-page/lego-1-set?utm_source=x.",
            "lego-1-set",
        ),
        ("www.brickborrow.com/product-page/lego-1-set", "lego-1-set"),
        ("lego-1-set", "lego-1-set"),
        ("https://example.com/product-page/lego-1-set", None),
        ("https://www.brickborrow.com/pickyoursets", None),
        ("", None),
    ],
)
def test_slug_from_url(raw, expected):
    assert slug_from_url(raw) == expected


def test_extract_multiple_links_from_one_message():
    text = (
        "want these two\n"
        "https://www.brickborrow.com/product-page/set-a\n"
        "https://www.brickborrow.com/product-page/set-b "
        "https://www.brickborrow.com/product-page/set-a"
    )
    assert extract_slugs(text) == ["set-a", "set-b"]


# ---------------- piece counts ----------------

from legobot.brickborrow import format_pieces, parse_pieces  # noqa: E402


def info(title, description):
    return [{"title": title, "description": description}]


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<p>757</p>\n", 757),          # the common wrapper
        ("<div>2912</div>\n", 2912),    # the store is inconsistent about tags
        ("<span>670</span>", 670),
        ("<p>2,532</p>", 2532),         # one entry uses a thousands separator
        ("  1  ", 1),                   # smallest real set
        ("<p>10001</p>", 10001),        # largest (Eiffel Tower)
    ],
)
def test_parse_pieces_handles_the_stores_html_variants(html, expected):
    assert parse_pieces(info("Pieces", html)) == expected


@pytest.mark.parametrize(
    "html",
    [
        "<p>01-3000+</p>",   # gift card / mystery box: a RANGE, not a count
        "<p>varies</p>",
        "<p></p>",
        "",
    ],
)
def test_parse_pieces_refuses_to_guess(html):
    """A wrong piece count is worse than an absent one — '01-3000+' must not become 1."""
    assert parse_pieces(info("Pieces", html)) is None


def test_parse_pieces_absent_field():
    assert parse_pieces(info("Age Range", "<p>18+</p>")) is None
    assert parse_pieces([]) is None
    assert parse_pieces(None) is None


def test_parse_pieces_is_case_insensitive_on_the_title():
    assert parse_pieces(info("pieces", "<p>42</p>")) == 42
    assert parse_pieces(info("Piece Count", "<p>42</p>")) == 42


def test_parse_pieces_ignores_other_spec_rows():
    rows = [
        {"title": "Age Range", "description": "<p>18+</p>"},
        {"title": "Pieces", "description": "<p>757</p>"},
        {"title": "Set No.", "description": "<p>72150</p>"},
    ]
    assert parse_pieces(rows) == 757


def test_product_exposes_pieces_from_the_api_payload():
    p = Product.from_api(
        {
            "id": "x",
            "name": "N",
            "urlPart": "u",
            "isInStock": True,
            "isSellable": True,
            "additionalInfo": [{"title": "Pieces", "description": "<p>2,532</p>"}],
        }
    )
    assert p.pieces == 2532
    assert p.pieces_label == "2,532 pcs"


def test_format_pieces_uses_thousands_separators_and_tolerates_none():
    assert format_pieces(10001) == "10,001 pcs"
    assert format_pieces(757) == "757 pcs"
    assert format_pieces(None) == ""
