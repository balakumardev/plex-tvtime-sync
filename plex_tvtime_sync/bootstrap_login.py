# plex_tvtime_sync/bootstrap_login.py
"""Authenticate with TV Time: mint an anonymous bootstrap JWT automatically and
exchange it (with credentials from config/.env) for account tokens.

Usage:
  python -m plex_tvtime_sync.bootstrap_login              # fully automatic
  python -m plex_tvtime_sync.bootstrap_login '<jwt>'      # manual override
                                                          # (browser localStorage 'flutter.jwtToken')
Tokens are written to config/tokens.json.
"""
import sys

from . import config as config_mod
from .tvtime_client import TVTimeClient


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv
    if len(argv) > 2:
        print("usage: python -m plex_tvtime_sync.bootstrap_login ['<bootstrap-jwt>']")
        return 2
    cfg = config_mod.load()
    client = TVTimeClient(cfg.config_dir, cfg.tvtime_user, cfg.tvtime_password)
    if len(argv) == 2:
        jwt = argv[1].strip().strip('"').strip("'")
    else:
        print("minting anonymous bootstrap JWT...")
        jwt = client.mint_bootstrap_jwt()
    client.login_with_bootstrap(jwt)
    print(f"OK: tokens written to {cfg.config_dir / 'tokens.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
