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
