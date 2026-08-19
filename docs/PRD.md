# PRD — the storefront availability watcher

## Problem & goal

the storefront lets a member borrow **one LEGO set at a time** from a ~1,100-set
catalogue. The desirable sets — big, complex, popular — are almost always out on
loan. The site offers a "notify me when available" button, but caps it at **10
sets**. Anyone interested in more than 10 has no way to watch the rest.

**Goal:** watch an unlimited list of sets and tell me, on Telegram, the moment
one becomes borrowable — fast enough that I can go and claim it.

**Success looks like:** I stop manually refreshing product pages, and I hear
about a set within ~30 minutes of it becoming available.

## Target user

Exactly one person: the repo owner. A single-user bot, on their own homelab, with
their own Telegram account. This is a deliberate constraint, not an oversight —
it removes accounts, auth, multi-tenancy, and a web UI from scope entirely.

## MVP scope

- Track an **unlimited** list of sets (soft cap 100, purely as a guard rail).
- Add sets by **pasting a product link** into Telegram.
- Add sets by **searching name or set number**, because the site's URLs
  frequently name the wrong set (see [RESEARCH.md](RESEARCH.md) §5).
- Check every **30 minutes, 07:00–19:00 UK time** — returns are processed by
  warehouse staff during working hours, so overnight polling is wasted.
- **Notify on the transition** into availability, once per transition.
- **Pause / resume**, for when a set is already on loan to me and I can't order.
- List / remove tracked sets; force an immediate check.
- Always report the **real set name** from the store, never the one implied by
  the URL.
- Run always-on, unattended, on the homelab.

## Explicitly out of scope

- **Auto-claiming a set.** The bot notifies; the human decides and clicks. This
  was specified by the user and is also the right call: an auto-claim would
  commit a real borrow (and a delivery) with no human in the loop.
- **Any use of the site's own notify-me feature.** It covers only a small
  number of sets, which is the gap this personal tracker fills.
- **The site's All / Available / Extras filters.** Reported as unreliable; the
  bot reads per-product truth instead.
- Multi-user support, a web UI, accounts, price/discount tracking, historical
  analytics, wishlist prioritisation.
- Storing the storefront password. Not needed — see ARCHITECTURE.

## User stories

1. As a borrower, I want to **paste a link** to a set into Telegram, so it starts
   being watched with no further ceremony.
2. As a borrower, I want to **search by set number** ("10349"), because I can't
   trust the URL to tell me which set a page is.
3. As a borrower, I want a **Telegram message the moment a set frees up**, so I
   can claim it before someone else.
4. As a borrower, I want **not to be told twice** about the same set, so I don't
   start ignoring the bot.
5. As a borrower, I want to **pause** while I already have a set on loan, so it
   stays quiet until I can actually borrow again.
6. As a borrower, I want to **see everything I'm watching and its status**, so I
   can prune the list.
7. As a borrower, I want to be **warned if a tracked link changes which set it
   points at**, because the store reuses URLs.

## Acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| 1 | Pasting a product link (with or without `/add`) starts tracking it | `test_bare_link_without_a_command_adds_it` |
| 2 | The confirmation shows the store's real name, not the URL's | `test_add_by_link_reports_the_real_name` |
| 3 | A set going unavailable→available produces exactly one alert | `test_notifies_on_unavailable_to_available` |
| 4 | A set that stays available produces **no** further alerts | `test_does_not_renotify_while_still_available` |
| 5 | After going unavailable, the alert re-arms | `test_trigger_rearms_after_going_unavailable` |
| 6 | Adding an already-available set doesn't double-announce it | `test_add_seeds_state_so_the_next_scan_is_not_a_false_edge` |
| 7 | No polling outside 07:00–19:00 | `test_after_hours_sleeps_until_the_window_opens` |
| 8 | `/pause` stops messages; `/resume` immediately reports what's free | `test_pause_and_resume_round_trip` |
| 9 | A renamed product raises a warning | `test_detects_rename_under_a_stable_url` |
| 10 | A set missing from the catalogue is reported, not silently marked unavailable | `test_missing_product_is_reported_not_guessed` |
| 11 | Tracking survives a container restart | `test_state_survives_reopen` |
| 12 | A store outage degrades gracefully, never crashes the bot | `test_add_survives_the_store_being_down` |
| 13 | Messages from anyone but the owner are ignored | `app.py::_command_loop` chat-id check |

## Open questions

1. **Is `isInStock && isSellable` exactly "Pick me"?** It matches both reference
   pages the user supplied (2/2), but that was verified through the API, not by
   viewing the logged-in page — product pages are members-only. Worth one
   eyeball confirmation on the first real alert.
2. **Should the window be weekdays only?** Currently every day. If Saturday
   never yields returns, narrowing it is a one-line env change.
3. **Is 30 minutes fast enough** for the most contested sets, or does a popular
   set get claimed within minutes of appearing? The interval is configurable;
   worth revisiting once there's real data on how quickly alerts go stale.
