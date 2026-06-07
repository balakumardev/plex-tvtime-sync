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
    # resolution and the lastViewedAt scan pass. _sections is None until first fetched.
    sections: dict | None = None
    excluded_ids: set[str] = set()
    if cfg is not None and cfg.excluded_libraries:
        # Fail closed: if we cannot resolve excluded sections, never risk syncing a
        # private library. Bail out entirely with the watermark untouched.
        try:
            sections = plex.sections()
        except Exception as e:
            log.error("excluded-library resolution failed (%s) - skipping run to stay fail-closed", e)
            return 0
        excluded_ids = {
            sections[title]["key"] for title in cfg.excluded_libraries if title in sections
        }

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
        except Exception:
            log.exception("unexpected error on %s - stopping run", entry.rating_key)
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
