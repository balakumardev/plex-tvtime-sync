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
