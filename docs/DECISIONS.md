# Decisions log

Newest first. Each entry: what was decided, why, and what was rejected.

---

## 2026-08-13 — Show piece counts, sourced from `additionalInfo`

**Decision.** Read the store's spec table (`additionalInfo`) and surface the
piece count in `/list`, `/add`, `/search`, `/check` and availability alerts.

**Why it's free.** `additionalInfo` is returned by the **bulk list query**, not
just the single-product one, so this adds **zero requests** per scan. Every one
of the 1,127 products carries "Age Range", "Pieces" and "Set No.".

**Parsing.** Values are HTML fragments and the store is inconsistent: `<p>757</p>`,
`<div>2912</div>` and `<span>…</span> ` all occur, and one entry uses a thousands
separator (`2,532`). The parser strips tags and accepts only a pure number.

**The deliberate refusal:** the Gift Card and Mystery Box list `01-3000+`, a
*range*. A naive `\d+` regex would report "1 piece". The parser rejects anything
that isn't wholly numeric and returns None, and the UI omits the count entirely.
**An absent piece count is much better than a confidently wrong one.**

**Storage.** Cached in SQLite so `/list` is instant and works offline, refreshed
on every scan. `record_scan` ignores a None so a transient malformed spec table
can't blank a count already known.

**Schema migration.** The bot was already deployed with a live database, and
`CREATE TABLE IF NOT EXISTS` does nothing to an existing table. Added a minimal
idempotent migration (`PRAGMA table_info` → `ALTER TABLE … ADD COLUMN`) that runs
at startup, with a test that opens a pre-migration DB and asserts existing rows
survive.

---

## 2026-08-13 — Poll every 10 minutes, not 30

**Decision.** Default `POLL_INTERVAL_MINUTES` is 10. 5 is also safe.

**Why.** The original 30 was chosen defensively, on the assumption that polling
would carry session/logout risk. It doesn't — the API is anonymous, so there is
no session to lose and nothing to log out of. Measurements:

- 60 back-to-back requests (5 full scans, zero pause) → **60× HTTP 200**,
  no 429s, no throttling, slowest single request 0.20 s.
- Responses carry `cache-control: no-cache` and `x-cache: MISS`, so they are
  **not** CDN-cached — polling faster genuinely returns fresher data rather
  than re-reading a stale edge copy.
- One scan is 12 requests / ~1.3 s. At 10 min over a 12-hour window that is
  ~860 requests/day, i.e. ~1 request/minute averaged. Negligible for a
  CDN-fronted commercial store.

Since the whole value of the bot is beating other members to a set, halving the
mean notification delay from ~15 min to ~5 min is the single cheapest
improvement available.

**Rejected:** going straight to 1–2 minutes. Nothing observed forbids it, but a
burst of 12 requests every 60 s starts to look like scraping rather than
browsing, and there is no evidence the warehouse updates stock that granularly.
10 min keeps a wide politeness margin for zero practical cost.

**Available if wanted:** the `inventoryStatus: IN_STOCK_STATUS` filter returns
only in-stock products (392 vs 1127), cutting a scan from 12 requests to 4. Not
adopted, because absent-from-that-list conflates "out of stock" with "delisted"
and would cost the rename/vanished detection. Worth revisiting only if the
interval ever drops below ~5 min.

---

## 2026-08-13 — Read the storefront catalog API instead of driving a browser

**Decision.** Get availability from Brick Borrow's Wix Stores GraphQL API,
anonymously. No Playwright, no login, no stored password.

**Why.** Recon (`docs/RESEARCH.md`) found that although product *pages* are
members-only and client-rendered, the *catalog API* answers anonymous requests
with exact stock. That collapses the problem from "automate a logged-in browser"
to "make an HTTP request".

**Rejected:** Playwright + login (needs the account password on the server,
~1.5 GB RAM, breaks on any layout change); HTML scraping (the data isn't in the
HTML at all); the site's own notify-me feature (that's the 10-set cap we're
routing around).

**Consequence.** No Brick Borrow credential exists anywhere in this system — a
whole class of risk designed out rather than mitigated.

---

## 2026-08-13 — Availability is `isInStock && isSellable`

**Decision.** Treat a set as borrowable only when both flags are true.

**Why.** Verified against both reference pages supplied: Munchlax
(`false/false`, page shows "Notify when available") and the happy-plants URL
(`true/true`, page shows "Pick me"). Requiring both means a disagreement between
the flags produces a missed alert rather than a false one — the better failure.

**Rejected:** `inventory.status`, which reads `"in_stock"` even on a set with
quantity 0. It appears to mean "inventory is tracked", not "there is stock".
Trusting it would have made the bot fire constantly.

---

## 2026-08-13 — Key tracked sets by Wix product id, not URL

**Decision.** Primary key is the immutable `product_id`; the slug is stored only
for building links, and the product name is stored to detect renames.

**Why.** Brick Borrow reuses URLs when they swap sets: `…/lego-10349-icons-happy-plants`
is really the *Bonsai Tree*, and the real Happy Plants set lives at the same slug
with a `-1` suffix. Slugs are unique but they lie about identity.

**Consequence.** Three user-facing features exist because of this one finding:
`/add` replies with the store's real name, `/search` exists so sets can be added
without trusting a URL, and a rename raises a warning.

---

## 2026-08-13 — Edge-triggered notifications

**Decision.** Alert on the unavailable→available *transition*, once. Re-arm when
the set goes unavailable again.

**Why.** Level-triggering would re-announce the same set every 30 minutes until
someone took it, which trains the user to mute the bot — at which point it has
negative value.

**Consequence.** `evaluate_scan()` is a pure function so the state machine is
directly unit-testable; "never notifies" and "notifies forever" are both
invisible in production and pinned by tests instead.

---

## 2026-08-13 — Full catalog sweep per scan

**Decision.** Page the entire catalogue (12 requests, ~1.3 s) rather than query
each tracked set.

**Why.** The API has no id-list filter — `productsByIds` rejects every argument
shape tried and `ProductFilters` has no id field. The sweep costs about the same
as ~20 individual lookups and adds a consistent snapshot plus rename detection
across the whole list for free. ~300 requests/day total.

---

## 2026-08-13 — SQLite, and in-process scheduling

**Decision.** SQLite in WAL mode on a named volume; scheduling via `asyncio.sleep`
inside the already-running process, with `TZ=Europe/London`.

**Why.** Every scan rewrites every tracked row, so a JSON file risks losing the
tracking list to a torn write; SQLite makes that atomic. Postgres would be a
server process for ~30 rows. Cron would mean a second process, a second SQLite
writer, and a violation of Telegram's one-poller-per-token rule — and a UTC
crontab would drift an hour at the DST change, a bug Tennis Bot already hit.

---

## 2026-08-13 — No Telegram bot framework

**Decision.** Hand-rolled Telegram client (~120 lines) over `python-telegram-bot`.

**Why.** For 12 commands and one user, the framework's ~20 transitive
dependencies are more surface to patch than the code they replace. The whole app
has exactly one runtime dependency: `httpx`.
