# tests/test_bootstrap_login.py
from plex_tvtime_sync import bootstrap_login


def test_usage_error_with_extra_args(capsys):
    rc = bootstrap_login.main(["prog", "a", "b"])
    assert rc == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_no_arg_auto_mints(monkeypatch, tmp_path, capsys):
    calls = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def mint_bootstrap_jwt(self):
            calls["minted"] = True
            return "MINTED.JWT"

        def login_with_bootstrap(self, jwt):
            calls["jwt"] = jwt

    class FakeCfg:
        config_dir = tmp_path
        tvtime_user = "u"
        tvtime_password = "p"

    monkeypatch.setattr(bootstrap_login, "TVTimeClient", FakeClient)
    monkeypatch.setattr(bootstrap_login.config_mod, "load", lambda: FakeCfg())
    rc = bootstrap_login.main(["prog"])
    assert rc == 0
    assert calls == {"minted": True, "jwt": "MINTED.JWT"}


def test_strips_quotes_and_whitespace(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def login_with_bootstrap(self, jwt):
            captured["jwt"] = jwt

    class FakeCfg:
        config_dir = tmp_path
        tvtime_user = "u"
        tvtime_password = "p"

    monkeypatch.setattr(bootstrap_login, "TVTimeClient", FakeClient)
    monkeypatch.setattr(bootstrap_login.config_mod, "load", lambda: FakeCfg())
    rc = bootstrap_login.main(["prog", ' "BOOT.JWT" '])
    assert rc == 0
    assert captured["jwt"] == "BOOT.JWT"
