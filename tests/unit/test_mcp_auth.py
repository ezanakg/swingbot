"""OAuth credential for the Robinhood MCP server: store, PKCE, loopback sign-in, refresh rotation. No network."""
import hashlib
import http.client
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from swingbot.broker import mcp_auth as ma
from swingbot.broker.retry import AuthError, TransientError

KEY = Fernet.generate_key().decode()
NOW = 1_800_000_000.0


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHttp:
    """Routes the registration and token endpoints; records every form posted to the token endpoint."""

    def __init__(self, token_responses=None, register_status=200):
        self.token_forms: list[dict] = []
        self.register_payloads: list[dict] = []
        self.token_responses = list(token_responses or [])
        self.register_status = register_status
        self.raise_next: Exception | None = None

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if url == ma.REGISTER_URL:
            self.register_payloads.append(dict(json))
            return Resp(self.register_status, {"client_id": "cid-1"} if self.register_status == 200 else {"error": "x"})
        if url == ma.TOKEN_URL:
            self.token_forms.append(dict(data))
            if self.token_responses:
                return self.token_responses.pop(0)
            return Resp(200, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 7 * 86400})
        raise AssertionError(f"unexpected POST {url}")


def _cred(expires_at, access="at0", refresh="rt0"):
    return ma.McpCredential(client_id="cid-1", access_token=access, refresh_token=refresh, expires_at=expires_at)


def test_credential_store_round_trip_and_key_handling(tmp_path):
    with pytest.raises(AuthError, match="SESSION_ENC_KEY"):
        ma.McpCredentialStore(tmp_path / "s", None)
    with pytest.raises(AuthError, match="valid Fernet"):
        ma.McpCredentialStore(tmp_path / "s", "not-a-key")
    store = ma.McpCredentialStore(tmp_path / "s", KEY)
    assert store.load() is None and not store.exists()
    cred = _cred(NOW + 100)
    store.save(cred)
    assert store.exists() and store.load() == cred
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert b"at0" not in store.path.read_bytes()  # encrypted at rest
    other = ma.McpCredentialStore(tmp_path / "s", Fernet.generate_key().decode())
    assert other.load() is None  # wrong key: ignored, not crashed
    store.clear()
    assert not store.exists()


def test_pkce_and_authorize_url():
    verifier, challenge = ma.pkce_pair()
    assert ma._b64url(hashlib.sha256(verifier.encode()).digest()) == challenge
    url = ma.build_authorize_url("cid-1", "http://127.0.0.1:1234/callback", "st", challenge)
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == "robinhood.com"
    assert q["response_type"] == ["code"] and q["client_id"] == ["cid-1"] and q["state"] == ["st"]
    assert q["code_challenge"] == [challenge] and q["code_challenge_method"] == ["S256"] and q["scope"] == ["internal"]


def _hit_callback(url: str, code: str | None, state: str | None, error: str | None = None) -> None:
    """Act as the browser: follow the redirect_uri from the authorize URL (no proxies: direct loopback)."""
    q = parse_qs(urlparse(url).query)
    redirect = urlparse(q["redirect_uri"][0])
    params = []
    if code is not None:
        params.append(f"code={code}")
    if error is not None:
        params.append(f"error={error}")
    params.append(f"state={state if state is not None else q['state'][0]}")

    def go():
        conn = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=5)
        conn.request("GET", f"{redirect.path}?{'&'.join(params)}")
        conn.getresponse().read()
        conn.close()

    threading.Thread(target=go, daemon=True).start()


