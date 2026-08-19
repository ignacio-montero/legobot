# Architecture

Grounded in [PRD.md](PRD.md) and the site recon in [RESEARCH.md](RESEARCH.md).
Read RESEARCH first — nearly every choice here is downstream of one finding.

## The shape of it

```
        Telegram                                    <storefront-domain>
           ▲ │                                             ▲
  outbound │ │ long-poll (getUpdates)          outbound    │ GraphQL
  sendMsg  │ ▼                                  HTTPS      │ (anonymous)
     ┌─────┴──────────────────────────────────────────────┴────┐
     │  legobot container  (no ports, no browser, no password)  │
     │                                                          │
     │   _command_loop ──┐                  ┌── _scan_loop      │
     │   (idle, waiting) │                  │  (every 30 min,   │
     │                   ▼                  ▼   07:00–19:00)    │
     │              CommandHandler     evaluate_scan()          │
     │                   │                  │  (pure function)  │
     │                   └────────┬─────────┘                   │
     │                            ▼                             │
     │                    SQLite  /data/legobot.sqlite3         │
     └──────────────────────────────────────────────────────────┘
```

## The decision that shapes everything: don't drive a browser

The obvious build is Playwright: log in, load each product page, look for a
"Pick me" button. That's how Tennis Bot works, because Everyone Active genuinely
requires a session.

Here it would have been **wrong**, and the recon says why: product pages are
client-rendered *and* members-only, but the **storefront catalog API answers
anonymously**. So:

| | Headless browser | **Catalog API (chosen)** |
|---|---|---|
| Credentials on the server | the storefront password | **none** |
| RAM | ~1.5 GB spike (Chromium) | **192 MB** |
| One full check | ~1–2 min, N page loads | **~1.3 s, 12 requests** |
| Breakage mode | Any CSS/layout change | Only a real API change |
| Load on the site | N full page renders | 12 JSON reads |

The security consequence is the one worth internalising: **a design choice
removed an entire class of risk.** There is no password in the `.env`, so a
compromised container leaks nothing about the storefront account. Compare
tennisbot, which must hold Everyone Active credentials and accepts that risk
because there is no alternative.

> **Concept — client-side rendering.** The HTML a server sends is only a shell;
> JavaScript then fetches the real data and builds the page in the browser. It's
> why `curl` sees no stock information here. The fix is never "parse harder" —
> it's to find the API the JavaScript is calling and ask it directly. That's
> what §4 of RESEARCH does, using the browser's own network log.

## Stack

| Layer | Choice | Why, and what was rejected |
|---|---|---|
| Language | Python 3.12 | Matches Tennis Bot and Plaque Hunter; one language across the homelab |
| HTTP | `httpx` (async) | One dependency, async-native. `requests` is sync and would need threads to run two loops |
| Concurrency | `asyncio`, two tasks | Both loops are I/O-bound and mostly *waiting*. Threads would add locking for no gain |
| Bot framework | **none** | `python-telegram-bot` is ~20 transitive deps to replace ~120 lines. For 12 commands, hand-rolling is less to maintain and less to patch |
| Storage | SQLite | See below |
| Scheduling | in-process `asyncio.sleep` | See below |
| Container | `python:3.12-slim`, non-root | Standard; no browser to install |

### Why SQLite, not Postgres or a JSON file

Socratic version: *single user, tens of rows, one writer, on a box with 8 GB of
soldered RAM — what would you reach for?*

- **Postgres**: a server process, ~200 MB resident, a network hop, and a backup
  story, to hold ~30 rows. Pure overhead at this scale.
- **JSON file**: tempting, but every scan rewrites every row. A crash mid-write
  truncates the file and the tracking list is gone. That's the failure this
  bot's whole value depends on not happening.
- **SQLite** ✅: a file, zero processes, but with **atomic transactions**, so a
  power cut leaves the DB either fully updated or fully not. WAL mode on top.

Same reasoning, same answer as Blue Plaque Hunter.

> **Concept — WAL (write-ahead logging).** Instead of writing changes over the
> database file, SQLite appends them to a side log and folds them in later. A
> crash mid-write leaves the main file intact, and readers aren't blocked by the
> writer. `PRAGMA journal_mode=WAL`, in [store.py](../src/legobot/store.py).

### Why in-process scheduling, not cron

The container is a long-running process that must hold a Telegram long-poll
open anyway. Given that, a cron job would mean a *second* process, a second
SQLite writer, and Telegram's one-poller-per-token rule broken. `asyncio.sleep`
inside the already-running loop costs nothing.

It also makes the DST question disappear: with `TZ=Europe/London` the window is
computed from local wall-clock time, so 07:00 stays 07:00 across the October
clock change. A UTC crontab would silently drift by an hour — a bug Tennis Bot
actually hit and fixed the same way.

## Data model

One table doing real work:

