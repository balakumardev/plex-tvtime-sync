# plex_tvtime_sync/plex_client.py
"""Minimal Plex HTTP API client (XML). Only what the sync needs: recent history + metadata."""
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests


class PlexError(Exception):
    """Malformed/unexpected Plex response (e.g. non-XML body). Treated as transient
    by the orchestrator — same as network errors — since the common cause is a
    server hiccup, not a persistently corrupt item."""


class PlexNotFound(Exception):
    pass


OWNER_ACCOUNT_ID = 1  # Plex server owner is always account 1; this tool is owner-only.


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
    guids: dict[str, str]
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
        try:
            return ET.fromstring(r.text)
        except ET.ParseError as e:
            raise PlexError(f"non-XML response from {path}: {e}") from e

    def recent_history(self, account_id: int = OWNER_ACCOUNT_ID, limit: int = 200) -> list[HistoryEntry]:
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
