"""Command-handling tests against a fake store client — no network."""

from __future__ import annotations

import pytest

from legobot.brickborrow import BrickBorrowError, Product
from legobot.commands import CommandHandler
from legobot.store import Store


class FakeClient:
    """Stands in for BrickBorrowClient. Records calls, can be made to fail."""

    def __init__(self, products=None, fail=False):
        self.products = {p.url_part: p for p in (products or [])}
        self.fail = fail

    async def product_by_slug(self, slug):
        if self.fail:
            raise BrickBorrowError("store is down")
        return self.products.get(slug)

    async def fetch_catalog(self):
        if self.fail:
            raise BrickBorrowError("store is down")
        return list(self.products.values())

    async def search(self, text, limit=10):
        if self.fail:
            raise BrickBorrowError("store is down")
        text = text.lower()
        return [
            p for p in self.products.values() if text in p.name.lower() or text in p.url_part
        ][:limit]


def product(pid="p1", name="LEGO (1) Set A", slug="lego-1-set-a", available=False,
            pieces=None):
    return Product(pid, name, slug, available, available, pieces)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def build(store, client, **kw):
    async def scan():
        return "scanned"

    async def status():
        return "status"

    calls = []

    async def apply_catalog(products, *, notify):
        """Mimics App.apply_catalog: persist state, report what's tracked."""
        from legobot.brickborrow import index_by_id
        from legobot.scanner import evaluate_scan

        calls.append({"count": len(products), "notify": notify})
        outcome = evaluate_scan(store.list_tracked(), index_by_id(products))
        for item, product in outcome.seen:
            store.record_scan(
                item.product_id, available=product.available, pieces=product.pieces
            )
        return outcome

    handler = CommandHandler(
        store,
        client,
        max_tracked=kw.get("max_tracked", 100),
        interval_minutes=30,
        active_start="07:00",
        active_end="19:00",
        tz_name="Europe/London",
        trigger_scan=scan,
        status_report=status,
        apply_catalog=None if kw.get("no_apply") else apply_catalog,
    )
    handler.apply_calls = calls
    return handler


URL_A = "https://www.brickborrow.com/product-page/lego-1-set-a"


# ---------------- adding ----------------


@pytest.mark.asyncio
async def test_add_by_link_reports_the_real_name(store):
    """The URL says 'happy plants'; the store says 'Bonsai'. Show the store's answer."""
    handler = build(
        store,
        FakeClient([product(name="LEGO (10348) Bonsai Tree", slug="lego-10349-happy-plants")]),
    )
    reply = await handler.handle(
        "/add https://www.brickborrow.com/product-page/lego-10349-happy-plants"
    )
    assert "Bonsai Tree" in reply
    assert store.count_tracked() == 1


@pytest.mark.asyncio
async def test_bare_link_without_a_command_adds_it(store):
    handler = build(store, FakeClient([product()]))
    reply = await handler.handle(f"look at this {URL_A}")
    assert "✅" in reply
    assert store.count_tracked() == 1


@pytest.mark.asyncio
async def test_adding_twice_is_not_an_error(store):
    handler = build(store, FakeClient([product()]))
    await handler.handle(f"/add {URL_A}")
    reply = await handler.handle(f"/add {URL_A}")
    assert "already tracking" in reply
    assert store.count_tracked() == 1


@pytest.mark.asyncio
async def test_add_seeds_state_so_the_next_scan_is_not_a_false_edge(store):
    """Adding an already-available set must not re-announce it on the next scan."""
    handler = build(store, FakeClient([product(available=True)]))
    await handler.handle(f"/add {URL_A}")
    assert store.get("p1").last_available is True


@pytest.mark.asyncio
async def test_add_unknown_slug_is_reported(store):
    handler = build(store, FakeClient([]))
    reply = await handler.handle(f"/add {URL_A}")
    assert "no such set" in reply
    assert store.count_tracked() == 0


@pytest.mark.asyncio
async def test_add_survives_the_store_being_down(store):
    handler = build(store, FakeClient([product()], fail=True))
    reply = await handler.handle(f"/add {URL_A}")
    assert "couldn't reach" in reply.lower()
    assert store.count_tracked() == 0


@pytest.mark.asyncio
async def test_add_respects_the_cap(store):
    handler = build(store, FakeClient([product("p1"), product("p2", slug="lego-2-b")]), max_tracked=1)
    await handler.handle(f"/add {URL_A}")
    reply = await handler.handle("/add https://www.brickborrow.com/product-page/lego-2-b")
    assert "maximum" in reply
    assert store.count_tracked() == 1


@pytest.mark.asyncio
async def test_add_multiple_links_in_one_message(store):
    handler = build(
        store, FakeClient([product("p1"), product("p2", name="Set B", slug="lego-2-b")])
    )
    await handler.handle(f"/add {URL_A} https://www.brickborrow.com/product-page/lego-2-b")
    assert store.count_tracked() == 2


