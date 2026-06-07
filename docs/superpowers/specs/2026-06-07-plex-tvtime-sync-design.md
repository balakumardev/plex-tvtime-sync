# plex-tvtime-sync — Design

**Date:** 2026-06-07
**Status:** Implemented 2026-06-07 (live on whatbox; see Implementation Notes at bottom)
**Repo:** `balakumardev/plex-tvtime-sync` (public, MIT)

## Problem

Sync Plex watch activity to TV Time without Plex Pass. The existing open-source solution
([Zggis/plex-tvtime](https://github.com/Zggis/plex-tvtime)) relies on Plex webhooks — a Plex
Pass feature — and is a Java 17 / Spring Boot / Docker app whose TV Time login drives headless
Chrome via Selenium. The target host (whatbox seedbox, shared, no root) has no Java, no Docker,
no Chrome — only Python 3.12. The repo also has **no license** (all rights reserved), so a
modified fork cannot be published; its reverse-engineered API knowledge (facts) is
reimplemented here in original code, with credit.

The same host already runs a proven pattern for this job: PlexTraktSync via cron
(poll-and-sync, flock + timeout, append logs). This project follows that pattern.

## Decisions (user-confirmed)

| Decision | Choice |
|---|---|
| Trigger mechanism | Cron poller against Plex HTTP API (no webhooks, no Plex Pass) |
| History backfill | **None** — forward-only; watermark initialized to "now" at first run |
| Media scope | TV episodes **and** movies |
| Plex accounts | Server owner only (`accountID=1`), matching plextraktsync `username_filter: true` |
| Hosting | whatbox, alongside the existing Plex automation pipeline |
| Repo | New public MIT repo `plex-tvtime-sync`; `Zggis/plex-tvtime` forked unmodified for reference + credited |
| Rewatches | Tracked via local ledger; repeat views sent with `is_rewatch=1` |

Related operational change (already executed 2026-06-07): the broken `plextraktsync watch`
daemon on whatbox was disabled (cron lines commented `#DISABLED-2026-06-07`, daemon killed).
The working 5-minute `plextraktsync sync` cron remains active.

## Architecture

```
cron (:03,:08,...,:58) → run.sh → sync.py
  1. read state.json            (watermark = viewedAt of last processed history entry)
  2. GET {PLEX_URL}/status/sessions/history/all
         ?viewedAt>={watermark - overlap}&accountID=1&sort=viewedAt:asc
  3. for each unprocessed view event (oldest first, capped per run):
       GET {PLEX_URL}/library/metadata/{ratingKey}  → GUIDs, type, titles, indices
       episode → tvdb id  → TVTime: mark episode watched (is_rewatch from ledger)
       movie   → search TVTime (tvdb → tmdb → imdb id fallback) → uuid → mark watched
       on success or permanent skip: advance watermark past this item
  4. write state.json + ledger.json
```

Polling `/status/sessions/history/all` (not live sessions) is deliberate:
- catches manual "mark as watched" events, which Plex webhooks never fire for;
- self-heals after downtime — anything watched while whatbox or TV Time was unreachable is
  picked up on the next successful run because the watermark only advances on success.

A fixed 5-minute overlap window (queries start at watermark − 300 s) plus a set of
recently-processed history keys in `state.json` guards against boundary duplicates; the TV Time API also
de-duplicates non-rewatch marks server-side.

## Components

Single Python package, stdlib + `requests` + `python-dotenv` only.

| Module | Responsibility |
|---|---|
| `config.py` | Load `.env` config; validate required keys |
| `plex_client.py` | History query + per-item metadata fetch (XML via `xml.etree`); GUID extraction (`tvdb://`, `tmdb://`, `imdb://`) |
| `tvtime_client.py` | Login exchange, token refresh, mark-episode, movie search + mark-movie; owns `tokens.json` |
| `state.py` | Watermark + processed-keys overlap set (`state.json`); all-time marked-episode ledger (`ledger.json`) |
| `sync.py` | Entry point; orchestration, ordering, caps, logging |
| `bootstrap_login.py` | Rarely-run helper: bootstrap JWT + creds → `tokens.json` |
| `run.sh` | Cron wrapper: venv activation, log self-rotation at 10 MB |

## TV Time API surface (reverse-engineered, reimplemented)

All write calls are tunneled through TV Time's own CORS sidecar:
`POST https://app.tvtime.com/sidecar?o=<upstream-url>` with `Host: app.tvtime.com:80` and
`Authorization: Bearer <jwt_token>`.

| Purpose | Upstream |
|---|---|
| Credential login | `auth.tvtime.com/v1/login` (body `{"username","password"}`, bearer = bootstrap JWT) → `data.jwt_token`, `data.jwt_refresh_token` |
| Mark episode watched | `api2.tozelabs.com/v2/watched_episodes/episode/{tvdb_episode_id}?is_rewatch=0|1` |
| Movie id → uuid | `search.tvtime.com/v1/search/series,movie?q={id}&offset=0&limit=1` |
| Mark movie watched | `msapi.tvtime.com/prod/v1/tracking/{uuid}/watch` (no refresh token in body) |

TV Time is TVDB-native for shows: the `tvdb://` GUID Plex already exposes **is** the TV Time
episode id — no mapping layer.

### Auth lifecycle

1. **One-time bootstrap (Mac):** read the *anonymous* `flutter.jwtToken` from
   `app.tvtime.com` localStorage in a real browser. This token exists pre-login; no
   credentials are ever typed into a web page.
2. `bootstrap_login.py` exchanges bootstrap JWT + credentials (from `.env`) at
   `auth.tvtime.com/v1/login`; writes `jwt_token` (~60-day lifetime) + `jwt_refresh_token`
   to `config/tokens.json` (chmod 600).
3. On 401 during sync: attempt refresh with `jwt_refresh_token`; if no working refresh
   endpoint exists, log `CRITICAL re-auth needed`, stop advancing the watermark (nothing is
   lost), and write a backoff marker suppressing further TV Time calls for 1 hour.
4. **Stretch (implementation-time investigation):** identify the HTTP call the Flutter web
   app uses to mint the anonymous JWT; if reproducible with plain `requests`, re-auth becomes
   fully automatic and the browser step disappears permanently.

## Deployment (whatbox)

```
~/apps/plex-tvtime-sync/          # git clone
  venv/                           # python3 -m venv; pip install requests python-dotenv
  config/.env                     # PLEX_URL, PLEX_TOKEN, TVTIME_USER, TVTIME_PASSWORD  (chmod 600, gitignored)
  config/tokens.json              # jwt_token + refresh (chmod 600, gitignored)
  config/state.json  config/ledger.json
~/logs/tvtime.log                 # self-rotated at 10 MB by run.sh (lesson: trakt.log reached 251 MB)
```

Crontab entry, slotting in after the existing pipeline (unwatcher :00 → trakt :01 → cleaner :02):

```
3,8,13,18,23,28,33,38,43,48,53,58 * * * * /usr/bin/timeout 240 /usr/bin/flock -n ~/.tmp/plex-tvtime.lock ~/apps/plex-tvtime-sync/run.sh >> ~/logs/tvtime.log 2>&1
```

Secrets live only in `config/` on whatbox — never in the repo, never in crontab.

## Error handling

| Failure | Behavior |
|---|---|
| Plex unreachable | Log, exit 0; watermark unmoved → automatic catch-up next run |
| Metadata 404 (item deleted by PlexCleaner) | Skip + log; 5-min poll cadence vs. cleaner's 2-day minimum makes this rare |
| Episode without `tvdb://` GUID | Permanent skip + log (TV Time cannot represent it) |
| Movie unresolvable via search (tvdb→tmdb→imdb) | Permanent skip + log |
| TV Time 401 | Refresh → on failure: CRITICAL log + 1 h backoff marker; watermark frozen |
| TV Time 5xx / network error on an item | Transient: stop the run; item retried next cycle |
| Runaway volume | Per-run cap of 50 items, ≥1 s spacing between TV Time calls |

Per-item failures never corrupt state: the watermark advances only past items that succeeded
or were permanently skipped.

## Testing

- **Unit (pytest, mocked HTTP via `responses`):** GUID extraction precedence; watermark +
  overlap dedup; ledger/rewatch flagging; exact TV Time request shapes (URL, headers, body)
  for episode, movie search, movie mark, login, refresh-failure path.
- **Live verification:** watch (or mark watched) one episode + one movie on Plex → run
  `sync.py` once manually on whatbox → confirm both appear on the TV Time profile →
  install cron entry.

## Out of scope (YAGNI)

- Historical backfill of Plex watch history
- Multi-user / managed-account syncing
- Tautulli event-driven trigger (possible later additive enhancement)
- Trakt interactions of any kind (PlexTraktSync continues to own that)
- Unmarking / two-way sync (TV Time → Plex)

## Credits

TV Time endpoint behavior reverse-engineered by the
[Zggis/plex-tvtime](https://github.com/Zggis/plex-tvtime) project (unlicensed; used as
reference only) and [TheIndra55/tvtime-api](https://github.com/TheIndra55/tvtime-api) docs.
This project is an independent Python implementation.

## Implementation Notes (post-build, 2026-06-07)

Deviations and discoveries from the live build, superseding the sections above where they conflict:

1. **Manual mark-as-watched does NOT sync** (design above claimed it would). E2E-verified:
   `/:/scrobble` does not create `/status/sessions/history/all` entries; that endpoint records
   playback sessions only. Accepted as correct-for-this-deployment: the owner's pipeline uses
   watched-state to trigger deletion (PlexCleaner), so marking-to-delete must not pollute TV Time.
2. **The browser bootstrap step was eliminated.** The web app mints its anonymous JWT via
   `POST https://app.tvtime.com/sidecar?o_b64=<b64(https://api2.tozelabs.com/v2/user)>&lang=en&country_code=us&source=web&version=2025082201`
   with no auth and no body; the response carries a TOP-LEVEL `jwt_token` (not `data`-wrapped).
   `bootstrap_login` now auto-mints with no arguments, and the 401 path self-heals:
   refresh attempt → full automatic re-login (mint + credentials) → only then CRITICAL + 1 h backoff.
3. **Movie search uses `limit=5`** and picks the first `type == "movie"` result (table above
   says `limit=1`); prevents a similarly-named series from shadowing the movie.
4. **Hardening beyond spec:** atomic JSON writes + corrupt-state recovery (state/ledger/tokens),
   typed `PlexError` for non-XML responses, client-side owner-account filter (defense in depth),
   catch-all per-item containment (a poison item stops the run cleanly instead of crashing
   unlogged), tokens written 0600-from-birth.
5. The legacy `o=` sidecar form used by the client still works (the web app itself moved to
   `o_b64=`); if `o=` ever breaks, switch the constants to base64 form.
6. **Three opt-in/behavioral additions (post-launch).** (a) `EXCLUDED_LIBRARIES` (comma-separated
   Plex library names): items in those libraries are filtered out of both passes; section ids are
   resolved once per run via `GET /library/sections`, and resolution is fail-closed (any error on
   that call aborts the run with the watermark untouched, so a private library can never be synced
   because resolution failed). (b) `MARK_PREVIOUS_EPISODES` (bool): on a first-watch episode mark,
   bulk-mark all earlier episodes of the show via
   `watched_episodes/show/{show}/until/episode/{eid}`, resolving the show's tvdb id from the
   episode's `grandparentRatingKey` with a per-run cache; catch-up failures are non-fatal (the
   watched episode still counts as processed). (c) **Manual mark-as-watched now syncs** (reversing
   note 1): because `/:/scrobble` only bumps the item's `lastViewedAt`/`viewCount` and writes no
   history entry, a second pass scans each show/movie library with
   `sort=lastViewedAt:desc` and runs the same per-item pipeline. A normal playback has
   `lastViewedAt == viewedAt`, so the scan sees those as already-processed and never double-marks;
   `MAX_ITEMS_PER_RUN` became a total across both passes. The scan runs only when the history pass
   completed without a transient/auth break. `sections()` was changed to return
   `{title: {"key", "type"}}` so the scan can pick the per-section Plex type.
