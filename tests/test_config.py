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


BASE_ENV = (
    "PLEX_URL=http://plex.example:32400\n"
    "PLEX_TOKEN=ptok\nTVTIME_USER=u@example.com\nTVTIME_PASSWORD=secret\n"
)


def test_excluded_libraries_defaults_empty(tmp_path, monkeypatch):
    for k in ("EXCLUDED_LIBRARIES", "MARK_PREVIOUS_EPISODES"):
        monkeypatch.delenv(k, raising=False)
    cfg = config_mod.load(write_env(tmp_path, BASE_ENV))
    assert cfg.excluded_libraries == []
    assert cfg.mark_previous_episodes is False


def test_excluded_libraries_parsed_stripped_and_emptied(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCLUDED_LIBRARIES", raising=False)
    cfg = config_mod.load(
        write_env(tmp_path, BASE_ENV + "EXCLUDED_LIBRARIES= Adult ,  Home Videos ,,\n")
    )
    assert cfg.excluded_libraries == ["Adult", "Home Videos"]


def test_mark_previous_episodes_truthy_values(tmp_path, monkeypatch):
    for raw in ("1", "true", "TRUE", "Yes", "yes"):
        monkeypatch.delenv("MARK_PREVIOUS_EPISODES", raising=False)
        cfg = config_mod.load(
            write_env(tmp_path, BASE_ENV + f"MARK_PREVIOUS_EPISODES={raw}\n")
        )
        assert cfg.mark_previous_episodes is True, raw


def test_mark_previous_episodes_falsey_values(tmp_path, monkeypatch):
    for raw in ("0", "false", "no", "off", ""):
        monkeypatch.delenv("MARK_PREVIOUS_EPISODES", raising=False)
        cfg = config_mod.load(
            write_env(tmp_path, BASE_ENV + f"MARK_PREVIOUS_EPISODES={raw}\n")
        )
        assert cfg.mark_previous_episodes is False, raw
