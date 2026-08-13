# Status & next steps

_Last updated: 2026-08-13_

## Where this stands

**The bot is built, tested, and verified against the live site.** It is not yet
deployed, because deployment needs two things only you can produce.

| Piece | State |
|---|---|
| Site recon | ✅ Done — `docs/RESEARCH.md` |
| PRD / architecture / decisions | ✅ Written |
| Brick Borrow API client | ✅ Verified live: 1127 products in 1.3 s |
| Tracking store (SQLite) | ✅ Done, restart-safe |
| Notification state machine | ✅ Done, 55 tests passing |
| Telegram commands | ✅ Done (add/search/list/remove/check/pause/resume/status) |
| Dockerfile + compose + runbook | ✅ Written — `deploy/` |
| Docker image built & pushed | ⛔ **Blocked** — Docker Desktop wasn't running |
| Deployed to homelab | ⛔ **Blocked** — needs the Telegram bot token |

## What's needed from you (~3 minutes)

1. **Create the bot:** Telegram → **@BotFather** → `/newbot` → copy the token.
2. **Get your chat id:** message the new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `result[0].message.chat.id`.
3. **Start Docker Desktop** so the image can be built and pushed.

Then `deploy/DEPLOY.md` is the runbook end to end.

I deliberately did not create the bot or handle the token myself — a bot token
is a live credential, and it should be minted by you and go straight into the
server's untracked `.env`.

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
