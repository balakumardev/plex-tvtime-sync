# tests/test_config.py
import pytest
from plex_tvtime_sync import config as config_mod


def write_env(tmp_path, body):
    (tmp_path / ".env").write_text(body)
    return tmp_path


def test_loads_all_keys(tmp_path, monkeypatch):
    for k in config_mod.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    cdir = write_env(
        tmp_path,
        "PLEX_URL=http://plex.example:32400/\n"
        "PLEX_TOKEN=ptok\nTVTIME_USER=u@example.com\nTVTIME_PASSWORD=secret\n",
    )
    cfg = config_mod.load(cdir)
    assert cfg.plex_url == "http://plex.example:32400"  # trailing slash stripped
    assert cfg.plex_token == "ptok"
    assert cfg.tvtime_user == "u@example.com"
    assert cfg.tvtime_password == "secret"
    assert cfg.config_dir == cdir


def test_missing_key_exits_with_clear_message(tmp_path, monkeypatch):
    for k in config_mod.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    cdir = write_env(tmp_path, "PLEX_URL=http://plex.example:32400\n")
    with pytest.raises(SystemExit) as exc:
        config_mod.load(cdir)
    msg = str(exc.value)
    assert "PLEX_TOKEN" in msg and "TVTIME_USER" in msg and "TVTIME_PASSWORD" in msg
