# LEGO Borrow Bot

A Telegram bot that watches [Brick Borrow](https://www.brickborrow.com) sets and
messages you the moment one becomes available to borrow.

Brick Borrow lets you subscribe to "notify me when available" for a maximum of
**10 sets**. This watches as many as you like.

```
🟢 Available now

LEGO® (10348) Botanicals™ Japanese Red Maple Bonsai Tree
474 pcs
Open on Brick Borrow →
```

## How it works

Every 5 minutes between 07:00 and 19:00 UK time, it reads Brick Borrow's
storefront catalog API and compares each tracked set against what it saw last
time. When one flips from unavailable to available, you get a message — **once**
per transition, not every cycle.

You get exactly two messages per availability cycle:

```
🟢 Available now              …then silence while it stays free…
LEGO® (31213) Art Mona Lisa
1,503 pcs

🔴 Gone again                 …then silence until it returns.
LEGO® (31213) Art Mona Lisa
Someone else took it. Still tracking — I'll tell you if it returns.
```

The "gone again" message only fires for sets I actually told you about, so you
are never notified that something ended which you were never told had started.

Piece counts come along for free: the store's spec table ("Age Range", "Pieces",
"Set No.") is returned by the same bulk query, so showing them costs zero extra
requests.

No browser, no login, no stored password: the catalogue turns out to be readable
anonymously. See [docs/RESEARCH.md](docs/RESEARCH.md) for how that was
established and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why it matters.

## Commands

| Command | What it does |
|---|---|
| `/available` | **Every set borrowable right now, biggest build first** |
| `/available 50` | Show more of them (default 20, max 100) |
| *(just paste a link)* | Starts tracking that set |
| `/add <link>` | Track one or more sets |
| `/search <text>` | Find a set by name or number, e.g. `/search 10349` |
| `/list` | Everything you're tracking, with status and piece counts |
| `/remove <n>` | Stop tracking (number from `/list`, or paste the link) |
| `/check` | Check everything right now |
| `/pause` / `/resume` | Silence notifications while you already have a set |
| `/status` | Running state, window, last check |
| `/help` | All of the above |

## Two ways to use it

**Monitoring** — track the sets you want and wait to be told when one frees up.

**Discovery** — `/available` answers "what could I borrow *tonight*", ranked by
piece count across the whole catalogue, with the ones you already track marked.
Useful because the biggest sets are almost never free: at the time of writing,
395 sets were available but the largest was 2,870 pieces, while the 10,001-piece
Eiffel Tower was out on loan.

## ⚠️ Brick Borrow's URLs name the wrong set

This is not a bug in the bot. The store reuses URL slugs when sets are swapped:

| The URL you'd copy | The set it actually is |
|---|---|
| `…/lego-10349-icons-happy-plants` | LEGO® (10348) **Bonsai Tree** |
| `…/lego-10349-icons-happy-plants-1` | LEGO® (10349) **Happy Plants** |
| `…/lego-10349-icons-nasa-space-shuttle-discovery` | LEGO® (10283) **Space Shuttle** |

So the bot always replies with the **real name** from the store when you add a
set, warns you if a tracked set gets renamed, and offers `/search` so you can add
sets by name or number instead of trusting a link.

## Running it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env      # then fill in the token and chat id
set -a && . ./.env && set +a
PYTHONPATH=src LEGOBOT_DB_PATH=./data/legobot.sqlite3 .venv/bin/python -m legobot
```

Tests (no network needed):

```bash
.venv/bin/python -m pytest -q
```

⚠️ Only one process may poll a Telegram bot token at a time — stop the deployed
container before running locally, or both get HTTP 409.

## Deploying

To the homelab, as a container with **no published ports** (everything it does is
outbound). Full runbook: [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Layout

```
src/legobot/
  brickborrow.py   Wix storefront API client + URL parsing
  scanner.py       the notification state machine (pure functions)
  store.py         SQLite persistence
  telegram.py      Telegram Bot API client (long polling)
  commands.py      command handlers
  app.py           the two concurrent loops
docs/
  RESEARCH.md      how the site works — read this first
  ARCHITECTURE.md  why it's built this way
  PRD.md           what it does and why
  DECISIONS.md     decision log
  NEXT_STEPS.md    current status
deploy/            Dockerfile, compose, runbook
```
