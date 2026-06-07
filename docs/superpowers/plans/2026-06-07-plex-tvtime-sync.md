# plex-tvtime-sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python cron poller on the whatbox seedbox that marks newly-watched Plex episodes/movies as watched on TV Time, without Plex Pass webhooks.

**Architecture:** Stateless-per-run CLI (`python -m plex_tvtime_sync.sync`) that polls the Plex history HTTP API since a persisted watermark, extracts TVDB ids from item GUIDs, and POSTs to TV Time's unofficial API (JWT bearer via their sidecar proxy). State (watermark + dedup + rewatch ledger + tokens) lives as JSON files in `config/`. Spec: `docs/superpowers/specs/2026-06-07-plex-tvtime-sync-design.md`.

**Tech Stack:** Python 3.12, `requests`, `python-dotenv`; tests: `pytest` + `responses`. No other deps (whatbox has no Java/Docker/Chrome).

**Security note for the executor:** TV Time + Plex credentials were provided by the user in-session / exist in the whatbox crontab. They go ONLY into `~/apps/plex-tvtime-sync/config/.env` on whatbox (chmod 600). Never into this repo, this plan, commits, or GitHub.

---

## File Structure

```
plex_tvtime_sync/
  __init__.py          # empty package marker
  config.py            # .env loading + validation → Config dataclass
  state.py             # watermark/processed-overlap (state.json) + rewatch ledger (ledger.json)
  plex_client.py       # Plex HTTP API: recent history + per-item metadata (XML)
  tvtime_client.py     # TV Time API: login/refresh/tokens, mark episode, movie search+mark, backoff
  sync.py              # orchestration entry point
  bootstrap_login.py   # rare manual helper: bootstrap JWT + creds → tokens.json
tests/
  test_config.py  test_state.py  test_plex_client.py  test_tvtime_client.py  test_sync.py
run.sh                 # cron wrapper: log rotation + venv python
requirements.txt  requirements-dev.txt  pytest.ini  LICENSE  README.md
```

---

### Task 1: Project scaffold

**Files:**
- Create: `plex_tvtime_sync/__init__.py`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `LICENSE`, `README.md`

- [ ] **Step 1: Create package + requirements + pytest config**

```bash
cd ~/personal/plex-tvtime-sync
mkdir -p plex_tvtime_sync tests config
touch plex_tvtime_sync/__init__.py
printf 'requests>=2.31\npython-dotenv>=1.0\n' > requirements.txt
printf 'pytest>=8.0\nresponses>=0.25\n-r requirements.txt\n' > requirements-dev.txt
printf '[pytest]\ntestpaths = tests\naddopts = -q\n' > pytest.ini
```

- [ ] **Step 2: Write LICENSE (MIT)**

