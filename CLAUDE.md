# LEGO-borrow-bot — orientation for a fresh session

## What this is

A single-user Telegram bot that watches [brickborrow.com](https://www.brickborrow.com)
LEGO sets and notifies the owner when one becomes borrowable. It exists to route
around the site's 10-set cap on its own "notify me" feature.

**Read [docs/RESEARCH.md](docs/RESEARCH.md) before touching anything.** Nearly
every design choice is downstream of one of its findings, and two of them are
counter-intuitive enough that you will otherwise "fix" working code.

## Current state (2026-08-13)

**Deployed and running** on the homelab as `@LEGOBorrowBot`, image
`ghcr.io/ignacio-montero/legobot:0.5.0`. 99 tests passing. Polls every 5 min,
07:00–19:00 Europe/London.

⚠️ **Every fresh catalogue must feed `App.apply_catalog`.** `/available` once
fetched live data and discarded it, so browsing could reveal a set as available
before the notifier reacted. If you add another command that fetches the
catalogue, route it through `apply_catalog` too.

⚠️ This Mac runs **Colima**, not Docker Desktop — `colima start` if the docker
socket is missing. Colima is ARM; the homelab is Intel, so always cross-build
with `--platform linux/amd64`.

⚠️ Only ONE process may long-poll the bot token. The container is using it, so
do not run the bot locally without stopping it first.

⚠️ **The `announced` flag is not the same as `last_available`.** It means "the
user has been told this is available" and gates the 🔴 gone-again alert, so we
never announce an ending we didn't announce the start of. Commands that report
availability in their reply must pass `inform=True`.

## The two things that will trip you up

**1. Availability is `isInStock && isSellable` — never `inventory.status`.**
That field reads `"in_stock"` on sets with quantity 0. Trusting it makes the bot
fire constantly.

**2. Brick Borrow's URL slugs name the wrong set.** `…/lego-10349-icons-happy-plants`
is really the *Bonsai Tree*; Happy Plants is at the `-1` suffixed slug. This is
why tracking is keyed on the immutable Wix `product_id`, why `/add` echoes the
store's real name back, why `/search` exists, and why renames raise a warning.
It looks like a bug in the bot and isn't.

## Architecture in one paragraph

No browser and no login: product pages are members-only and client-rendered, but
the Wix Stores **catalog GraphQL API answers anonymously**, so the bot just makes
HTTP requests (12 per scan, ~1.3 s for all 1,127 products). One process runs two
asyncio loops — a Telegram long-poll for commands and a timed catalog sweep.
State is SQLite on a named volume. Notifications are **edge-triggered**: alert on
the transition into availability, never on the state, or the bot re-announces the
same set every 30 minutes until you mute it. Details and rejected alternatives in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Where the logic lives

- `src/legobot/scanner.py` — `evaluate_scan()` is the whole product, and it's a
  **pure function** (tracked sets + catalog snapshot → events). Keep it pure;
  that's what makes the state machine testable without a network.
- `src/legobot/brickborrow.py` — API client, token minting, URL parsing.
- `src/legobot/app.py` — the two loops, error handling, alert dispatch.

## Running things

```bash
.venv/bin/python -m pytest -q          # 55 tests, no network needed
```

⚠️ Only ONE process may long-poll a Telegram bot token. Running locally while
the container is up gives both HTTP 409.

## Conventions

- Tests are not optional for new logic in `scanner.py` — its failure modes
  ("never notifies", "notifies forever") are invisible in production.
- The bot **notifies, it never claims a set.** Auto-claiming would commit a real
  borrow and delivery with no human in the loop. Out of scope by design.
- No Brick Borrow credentials anywhere — not in the repo, image, or server. If a
  change seems to need a login, re-read RESEARCH.md first; it probably doesn't.
- Deploy target is the homelab; `~/Development/homelab` is the source of truth
  for the box. Runbook: [deploy/DEPLOY.md](deploy/DEPLOY.md).
