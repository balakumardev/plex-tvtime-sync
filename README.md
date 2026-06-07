# plex-tvtime-sync

Sync your Plex watch activity (TV episodes and movies) to [TV Time](https://tvtime.com) **without a Plex Pass**. Instead of relying on Plex webhooks, it polls the Plex HTTP API on a cron schedule, reads your watch history since a saved watermark, and marks each item watched on TV Time. It is a small, dependency-light Python package (stdlib plus `requests` and `python-dotenv`) designed to run on a shared host or seedbox with no root, no Docker, and no browser.

## Why not webhooks?

Plex webhooks have two problems for this job: they require a Plex Pass, and even with one they **never fire for items you mark as watched manually** (only for actual playback). Polling the history endpoint catches everything, playback and manual marks alike. It also self-heals: because the watermark only advances past items that succeed, anything watched while your host or TV Time was unreachable is picked up on the next successful run.

## How it works

```
cron → run.sh → sync.py
  1. read config/state.json            (watermark = viewedAt of last processed item)
  2. GET /status/sessions/history/all  (accountID=1, newest first)
  3. for each new view event (oldest first, capped per run):
       GET /library/metadata/{ratingKey}  → GUIDs, type, season/episode
       episode → tvdb id → mark episode watched on TV Time
       movie   → search TV Time (tvdb, then tmdb, then imdb) → uuid → mark watched
  4. write state.json + ledger.json
```

TV Time is TVDB-native for shows, so the `tvdb://` GUID Plex already exposes **is** the TV Time episode id, with no mapping layer. Episodes that lack a `tvdb` GUID are skipped (TV Time cannot represent them). Legacy Plex agents (`com.plexapp.agents.*`) do not produce the modern `tvdb://` GUIDs and are not supported, so use the current Plex TV Series / Plex Movie agents.

All TV Time write calls tunnel through TV Time's own CORS sidecar (`/sidecar?o=<upstream>`) with a JWT bearer token:

| Purpose | Upstream endpoint |
|---|---|
| Credential login | `auth.tvtime.com/v1/login` (body `{username, password}`, bearer = bootstrap JWT) |
| Mark episode watched | `api2.tozelabs.com/v2/watched_episodes/episode/{tvdb_episode_id}?is_rewatch=0|1` |
| Movie search | `search.tvtime.com/v1/search/series,movie?q={id}&offset=0&limit=5` |
| Mark movie watched | `msapi.tvtime.com/prod/v1/tracking/{uuid}/watch` |

Note the movie search deliberately requests `limit=5` and then keeps the first result whose `type == "movie"`, rather than blindly taking the single top hit, so a closely-named series does not shadow the movie you actually watched.

## Install

```bash
git clone https://github.com/balakumardev/plex-tvtime-sync.git
cd plex-tvtime-sync
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

Create `config/.env`:

```ini
PLEX_URL=http://YOUR_PLEX_HOST:32400
PLEX_TOKEN=YOUR_PLEX_TOKEN
TVTIME_USER=you@example.com
TVTIME_PASSWORD=your-password
```

```bash
chmod 600 config/.env
```

The config directory defaults to `config/` next to the package. Override it with the `PTVS_CONFIG_DIR` environment variable if you keep secrets elsewhere.

## Authenticate with TV Time (one-time, roughly every 60 days)

TV Time has no API key. You bootstrap a session once from a browser, then the tool reuses the resulting tokens. The bootstrap JWT is an **anonymous** token that exists before you log in, so no password is ever typed into any web page:

1. Open `https://app.tvtime.com/welcome?mode=auth` in any browser.
2. In the DevTools console, run: `localStorage.getItem('flutter.jwtToken')`
3. Exchange it for account tokens (credentials are read from `config/.env`):

```bash
./venv/bin/python -m plex_tvtime_sync.bootstrap_login '<bootstrap-jwt>'
```

This writes `jwt_token` and `jwt_refresh_token` to `config/tokens.json`. The `jwt_token` lasts roughly 60 days. On an HTTP 401 during sync the tool attempts an automatic refresh; if that fails it logs a line starting with `CRITICAL` and applies a 1-hour backoff (suppressing further TV Time calls and freezing the watermark so nothing is lost). When you see that `CRITICAL` line, rerun the three bootstrap steps above to mint a fresh token.

## Run on cron

The very first run does not sync anything: it initializes the watermark to "now" and exits. This is intentional (forward-only, no history backfill), so install the cron line, watch something new, and it will sync from there.

```cron
3,8,13,18,23,28,33,38,43,48,53,58 * * * * /usr/bin/timeout 240 /usr/bin/flock -n ~/.tmp/plex-tvtime.lock ~/apps/plex-tvtime-sync/run.sh
```

`flock` prevents overlapping runs and `timeout` caps each run at 240 seconds. `run.sh` runs a single sync cycle with the venv's Python and self-rotates `~/logs/tvtime.log` once it passes 10 MB. Secrets live only in `config/` on the host, never in the repo and never in the crontab.

## State files

All live in the config directory and are gitignored:

| File | Contents |
|---|---|
| `config/state.json` | Watermark plus a recently-processed-key set for boundary dedup |
| `config/ledger.json` | All-time marked counts per episode/movie (drives rewatch detection) |
| `config/tokens.json` | TV Time `jwt_token` and `jwt_refresh_token` (written mode `0600`) |
| `config/auth_backoff` | Empty marker file; its mtime gates the 1-hour auth backoff |

## Behavior and limitations

- **Owner account only.** Syncs the Plex server owner (`accountID=1`); managed and shared users are ignored.
- **Forward-only.** No historical backfill; the watermark starts at the time of the first run.
- **Rewatches.** A repeat view of something already in the local ledger is sent with `is_rewatch=1`.
- **Rate-limited.** At most 50 items per run, with at least 1 second between TV Time calls, so a large catch-up never floods the API.
- **Permanent skips are logged.** Items deleted from Plex, episodes without a `tvdb` GUID, and movies that cannot be resolved via search are skipped once, logged, and the watermark advances past them.
- **Unofficial API.** TV Time exposes no public API; the endpoints here were reverse-engineered and may change or break without notice.

## Development

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/pytest
```

The suite is 50 tests, all HTTP mocked (`responses`): GUID extraction precedence, watermark and overlap dedup, ledger and rewatch flagging, and the exact TV Time request shapes (URL, headers, body) for login, episode mark, movie search, and movie mark, including the auth-failure path.

## Credits and license

Endpoint behavior was learned from the [Zggis/plex-tvtime](https://github.com/Zggis/plex-tvtime) project (which has no license, so it was used as a **reference only, with zero code reused**) and the [TheIndra55/tvtime-api](https://github.com/TheIndra55/tvtime-api) docs. This is an independent Python implementation. Licensed under the [MIT License](LICENSE).
