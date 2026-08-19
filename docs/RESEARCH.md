# Site recon — methodology

> **Anonymised for publication.** This bot watches a rental catalogue on a
> third-party storefront for the author's own use. The site is not named here,
> and the concrete integration details — domain, API paths, application ids,
> token-minting steps, and the runnable probe scripts — have been removed.
> Publishing them would amount to a ready-made scraping recipe against a small
> business, which isn't something this project wants to hand out. The full notes
> are kept privately (`docs/RESEARCH.local.md`, gitignored).
>
> What remains is the reasoning, which is the transferable part.

## 1. A client-rendered storefront on a hosted platform

The site runs on a mainstream hosted e-commerce platform. The initial HTML is
effectively empty — a shell plus a JavaScript bundle — so the catalogue is
rendered in the browser from a JSON API rather than served as markup.

**Consequence:** scraping the HTML is pointless. Either drive a real browser
(expensive: a headless Chromium per poll) or talk to the same read-only
catalogue API the page itself uses. The bot does the latter, which is why it
runs in a 192 MB container instead of the 1.5 GB a browser-based watcher needs.

## 2. Product detail is members-only; catalogue availability is not

Individual product *pages* sit behind a login, but the catalogue query the
storefront uses to render listings answers anonymously with the fields that
matter here: product name, stock status, and sellability.

**Consequence — and the reason there are no site credentials anywhere in this
project:** the bot never logs in, stores no account password, and reads only
what an anonymous visitor's browser already reads. The only secret it holds is
its own Telegram bot token.

## 3. ⚠️ The finding that shaped the design: URL slugs lie

Catalogue entries appear to be created by duplicating an existing product and
renaming it — **without regenerating the URL slug**. The slug stays frozen at
whatever the *original* product was called.

The practical effect: a URL reading `.../lego-10349-happy-plants` can resolve to
an entirely different set, and the real Happy Plants product can live at a
`-1`-suffixed variant of that same slug. This is not a one-off; several products
in the catalogue have slugs describing a different set number entirely.

**Everything below is designed around that fact:**

1. **A human cannot trust a URL.** When a set is added by link, the bot replies
   with the *resolved product name* and asks the user to sanity-check it.
2. **Slugs are a usable key but not the right one.** They happen to be unique
   across the catalogue, but the platform's immutable product `id` is the
   primary key here; the slug is retained only for building links.
3. **Adding by name or set number** is offered precisely because copying a URL
   is unreliable.
4. **Renames are alerted on.** The name observed at add time is stored, so a
   product silently changing identity under a stable URL is surfaced rather than
   missed.

This is the general lesson worth taking away: **never key on a
human-readable identifier that the upstream system allows to drift.** Use the
opaque immutable id and treat the readable one as display data.

## 4. Carts do not reserve inventory

Verified: adding an item to a cart does not hold it. This ruled out any
"reserve it automatically" behaviour and settled the bot's scope as
**notify-only** — it tells the user something became available and the human
decides what to do. That is a deliberate boundary, not a missing feature.

## 5. Polling budget

Availability is checked on two tiers: a slow full sweep that also catches
renames and delistings, and a faster in-stock-only check during active hours.
Both are ordinary read-only catalogue queries, are confined to daytime hours,
and together stay far below the traffic a person browsing the site would
generate. There is no login, no write operation, and no attempt to work around
any rate limit or access control.