```sql
tracked(
  product_id      TEXT PRIMARY KEY,  -- Wix id: immutable, the real identity
  url_part        TEXT NOT NULL,     -- slug, for building links (and it can change)
  name            TEXT NOT NULL,     -- last seen name, for rename detection
  added_at        INTEGER NOT NULL,
  last_available  INTEGER,           -- 0 / 1 / NULL = never scanned
  last_seen_at    INTEGER,
  notified_at     INTEGER
)
meta(key, value)  -- paused flag, Telegram update cursor
```

**Why `product_id` is the primary key and not the URL.** RESEARCH §5 found that
the storefront renames products in place while keeping the old slug — the page at
`…/lego-10349-icons-happy-plants` is really the *Bonsai Tree*. The slug is
unique but semantically unreliable; the Wix `id` is stable and meaningless,
which is exactly what you want in a key. Storing `name` alongside it is what
lets the bot notice a rename and warn you.

**`last_available` is nullable on purpose.** `NULL` means "never scanned", which
is genuinely different from "scanned, unavailable" — it decides whether the very
first scan of a set should alert.

## The notification state machine

The core of the product, and the easiest thing to get subtly wrong.

```
                    ┌──────────────── available ──────────────┐
                    │                                          │
  NULL ──available──┴──► ALERT ──still available──► (silent)   │
   │                                                            │
   └──unavailable──► (silent) ──available──► ALERT ─────────────┘
                          ▲                       │
                          └──── unavailable ──────┘  (re-arms, silent)
```

**Edge-triggered, not level-triggered.** We alert on the *transition* into
availability, never on the *state* of being available. Level-triggering would
re-announce the same set every 30 minutes until someone took it — which trains
you to mute the bot, at which point it has negative value.

> **Name the pattern:** this is *edge-triggered* vs *level-triggered*
> notification, the same distinction as in interrupt handling and in alerting
> systems like Prometheus. Reach for edge-triggering whenever the consumer is a
> human who will get annoyed.

Three consequences worth noting:

1. **Adding a set seeds its state.** `/add` records the availability it saw, so
   adding an already-free set tells you once in the confirmation and doesn't
   re-alert on the next scan.
2. **Missing ≠ unavailable.** If a tracked product isn't in the catalogue, we
   report "no longer listed" rather than recording it as unavailable — otherwise
   a store-side hiccup would silently re-arm every trigger and then fire a
   burst of false alerts when it came back.
3. **Pause doesn't stop scanning**, it stops *sending*. State stays current, so
   `/resume` can immediately tell you what's free rather than making you wait
   30 minutes for the truth. That's why `/resume` runs a scan inline.

`evaluate_scan()` is a **pure function** — tracked sets in, events out, no I/O,
no clock, no network. That's what makes all of the above testable without a
network or a fake Telegram; see [test_scanner.py](../tests/test_scanner.py).

## Why a full catalog sweep

The API has no "fetch these N ids" filter (`productsByIds` rejects every
argument shape tried, and `ProductFilters` has no id field). The options were N
single-product queries or one paged sweep. The sweep is **12 requests / ~1.3 s
for all 1,127 products** — comparable to ~20 individual lookups, and it comes
with two freebies: a consistent snapshot (no set observed at a different instant
from its neighbour) and rename detection across the whole tracked list.

At 30-minute intervals across a 12-hour window that's **~300 requests/day**.
Politely small.

## Failure handling

The rule: **a failed check must never kill the bot, and must never fake a
result.**

| Failure | Behaviour |
|---|---|
| Store unreachable / 5xx | Log, skip the cycle, retry next interval. State untouched |
| Wix token expired | Detected as 401/403, re-minted, request retried once |
| Empty catalog response | Treated as a **failure**, not "everything vanished" — this is the guard that prevents 30 false "no longer listed" alerts |
| Telegram down | Exponential backoff capped at 60 s; scanning continues |
| Send fails | Logged and dropped. State was already persisted, so the bot doesn't loop re-sending |
| Unexpected exception in a loop | Caught, logged, loop continues |
| Container restart | SQLite holds tracking, pause state, and the Telegram cursor, so no commands replay |

## Security posture

- **No the storefront credentials anywhere.** The API is anonymous.
- **No inbound ports.** Both conversations are outbound, so the container needs
  no firewall rule and is unreachable from the LAN or the internet.
- **Single-user lockdown**: messages from any chat id other than the owner's are
  logged and discarded.
- **Non-root container** (`uid 10001`), writable only on the data volume.
- Telegram token lives in an untracked `.env` **on the server**, never in git.
- All Telegram output is HTML-escaped — product names contain `®` and `™` and
  come from a third party, so they're treated as untrusted.

## Scale

Not a consideration, and that's a deliberate statement rather than an omission:
one user, ~30 tracked sets, ~300 requests/day, a few hundred KB of SQLite. The
box has 8 GB; this asks for 192 MB. If the catalogue grew 10×, the sweep would
still take ~13 s.
