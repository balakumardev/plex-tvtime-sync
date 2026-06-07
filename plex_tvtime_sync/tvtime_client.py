# plex_tvtime_sync/tvtime_client.py
"""TV Time unofficial API client (JWT bearer via TV Time's sidecar CORS proxy).

Endpoint knowledge credit: Zggis/plex-tvtime (reference only) and TheIndra55/tvtime-api.
"""
import json
import os
import time
from pathlib import Path

import requests

APP = "https://app.tvtime.com"
BETA = "https://beta-app.tvtime.com"
LOGIN_SIDECAR = f"{BETA}/sidecar?o=https://auth.tvtime.com/v1/login"
REFRESH_SIDECAR = f"{BETA}/sidecar?o=https://auth.tvtime.com/v1/refresh"
EPISODE_SIDECAR = (
    APP + "/sidecar?o=https://api2.tozelabs.com/v2/watched_episodes/episode/{eid}&is_rewatch={rw}"
)
MARK_PREVIOUS_SIDECAR = (
    APP + "/sidecar?o=https://api2.tozelabs.com/v2/watched_episodes/show/{show_id}/until/episode/{eid}"
)
SEARCH_SIDECAR = APP + "/sidecar?o=https://search.tvtime.com/v1/search/series,movie&q={q}&offset=0&limit=5"
MOVIE_SIDECAR = APP + "/sidecar?o=https://msapi.tvtime.com/prod/v1/tracking/{uuid}/watch"
MINT_SIDECAR = (
    APP
    + "/sidecar?o_b64=aHR0cHM6Ly9hcGkyLnRvemVsYWJzLmNvbS92Mi91c2Vy"
    + "&lang=en&country_code=us&source=web&version=2025082201"
)  # o_b64 decodes to https://api2.tozelabs.com/v2/user - mints an anonymous account + bootstrap JWT
BACKOFF_SECONDS = 3600


class TVTimeAuthError(Exception):
    """Tokens missing/expired and unrecoverable without a new bootstrap login."""


class TVTimeError(Exception):
    """Non-auth TV Time failure (treat as transient)."""


