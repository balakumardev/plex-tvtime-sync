# plex_tvtime_sync/sync.py
"""Orchestration: poll Plex history since watermark → mark watched on TV Time. Cron entry point."""
import logging
import time

import requests

from . import config as config_mod
from .plex_client import OWNER_ACCOUNT_ID, PlexClient, PlexError, PlexNotFound
from .state import OVERLAP_SECONDS, State
from .tvtime_client import TVTimeAuthError, TVTimeClient, TVTimeError

MAX_ITEMS_PER_RUN = 50
CALL_SPACING_SECONDS = 1.0
MOVIE_GUID_ORDER = ("tvdb", "tmdb", "imdb")

# Per-entry outcomes from _process_entry.
MARKED = "marked"    # a TV Time write happened (counts toward the per-run cap)
SKIPPED = "skipped"  # permanent skip: marked processed, watermark advanced, no write
STOP = "stop"        # break the run: transient/auth/unexpected (backoff + mark_processed
                     # already applied by the helper where the policy requires it)

log = logging.getLogger("plex-tvtime-sync")


def _resolve_show_tvdb(plex, grandparent_rating_key, cache) -> str | None:
    """Resolve a show's tvdb id from its grandparent ratingKey, memoized per run so a
    binge of one show costs at most one extra metadata fetch. Returns None (and caches
    None) when the grandparent is missing or has no tvdb guid."""
    if grandparent_rating_key is None:
        return None
    if grandparent_rating_key in cache:
        return cache[grandparent_rating_key]
    try:
        show = plex.metadata(grandparent_rating_key)
        tvdb = show.guids.get("tvdb")
    except (PlexNotFound, PlexError, requests.RequestException) as e:
        log.warning("mark-previous: show id resolution failed for %s: %s", grandparent_rating_key, e)
        tvdb = None
    cache[grandparent_rating_key] = tvdb
    return tvdb


def _catch_up_previous(plex, tvtime, item, episode_tvdb, cache) -> None:
    """Bulk-mark every prior episode of the show up to this one (first-watch only).
    Swallows resolution + transient failures (the episode itself was already marked,
    so we never retry the whole item just for the catch-up). Re-raises only
    TVTimeAuthError, which the caller must treat as the standard auth break path."""
    show_tvdb = _resolve_show_tvdb(plex, item.grandparent_rating_key, cache)
    if not show_tvdb:
        return  # already logged in resolver; skip catch-up, item still counts as processed
    try:
        tvtime.mark_previous_episodes(show_tvdb, episode_tvdb)
        log.info("marked previous episodes of show tvdb %s up to episode %s", show_tvdb, episode_tvdb)
    except TVTimeAuthError:
        raise  # caller marks the (already-marked) episode processed, then backs off + breaks
    except (TVTimeError, requests.RequestException) as e:
        log.warning("mark-previous catch-up failed (non-fatal) for show tvdb %s: %s", show_tvdb, e)


