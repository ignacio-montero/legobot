# Status & next steps

_Last updated: 2026-08-13_

## Where this stands

**DEPLOYED AND RUNNING** on the homelab since 2026-08-13 as `@LEGOBorrowBot`.
Container healthy, using ~24 MB of its 192 MB limit, no published ports.

| Piece | State |
|---|---|
| Site recon | ✅ Done — `docs/RESEARCH.md` |
| PRD / architecture / decisions | ✅ Written |
| Brick Borrow API client | ✅ Verified live: 1127 products in 1.3 s |
| Tracking store (SQLite) | ✅ Done, restart-safe |
| Notification state machine | ✅ Done, 55 tests passing |
| Telegram commands | ✅ Done (add/search/list/remove/check/pause/resume/status) |
| Dockerfile + compose + runbook | ✅ Written — `deploy/` |
| Docker image built & pushed | ✅ `ghcr.io/ignacio-montero/legobot:0.1.0` (amd64, 44 MB) |
| Deployed to homelab | ✅ Running, healthy, verified no LAN exposure |

## v0.2.0 — piece counts (2026-08-13)

Piece counts now appear in `/list`, `/add`, `/search`, `/check` and availability
alerts, at zero extra request cost (they ride along in the existing bulk query).

⚠️ **Sets added before v0.2.0 show no count until the next scan populates them.**
Send `/check` to fill them in immediately rather than waiting for 07:00.

## Using it

Message **@LEGOBorrowBot** on Telegram. Paste a Brick Borrow product link to
start tracking; `/help` lists everything.

⚠️ It polls **07:00–19:00 Europe/London**, so outside those hours it is silent
by design. Send **`/check`** to force a scan immediately.

The token lives only in `services/legobot/.env` on the server (chmod 600) and in
a gitignored `.env` on the Mac. It was never committed.

**Note on Docker:** this Mac has **Colima**, not Docker Desktop. If `docker`
commands fail with a socket error, run `colima start`. Colima is ARM, so images
for the homelab must be cross-built with `--platform linux/amd64` (the runbook
does this).

## Also worth knowing

You gave the Brick Borrow account password. **It turned out to be unnecessary**
— the catalogue is readable anonymously — so it is not stored anywhere in this
repo, in the image, or on the server. Since it was shared in chat, changing it
is cheap peace of mind.

## Open questions (from the PRD)

1. **Confirm the availability mapping** on the first real alert — that a set the
   bot calls available really does show "Pick me". It matched both reference
   pages via the API, but product pages are members-only so it wasn't eyeballed.
2. **Weekdays only?** The window is currently every day, 07:00–19:00. If
   weekends never yield returns, that's a one-line env change.
3. **Is 30 minutes fast enough** for the most contested sets? Configurable via
   `POLL_INTERVAL_MINUTES`; revisit once there's real data on how often an alert
   arrives too late.

## Possible later work

- A weekly digest ("3 of your 12 sets were available at some point this week").
- Auto-pause: detect that you currently have a set on loan and go quiet by
  itself. Would need the members-only area, i.e. a login — worth it only if
  manual `/pause` proves annoying.
- Priority tiers, so a top-tier set could be polled more often than the rest.
