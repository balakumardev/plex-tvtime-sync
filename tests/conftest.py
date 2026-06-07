# tests/conftest.py
import pytest

from plex_tvtime_sync.config import REQUIRED


@pytest.fixture(autouse=True)
def _clean_required_env(monkeypatch):
    """config.load() populates os.environ; keep tests isolated from each other."""
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