Create `LICENSE` with the standard MIT text, first line of the copyright notice:
`Copyright (c) 2026 Bala Kumar`
(Full canonical MIT template from https://opensource.org/license/mit — 21 lines, verbatim.)

- [ ] **Step 3: Write README stub**

```markdown
# plex-tvtime-sync

Sync Plex watch activity to [TV Time](https://tvtime.com) **without Plex Pass**.
Polls the Plex HTTP API on a cron instead of using webhooks. WIP — see
`docs/superpowers/specs/` for the design. Full README lands at the end of the build.
```

- [ ] **Step 4: Create venv, install dev deps, verify pytest collects**

```bash
cd ~/personal/plex-tvtime-sync
python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
./venv/bin/pytest
```
Expected: `no tests ran` (exit code 5 is fine — nothing collected yet).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: scaffold package, deps, license, readme stub"
```

---

### Task 2: `config.py`

**Files:**
- Create: `plex_tvtime_sync/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import pytest
from plex_tvtime_sync import config as config_mod


def write_env(tmp_path, body):
    (tmp_path / ".env").write_text(body)
    return tmp_path


def test_loads_all_keys(tmp_path, monkeypatch):
    for k in config_mod.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    cdir = write_env(
        tmp_path,
        "PLEX_URL=http://plex.example:32400/\n"
        "PLEX_TOKEN=ptok\nTVTIME_USER=u@example.com\nTVTIME_PASSWORD=secret\n",
    )
    cfg = config_mod.load(cdir)
    assert cfg.plex_url == "http://plex.example:32400"  # trailing slash stripped
    assert cfg.plex_token == "ptok"
    assert cfg.tvtime_user == "u@example.com"
    assert cfg.tvtime_password == "secret"
    assert cfg.config_dir == cdir


def test_missing_key_exits_with_clear_message(tmp_path, monkeypatch):
    for k in config_mod.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    cdir = write_env(tmp_path, "PLEX_URL=http://plex.example:32400\n")
    with pytest.raises(SystemExit) as exc:
        config_mod.load(cdir)
    msg = str(exc.value)
    assert "PLEX_TOKEN" in msg and "TVTIME_USER" in msg and "TVTIME_PASSWORD" in msg
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_config.py -v` — Expected: FAIL (`No module named 'plex_tvtime_sync.config'`... import error).

- [ ] **Step 3: Implement**

```python
# plex_tvtime_sync/config.py
"""Load config from <config_dir>/.env. Required keys listed in REQUIRED."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REQUIRED = ["PLEX_URL", "PLEX_TOKEN", "TVTIME_USER", "TVTIME_PASSWORD"]
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass
class Config:
    plex_url: str
    plex_token: str
    tvtime_user: str
    tvtime_password: str
    config_dir: Path


def load(config_dir: Path | str | None = None) -> Config:
    cdir = Path(config_dir or os.environ.get("PTVS_CONFIG_DIR") or DEFAULT_CONFIG_DIR)
    load_dotenv(cdir / ".env", override=True)
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required config keys: {', '.join(missing)} (expected in {cdir / '.env'})"
        )
    return Config(
        plex_url=os.environ["PLEX_URL"].rstrip("/"),
        plex_token=os.environ["PLEX_TOKEN"],
        tvtime_user=os.environ["TVTIME_USER"],
        tvtime_password=os.environ["TVTIME_PASSWORD"],
        config_dir=cdir,
    )
```

(`PTVS_CONFIG_DIR`, not `PTS_CONFIG_DIR` — the latter is already used by plextraktsync on the same box.)

- [ ] **Step 4: Run to verify pass** — `./venv/bin/pytest tests/test_config.py -v` → 2 passed.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: config loading with validation"`

---

### Task 3: `state.py`

**Files:**
- Create: `plex_tvtime_sync/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
import json

from plex_tvtime_sync.state import OVERLAP_SECONDS, State


def test_first_run_initializes_watermark_to_now(tmp_path):
    s = State(tmp_path)
    assert s.first_run is True
    assert s.watermark > 1_700_000_000  # sane epoch


def test_roundtrip_not_first_run(tmp_path):
    s = State(tmp_path)
    s.save()
    s2 = State(tmp_path)
    assert s2.first_run is False
    assert s2.watermark == s.watermark


def test_mark_processed_advances_watermark_and_dedups(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"watermark": 1000, "processed": {}}))
    s = State(tmp_path)
    assert not s.is_processed("42:2000")
    s.mark_processed("42:2000", 2000)
    assert s.is_processed("42:2000")
    assert s.watermark == 2000
    s.mark_processed("41:1500", 1500)  # older item must not regress watermark
    assert s.watermark == 2000


def test_save_prunes_processed_outside_overlap(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"watermark": 1000, "processed": {}}))
    s = State(tmp_path)
    s.mark_processed("1:1000", 1000)
    s.mark_processed("2:50000", 50000)
    s.save()
    kept = json.loads((tmp_path / "state.json").read_text())["processed"]
    assert "2:50000" in kept
    assert "1:1000" not in kept  # 1000 < 50000 - OVERLAP_SECONDS


def test_ledger_counts_rewatches(tmp_path):
    s = State(tmp_path)
    assert s.seen_count("episodes", "123") == 0
    s.record_mark("episodes", "123")
    s.save()
    s2 = State(tmp_path)
    assert s2.seen_count("episodes", "123") == 1
    assert s2.seen_count("movies", "uuid-1") == 0
```

- [ ] **Step 2: Run to verify failure** — `./venv/bin/pytest tests/test_state.py -v` → import error FAIL.

- [ ] **Step 3: Implement**

```python
# plex_tvtime_sync/state.py
"""Persisted sync state: watermark + dedup overlap set (state.json), rewatch ledger (ledger.json)."""
import json
import time
from pathlib import Path

OVERLAP_SECONDS = 300  # spec: fixed 5-minute overlap window


class State:
    def __init__(self, config_dir: Path):
        self.path = Path(config_dir) / "state.json"
        self.ledger_path = Path(config_dir) / "ledger.json"
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.first_run = "watermark" not in data
        self.watermark: int = data.get("watermark", int(time.time()))
        self.processed: dict[str, int] = data.get("processed", {})
        self.ledger = (
            json.loads(self.ledger_path.read_text())
            if self.ledger_path.exists()
            else {"episodes": {}, "movies": {}}
        )

    def is_processed(self, key: str) -> bool:
        return key in self.processed

    def mark_processed(self, key: str, viewed_at: int) -> None:
        self.processed[key] = viewed_at
        if viewed_at > self.watermark:
            self.watermark = viewed_at

    def seen_count(self, kind: str, media_id) -> int:
        return self.ledger[kind].get(str(media_id), 0)

    def record_mark(self, kind: str, media_id) -> None:
        self.ledger[kind][str(media_id)] = self.ledger[kind].get(str(media_id), 0) + 1

    def save(self) -> None:
        cutoff = self.watermark - OVERLAP_SECONDS
        self.processed = {k: v for k, v in self.processed.items() if v >= cutoff}
        self.path.write_text(json.dumps({"watermark": self.watermark, "processed": self.processed}))
        self.ledger_path.write_text(json.dumps(self.ledger))
```

- [ ] **Step 4: Run to verify pass** — `./venv/bin/pytest tests/test_state.py -v` → 5 passed.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: watermark state and rewatch ledger"`

---

### Task 4: `plex_client.py`

**Files:**
- Create: `plex_tvtime_sync/plex_client.py`
- Test: `tests/test_plex_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_plex_client.py
import pytest
import responses

from plex_tvtime_sync.plex_client import PlexClient, PlexNotFound

BASE = "http://plex.example:32400"

HISTORY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="3">
  <Video historyKey="/status/sessions/history/1" ratingKey="101" key="/library/metadata/101"
         title="Pilot" grandparentTitle="Some Show" type="episode" viewedAt="2000" accountID="1"/>
  <Video historyKey="/status/sessions/history/2" ratingKey="202" key="/library/metadata/202"
         title="Some Movie" type="movie" viewedAt="3000" accountID="1"/>
  <Video historyKey="/status/sessions/history/3" title="Ghost entry (deleted)" type="episode"
         viewedAt="2500" accountID="1"/>
</MediaContainer>"""

EPISODE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="1">
  <Video ratingKey="101" type="episode" title="Pilot" grandparentTitle="Some Show"
         parentIndex="1" index="3">
    <Guid id="imdb://tt0959621"/>
    <Guid id="tmdb://62085"/>
    <Guid id="tvdb://349232"/>
  </Video>
</MediaContainer>"""


@responses.activate
def test_recent_history_parses_and_skips_keyless_entries():
    responses.get(f"{BASE}/status/sessions/history/all", body=HISTORY_XML)
    entries = PlexClient(BASE, "tok").recent_history()
    assert [e.rating_key for e in entries] == ["101", "202"]
    e = entries[0]
    assert (e.viewed_at, e.account_id, e.title, e.type) == (2000, 1, "Pilot", "episode")
    assert e.dedup_key == "101:2000"
    # token + filters sent
    assert "X-Plex-Token=tok" in responses.calls[0].request.url
    assert "accountID=1" in responses.calls[0].request.url


@responses.activate
def test_metadata_extracts_guids_and_episode_fields():
    responses.get(f"{BASE}/library/metadata/101", body=EPISODE_XML)
    item = PlexClient(BASE, "tok").metadata("101")
    assert item.type == "episode"
    assert item.guids == {"imdb": "tt0959621", "tmdb": "62085", "tvdb": "349232"}
    assert (item.grandparent_title, item.season, item.episode) == ("Some Show", 1, 3)


@responses.activate
def test_metadata_404_raises_not_found():
    responses.get(f"{BASE}/library/metadata/999", status=404)
    with pytest.raises(PlexNotFound):
        PlexClient(BASE, "tok").metadata("999")


@responses.activate
def test_metadata_empty_container_raises_not_found():
    responses.get(
        f"{BASE}/library/metadata/998",
        body='<?xml version="1.0"?><MediaContainer size="0"></MediaContainer>',
    )
    with pytest.raises(PlexNotFound):
        PlexClient(BASE, "tok").metadata("998")
```

- [ ] **Step 2: Run to verify failure** — `./venv/bin/pytest tests/test_plex_client.py -v` → import error FAIL.

- [ ] **Step 3: Implement**

```python
# plex_tvtime_sync/plex_client.py
"""Minimal Plex HTTP API client (XML). Only what the sync needs: recent history + metadata."""
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests


class PlexNotFound(Exception):
    pass


@dataclass
class HistoryEntry:
    rating_key: str
    viewed_at: int
    account_id: int
    title: str
    type: str  # "episode" | "movie"

    @property
    def dedup_key(self) -> str:
        return f"{self.rating_key}:{self.viewed_at}"


@dataclass
class MediaItem:
    type: str
    guids: dict
    title: str
    grandparent_title: str | None = None
    season: int | None = None
    episode: int | None = None

    def label(self) -> str:
        if self.type == "episode" and self.grandparent_title:
            return f"{self.grandparent_title} S{self.season or 0:02d}E{self.episode or 0:02d} - {self.title}"
        return self.title


class PlexClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, **params) -> ET.Element:
        params["X-Plex-Token"] = self.token
        r = requests.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        if r.status_code == 404:
            raise PlexNotFound(path)
        r.raise_for_status()
        return ET.fromstring(r.text)

    def recent_history(self, account_id: int = 1, limit: int = 200) -> list[HistoryEntry]:
        """Most recent view events, newest first. Entries without ratingKey (deleted items
        whose metadata is gone) are skipped — they cannot be resolved to GUIDs anyway."""
        root = self._get(
            "/status/sessions/history/all",
            **{
                "sort": "viewedAt:desc",
                "accountID": account_id,
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": limit,
            },
        )
        out: list[HistoryEntry] = []
        for v in root:
            rating_key, viewed_at = v.get("ratingKey"), v.get("viewedAt")
            if not rating_key or not viewed_at:
                continue
            if v.get("type") not in ("episode", "movie"):
                continue
            out.append(
                HistoryEntry(
                    rating_key=rating_key,
                    viewed_at=int(viewed_at),
                    account_id=int(v.get("accountID", 0)),
                    title=v.get("title", ""),
                    type=v.get("type", ""),
                )
            )
        return out

    def metadata(self, rating_key: str) -> MediaItem:
        root = self._get(f"/library/metadata/{rating_key}")
        if len(root) == 0:
            raise PlexNotFound(rating_key)
        v = root[0]
        guids = {}
        for g in v.findall("Guid"):
            gid = g.get("id", "")
            if "://" in gid:
                scheme, val = gid.split("://", 1)
                guids[scheme] = val
        return MediaItem(
            type=v.get("type", ""),
            guids=guids,
            title=v.get("title", ""),
            grandparent_title=v.get("grandparentTitle"),
            season=int(v.get("parentIndex")) if v.get("parentIndex") else None,
            episode=int(v.get("index")) if v.get("index") else None,
        )
```

- [ ] **Step 4: Run to verify pass** — `./venv/bin/pytest tests/test_plex_client.py -v` → 4 passed.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: plex history + metadata client"`

---

### Task 5: `tvtime_client.py`

**Files:**
- Create: `plex_tvtime_sync/tvtime_client.py`
- Test: `tests/test_tvtime_client.py`

API shapes per spec (reverse-engineered; sidecar = TV Time's own CORS proxy):
- login: `POST https://beta-app.tvtime.com/sidecar?o=https://auth.tvtime.com/v1/login`, bearer = bootstrap JWT, JSON body `{"username","password"}` → `data.jwt_token`, `data.jwt_refresh_token`
- episode: `POST https://app.tvtime.com/sidecar?o=https://api2.tozelabs.com/v2/watched_episodes/episode/{id}&is_rewatch={0|1}`, bearer = jwt_token, `Host: app.tvtime.com:80`, body = creds JSON (mirrors reference impl)
- movie search: `GET https://app.tvtime.com/sidecar?o=https://search.tvtime.com/v1/search/series,movie&q={q}&offset=0&limit=5`
- movie mark: `POST https://app.tvtime.com/sidecar?o=https://msapi.tvtime.com/prod/v1/tracking/{uuid}/watch`
- refresh (experimental, may not exist): `POST https://beta-app.tvtime.com/sidecar?o=https://auth.tvtime.com/v1/refresh` body `{"refresh_token"}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tvtime_client.py
import json

import pytest
import responses

from plex_tvtime_sync.tvtime_client import (
    EPISODE_SIDECAR,
    LOGIN_SIDECAR,
    MOVIE_SIDECAR,
    REFRESH_SIDECAR,
    SEARCH_SIDECAR,
    TVTimeAuthError,
    TVTimeClient,
)


def make_client(tmp_path, tokens=None):
    if tokens is not None:
        (tmp_path / "tokens.json").write_text(json.dumps(tokens))
    return TVTimeClient(tmp_path, "u@example.com", "pw")


@responses.activate
def test_login_with_bootstrap_saves_tokens(tmp_path):
    responses.post(
        LOGIN_SIDECAR,
        json={"data": {"jwt_token": "JWT1", "jwt_refresh_token": "RT1"}},
    )
    c = make_client(tmp_path)
    c.login_with_bootstrap("BOOT")
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer BOOT"
    assert json.loads(req.body) == {"username": "u@example.com", "password": "pw"}
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved == {"jwt_token": "JWT1", "jwt_refresh_token": "RT1"}
    assert oct((tmp_path / "tokens.json").stat().st_mode)[-3:] == "600"


@responses.activate
def test_mark_episode_request_shape(tmp_path):
    url = EPISODE_SIDECAR.format(eid="349232", rw=0)
    responses.post(url, json={"result": "OK"})
    c = make_client(tmp_path, {"jwt_token": "JWT1", "jwt_refresh_token": "RT1"})
    c.mark_episode("349232", rewatch=False)
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer JWT1"
    assert req.headers["Host"] == "app.tvtime.com:80"


@responses.activate
def test_mark_episode_rewatch_flag(tmp_path):
    url = EPISODE_SIDECAR.format(eid="349232", rw=1)
    responses.post(url, json={"result": "OK"})
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    c.mark_episode("349232", rewatch=True)  # would 404 ConnectionError if URL wrong


@responses.activate
def test_401_triggers_refresh_then_retry(tmp_path):
    url = EPISODE_SIDECAR.format(eid="1", rw=0)
    responses.post(url, status=401)
    responses.post(REFRESH_SIDECAR, json={"data": {"jwt_token": "JWT2"}})
    responses.post(url, json={"result": "OK"})
    c = make_client(tmp_path, {"jwt_token": "OLD", "jwt_refresh_token": "RT1"})
    c.mark_episode("1")
    assert json.loads((tmp_path / "tokens.json").read_text())["jwt_token"] == "JWT2"


@responses.activate
def test_401_with_failed_refresh_raises_auth_error(tmp_path):
    responses.post(EPISODE_SIDECAR.format(eid="1", rw=0), status=401)
    responses.post(REFRESH_SIDECAR, status=400)
    c = make_client(tmp_path, {"jwt_token": "OLD", "jwt_refresh_token": "RT1"})
    with pytest.raises(TVTimeAuthError):
        c.mark_episode("1")


def test_no_tokens_raises_auth_error(tmp_path):
    c = make_client(tmp_path)
    with pytest.raises(TVTimeAuthError):
        c.mark_episode("1")


@responses.activate
def test_search_movie_uuid_picks_movie_type(tmp_path):
    responses.get(
        SEARCH_SIDECAR.format(q="603"),
        json={"data": [{"type": "series", "uuid": "S-1"}, {"type": "movie", "uuid": "M-1"}]},
    )
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    assert c.search_movie_uuid("603") == "M-1"


@responses.activate
def test_search_movie_uuid_none_when_no_movie(tmp_path):
    responses.get(SEARCH_SIDECAR.format(q="603"), json={"data": []})
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    assert c.search_movie_uuid("603") is None


@responses.activate
def test_mark_movie_posts_to_tracking(tmp_path):
    responses.post(MOVIE_SIDECAR.format(uuid="M-1"), json={"ok": True})
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    c.mark_movie("M-1")


def test_backoff_marker_roundtrip(tmp_path):
    c = make_client(tmp_path)
    assert c.in_backoff() is False
    c.set_backoff()
    assert c.in_backoff() is True
    c.clear_backoff()
    assert c.in_backoff() is False
```

- [ ] **Step 2: Run to verify failure** — `./venv/bin/pytest tests/test_tvtime_client.py -v` → import error FAIL.

- [ ] **Step 3: Implement**

```python
# plex_tvtime_sync/tvtime_client.py
"""TV Time unofficial API client (JWT bearer via TV Time's sidecar CORS proxy).

Endpoint knowledge credit: Zggis/plex-tvtime (reference only) and TheIndra55/tvtime-api.
"""
import json
import time
from pathlib import Path

import requests

APP = "https://app.tvtime.com"
BETA = "https://beta-app.tvtime.com"
LOGIN_SIDECAR = f"{BETA}/sidecar?o=https://auth.tvtime.com/v1/login"
REFRESH_SIDECAR = f"{BETA}/sidecar?o=https://auth.tvtime.com/v1/refresh"
EPISODE_SIDECAR = (
    APP + "/sidecar?o=https://api2.tozelabs.com/v2/watched_episodes/episode/{eid}&is_rewatch={rw}"
)
SEARCH_SIDECAR = APP + "/sidecar?o=https://search.tvtime.com/v1/search/series,movie&q={q}&offset=0&limit=5"
MOVIE_SIDECAR = APP + "/sidecar?o=https://msapi.tvtime.com/prod/v1/tracking/{uuid}/watch"
BACKOFF_SECONDS = 3600


class TVTimeAuthError(Exception):
    """Tokens missing/expired and unrecoverable without a new bootstrap login."""


class TVTimeError(Exception):
    """Non-auth TV Time failure (treat as transient)."""


class TVTimeClient:
    def __init__(self, config_dir: Path, username: str, password: str, timeout: int = 30):
        config_dir = Path(config_dir)
        self.tokens_path = config_dir / "tokens.json"
        self.backoff_path = config_dir / "auth_backoff"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.tokens = (
            json.loads(self.tokens_path.read_text()) if self.tokens_path.exists() else {}
        )

    # ---- backoff marker (spec: 1h suppression after unrecoverable auth failure) ----
    def in_backoff(self) -> bool:
        try:
            return time.time() - self.backoff_path.stat().st_mtime < BACKOFF_SECONDS
        except FileNotFoundError:
            return False

    def set_backoff(self) -> None:
        self.backoff_path.touch()

    def clear_backoff(self) -> None:
        self.backoff_path.unlink(missing_ok=True)

    # ---- auth ----
    def _save_tokens(self) -> None:
        self.tokens_path.write_text(json.dumps(self.tokens))
        self.tokens_path.chmod(0o600)

    def login_with_bootstrap(self, bootstrap_jwt: str) -> None:
        r = requests.post(
            LOGIN_SIDECAR,
            json={"username": self.username, "password": self.password},
            headers={"Authorization": f"Bearer {bootstrap_jwt}"},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise TVTimeAuthError(f"login failed: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()["data"]
        self.tokens = {
            "jwt_token": data["jwt_token"],
            "jwt_refresh_token": data.get("jwt_refresh_token"),
        }
        self._save_tokens()
        self.clear_backoff()

    def try_refresh(self) -> None:
        """Experimental: the refresh endpoint is unverified. Failure → TVTimeAuthError."""
        rt = self.tokens.get("jwt_refresh_token")
        if not rt:
            raise TVTimeAuthError("no refresh token stored")
        try:
            r = requests.post(
                REFRESH_SIDECAR,
                json={"refresh_token": rt},
                headers={"Authorization": f"Bearer {rt}"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise TVTimeAuthError(f"refresh request failed: {e}") from e
        if r.status_code != 200:
            raise TVTimeAuthError(f"refresh rejected: HTTP {r.status_code}")
        data = r.json().get("data") or {}
        if "jwt_token" not in data:
            raise TVTimeAuthError("refresh response missing jwt_token")
        self.tokens["jwt_token"] = data["jwt_token"]
        self.tokens["jwt_refresh_token"] = data.get("jwt_refresh_token", rt)
        self._save_tokens()

    # ---- request plumbing ----
    def _headers(self) -> dict:
        if not self.tokens.get("jwt_token"):
            raise TVTimeAuthError("not logged in — run bootstrap_login first")
        return {
            "Authorization": f"Bearer {self.tokens['jwt_token']}",
            "Host": "app.tvtime.com:80",
        }

    def _body(self) -> dict:
        return {"username": self.username, "password": self.password}

    def _post(self, url: str) -> requests.Response:
        r = requests.post(url, json=self._body(), headers=self._headers(), timeout=self.timeout)
        if r.status_code == 401:
            self.try_refresh()  # raises TVTimeAuthError if unrecoverable
            r = requests.post(url, json=self._body(), headers=self._headers(), timeout=self.timeout)
            if r.status_code == 401:
                raise TVTimeAuthError("still 401 after token refresh")
        if r.status_code >= 400:
            raise TVTimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r

    # ---- public API ----
    def mark_episode(self, tvdb_episode_id: str, rewatch: bool = False) -> None:
        self._post(EPISODE_SIDECAR.format(eid=tvdb_episode_id, rw=1 if rewatch else 0))

    def search_movie_uuid(self, query: str) -> str | None:
        r = requests.get(SEARCH_SIDECAR.format(q=query), headers=self._headers(), timeout=self.timeout)
        if r.status_code == 401:
            raise TVTimeAuthError("401 on movie search")
        if r.status_code >= 400:
            raise TVTimeError(f"search HTTP {r.status_code}")
        for item in r.json().get("data") or []:
            if item.get("type") == "movie" and item.get("uuid"):
                return item["uuid"]
        return None

    def mark_movie(self, uuid: str) -> None:
        self._post(MOVIE_SIDECAR.format(uuid=uuid))
```

- [ ] **Step 4: Run to verify pass** — `./venv/bin/pytest tests/test_tvtime_client.py -v` → 10 passed.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: tvtime client - login, refresh, episode/movie marking, backoff"`

---

### Task 6: `sync.py` (orchestration)

**Files:**
- Create: `plex_tvtime_sync/sync.py`
- Test: `tests/test_sync.py`

- [ ] **Step 1: Write the failing tests** (fakes, no HTTP)

```python
# tests/test_sync.py
import json

import pytest

from plex_tvtime_sync import sync as sync_mod
from plex_tvtime_sync.plex_client import HistoryEntry, MediaItem, PlexNotFound
from plex_tvtime_sync.state import State
from plex_tvtime_sync.tvtime_client import TVTimeAuthError, TVTimeError


class FakePlex:
    def __init__(self, history=None, meta=None, history_error=None):
        self.history = history or []
        self.meta = meta or {}
        self.history_error = history_error

    def recent_history(self):
        if self.history_error:
            raise self.history_error
        return self.history

    def metadata(self, rating_key):
        if rating_key not in self.meta:
            raise PlexNotFound(rating_key)
        return self.meta[rating_key]


class FakeTVTime:
    def __init__(self, fail_with=None, search_result="M-UUID"):
        self.episodes, self.movies, self.searches = [], [], []
        self.fail_with = fail_with
        self.search_result = search_result
        self.backoff_set = False
        self._in_backoff = False

    def in_backoff(self):
        return self._in_backoff

    def set_backoff(self):
        self.backoff_set = True

    def mark_episode(self, eid, rewatch=False):
        if self.fail_with:
            raise self.fail_with
        self.episodes.append((eid, rewatch))

    def search_movie_uuid(self, q):
        self.searches.append(q)
        return self.search_result

    def mark_movie(self, uuid):
        if self.fail_with:
            raise self.fail_with
        self.movies.append(uuid)


def seeded_state(tmp_path, watermark=1000):
    (tmp_path / "state.json").write_text(json.dumps({"watermark": watermark, "processed": {}}))
    return State(tmp_path)


def ep_entry(rk="101", viewed=2000):
    return HistoryEntry(rating_key=rk, viewed_at=viewed, account_id=1, title="Pilot", type="episode")


def ep_item(tvdb="349232"):
    guids = {"tvdb": tvdb} if tvdb else {}
    return MediaItem(type="episode", guids=guids, title="Pilot", grandparent_title="Show", season=1, episode=3)


def test_first_run_only_initializes(tmp_path):
    state = State(tmp_path)  # no state.json → first run
    tvtime = FakeTVTime()
    rc = sync_mod.run(plex=FakePlex(history=[ep_entry()]), tvtime=tvtime, state=state, sleep=lambda s: None)
    assert rc == 0
    assert tvtime.episodes == []
    assert (tmp_path / "state.json").exists()


def test_marks_new_episode_and_advances_watermark(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item()})
    sync_mod.run(plex=plex, tvtime=tvtime, state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["watermark"] == 2000
    assert "101:2000" in saved["processed"]


def test_rewatch_flag_from_ledger(tmp_path):
    state = seeded_state(tmp_path)
    state.record_mark("episodes", "349232")
    tvtime = FakeTVTime()
    sync_mod.run(plex=FakePlex(history=[ep_entry()], meta={"101": ep_item()}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", True)]


def test_movie_uses_guid_fallback_order(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    movie = MediaItem(type="movie", guids={"imdb": "tt0133093", "tmdb": "603"}, title="The Matrix")
    entry = HistoryEntry(rating_key="202", viewed_at=2100, account_id=1, title="The Matrix", type="movie")
    sync_mod.run(plex=FakePlex(history=[entry], meta={"202": movie}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.searches == ["603"]  # tvdb missing → tmdb first, imdb not needed
    assert tvtime.movies == ["M-UUID"]


def test_skips_already_processed_and_old(tmp_path):
    state = seeded_state(tmp_path, watermark=5000)
    state.mark_processed("101:5100", 5100)
    tvtime = FakeTVTime()
    history = [ep_entry(viewed=5100), ep_entry(rk="100", viewed=100)]  # dup + ancient
    sync_mod.run(plex=FakePlex(history=history, meta={"101": ep_item()}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == []


def test_deleted_item_permanent_skip_advances(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    sync_mod.run(plex=FakePlex(history=[ep_entry()], meta={}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)  # metadata raises PlexNotFound
    assert tvtime.episodes == []
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 2000


def test_episode_without_tvdb_guid_permanent_skip(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    sync_mod.run(plex=FakePlex(history=[ep_entry()], meta={"101": ep_item(tvdb=None)}),
                 tvtime=tvtime, state=state, sleep=lambda s: None)
    assert tvtime.episodes == []
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 2000


def test_auth_error_sets_backoff_and_freezes_watermark(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime(fail_with=TVTimeAuthError("expired"))
    sync_mod.run(plex=FakePlex(history=[ep_entry()], meta={"101": ep_item()}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.backoff_set is True
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 1000


def test_backoff_active_skips_run(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    tvtime._in_backoff = True
    plex = FakePlex(history_error=AssertionError("should not be called"))
    rc = sync_mod.run(plex=plex, tvtime=tvtime, state=state, sleep=lambda s: None)
    assert rc == 0


def test_transient_tvtime_error_stops_without_advancing(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime(fail_with=TVTimeError("503"))
    sync_mod.run(plex=FakePlex(history=[ep_entry()], meta={"101": ep_item()}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.backoff_set is False
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 1000


def test_plex_unreachable_is_quiet_noop(tmp_path):
    state = seeded_state(tmp_path)
    rc = sync_mod.run(plex=FakePlex(history_error=ConnectionError("down")), tvtime=FakeTVTime(),
                      state=state, sleep=lambda s: None)
    assert rc == 0


def test_per_run_cap_and_oldest_first(tmp_path):
    state = seeded_state(tmp_path)
    history = [ep_entry(rk=str(100 + i), viewed=2000 + i) for i in range(60)]
    meta = {str(100 + i): ep_item(tvdb=str(9000 + i)) for i in range(60)}
    tvtime = FakeTVTime()
    sync_mod.run(plex=FakePlex(history=history, meta=meta), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert len(tvtime.episodes) == 50
    assert tvtime.episodes[0][0] == "9000"  # oldest first
```

- [ ] **Step 2: Run to verify failure** — `./venv/bin/pytest tests/test_sync.py -v` → import error FAIL.

- [ ] **Step 3: Implement**

```python
# plex_tvtime_sync/sync.py
"""Orchestration: poll Plex history since watermark → mark watched on TV Time. Cron entry point."""
import logging
import time

import requests

from . import config as config_mod
from .plex_client import PlexClient, PlexNotFound
from .state import OVERLAP_SECONDS, State
from .tvtime_client import TVTimeAuthError, TVTimeClient, TVTimeError

MAX_ITEMS_PER_RUN = 50
CALL_SPACING_SECONDS = 1.0
MOVIE_GUID_ORDER = ("tvdb", "tmdb", "imdb")

log = logging.getLogger("plex-tvtime-sync")


def run(cfg=None, plex=None, tvtime=None, state=None, sleep=time.sleep) -> int:
    if cfg is None and (plex is None or tvtime is None or state is None):
        cfg = config_mod.load()
    plex = plex or PlexClient(cfg.plex_url, cfg.plex_token)
    tvtime = tvtime or TVTimeClient(cfg.config_dir, cfg.tvtime_user, cfg.tvtime_password)
    state = state or State(cfg.config_dir)

    if state.first_run:
        log.info("first run: watermark initialized to now (%s); nothing to sync", state.watermark)
        state.save()
        return 0
    if tvtime.in_backoff():
        log.warning("auth backoff active - skipping run (re-auth with bootstrap_login to clear)")
        return 0

    try:
        entries = plex.recent_history()
    except Exception as e:  # Plex down: quiet no-op, watermark untouched
        log.error("plex unreachable: %s", e)
        return 0

    cutoff = state.watermark - OVERLAP_SECONDS
    todo = sorted(
        (e for e in entries if e.viewed_at >= cutoff and not state.is_processed(e.dedup_key)),
        key=lambda e: e.viewed_at,
    )[:MAX_ITEMS_PER_RUN]

    for entry in todo:
        try:
            item = plex.metadata(entry.rating_key)
        except PlexNotFound:
            log.info("skip (deleted from plex): %s", entry.title)
            state.mark_processed(entry.dedup_key, entry.viewed_at)
            continue
        except Exception as e:
            log.error("plex metadata error for %s: %s - retrying next run", entry.rating_key, e)
            break

        try:
            if item.type == "episode":
                tvdb_id = item.guids.get("tvdb")
                if not tvdb_id:
                    log.warning("skip (no tvdb guid): %s", item.label())
                    state.mark_processed(entry.dedup_key, entry.viewed_at)
                    continue
                rewatch = state.seen_count("episodes", tvdb_id) > 0
                tvtime.mark_episode(tvdb_id, rewatch=rewatch)
                state.record_mark("episodes", tvdb_id)
                log.info("marked episode%s: %s (tvdb %s)", " [rewatch]" if rewatch else "", item.label(), tvdb_id)
            elif item.type == "movie":
                uuid = None
                for scheme in MOVIE_GUID_ORDER:
                    if item.guids.get(scheme):
                        uuid = tvtime.search_movie_uuid(item.guids[scheme])
                        if uuid:
                            break
                if not uuid:
                    log.warning("skip (movie not found on tvtime): %s %s", item.label(), item.guids)
                    state.mark_processed(entry.dedup_key, entry.viewed_at)
                    continue
                tvtime.mark_movie(uuid)
                state.record_mark("movies", uuid)
                log.info("marked movie: %s (uuid %s)", item.label(), uuid)
            else:
                state.mark_processed(entry.dedup_key, entry.viewed_at)
                continue
            state.mark_processed(entry.dedup_key, entry.viewed_at)
            sleep(CALL_SPACING_SECONDS)
        except TVTimeAuthError as e:
            log.critical("TVTIME AUTH EXPIRED - run bootstrap_login again. %s", e)
            tvtime.set_backoff()
            break
        except (TVTimeError, requests.RequestException) as e:
            log.error("tvtime transient error on %s: %s - retrying next run", item.label(), e)
            break

    state.save()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify pass** — `./venv/bin/pytest tests/test_sync.py -v` → 12 passed; then full suite `./venv/bin/pytest` → all pass.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: sync orchestration with watermark, dedup, caps and failure isolation"`

---

### Task 7: `bootstrap_login.py` + `run.sh`

**Files:**
- Create: `plex_tvtime_sync/bootstrap_login.py`, `run.sh`
- Test: `tests/test_bootstrap_login.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap_login.py
from plex_tvtime_sync import bootstrap_login


def test_usage_error_without_arg(capsys):
    rc = bootstrap_login.main(["prog"])
    assert rc == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_strips_quotes_and_whitespace(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def login_with_bootstrap(self, jwt):
            captured["jwt"] = jwt

    class FakeCfg:
        config_dir = tmp_path
        tvtime_user = "u"
        tvtime_password = "p"

    monkeypatch.setattr(bootstrap_login, "TVTimeClient", FakeClient)
    monkeypatch.setattr(bootstrap_login.config_mod, "load", lambda: FakeCfg())
    rc = bootstrap_login.main(["prog", ' "BOOT.JWT" '])
    assert rc == 0
    assert captured["jwt"] == "BOOT.JWT"
```

- [ ] **Step 2: Run to verify failure** — `./venv/bin/pytest tests/test_bootstrap_login.py -v` → FAIL.

- [ ] **Step 3: Implement bootstrap_login.py**

```python
# plex_tvtime_sync/bootstrap_login.py
"""Rare manual step: exchange a bootstrap JWT for account tokens.

Get the bootstrap JWT from any browser (no login needed):
  1. Open https://app.tvtime.com/welcome?mode=auth
  2. DevTools console: localStorage.getItem('flutter.jwtToken')
  3. python -m plex_tvtime_sync.bootstrap_login '<token>'
Credentials come from config/.env; tokens are written to config/tokens.json.
"""
import sys

from . import config as config_mod
from .tvtime_client import TVTimeClient


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv
    if len(argv) != 2:
        print("usage: python -m plex_tvtime_sync.bootstrap_login '<bootstrap-jwt>'")
        return 2
    jwt = argv[1].strip().strip('"').strip("'")
    cfg = config_mod.load()
    client = TVTimeClient(cfg.config_dir, cfg.tvtime_user, cfg.tvtime_password)
    client.login_with_bootstrap(jwt)
    print(f"OK: tokens written to {cfg.config_dir / 'tokens.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Write run.sh** (log rotation lives here so the rotated file never loses the cron fd)

```bash
#!/bin/bash
# Cron wrapper: rotate log at 10MB, then run one sync cycle inside the venv.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/logs/tvtime.log"
mkdir -p "$HOME/logs"
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || stat -f%z "$LOG")" -gt 10485760 ]; then
    mv "$LOG" "$LOG.1"
fi
exec "$DIR/venv/bin/python" -m plex_tvtime_sync.sync >> "$LOG" 2>&1
```

```bash
chmod +x run.sh
```

- [ ] **Step 5: Run full suite + commit**

```bash
./venv/bin/pytest                      # expected: all pass
git add -A && git commit -m "feat: bootstrap login helper and cron wrapper with log rotation"
```

---

### Task 8: README, GitHub repo + reference fork

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Write the full README**

Sections (write actual prose, concise): What it is (Plex→TV Time sync without Plex Pass — polls Plex HTTP API on cron); Why not webhooks (Plex Pass-only, and they miss manual mark-watched); How it works (watermark → history → TVDB GUID → TV Time API, 4-row endpoint table from the spec); Install (git clone, venv, `pip install -r requirements.txt`, `config/.env` template block with the 4 keys and placeholder values); Auth bootstrap (the 3-step localStorage instruction from `bootstrap_login.py`'s docstring, plus "~60-day token life; rerun on CRITICAL auth log lines"); Cron example (the exact line from Task 10 Step 3); State files table (state.json / ledger.json / tokens.json / auth_backoff); Limitations (forward-only, owner account only, TVDB-keyed, unofficial API may break); Credits (Zggis/plex-tvtime — unlicensed, used as endpoint reference only, no code reused; TheIndra55/tvtime-api docs); License (MIT).

- [ ] **Step 2: Commit** — `git add README.md && git commit -m "docs: full README"`

- [ ] **Step 3: Switch gh to personal account + create repo + push** (user pre-approved public repo)

```bash
gh auth status   # if active account is not balakumardev: gh auth switch --user balakumardev
cd ~/personal/plex-tvtime-sync
gh repo create balakumardev/plex-tvtime-sync --public --source=. --remote=origin --push
```
Expected: repo URL printed; `git log origin/main --oneline` matches local.

- [ ] **Step 4: Fork the reference project (unmodified, credit/reference only)**

```bash
gh repo fork Zggis/plex-tvtime --clone=false
```
Expected: `balakumardev/plex-tvtime` created on GitHub. Do not modify it (no license).

---

### Task 9: Deploy to whatbox

**Files:** (remote) `~/apps/plex-tvtime-sync/` on whatbox

- [ ] **Step 1: Clone + venv on whatbox**

```bash
ssh whatbox 'git clone https://github.com/balakumardev/plex-tvtime-sync.git ~/apps/plex-tvtime-sync \
  && cd ~/apps/plex-tvtime-sync && python3 -m venv venv && ./venv/bin/pip install -q -r requirements.txt \
  && ./venv/bin/python -c "import plex_tvtime_sync.sync; print(\"import OK\")"'
```
Expected: `import OK`.

- [ ] **Step 2: Write config/.env on whatbox (secrets — never echo to logs/commits)**

`PLEX_URL` and `PLEX_TOKEN`: copy the values already present in the whatbox crontab (the `plex-unwatcher.py` line exports both — `crontab -l | grep PLEX_URL`). `TVTIME_USER`/`TVTIME_PASSWORD`: provided by the user in-session.

```bash
ssh whatbox 'umask 077 && cat > ~/apps/plex-tvtime-sync/config/.env' <<'EOF'
PLEX_URL=<value from existing crontab>
PLEX_TOKEN=<value from existing crontab>
TVTIME_USER=<user-provided>
TVTIME_PASSWORD=<user-provided>
EOF
ssh whatbox 'chmod 600 ~/apps/plex-tvtime-sync/config/.env && ls -la ~/apps/plex-tvtime-sync/config/'
```
Expected: `.env` exists with `-rw-------`.

- [ ] **Step 3: First-run watermark init (no TV Time tokens needed yet)**

```bash
ssh whatbox 'cd ~/apps/plex-tvtime-sync && ./venv/bin/python -m plex_tvtime_sync.sync'
```
Expected output: `first run: watermark initialized to now ... nothing to sync`; `config/state.json` created.

---

### Task 10: TV Time auth bootstrap + live E2E verification

- [ ] **Step 1: Get bootstrap JWT (Mac browser, no credentials typed anywhere)**

Open `https://app.tvtime.com/welcome?mode=auth` (Chrome MCP or manually), then read
`localStorage.getItem('flutter.jwtToken')` from the console — this anonymous token exists pre-login.

- [ ] **Step 2: Exchange it on whatbox**

```bash
ssh whatbox 'cd ~/apps/plex-tvtime-sync && ./venv/bin/python -m plex_tvtime_sync.bootstrap_login "<JWT>"'
```
Expected: `OK: tokens written to .../config/tokens.json`. If HTTP 400: the bootstrap token may be stale — re-grab it (they're short-lived) and retry within a minute or two.

- [ ] **Step 3: Live E2E — episode**

⚠️ Marking items watched in Plex feeds the user's PlexCleaner (deletes watched content after 2 days). Use the scrobble/unscrobble pair, and confirm the test item with the user first.

```bash
# Pick a recent unwatched episode WITH the user, get its ratingKey, then:
ssh whatbox 'source <(crontab -l | grep -o "export PLEX_URL=[^;]*"); source <(crontab -l | grep -o "export PLEX_TOKEN=[^;]*"); \
  curl -s "$PLEX_URL/:/scrobble?key=<ratingKey>&identifier=com.plexapp.plugins.library&X-Plex-Token=$PLEX_TOKEN"'
ssh whatbox 'cd ~/apps/plex-tvtime-sync && ./venv/bin/python -m plex_tvtime_sync.sync'
```
Expected log: `marked episode: <Show> SxxExx ... (tvdb <id>)`. User verifies it shows on their TV Time profile. Then revert Plex state (protects from cleaner): same curl with `/:/unscrobble`. If scrobble does not produce a history entry (possible Plex quirk), fall back to the user playing 2+ minutes of something to >90%.

- [ ] **Step 4: Live E2E — movie**

Same scrobble→sync→verify→unscrobble cycle with a movie ratingKey. Expected log: `marked movie: <Title> (uuid ...)`. If the search→uuid step 4xx/5xxes or marks fail only for movies, adjust `mark_movie` body per actual API response (research note: movie calls may need the refresh token stripped from the body), update tests, commit, redeploy (`ssh whatbox 'cd ~/apps/plex-tvtime-sync && git pull'`).

---

### Task 11: Install cron + monitor

- [ ] **Step 1: Append cron entry on whatbox** (offsets :03,:08… — after unwatcher/trakt/cleaner)

```bash
ssh whatbox 'crontab -l > /tmp/ct.new && cat >> /tmp/ct.new <<"EOF"

###################### Plex -> TVTime Sync ###################################
3,8,13,18,23,28,33,38,43,48,53,58 * * * * /usr/bin/timeout 240 /usr/bin/flock -n ~/.tmp/plex-tvtime.lock ~/apps/plex-tvtime-sync/run.sh
EOF
crontab /tmp/ct.new && crontab -l | tail -4'
```
Expected: new block visible. (run.sh handles its own log redirection/rotation.)

- [ ] **Step 2: Watch 2-3 cycles**

```bash
ssh whatbox 'sleep 660; tail -20 ~/logs/tvtime.log'
```
Expected: timestamped runs every 5 min, no tracebacks, idle runs are quiet.

- [ ] **Step 3: Mark spec implemented + final commit**

Update spec header `**Status:**` → `Implemented 2026-06-07`. `git add -A && git commit -m "docs: mark spec implemented" && git push`.

---

### Task 12 (OPTIONAL stretch): kill the browser step forever

Investigate how the Flutter web app mints the anonymous `flutter.jwtToken`: open `https://app.tvtime.com/welcome?mode=auth` with DevTools Network tab in an incognito window, filter XHR for the request returning a JWT (likely `auth.tvtime.com/v1/...` e.g. `/anonymous`, `/device`, or a `sidecar?o=` call). If reproducible with plain `requests`, add `TVTimeClient.bootstrap_anonymous()` + tests mirroring the captured shape, call it from `try_refresh()`'s failure path before giving up, commit, push, redeploy. If not reproducible in ~30 min, stop — the 60-day manual path is acceptable (YAGNI).

---

## Self-Review Notes

- **Spec coverage:** config/state/plex/tvtime/sync modules (Tasks 2-6) ✅; bootstrap + run.sh + rotation (Task 7) ✅; repo/license/credits (Tasks 1, 8) ✅; whatbox deploy layout + .env perms (Task 9) ✅; auth lifecycle incl. backoff (Tasks 5, 10) ✅; cron offsets + monitoring (Task 11) ✅; error-handling table → sync tests (Task 6) ✅; live verification (Task 10) ✅; stretch anonymous-JWT (Task 12) ✅; out-of-scope items have no tasks ✅.
- **Placeholders:** `<value from existing crontab>` / `<user-provided>` / `<JWT>` / `<ratingKey>` are deliberate secret/runtime placeholders, not plan gaps — sources are stated inline.
- **Type consistency:** `State(is_processed/mark_processed/seen_count/record_mark/save/first_run/watermark)`, `PlexClient(recent_history/metadata)`, `HistoryEntry(dedup_key/viewed_at/rating_key/title/type)`, `MediaItem(type/guids/label()/grandparent_title/season/episode)`, `TVTimeClient(in_backoff/set_backoff/clear_backoff/login_with_bootstrap/try_refresh/mark_episode/search_movie_uuid/mark_movie)` — verified consistent across Tasks 2-7 code and tests.
