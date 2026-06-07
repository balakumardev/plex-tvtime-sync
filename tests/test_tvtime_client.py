# tests/test_tvtime_client.py
import json

import pytest
import responses

from plex_tvtime_sync.tvtime_client import (
    EPISODE_SIDECAR,
    LOGIN_SIDECAR,
    MOVIE_SIDECAR,
    REFRESH_SIDECAR,
    SEARCH_SIDECAR,
    TVTimeAuthError,
    TVTimeClient,
    TVTimeError,
)


def make_client(tmp_path, tokens=None):
    if tokens is not None:
        (tmp_path / "tokens.json").write_text(json.dumps(tokens))
    return TVTimeClient(tmp_path, "u@example.com", "pw")


@responses.activate
def test_login_with_bootstrap_saves_tokens(tmp_path):
    responses.post(
        LOGIN_SIDECAR,
        json={"data": {"jwt_token": "JWT1", "jwt_refresh_token": "RT1"}},
    )
    c = make_client(tmp_path)
    c.login_with_bootstrap("BOOT")
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer BOOT"
    assert json.loads(req.body) == {"username": "u@example.com", "password": "pw"}
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved == {"jwt_token": "JWT1", "jwt_refresh_token": "RT1"}
    assert oct((tmp_path / "tokens.json").stat().st_mode)[-3:] == "600"


@responses.activate
def test_mark_episode_request_shape(tmp_path):
    url = EPISODE_SIDECAR.format(eid="349232", rw=0)
    responses.post(url, json={"result": "OK"})
    c = make_client(tmp_path, {"jwt_token": "JWT1", "jwt_refresh_token": "RT1"})
    c.mark_episode("349232", rewatch=False)
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer JWT1"
    assert req.headers["Host"] == "app.tvtime.com:80"


@responses.activate
def test_mark_episode_rewatch_flag(tmp_path):
    url = EPISODE_SIDECAR.format(eid="349232", rw=1)
    responses.post(url, json={"result": "OK"})
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    c.mark_episode("349232", rewatch=True)  # would 404 ConnectionError if URL wrong


@responses.activate
def test_401_triggers_refresh_then_retry(tmp_path):
    url = EPISODE_SIDECAR.format(eid="1", rw=0)
    responses.post(url, status=401)
    responses.post(REFRESH_SIDECAR, json={"data": {"jwt_token": "JWT2"}})
    responses.post(url, json={"result": "OK"})
    c = make_client(tmp_path, {"jwt_token": "OLD", "jwt_refresh_token": "RT1"})
    c.mark_episode("1")
    assert json.loads((tmp_path / "tokens.json").read_text())["jwt_token"] == "JWT2"


@responses.activate
def test_401_with_failed_refresh_raises_auth_error(tmp_path):
    responses.post(EPISODE_SIDECAR.format(eid="1", rw=0), status=401)
    responses.post(REFRESH_SIDECAR, status=400)
    c = make_client(tmp_path, {"jwt_token": "OLD", "jwt_refresh_token": "RT1"})
    with pytest.raises(TVTimeAuthError):
        c.mark_episode("1")


def test_no_tokens_raises_auth_error(tmp_path):
    c = make_client(tmp_path)
    with pytest.raises(TVTimeAuthError):
        c.mark_episode("1")


@responses.activate
def test_search_movie_uuid_picks_movie_type(tmp_path):
    responses.get(
        SEARCH_SIDECAR.format(q="603"),
        json={"data": [{"type": "series", "uuid": "S-1"}, {"type": "movie", "uuid": "M-1"}]},
    )
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    assert c.search_movie_uuid("603") == "M-1"


@responses.activate
def test_search_movie_uuid_none_when_no_movie(tmp_path):
    responses.get(SEARCH_SIDECAR.format(q="603"), json={"data": []})
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    assert c.search_movie_uuid("603") is None


@responses.activate
def test_mark_movie_posts_to_tracking(tmp_path):
    responses.post(MOVIE_SIDECAR.format(uuid="M-1"), json={"ok": True})
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    c.mark_movie("M-1")


def test_backoff_marker_roundtrip(tmp_path):
    c = make_client(tmp_path)
    assert c.in_backoff() is False
    c.set_backoff()
    assert c.in_backoff() is True
    c.clear_backoff()
    assert c.in_backoff() is False


@responses.activate
def test_mark_calls_send_creds_body(tmp_path):
    responses.post(EPISODE_SIDECAR.format(eid="7", rw=0), json={"result": "OK"})
    responses.post(MOVIE_SIDECAR.format(uuid="M-9"), json={"ok": True})
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    c.mark_episode("7")
    c.mark_movie("M-9")
    for call in responses.calls:
        assert json.loads(call.request.body) == {"username": "u@example.com", "password": "pw"}


@responses.activate
def test_refresh_request_shape(tmp_path):
    responses.post(EPISODE_SIDECAR.format(eid="1", rw=0), status=401)
    responses.post(REFRESH_SIDECAR, json={"data": {"jwt_token": "JWT2"}})
    responses.post(EPISODE_SIDECAR.format(eid="1", rw=0), json={"result": "OK"})
    c = make_client(tmp_path, {"jwt_token": "OLD", "jwt_refresh_token": "RT1"})
    c.mark_episode("1")
    refresh_call = responses.calls[1].request
    assert json.loads(refresh_call.body) == {"refresh_token": "RT1"}
    assert refresh_call.headers["Authorization"] == "Bearer RT1"


@responses.activate
def test_login_malformed_200_raises_auth_error(tmp_path):
    responses.post(LOGIN_SIDECAR, json={"data": {}})
    c = make_client(tmp_path)
    with pytest.raises(TVTimeAuthError):
        c.login_with_bootstrap("BOOT")


@responses.activate
def test_search_non_json_raises_tvtime_error(tmp_path):
    responses.get(SEARCH_SIDECAR.format(q="603"), body="<html>oops</html>")
    c = make_client(tmp_path, {"jwt_token": "JWT1"})
    with pytest.raises(TVTimeError):
        c.search_movie_uuid("603")
