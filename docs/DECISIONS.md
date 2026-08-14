# Decisions log

Newest first. Each entry: what was decided, why, and what was rejected.

---

## 2026-08-14 — Two-tier polling (30s fast + 5min full); auto-cart rejected

**The ask.** Desirable sets are taken in under 5 minutes, so a 5-minute cadence
reports them already gone. Proposal explored: have the bot log in and put the set
in the basket to hold it.

**Auto-cart: rejected, because the mechanism does not exist.** Wix does not
reserve inventory on add-to-cart — it decrements at purchase, and
reserve-on-cart is an open feature request. Corroborated here by a read-only
storefront API (zero mutations) and no reserve/hold field on `Inventory` or
`Product`. A set in your basket stays claimable by anyone else, so the plan would
have bought a stored password, a login session, a headless browser and account
risk **in exchange for nothing**. The only action that claims a set is completing
the pick — the purchasing power the owner explicitly ruled out. Full write-up in
`RESEARCH.md` §4f, including the one test not run and how to run it.

**Decision: attack latency instead.** Two tiers:

| Tier | Query | Cost | Cadence | Owns |
|---|---|---|---|---|
| Fast | `inventoryStatus: IN_STOCK_STATUS` | 5 req / 0.75 s | **30 s** | 🟢 became-available |
| Slow | full sweep | 12 req / 1.4 s | 5 min | 🔴 gone, renames, delisting, piece counts |

Median time-to-detect drops **~150 s → ~15 s**, at 0.17 req/s.

**The fast tier is deliberately one-directional — it can turn a set ON, never
off.** Comparing the two live queries showed they are *not* exact complements:
five variant-managed merch products read as available in a full sweep but never
appear in the filtered list. Had absence in the fast tier meant "unavailable",
those would flap 🔴/🟢 between tiers forever. Making absence mean *no new
information* removes the whole class of bug, and costs nothing: going unavailable
is not latency-critical.

**Rejected:** polling individual tracked sets on a tight loop. For 10 sets at 30 s
that is 14,400 req/day and covers only those 10; the filtered sweep is half the
requests and covers all 48.

**Not changed:** no login, no password, no browser. Every security property in
ARCHITECTURE.md survives.

---

## 2026-08-14 — "Gone again" alerts, gated by an `announced` flag

**The question.** If a set is announced as available and not acted on, does the
bot keep notifying every scan? **No** — it never did; that is the edge-triggered
design, and it is what was observed with Mona Lisa. But the requested other half,
telling you when it is taken again, was **computed and then thrown away**:
`evaluate_scan` had produced `became_unavailable` since day one and
`_send_alerts` simply never rendered it.

**Decision.** Send a 🔴 "Gone again" alert on the available→unavailable edge, so
each availability cycle produces exactly two messages and nothing in between.

**The subtlety — why a new `announced` column.** The naive version notifies on
every downward edge, which produces "X is no longer available" for a set you were
never told had become available. That happens in two real cases: a set that
freed up and was taken while the bot was **paused**, and the first scan after
adding a set. So the alert is gated on `announced` — "the user has been told this
set is currently available" — which is set when we push an alert *and* when a
command reply lists it (`/check`, `/available`, `/resume` all inform without
pushing). It clears the moment the set goes unavailable, re-arming both alerts.

**Rule this encodes:** never announce the end of something you never announced
the start of.

**Migration.** Second schema migration, and the first with a **data backfill**:
`UPDATE tracked SET announced = 1 WHERE last_available = 1 AND notified_at IS
NOT NULL`. Without it, the set already flagged available in the live database
would have gone quiet forever instead of reporting its removal. Schema migration
and data migration are separate jobs; this one needed both.

**Testing.** Added `tests/test_app_lifecycle.py`, which drives the real
`App.apply_catalog` with a stub Telegram and asserts the full cycle sends
**exactly two** messages across eleven scans — the unit tests pin the pure
function, this pins what actually gets sent.

---

## 2026-08-14 — Any fresh catalogue feeds the notifier; poll every 5 min

**The report.** A tracked set (Mona Lisa) was seen as available via `/available`
at 09:02, but no notification arrived at that moment.

**What actually happened.** The notifier was working. Timeline from the logs and
the database:

| Time | Event |
|---|---|
| 09:00:32 | scheduled scan — Mona Lisa **not** available |
| ~09:01 | it became available |
| 09:02:39 | `/available` run by hand — shows it as available |
| 09:10:34 | scheduled scan — edge detected, **notification sent** (`notified_at=09:10:34`, zero send failures in the logs) |

So this was an **8-minute observation window**, not a lost notification: a
manual browse read live data while the notifier was still on its cadence.

**The real defect, though, is that the window existed at all.** `/available`
fetched a complete fresh catalogue — exactly the data a scan needs — and then
**threw it away**. Having the answer in hand and not acting on it is the bug.

**Fix 1: every fresh catalogue feeds the state machine.** Extracted
`App.apply_catalog(products, notify=)`; the timed scan, `/check`, `/resume` and
`/available` all funnel through it. Browsing can no longer reveal something the
notifier hasn't reacted to.

`/available` applies with `notify=False` deliberately: the reply itself now pins
your tracked-and-available sets at the top, so you are being told *right there*,
and a duplicate push a second later would be noise. Consuming the edge is
correct precisely because the user was informed.

**Fix 2: tracked sets are pinned above the ranking.** Previously they were only
ticked ✅ inline, so a tracked set ranking below the display limit was invisible.
The whole point of the bot must never be buried by a piece-count sort.

**Fix 3: poll every 5 minutes, not 10.** Halves the worst-case delay. Safe on
the evidence already gathered (60 back-to-back requests → 60× HTTP 200, no
throttling; responses are `no-cache` so faster polling reads fresher data).
~1,700 requests/day, still ~1/minute averaged.

**Not changed:** the edge-triggered design. It behaved exactly as specified.

---

## 2026-08-13 — `/available`: catalogue-wide discovery, ranked by pieces

**Decision.** New command listing every borrowable set in the catalogue, sorted
by piece count descending, default top 20, `/available N` up to 100. Sets you
already track are marked ✅.

**Why it's a different command from `/list`.** `/list` is *monitoring* — what
you're waiting for. This is *discovery* — what you could take right now. Ranking
by pieces reflects why you'd ask: to find a big build. The two answer different
questions and shouldn't be merged.

**Why a limit at all.** 395 of 1,127 sets were available when this shipped.
Rendering all of them is ~65,000 characters — Telegram caps a message at 4,096,
so it would arrive as ~17 notifications. The default of 20 fits in **one**
message; `/available 100` costs 5. **Design the default around the output
channel, not around the data.**

**Cost.** One full catalogue sweep (12 requests, ~1.3 s) per invocation, on
demand only. No change to the background scan.

**Excluded from the ranking:** the Gift Card and Mystery Box, which have no real
piece count (`01-3000+`). A piece-count ranking has no meaningful slot for them.

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
