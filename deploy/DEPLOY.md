# Deploying legobot to the homelab

The bot publishes **no ports** — both of its conversations (Telegram and Brick
Borrow) are outbound — so this is one of the simplest possible homelab deploys:
no UFW rule, no tailnet binding, no reverse proxy.

Follow the homelab control repo's standing orders in
`~/Development/homelab/CLAUDE.md`. This file is the service-specific part.

---

## 0. Prerequisites — do these first (they need your Telegram account)

### a. Create the bot

1. Open Telegram, message **@BotFather**.
2. Send `/newbot`, pick a name and a username ending in `bot`.
3. Copy the token it gives you — it looks like `123456789:AAE…`.

### b. Find your chat id

1. Send any message (`hi`) to your new bot.
2. Open in a browser, substituting your token:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Read `result[0].message.chat.id` — a number, e.g. `987654321`.

Keep both. The token is a credential: it's the full authority to act as the bot.

---

## 1. Publish the image

The homelab pulls prebuilt images from GHCR and never builds — same convention
as Blue Plaque Hunter and tennisbot.

⚠️ **Build for `linux/amd64`.** The homelab is an Intel N95; an image built on
an Apple Silicon Mac defaults to arm64 and will refuse to run.

```bash
cd ~/Development/LEGO-borrow-bot
docker buildx build --platform linux/amd64 -f deploy/Dockerfile -t ghcr.io/ignacio-montero/legobot:0.1.0 --push .
```

If `docker login ghcr.io` is needed, use a GitHub PAT with `write:packages`.

---

## 2. Add the service to the control repo

```bash
mkdir -p ~/Development/homelab/services/legobot
cp ~/Development/LEGO-borrow-bot/deploy/docker-compose.yml \
   ~/Development/homelab/services/legobot/docker-compose.yml
```

Add the include line to `~/Development/homelab/compose.yaml`:

```yaml
include:
  # …existing services…
  - services/legobot/docker-compose.yml
```

Then commit and push the control repo:

```bash
cd ~/Development/homelab && git add -A && git commit -m "feat: add legobot service" && git push
```

---

## 3. Put the secrets on the server (never in git)

```bash
ssh homelab 'mkdir -p ~/homelab/services/legobot && cat > ~/homelab/services/legobot/.env' <<'EOF'
TELEGRAM_BOT_TOKEN=paste-your-token-here
TELEGRAM_CHAT_ID=paste-your-chat-id-here
EOF
ssh homelab 'chmod 600 ~/homelab/services/legobot/.env'
```

> Written directly over SSH rather than committed, so the token never touches
> the repo. `.env` is already gitignored, but the safest secret is one that was
> never in the working tree.

---

## 4. Deploy

```bash
ssh homelab 'cd ~/homelab && git pull && docker compose up -d legobot'
```

---

## 5. Verify

```bash
ssh homelab 'docker ps --filter name=legobot --format "{{.Status}}"'
ssh homelab 'docker logs --tail 30 legobot'
```

Expect a startup line and, in Telegram, a message like
*"🤖 Brick Borrow watcher is up."*

Confirm it publishes nothing (should print **nothing at all**):

```bash
ssh homelab 'docker port legobot'
```

Then in Telegram: `/help`, `/search 10349`, `/status`.

---

## 6. Log the change

Per homelab standing orders, append a dated entry to
`~/Development/homelab/docs/decisions.md` (what changed, why, **how to roll
back**), add the row to `docs/services.md`, re-run `./scripts/snapshot.sh`, and
commit.

Suggested `services.md` row:

| Service | Purpose | Bind / Port | Compose path | RAM limit | Data location |
|---------|---------|-------------|--------------|-----------|---------------|
| legobot | Brick Borrow set-availability watcher → Telegram | **none** (outbound only) | `services/legobot/` | 192 MB | volume `legobot-data` (SQLite: tracked sets + state) |

---

## Updating

```bash
docker buildx build --platform linux/amd64 -f deploy/Dockerfile -t ghcr.io/ignacio-montero/legobot:0.2.0 --push .
# bump the tag in services/legobot/docker-compose.yml, commit, push, then:
ssh homelab 'cd ~/homelab && git pull && docker compose pull legobot && docker compose up -d legobot'
```

Config-only changes (interval, hours) skip the rebuild entirely — edit the
compose `environment:` block, push, and `up -d legobot`.

## Rolling back

```bash
# pin the previous tag in the compose file, then
ssh homelab 'cd ~/homelab && git pull && docker compose up -d legobot'
```

The `legobot-data` volume is independent of the image, so a rollback keeps your
tracked sets.

## Gotchas

- ⚠️ **One poller per bot token.** Running the bot locally while the container
  is up makes both fail with HTTP 409. Stop one.
- ⚠️ **Never `docker compose down -v`** — `-v` destroys `legobot-data` and with
  it your entire tracking list.
- The bot is **silent outside 07:00–19:00** by design. A quiet night is not a
  fault; check `/status` for the next scan.
- If alerts stop entirely, the likeliest cause is a change to the Wix storefront
  API. `docs/RESEARCH.md` has a one-liner to probe it directly.
