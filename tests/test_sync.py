# tests/test_sync.py
import json

import pytest

from plex_tvtime_sync import sync as sync_mod
from plex_tvtime_sync.plex_client import HistoryEntry, MediaItem, PlexNotFound
from plex_tvtime_sync.state import State
from plex_tvtime_sync.tvtime_client import TVTimeAuthError, TVTimeError


class FakePlex:
    def __init__(self, history=None, meta=None, history_error=None,
                 sections=None, sections_error=None, viewed=None):
        self.history = history or []
        self.meta = meta or {}
        self.history_error = history_error
        self._sections = sections if sections is not None else {}
        self.sections_error = sections_error
        self._viewed = viewed or {}  # {section_id: [HistoryEntry, ...]}
        self.sections_calls = 0
        self.viewed_calls = []
        self.metadata_calls = []

    def recent_history(self):
        if self.history_error:
            raise self.history_error
        return self.history

    def metadata(self, rating_key):
        self.metadata_calls.append(rating_key)
        if rating_key not in self.meta:
            raise PlexNotFound(rating_key)
        return self.meta[rating_key]

    def sections(self):
        self.sections_calls += 1
        if self.sections_error:
            raise self.sections_error
        return self._sections

    def recently_viewed(self, section_id, plex_type, limit=100):
        self.viewed_calls.append((section_id, plex_type))
        return self._viewed.get(section_id, [])


class FakeTVTime:
    def __init__(self, fail_with=None, search_result="M-UUID", prev_fail_with=None):
        self.episodes, self.movies, self.searches = [], [], []
        self.previous = []  # (show_tvdb, episode_tvdb) from mark_previous_episodes
        self.fail_with = fail_with
        self.prev_fail_with = prev_fail_with
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

    def mark_previous_episodes(self, show_tvdb, episode_tvdb):
        if self.prev_fail_with:
            raise self.prev_fail_with
        self.previous.append((show_tvdb, episode_tvdb))

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


def ep_entry(rk="101", viewed=2000, section_id=None):
    return HistoryEntry(rating_key=rk, viewed_at=viewed, account_id=1, title="Pilot",
                        type="episode", library_section_id=section_id)


def ep_item(tvdb="349232", grandparent_rating_key="55"):
    guids = {"tvdb": tvdb} if tvdb else {}
    return MediaItem(type="episode", guids=guids, title="Pilot", grandparent_title="Show",
                     season=1, episode=3, grandparent_rating_key=grandparent_rating_key)


def show_item(tvdb="349"):
    """The grandparent (show) MediaItem that resolves an episode's show-level tvdb id."""
    guids = {"tvdb": tvdb} if tvdb else {}
    return MediaItem(type="show", guids=guids, title="Show")


class FakeCfg:
    """Stand-in for config.Config; only the fields sync.run reads from cfg."""
    def __init__(self, excluded_libraries=None, mark_previous_episodes=False):
        self.excluded_libraries = excluded_libraries or []
        self.mark_previous_episodes = mark_previous_episodes


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