class TVTimeClient:
    def __init__(self, config_dir: Path, username: str, password: str, timeout: int = 30):
        config_dir = Path(config_dir)
        self.tokens_path = config_dir / "tokens.json"
        self.backoff_path = config_dir / "auth_backoff"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.tokens = (
            json.loads(self.tokens_path.read_text()) if self.tokens_path.exists() else {}
        )

    # ---- backoff marker (spec: 1h suppression after unrecoverable auth failure) ----
    def in_backoff(self) -> bool:
        try:
            return time.time() - self.backoff_path.stat().st_mtime < BACKOFF_SECONDS
        except FileNotFoundError:
            return False

    def set_backoff(self) -> None:
        self.backoff_path.touch()

    def clear_backoff(self) -> None:
        self.backoff_path.unlink(missing_ok=True)

    # ---- auth ----
    def _save_tokens(self) -> None:
        tmp = self.tokens_path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self.tokens))
        os.replace(tmp, self.tokens_path)

    def login_with_bootstrap(self, bootstrap_jwt: str) -> None:
        r = requests.post(
            LOGIN_SIDECAR,
            json={"username": self.username, "password": self.password},
            headers={"Authorization": f"Bearer {bootstrap_jwt}"},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise TVTimeAuthError(f"login failed: HTTP {r.status_code} {r.text[:300]}")
        try:
            data = (r.json() or {}).get("data") or {}
        except ValueError as e:
            raise TVTimeAuthError(f"login returned non-JSON: {e}") from e
        if "jwt_token" not in data:
            raise TVTimeAuthError("login response missing jwt_token")
        self.tokens = {
            "jwt_token": data["jwt_token"],
            "jwt_refresh_token": data.get("jwt_refresh_token"),
        }
        self._save_tokens()
        self.clear_backoff()

    def try_refresh(self) -> None:
        """Experimental: the refresh endpoint is unverified. Failure → TVTimeAuthError."""
        rt = self.tokens.get("jwt_refresh_token")
        if not rt:
            raise TVTimeAuthError("no refresh token stored")
        try:
            r = requests.post(
                REFRESH_SIDECAR,
                json={"refresh_token": rt},
                headers={"Authorization": f"Bearer {rt}"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise TVTimeAuthError(f"refresh request failed: {e}") from e
        if r.status_code != 200:
            raise TVTimeAuthError(f"refresh rejected: HTTP {r.status_code}")
        try:
            data = r.json().get("data") or {}
        except ValueError as e:
            raise TVTimeAuthError(f"refresh returned non-JSON: {e}") from e
        if "jwt_token" not in data:
            raise TVTimeAuthError("refresh response missing jwt_token")
        self.tokens["jwt_token"] = data["jwt_token"]
        self.tokens["jwt_refresh_token"] = data.get("jwt_refresh_token", rt)
        self._save_tokens()

    def mint_bootstrap_jwt(self) -> str:
        """Mint an anonymous bootstrap JWT (no auth, no body). Replaces the manual browser step."""
        try:
            r = requests.post(MINT_SIDECAR, timeout=self.timeout)
        except requests.RequestException as e:
            raise TVTimeAuthError(f"bootstrap mint failed: {e}") from e
        if r.status_code != 200:
            raise TVTimeAuthError(f"bootstrap mint rejected: HTTP {r.status_code}")
        try:
            token = (r.json() or {}).get("jwt_token")  # NOTE: top-level, not under "data"
        except ValueError as e:
            raise TVTimeAuthError(f"bootstrap mint returned non-JSON: {e}") from e
        if not token:
            raise TVTimeAuthError("bootstrap mint response missing jwt_token")
        return token

    def relogin(self) -> None:
        """Full automatic re-auth: mint an anonymous JWT, exchange it with stored credentials."""
        self.login_with_bootstrap(self.mint_bootstrap_jwt())

    # ---- request plumbing ----
    def _headers(self) -> dict:
        if not self.tokens.get("jwt_token"):
            raise TVTimeAuthError("not logged in — run bootstrap_login first")
        return {
            "Authorization": f"Bearer {self.tokens['jwt_token']}",
            "Host": "app.tvtime.com:80",
        }

    def _body(self) -> dict:
        return {"username": self.username, "password": self.password}

    def _authed(self, send) -> requests.Response:
        """Run a request; on 401 refresh (or fully re-login) once, then retry."""
        r = send()
        if r.status_code == 401:
            try:
                self.try_refresh()
            except TVTimeAuthError:
                self.relogin()  # raises TVTimeAuthError if mint/credentials fail
            r = send()
            if r.status_code == 401:
                raise TVTimeAuthError("still 401 after refresh and re-login")
        return r

    def _post(self, url: str) -> requests.Response:
        r = self._authed(
            lambda: requests.post(url, json=self._body(), headers=self._headers(), timeout=self.timeout)
        )
        if r.status_code >= 400:
            raise TVTimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r

    # ---- public API ----
    def mark_episode(self, tvdb_episode_id: str, rewatch: bool = False) -> None:
        self._post(EPISODE_SIDECAR.format(eid=tvdb_episode_id, rw=1 if rewatch else 0))

    def mark_previous_episodes(self, tvdb_show_id: str, tvdb_episode_id: str) -> None:
        """Bulk-mark every episode of the show up to (and including) the given one."""
        self._post(MARK_PREVIOUS_SIDECAR.format(show_id=tvdb_show_id, eid=tvdb_episode_id))

    def search_movie_uuid(self, query: str) -> str | None:
        r = self._authed(
            lambda: requests.get(SEARCH_SIDECAR.format(q=query), headers=self._headers(), timeout=self.timeout)
        )
        if r.status_code >= 400:
            raise TVTimeError(f"search HTTP {r.status_code}")
        try:
            results = r.json().get("data") or []
        except ValueError as e:
            raise TVTimeError(f"search returned non-JSON: {e}") from e
        for item in results:
            if item.get("type") == "movie" and item.get("uuid"):
                return item["uuid"]
        return None

    def mark_movie(self, uuid: str) -> None:
        self._post(MOVIE_SIDECAR.format(uuid=uuid))
