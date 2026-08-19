# Container deployment

`Dockerfile` builds a small Python image (no browser — the bot is an HTTP +
SQLite client, which is why it runs comfortably in a 192 MB container).

The container is **outbound-only**: both of its conversations (the Telegram API
and the storefront's catalogue API) are outbound, so it publishes no ports and
needs no inbound firewall rule. Secrets come from an untracked `.env` in the
service directory on the host; `.env.example` lists the variable names.

> ⚠️ Only one process may long-poll a given Telegram bot token — running this
> locally while the deployed container is up gives the second one HTTP 409.

> The operator runbook (host specifics, update loop, rollback) lives in the
> private infrastructure repo alongside the service's compose file, and is
> deliberately not published here.
