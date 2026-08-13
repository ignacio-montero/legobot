# Decisions log

Newest first. Each entry: what was decided, why, and what was rejected.

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
