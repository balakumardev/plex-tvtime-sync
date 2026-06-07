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