def test_movie_search_no_result_permanent_skip(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime(search_result=None)
    movie = MediaItem(type="movie", guids={"tvdb": "77"}, title="Obscure Film")
    entry = HistoryEntry(rating_key="303", viewed_at=2200, account_id=1, title="Obscure Film", type="movie")
    sync_mod.run(plex=FakePlex(history=[entry], meta={"303": movie}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.movies == []
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 2200


def test_plex_error_on_metadata_is_transient(tmp_path):
    from plex_tvtime_sync.plex_client import PlexError

    class ErroringPlex(FakePlex):
        def metadata(self, rating_key):
            raise PlexError("non-XML response")

    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    sync_mod.run(plex=ErroringPlex(history=[ep_entry()]), tvtime=tvtime, state=state,
                 sleep=lambda s: None)
    assert tvtime.episodes == []
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 1000


def test_mixed_batch_persists_prefix_on_transient_failure(tmp_path):
    state = seeded_state(tmp_path)

    class FlakyTVTime(FakeTVTime):
        def mark_episode(self, eid, rewatch=False):
            if eid == "9001":
                raise TVTimeError("503")
            super().mark_episode(eid, rewatch)

    history = [ep_entry(rk=str(100 + i), viewed=2000 + i) for i in range(3)]
    meta = {str(100 + i): ep_item(tvdb=str(9000 + i)) for i in range(3)}
    tvtime = FlakyTVTime()
    sync_mod.run(plex=FakePlex(history=history, meta=meta), tvtime=tvtime, state=state,
                 sleep=lambda s: None)
    assert [e[0] for e in tvtime.episodes] == ["9000"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["watermark"] == 2000  # advanced past item 1 only
    assert "100:2000" in saved["processed"]
    assert "101:2001" not in saved["processed"]


def test_auth_error_on_movie_search_sets_backoff(tmp_path):
    state = seeded_state(tmp_path)

    class AuthFailSearch(FakeTVTime):
        def search_movie_uuid(self, q):
            raise TVTimeAuthError("401 on movie search")

    movie = MediaItem(type="movie", guids={"tvdb": "77"}, title="M")
    entry = HistoryEntry(rating_key="404", viewed_at=2300, account_id=1, title="M", type="movie")
    tvtime = AuthFailSearch()
    sync_mod.run(plex=FakePlex(history=[entry], meta={"404": movie}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.backoff_set is True
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 1000


def test_unexpected_error_logged_and_stops_cleanly(tmp_path):
    state = seeded_state(tmp_path)

    class ExplodingTVTime(FakeTVTime):
        def mark_episode(self, eid, rewatch=False):
            raise TypeError("boom")

    tvtime = ExplodingTVTime()
    rc = sync_mod.run(plex=FakePlex(history=[ep_entry()], meta={"101": ep_item()}),
                      tvtime=tvtime, state=state, sleep=lambda s: None)
    assert rc == 0  # crash contained, run ends cleanly
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 1000


def test_other_account_history_ignored(tmp_path):
    state = seeded_state(tmp_path)
    other = HistoryEntry(rating_key="500", viewed_at=2400, account_id=7, title="Not mine", type="episode")
    tvtime = FakeTVTime()
    sync_mod.run(plex=FakePlex(history=[other], meta={"500": ep_item()}), tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == []


# ---------------------------------------------------------------------------
# Feature 1: EXCLUDED_LIBRARIES
# ---------------------------------------------------------------------------

def test_cfg_none_never_calls_sections(tmp_path):
    """Legacy path (cfg=None) must not resolve sections at all."""
    class NoSectionsPlex(FakePlex):
        def sections(self):
            raise AssertionError("sections() must not be called when cfg is None")

    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = NoSectionsPlex(history=[ep_entry()], meta={"101": ep_item()})
    sync_mod.run(plex=plex, tvtime=tvtime, state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]


def test_no_exclusions_configured_skips_sections(tmp_path):
    """Empty excluded_libraries → no sections() call (history pass only)."""
    class NoSectionsPlex(FakePlex):
        def sections(self):
            raise AssertionError("sections() must not be called with empty exclusions")

    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = NoSectionsPlex(history=[ep_entry()], meta={"101": ep_item()})
    sync_mod.run(cfg=FakeCfg(excluded_libraries=[]), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]


def test_excluded_library_invisible_to_history_pass(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    # entry in section 9 ("Adult") must be silently skipped; entry in section 2 synced
    private = ep_entry(rk="700", viewed=2050, section_id="9")
    public = ep_entry(rk="101", viewed=2000, section_id="2")
    sections = {"Adult": {"key": "9", "type": "movie"}, "TV Shows": {"key": "2", "type": "show"}}
    plex = FakePlex(history=[private, public], meta={"101": ep_item(), "700": ep_item(tvdb="999")},
                    sections=sections)
    sync_mod.run(cfg=FakeCfg(excluded_libraries=["Adult"]), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]  # only the public one
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "700:2050" not in saved["processed"]  # excluded item never marked processed


def test_sections_error_with_exclusions_fails_closed(tmp_path):
    """If sections() raises while exclusions are configured: return 0, watermark frozen."""
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = FakePlex(history=[ep_entry(section_id="2")], meta={"101": ep_item()},
                    sections_error=RuntimeError("plex sections down"))
    rc = sync_mod.run(cfg=FakeCfg(excluded_libraries=["Adult"]), plex=plex, tvtime=tvtime,
                      state=state, sleep=lambda s: None)
    assert rc == 0
    assert tvtime.episodes == []  # nothing synced
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 1000  # frozen


def test_excluded_name_not_in_sections_is_ignored(tmp_path):
    """An excluded library name that doesn't match any section excludes nothing."""
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    sections = {"TV Shows": {"key": "2", "type": "show"}}
    plex = FakePlex(history=[ep_entry(section_id="2")], meta={"101": ep_item()}, sections=sections)
    sync_mod.run(cfg=FakeCfg(excluded_libraries=["Nonexistent"]), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]


# ---------------------------------------------------------------------------
# Feature 2: MARK_PREVIOUS_EPISODES
# ---------------------------------------------------------------------------

def test_mark_previous_fires_on_first_watch(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item(), "55": show_item("349")})
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=True), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]
    assert tvtime.previous == [("349", "349232")]  # show tvdb + episode tvdb
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "101:2000" in saved["processed"]  # episode still marked processed


def test_mark_previous_not_on_rewatch(tmp_path):
    state = seeded_state(tmp_path)
    state.record_mark("episodes", "349232")  # already seen → rewatch
    tvtime = FakeTVTime()
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item(), "55": show_item("349")})
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=True), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", True)]
    assert tvtime.previous == []  # rewatch must not trigger catch-up


def test_mark_previous_off_by_default(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item(), "55": show_item("349")})
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=False), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.previous == []
    assert plex.metadata_calls == ["101"]  # no grandparent fetch when feature off