def _process_entry(entry, plex, tvtime, state, mark_previous, show_tvdb_cache, sleep) -> str:
    """Resolve one entry (history OR scan) to a TV Time mark. Shared by both passes so
    they have identical metadata fetch, GUID handling, rewatch flagging, catch-up and
    error ladder. Returns MARKED / SKIPPED / STOP; performs its own mark_processed for
    MARKED and SKIPPED (and, for the auth-during-catch-up case, before STOP)."""
    try:
        item = plex.metadata(entry.rating_key)
    except PlexNotFound:
        log.info("skip (deleted from plex): %s", entry.title)
        state.mark_processed(entry.dedup_key, entry.viewed_at)
        return SKIPPED
    except Exception as e:
        log.error("plex metadata error for %s: %s - retrying next run", entry.rating_key, e)
        return STOP

    try:
        if item.type == "episode":
            tvdb_id = item.guids.get("tvdb")
            if not tvdb_id:
                log.warning("skip (no tvdb guid): %s", item.label())
                state.mark_processed(entry.dedup_key, entry.viewed_at)
                return SKIPPED
            rewatch = state.seen_count("episodes", tvdb_id) > 0
            tvtime.mark_episode(tvdb_id, rewatch=rewatch)
            state.record_mark("episodes", tvdb_id)
            log.info("marked episode%s: %s (tvdb %s)", " [rewatch]" if rewatch else "", item.label(), tvdb_id)
            if mark_previous and not rewatch:
                # primary mark succeeded -> catch up prior episodes (first watch only).
                # The episode is already marked, so on auth failure we must record it
                # processed BEFORE the standard backoff break, never re-mark it.
                try:
                    _catch_up_previous(plex, tvtime, item, tvdb_id, show_tvdb_cache)
                except TVTimeAuthError:
                    state.mark_processed(entry.dedup_key, entry.viewed_at)
                    raise
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
                return SKIPPED
            tvtime.mark_movie(uuid)
            state.record_mark("movies", uuid)  # reserved for future movie-rewatch support (TVTime movie mark has no rewatch flag)
            log.info("marked movie: %s (uuid %s)", item.label(), uuid)
        else:
            state.mark_processed(entry.dedup_key, entry.viewed_at)
            return SKIPPED
        state.mark_processed(entry.dedup_key, entry.viewed_at)
        sleep(CALL_SPACING_SECONDS)
        return MARKED
    except TVTimeAuthError as e:
        log.critical("TVTIME AUTH EXPIRED - run bootstrap_login again. %s", e)
        tvtime.set_backoff()
        return STOP
    except (TVTimeError, requests.RequestException) as e:
        log.error("tvtime transient error on %s: %s - retrying next run", item.label(), e)
        return STOP
    except Exception:
        log.exception("unexpected error on %s - stopping run", entry.rating_key)
        return STOP


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
        log.error("plex history fetch failed: %s", e)
        return 0

    # sections() is fetched at most once per run and shared by the excluded-library
    # resolution and the lastViewedAt scan pass. Memoized in a single-slot cache.
    _sections_cache: dict = {}

    def get_sections() -> dict:
        if "v" not in _sections_cache:
            _sections_cache["v"] = plex.sections()
        return _sections_cache["v"]

    excluded_ids: set[str] = set()
    if cfg is not None and cfg.excluded_libraries:
        # Fail closed: if we cannot resolve excluded sections, never risk syncing a
        # private library. Bail out entirely with the watermark untouched.
        try:
            sections = get_sections()
        except Exception as e:
            log.error("excluded-library resolution failed (%s) - skipping run to stay fail-closed", e)
            return 0
        # Match titles case-insensitively (and trimmed) so a "private" vs "Private" slip
        # never silently fails open and leaks a section. Every configured name that
        # resolves to no section is almost certainly a typo: warn loudly (the guardrail)
        # but stay fail-open — fail-closed already covers an actual sections() failure.
        by_lower = {title.lower(): meta["key"] for title, meta in sections.items()}
        for title in cfg.excluded_libraries:
            key = by_lower.get(title.strip().lower())
            if key is None:
                log.warning("excluded library %r does not match any Plex library - check spelling", title)
            else:
                excluded_ids.add(key)

    cutoff = state.watermark - OVERLAP_SECONDS
    todo = sorted(
        (
            e
            for e in entries
            if e.viewed_at >= cutoff
            and e.account_id == OWNER_ACCOUNT_ID
            and e.library_section_id not in excluded_ids
            and not state.is_processed(e.dedup_key)
        ),
        key=lambda e: e.viewed_at,
    )[:MAX_ITEMS_PER_RUN]

    mark_previous = bool(cfg and cfg.mark_previous_episodes)
    show_tvdb_cache: dict[str, str | None] = {}  # grandparent_rating_key -> show tvdb (per run)

    # ---- Pass 1: playback history ----
    marked_count = 0
    history_stopped = False
    for entry in todo:
        outcome = _process_entry(entry, plex, tvtime, state, mark_previous, show_tvdb_cache, sleep)
        if outcome == STOP:
            history_stopped = True
            break
        if outcome == MARKED:
            marked_count += 1

    # ---- Pass 2: lastViewedAt scan (manual mark-as-watched) ----
    # Runs only when the history pass completed without a transient/auth break (a broken
    # history pass means TV Time/Plex is unhealthy, so don't pile a second pass on top).
    # Gated on cfg presence: production always passes a real Config, while the pure
    # dependency-injection path (cfg=None) stays legacy and never touches sections().
    if cfg is not None and not history_stopped:
        _scan_pass(plex, tvtime, state, cutoff, excluded_ids, get_sections,
                   mark_previous, show_tvdb_cache, sleep, marked_count)

    state.save()
    return 0


