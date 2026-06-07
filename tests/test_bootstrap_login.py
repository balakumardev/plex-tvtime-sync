# tests/test_bootstrap_login.py
from plex_tvtime_sync import bootstrap_login


def test_usage_error_without_arg(capsys):
    rc = bootstrap_login.main(["prog"])
    assert rc == 2
    assert "usage" in capsys.readouterr().out.lower()


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