# ---------------- listing and removing ----------------


@pytest.mark.asyncio
async def test_remove_by_index_from_list(store):
    handler = build(
        store, FakeClient([product("p1"), product("p2", name="Set B", slug="lego-2-b")])
    )
    await handler.handle(f"/add {URL_A} https://www.brickborrow.com/product-page/lego-2-b")
    reply = await handler.handle("/remove 1")
    assert "Removed" in reply
    assert [t.product_id for t in store.list_tracked()] == ["p2"]


@pytest.mark.asyncio
async def test_remove_by_link(store):
    handler = build(store, FakeClient([product()]))
    await handler.handle(f"/add {URL_A}")
    await handler.handle(f"/remove {URL_A}")
    assert store.count_tracked() == 0


@pytest.mark.asyncio
async def test_remove_out_of_range_index(store):
    handler = build(store, FakeClient([product()]))
    await handler.handle(f"/add {URL_A}")
    reply = await handler.handle("/remove 7")
    assert "no #7" in reply
    assert store.count_tracked() == 1


@pytest.mark.asyncio
async def test_list_empty(store):
    handler = build(store, FakeClient())
    assert "not tracking anything" in await handler.handle("/list")


# ---------------- pause / resume ----------------


@pytest.mark.asyncio
async def test_pause_and_resume_round_trip(store):
    handler = build(store, FakeClient([product()]))
    await handler.handle(f"/add {URL_A}")

    assert store.paused is False
    reply = await handler.handle("/pause")
    assert "Paused" in reply
    assert store.paused is True

    assert "Already paused" in await handler.handle("/pause")

    reply = await handler.handle("/resume")
    assert store.paused is False
    # Resume should immediately report what's available rather than make you wait.
    assert "scanned" in reply


# ---------------- misc ----------------


@pytest.mark.asyncio
async def test_search_offers_a_ready_to_paste_add_command(store):
    handler = build(store, FakeClient([product(name="LEGO (10349) Happy Plants")]))
    reply = await handler.handle("/search happy")
    assert "Happy Plants" in reply
    assert "/add https://www.brickborrow.com/product-page/lego-1-set-a" in reply


@pytest.mark.asyncio
async def test_non_link_text_gets_guidance_not_silence(store):
    handler = build(store, FakeClient())
    reply = await handler.handle("hello there")
    assert "/search" in reply


@pytest.mark.asyncio
async def test_clear_requires_confirmation(store):
    handler = build(store, FakeClient([product()]))
    await handler.handle(f"/add {URL_A}")
    assert "sure" in await handler.handle("/clear")
    assert store.count_tracked() == 1
    await handler.handle("/clear confirm")
    assert store.count_tracked() == 0


@pytest.mark.asyncio
async def test_unknown_command(store):
    handler = build(store, FakeClient())
    assert "Unknown command" in await handler.handle("/wat")


@pytest.mark.asyncio
async def test_add_accepts_a_bare_slug_typed_by_hand(store):
    handler = build(store, FakeClient([product()]))
    reply = await handler.handle("/add lego-1-set-a")
    assert "✅" in reply
    assert store.count_tracked() == 1


@pytest.mark.asyncio
async def test_free_text_prose_is_not_mistaken_for_slugs(store):
    """Guards the bug where every word in a sentence parsed as a product slug."""
    handler = build(store, FakeClient([product()]))
    reply = await handler.handle("hey can you check on that set for me please")
    assert store.count_tracked() == 0
    assert "/search" in reply


# ---------------- /available ----------------


def catalogue():
    """A mixed catalogue: available/unavailable, with and without piece counts."""
    return [
        product("big", "Eiffel Tower", "eiffel", available=True, pieces=10001),
        product("mid", "Titanic", "titanic", available=True, pieces=9090),
        product("small", "Munchlax", "munchlax", available=True, pieces=757),
        product("gone", "Death Star", "deathstar", available=False, pieces=9023),
        product("card", "Gift Card", "giftcard", available=True, pieces=None),
    ]


@pytest.mark.asyncio
async def test_available_ranks_by_pieces_descending(store):
    handler = build(store, FakeClient(catalogue()))
    reply = await handler.handle("/available")
    assert reply.index("Eiffel Tower") < reply.index("Titanic") < reply.index("Munchlax")
    assert "10,001" in reply


@pytest.mark.asyncio
async def test_available_excludes_unavailable_sets(store):
    handler = build(store, FakeClient(catalogue()))
    reply = await handler.handle("/available")
    assert "Death Star" not in reply       # 9023 pcs, but not borrowable
    assert "4 sets available now" in reply  # gift card counts as available...


@pytest.mark.asyncio
async def test_available_excludes_items_with_no_piece_count(store):
    """A piece-count ranking has no place for the gift card / mystery box."""
    handler = build(store, FakeClient(catalogue()))
    reply = await handler.handle("/available")
    assert "Gift Card" not in reply


