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
