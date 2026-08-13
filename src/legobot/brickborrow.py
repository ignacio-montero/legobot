"""Client for the Brick Borrow (Wix Stores) storefront catalog API.

See docs/RESEARCH.md for how this API was found and why we talk to it instead of
scraping the page. The short version:

  * Product *pages* are members-only and client-rendered, so there is nothing to
    scrape without a logged-in headless browser.
  * The *catalog* GraphQL API is readable anonymously, returns exact stock, and
    a full 1127-product sweep costs 12 requests / ~2 seconds.

So the bot needs no password and no browser.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse

import httpx

log = logging.getLogger(__name__)

SITE = "https://www.brickborrow.com"
TOKENS_URL = f"{SITE}/_api/v1/access-tokens"
GRAPHQL_URL = f"{SITE}/_api/wix-ecommerce-storefront-web/api"

# The Wix Stores app. Stable per-app, not per-site.
STORES_APP_ID = "1380b703-ce81-ff05-f115-39571d94dfcd"

PAGE_SIZE = 100

# Fields we read off every product. `isInStock`/`isSellable` are the honest
# availability signals; `inventory.status` is NOT (it reads "in_stock" on
# out-of-stock products — see RESEARCH.md §4c).
#
# `additionalInfo` carries the store's spec table — "Age Range", "Pieces",
# "Set No." — and costs nothing extra: it comes back in the bulk list query, so
# piece counts add zero requests to a scan.
_PRODUCT_FIELDS = (
    "id name urlPart isInStock isSellable additionalInfo { title description }"
)

# Values arrive as HTML fragments, and the store is inconsistent about the
# wrapper: <p>757</p>, <div>2912</div> and <span>…</span> all occur. One entry
# uses a thousands separator ("2,532").
_TAG_RE = re.compile(r"<[^>]+>")
_PIECES_RE = re.compile(r"\d[\d,]*")

_LIST_QUERY = """
query($offset: Int!, $limit: Int!) {
  catalog {
    products(offset: $offset, limit: $limit, onlyVisible: true) {
      totalCount
      list { %s }
    }
  }
}
""" % _PRODUCT_FIELDS

_BY_SLUG_QUERY = """
query($slug: String!) {
  catalog { product(slug: $slug, onlyVisible: true) { %s } }
}
""" % _PRODUCT_FIELDS

_SEARCH_QUERY = """
query($filters: ProductFilters, $limit: Int!) {
  catalog {
    products(offset: 0, limit: $limit, filters: $filters, onlyVisible: true) {
      totalCount
      list { %s }
    }
  }
}
""" % _PRODUCT_FIELDS


class BrickBorrowError(RuntimeError):
    """Any failure talking to the store. Callers treat this as 'try again later'."""


def _strip_html(value: str) -> str:
    return _TAG_RE.sub("", value or "").replace("&nbsp;", " ").strip()


def parse_pieces(additional_info: Optional[list]) -> Optional[int]:
    """Pull the piece count out of the store's spec table.

    Returns None rather than guessing when the value isn't a plain number — the
    gift card and mystery box list "01-3000+", which is a range, not a count.
    A wrong piece count is worse than an absent one.
    """
    for entry in additional_info or []:
        title = (entry.get("title") or "").strip().lower()
        if not title.startswith("piece"):
            continue
        text = _strip_html(entry.get("description") or "")
        # Reject anything that isn't purely a number: "01-3000+" must not
        # silently become 1.
        if not re.fullmatch(r"[\d,]+", text):
            return None
        match = _PIECES_RE.search(text)
        if not match:
            return None
        try:
            return int(match.group(0).replace(",", ""))
        except ValueError:
            return None
    return None


def format_pieces(pieces: Optional[int]) -> str:
    """Human-readable piece count, or empty string when unknown."""
    if pieces is None:
        return ""
    return f"{pieces:,} pcs"


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    url_part: str
    is_in_stock: bool
    is_sellable: bool
    pieces: Optional[int] = None

    @property
    def available(self) -> bool:
        """Available to borrow == the page would show 'Pick me'.

        Both flags are required: `isInStock` alone has been seen to disagree with
        sellability, and we would rather miss a notification than send a false one.
        """
        return bool(self.is_in_stock and self.is_sellable)

    @property
    def url(self) -> str:
        return f"{SITE}/product-page/{self.url_part}"

    @property
    def pieces_label(self) -> str:
        return format_pieces(self.pieces)

    @classmethod
    def from_api(cls, raw: dict) -> "Product":
        return cls(
            id=raw["id"],
            name=raw.get("name") or "(unnamed)",
            url_part=raw.get("urlPart") or "",
            is_in_stock=bool(raw.get("isInStock")),
            is_sellable=bool(raw.get("isSellable")),
            pieces=parse_pieces(raw.get("additionalInfo")),
        )


def slug_from_url(text: str, *, require_url: bool = False) -> Optional[str]:
    """Pull the product slug out of a Brick Borrow product URL.

    Accepts the messy things a phone actually sends: tracking query strings,
    trailing punctuation, and percent-encoded accents (Pokémon slugs contain a
    real `é`, which a browser copies as `%C3%A9`).

    `require_url=True` refuses a bare slug. That matters when scanning free-form
    prose: without it, every ordinary word in "look at this one" parses as a
    valid slug. Explicit single-argument callers (`/remove <x>`) leave it off so
    a hand-typed slug still works.
    """
    candidate = text.strip()
    if not candidate:
        return None

    looks_like_url = "://" in candidate or candidate.startswith("www.")
    if require_url and not looks_like_url:
        return None

    if looks_like_url:
        if candidate.startswith("www."):
            candidate = "https://" + candidate
        parsed = urlparse(candidate)
        if "brickborrow.com" not in (parsed.netloc or "").lower():
            return None
        match = re.search(r"/product-page/([^/?#]+)", parsed.path)
        if not match:
            return None
        candidate = match.group(1)

    candidate = unquote(candidate).strip().strip(".,;)")
    if not candidate or "/" in candidate or " " in candidate:
        return None
    return candidate


def extract_slugs(text: str) -> list[str]:
    """Find every product link in a free-form message, preserving order.

    Only real URLs count here — see `require_url` above.
    """
    found: list[str] = []
    for token in re.split(r"\s+", text or ""):
        slug = slug_from_url(token, require_url=True)
        if slug and slug not in found:
            found.append(slug)
    return found


class BrickBorrowClient:
    """Thin async wrapper over the storefront GraphQL API.

    The Wix `instance` token is short-lived and free to mint, so we fetch a fresh
    one lazily and re-mint on the first auth failure rather than trying to track
    its expiry.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._token: Optional[str] = None

    async def _fetch_token(self) -> str:
        try:
            resp = await self._http.get(TOKENS_URL)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise BrickBorrowError(f"could not fetch access token: {exc}") from exc

        try:
            token = data["apps"][STORES_APP_ID]["instance"]
        except (KeyError, TypeError) as exc:
            raise BrickBorrowError(
                "access-tokens response has no Wix Stores instance — the site's "
                "app set may have changed"
            ) from exc
        log.debug("minted a fresh Wix Stores instance token")
        return token

    async def _token_value(self, force: bool = False) -> str:
        if force or not self._token:
            self._token = await self._fetch_token()
        return self._token

    async def _graphql(self, query: str, variables: dict) -> dict:
        payload = {"query": query, "variables": variables}

        for attempt in (1, 2):
            token = await self._token_value(force=attempt == 2)
            try:
                resp = await self._http.post(
                    GRAPHQL_URL,
                    json=payload,
                    headers={"Authorization": token, "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise BrickBorrowError(f"network error talking to the store: {exc}") from exc

            # A stale token shows up as 401/403; mint a new one and retry once.
            if resp.status_code in (401, 403) and attempt == 1:
                log.info("storefront token rejected (%s), refreshing", resp.status_code)
                continue

            if resp.status_code != 200:
                raise BrickBorrowError(
                    f"store returned HTTP {resp.status_code}: {resp.text[:200]}"
                )

            try:
                body = resp.json()
            except ValueError as exc:
                raise BrickBorrowError("store returned a non-JSON response") from exc

            if body.get("errors"):
                raise BrickBorrowError(f"GraphQL error: {body['errors']!r:.300}")

            data = body.get("data")
            if data is None:
                raise BrickBorrowError(f"store returned no data: {str(body)[:200]}")
            return data

        raise BrickBorrowError("exhausted token refresh attempts")

    async def fetch_catalog(self) -> list[Product]:
        """Page through every visible product. ~12 requests, ~2 seconds.

        We sweep the whole catalog rather than querying each tracked set
        individually because the API has no "give me these ids" filter, and the
        full sweep is cheap enough that it costs less than ~20 single lookups
        while also powering rename detection and `/search`.
        """
        products: list[Product] = []
        offset = 0
        total = None

        while True:
            data = await self._graphql(_LIST_QUERY, {"offset": offset, "limit": PAGE_SIZE})
            block = (data.get("catalog") or {}).get("products") or {}
            page = block.get("list") or []
            if total is None:
                total = int(block.get("totalCount") or 0)

            products.extend(Product.from_api(p) for p in page)
            offset += PAGE_SIZE

            if not page or offset >= total:
                break
            if offset > 20_000:  # paranoia: never loop forever on a bad totalCount
                log.warning("catalog paging exceeded 20k, stopping early")
                break

        log.info("fetched %d products (store reports %s)", len(products), total)
        if not products:
            raise BrickBorrowError("catalog came back empty — treating as a failed scan")
        return products

    async def product_by_slug(self, slug: str) -> Optional[Product]:
        data = await self._graphql(_BY_SLUG_QUERY, {"slug": slug})
        raw = (data.get("catalog") or {}).get("product")
        return Product.from_api(raw) if raw else None

    async def search(self, text: str, limit: int = 10) -> list[Product]:
        data = await self._graphql(
            _SEARCH_QUERY, {"filters": {"freeText": text}, "limit": limit}
        )
        block = (data.get("catalog") or {}).get("products") or {}
        return [Product.from_api(p) for p in (block.get("list") or [])]


def index_by_id(products: Iterable[Product]) -> dict[str, Product]:
    return {p.id: p for p in products}