@pytest.mark.asyncio
async def test_available_marks_sets_you_already_track(store):
    handler = build(store, FakeClient(catalogue()))
    await handler.handle("/add https://www.brickborrow.com/product-page/titanic")
    reply = await handler.handle("/available")

    # Tracked sets are pinned at the top AND ticked in the ranking below, so
    # assert against the numbered ranking specifically.
    ranking = reply.split("sets available now", 1)[1]
    titanic = [ln for ln in ranking.split("\n") if "Titanic" in ln][0]
    eiffel = [ln for ln in ranking.split("\n") if "Eiffel" in ln][0]
    assert "✅" in titanic
    assert "✅" not in eiffel


@pytest.mark.asyncio
async def test_available_respects_an_explicit_limit(store):
    handler = build(store, FakeClient(catalogue()))
    reply = await handler.handle("/available 2")
    assert "Eiffel Tower" in reply and "Titanic" in reply
    assert "Munchlax" not in reply
    assert "1 more" in reply


@pytest.mark.asyncio
async def test_available_caps_the_limit(store):
    from legobot.commands import MAX_AVAILABLE_LIMIT

    many = [
        product(f"p{i}", f"Set {i}", f"s{i}", available=True, pieces=i + 1)
        for i in range(MAX_AVAILABLE_LIMIT + 30)
    ]
    handler = build(store, FakeClient(many))
    reply = await handler.handle("/available 9999")
    assert reply.count(" pcs — ") == MAX_AVAILABLE_LIMIT


@pytest.mark.asyncio
async def test_available_rejects_a_non_numeric_argument(store):
    handler = build(store, FakeClient(catalogue()))
    reply = await handler.handle("/available lots")
    assert "number" in reply.lower()


@pytest.mark.asyncio
async def test_available_when_nothing_is_available(store):
    handler = build(store, FakeClient([product("a", "A", "a", available=False, pieces=100)]))
    assert "Nothing is available" in await handler.handle("/available")


@pytest.mark.asyncio
async def test_available_survives_the_store_being_down(store):
    handler = build(store, FakeClient(catalogue(), fail=True))
    reply = await handler.handle("/available")
    assert "couldn't reach" in reply.lower()


@pytest.mark.asyncio
async def test_available_aliases(store):
    handler = build(store, FakeClient(catalogue()))
    for alias in ("/avail", "/browse", "/top"):
        assert "Eiffel Tower" in await handler.handle(alias)


# ---------------- /available feeds the notifier ----------------
#
# Regression: /available used to fetch a live catalogue and throw it away, so
# browsing could reveal a tracked set as available minutes before the scheduled
# scan reacted. Any fresh catalogue must now update tracked state.


@pytest.mark.asyncio
async def test_available_updates_tracked_state_from_its_own_fetch(store):
    catalog = [product("mona", "Mona Lisa", "mona", available=False, pieces=1503)]
    client = FakeClient(catalog)
    handler = build(store, client)
    await handler.handle("/add https://www.brickborrow.com/product-page/mona")
    assert store.get("mona").last_available is False

    # The set becomes available; the user browses before the next scheduled scan.
    client.products["mona"] = product("mona", "Mona Lisa", "mona", available=True, pieces=1503)
    await handler.handle("/available")

    assert store.get("mona").last_available is True
    assert handler.apply_calls[-1]["notify"] is False


@pytest.mark.asyncio
async def test_available_pins_your_tracked_sets_above_the_ranking(store):
    """A tracked set must never be buried below the piece-count cutoff."""
    catalog = [
        product(f"big{i}", f"Huge Set {i}", f"big{i}", available=True, pieces=9000 - i)
        for i in range(25)
    ]
    catalog.append(product("mona", "Mona Lisa", "mona", available=True, pieces=1503))
    handler = build(store, FakeClient(catalog))
    await handler.handle("/add https://www.brickborrow.com/product-page/mona")

    reply = await handler.handle("/available")
    assert "set(s) you track are available" in reply
    # It appears before the catalogue-wide ranking, despite ranking ~26th.
    assert reply.index("Mona Lisa") < reply.index("sets available now")


@pytest.mark.asyncio
async def test_available_has_no_pinned_section_when_none_of_yours_are_free(store):
    catalog = [product("mona", "Mona Lisa", "mona", available=False, pieces=1503),
               product("big", "Big Set", "big", available=True, pieces=5000)]
    handler = build(store, FakeClient(catalog))
    await handler.handle("/add https://www.brickborrow.com/product-page/mona")
    reply = await handler.handle("/available")
    assert "you track are available" not in reply
    assert "Big Set" in reply


@pytest.mark.asyncio
async def test_available_still_works_without_the_hook(store):
    handler = build(store, FakeClient(catalogue()), no_apply=True)
    assert "Eiffel Tower" in await handler.handle("/available")
