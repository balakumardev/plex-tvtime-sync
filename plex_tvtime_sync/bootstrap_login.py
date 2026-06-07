# plex_tvtime_sync/bootstrap_login.py
"""Rare manual step: exchange a bootstrap JWT for account tokens.

Get the bootstrap JWT from any browser (no login needed):
  1. Open https://app.tvtime.com/welcome?mode=auth
  2. DevTools console: localStorage.getItem('flutter.jwtToken')
  3. python -m plex_tvtime_sync.bootstrap_login '<token>'
Credentials come from config/.env; tokens are written to config/tokens.json.
"""
import sys

from . import config as config_mod
from .tvtime_client import TVTimeClient


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv
    if len(argv) != 2:
        print("usage: python -m plex_tvtime_sync.bootstrap_login '<bootstrap-jwt>'")
        return 2
    jwt = argv[1].strip().strip('"').strip("'")
    cfg = config_mod.load()
    client = TVTimeClient(cfg.config_dir, cfg.tvtime_user, cfg.tvtime_password)
    client.login_with_bootstrap(jwt)
    print(f"OK: tokens written to {cfg.config_dir / 'tokens.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