# episode sections scan as type=4, movies as type=1; other library types are skipped.
_SCAN_TYPE = {"show": 4, "movie": 1}


def _same_recent_view(state, entry) -> bool:
    """A scan hit within OVERLAP_SECONDS of an already-processed view of the same
    item is the same playback seen through a second lens: Plex writes the item's
    lastViewedAt and the history row's viewedAt in separate operations, so the
    two timestamps can differ by a second or two. Without this check the scan
    would re-mark the playback as a phantom rewatch. A genuine rewatch of the
    same item cannot complete within the overlap window."""
    prefix = f"{entry.rating_key}:"
    for key, ts in state.processed.items():
        if key.startswith(prefix) and abs(ts - entry.viewed_at) <= OVERLAP_SECONDS:
            return True
    return False


def _scan_pass(plex, tvtime, state, cutoff, excluded_ids, get_sections,
               mark_previous, show_tvdb_cache, sleep, marked_count) -> None:
    """Second detection pass: items whose lastViewedAt was bumped by a manual mark-watched
    (no playback session, so no history entry). Shares the per-entry pipeline, cutoff,
    dedup map and per-run cap with the history pass."""
    try:
        sections = get_sections()
    except Exception as e:
        log.error("scan: sections fetch failed (%s) - skipping scan pass", e)
        return
    for title, meta in sections.items():
        if marked_count >= MAX_ITEMS_PER_RUN:
            break
        sect_key = meta["key"]
        if sect_key in excluded_ids:
            continue
        plex_type = _SCAN_TYPE.get(meta.get("type"))
        if plex_type is None:
            continue  # not a show/movie library
        try:
            viewed = plex.recently_viewed(sect_key, plex_type)
        except Exception as e:
            log.error("scan: section %s (%s) fetch failed (%s) - skipping section", title, sect_key, e)
            continue
        # A normal playback usually sets lastViewedAt == the history viewedAt, so the
        # scan sees that ratingKey:lastViewedAt as already processed and skips it. That
        # equality is the common case; the epsilon check below (_same_recent_view) covers
        # the skew where Plex writes lastViewedAt a second or two off from the history row.
        candidates = []
        for e in viewed:
            if e.viewed_at < cutoff or state.is_processed(e.dedup_key):
                continue
            if _same_recent_view(state, e):
                # Same playback as an already-marked view, just timestamp-skewed. Record
                # the skewed key so future runs dedup it cheaply, then skip with no TV
                # Time call and no ledger write — it is not a rewatch.
                state.mark_processed(e.dedup_key, e.viewed_at)
                continue
            candidates.append(e)
        candidates.sort(key=lambda e: e.viewed_at)
        for entry in candidates:
            if marked_count >= MAX_ITEMS_PER_RUN:
                return
            outcome = _process_entry(entry, plex, tvtime, state, mark_previous, show_tvdb_cache, sleep)
            if outcome == STOP:
                return  # auth → backoff already set; transient → stop the scan too
            if outcome == MARKED:
                marked_count += 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