def test_mark_previous_cfg_none_does_nothing(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item(), "55": show_item("349")})
    sync_mod.run(plex=plex, tvtime=tvtime, state=state, sleep=lambda s: None)
    assert tvtime.previous == []


def test_mark_previous_show_resolution_failure_still_processes(tmp_path):
    """Grandparent missing from Plex → catch-up skipped, episode still counts as processed."""
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    # no "55" in meta → metadata(grandparent) raises PlexNotFound
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item()})
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=True), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]
    assert tvtime.previous == []
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["watermark"] == 2000  # episode marked, advanced


def test_mark_previous_no_show_tvdb_guid_still_processes(tmp_path):
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item(), "55": show_item(tvdb=None)})
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=True), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.previous == []
    assert json.loads((tmp_path / "state.json").read_text())["watermark"] == 2000


def test_mark_previous_transient_failure_still_advances_item(tmp_path):
    """mark_previous raises TVTimeError → warning only; item still processed, no break."""
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime(prev_fail_with=TVTimeError("503 on catch-up"))
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item(), "55": show_item("349")})
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=True), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]
    assert tvtime.backoff_set is False
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["watermark"] == 2000
    assert "101:2000" in saved["processed"]


def test_mark_previous_auth_error_marks_processed_then_backs_off(tmp_path):
    """mark_previous raises TVTimeAuthError → episode already marked, so mark_processed
    BEFORE breaking; backoff set."""
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime(prev_fail_with=TVTimeAuthError("401 on catch-up"))
    plex = FakePlex(history=[ep_entry()], meta={"101": ep_item(), "55": show_item("349")})
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=True), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    assert tvtime.episodes == [("349232", False)]  # primary mark happened
    assert tvtime.backoff_set is True
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "101:2000" in saved["processed"]  # marked processed before break
    assert saved["watermark"] == 2000


def test_mark_previous_caches_show_resolution_across_binge(tmp_path):
    """A binge of one show costs exactly one extra grandparent metadata fetch."""
    state = seeded_state(tmp_path)
    tvtime = FakeTVTime()
    # 3 episodes of the same show (same grandparent 55), each distinct episode tvdb
    history = [ep_entry(rk=str(100 + i), viewed=2000 + i) for i in range(3)]
    meta = {str(100 + i): ep_item(tvdb=str(900 + i), grandparent_rating_key="55") for i in range(3)}
    meta["55"] = show_item("349")
    plex = FakePlex(history=history, meta=meta)
    sync_mod.run(cfg=FakeCfg(mark_previous_episodes=True), plex=plex, tvtime=tvtime,
                 state=state, sleep=lambda s: None)
    # grandparent "55" fetched exactly once despite 3 episodes
    assert plex.metadata_calls.count("55") == 1
    assert len(tvtime.previous) == 3  # catch-up fired for each first-watch episode
