# plex_tvtime_sync/state.py
"""Persisted sync state: watermark + dedup overlap set (state.json), rewatch ledger (ledger.json)."""
import json
import logging
import os
import time
from pathlib import Path

OVERLAP_SECONDS = 300  # spec: fixed 5-minute overlap window

log = logging.getLogger(__name__)


def _load_json(path: Path, default):
    """Read JSON, recovering from missing or corrupt files (truncated write, disk issues)."""
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("corrupt %s (%s) - resetting", path.name, e)
        return default


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


class State:
    def __init__(self, config_dir: Path):
        self.path = Path(config_dir) / "state.json"
        self.ledger_path = Path(config_dir) / "ledger.json"
        data = _load_json(self.path, {})
        self.first_run = "watermark" not in data
        self.watermark: int = data.get("watermark", int(time.time()))
        self.processed: dict[str, int] = data.get("processed", {})
        self.ledger = _load_json(self.ledger_path, {})
        self.ledger.setdefault("episodes", {})
        self.ledger.setdefault("movies", {})

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
        _atomic_write(self.path, {"watermark": self.watermark, "processed": self.processed})
        _atomic_write(self.ledger_path, self.ledger)