def test_interactive_login_round_trip(tmp_path):
    store = ma.McpCredentialStore(tmp_path / "s", KEY)
    http_ = FakeHttp()
    seen = {}

    def open_browser(url):
        seen["url"] = url
        _hit_callback(url, code="abc", state=None)

    oauth = ma.McpOAuth(store, http_, open_browser=open_browser, clock=lambda: NOW)
    cred = oauth.interactive_login(timeout_sec=10, print_fn=lambda s: None)
    assert cred.access_token == "at1" and cred.refresh_token == "rt1" and cred.expires_at == NOW + 7 * 86400
    assert store.load() == cred
    reg = http_.register_payloads[0]
    assert reg["token_endpoint_auth_method"] == "none" and reg["grant_types"] == ["authorization_code", "refresh_token"]
    assert reg["redirect_uris"][0].startswith("http://127.0.0.1:")
    form = http_.token_forms[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "abc" and form["client_id"] == "cid-1"
    challenge = parse_qs(urlparse(seen["url"]).query)["code_challenge"][0]
    assert ma._b64url(hashlib.sha256(form["code_verifier"].encode()).digest()) == challenge


def test_interactive_login_rejects_bad_state_and_reports_errors(tmp_path):
    store = ma.McpCredentialStore(tmp_path / "s", KEY)
    http_ = FakeHttp()
    oauth = ma.McpOAuth(store, http_, open_browser=lambda u: _hit_callback(u, code="abc", state="wrong"),
                        clock=lambda: NOW)
    with pytest.raises(AuthError, match="timed out"):
        oauth.interactive_login(timeout_sec=0.5, print_fn=lambda s: None)
    assert http_.token_forms == [] and store.load() is None
    oauth = ma.McpOAuth(store, http_, open_browser=lambda u: _hit_callback(u, code=None, state=None, error="access_denied"),
                        clock=lambda: NOW)
    with pytest.raises(AuthError, match="access_denied"):
        oauth.interactive_login(timeout_sec=5, print_fn=lambda s: None)
    assert store.load() is None
    http_ = FakeHttp(register_status=500)
    oauth = ma.McpOAuth(store, http_, open_browser=lambda u: None, clock=lambda: NOW)
    with pytest.raises(AuthError, match="registration failed"):
        oauth.interactive_login(timeout_sec=1, print_fn=lambda s: None)


def test_access_token_refreshes_near_expiry_and_rotates(tmp_path):
    store = ma.McpCredentialStore(tmp_path / "s", KEY)
    http_ = FakeHttp()
    oauth = ma.McpOAuth(store, http_, open_browser=lambda u: None, clock=lambda: NOW)
    with pytest.raises(AuthError, match="swingbot auth"):
        oauth.access_token()
    store.save(_cred(NOW + 2 * ma.REFRESH_SKEW_SEC))
    assert oauth.access_token() == "at0" and http_.token_forms == []  # fresh enough: no refresh
    store.save(_cred(NOW + ma.REFRESH_SKEW_SEC / 2, refresh="rt-old"))
    assert oauth.access_token() == "at1"
    assert http_.token_forms[-1] == {"grant_type": "refresh_token", "refresh_token": "rt-old", "client_id": "cid-1"}
    assert store.load().refresh_token == "rt1"  # rotated pair persisted before use
    # a 401 from the MCP server forces a refresh regardless of nominal expiry
    assert oauth.force_refresh() == "at1" and len(http_.token_forms) == 2


def test_refresh_rejection_paths(tmp_path):
    store = ma.McpCredentialStore(tmp_path / "s", KEY)
    http_ = FakeHttp(token_responses=[Resp(400, {"error": "invalid_grant"})])
    oauth = ma.McpOAuth(store, http_, open_browser=lambda u: None, clock=lambda: NOW)
    store.save(_cred(NOW + 10))
    with pytest.raises(AuthError, match="swingbot auth"):
        oauth.access_token()
    # another process rotated the pair between our load and our refresh: adopt its credential instead
    http_ = FakeHttp(token_responses=[Resp(400, {"error": "invalid_grant"})])
    oauth = ma.McpOAuth(store, http_, open_browser=lambda u: None, clock=lambda: NOW)
    store.save(_cred(NOW + 10, refresh="stale"))
    original_load = store.load

    def load_then_rotate():
        cred = original_load()
        if cred is not None and cred.refresh_token == "stale":
            store.save(_cred(NOW + 5 * ma.REFRESH_SKEW_SEC, access="at-other", refresh="rt-other"))
        return cred

    store.load = load_then_rotate  # type: ignore[method-assign]
    assert oauth.access_token() == "at-other"
    # transient failures at the token endpoint propagate as TransientError (retry later, do not re-login)
    http_ = FakeHttp(token_responses=[Resp(503)])
    oauth = ma.McpOAuth(store, http_, open_browser=lambda u: None, clock=lambda: NOW)
    store.save(_cred(NOW + 10))
    with pytest.raises(TransientError):
        oauth.access_token()
