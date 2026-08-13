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

    async def search(self, text, limit=10):
        if self.fail:
            raise BrickBorrowError("store is down")
        text = text.lower()
        return [
            p for p in self.products.values() if text in p.name.lower() or text in p.url_part
        ][:limit]


def product(pid="p1", name="LEGO (1) Set A", slug="lego-1-set-a", available=False):
    return Product(pid, name, slug, available, available)


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

    return CommandHandler(
        store,
        client,
        max_tracked=kw.get("max_tracked", 100),
        interval_minutes=30,
        active_start="07:00",
        active_end="19:00",
        tz_name="Europe/London",
        trigger_scan=scan,
        status_report=status,
    )


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
