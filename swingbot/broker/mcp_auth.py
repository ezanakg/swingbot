"""OAuth credential for Robinhood's hosted Trading MCP server (``agent.robinhood.com``).

Robinhood's agentic-trading surface authenticates third-party agents with OAuth 2.1: dynamic client registration,
an authorization-code grant with PKCE that the account holder approves in a browser, and single-use refresh
tokens that rotate on every refresh. This module owns that credential and nothing else: the MCP transport lives in
``broker/robinhood_mcp.py`` and is the only module that calls it.

ASSUMPTIONS (endpoints observed in Robinhood's published agent integration as of 2026-09-28; each is a constant so
drift is a one-line fix and shows up as a clear ``AuthError``):
* Client registration: ``POST https://agent.robinhood.com/oauth/trading/register`` (RFC 7591, public client,
  ``token_endpoint_auth_method=none``, scope ``internal``).
* Authorization: ``https://robinhood.com/oauth`` with ``response_type=code`` and an S256 PKCE challenge; the
  redirect goes to a loopback listener on 127.0.0.1 (RFC 8252).
* Tokens: ``POST https://api.robinhood.com/oauth2/token/`` (form-encoded) for both the code exchange and
  ``grant_type=refresh_token``. The refresh token is single-use: the rotated pair is persisted *before* it is
  used, and a rejected refresh re-reads the store in case another process rotated it first.
* Access tokens live for days, not hours; we refresh within ``REFRESH_SKEW_SEC`` of expiry and on any 401.

The browser step is only ever run by ``swingbot auth`` (interactive). Scheduled runs never prompt: without a
stored credential they fail with an ``AuthError`` naming that command.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets as pysecrets
import stat
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken

from swingbot.broker.retry import AuthError, TransientError, classify_exception

log = logging.getLogger(__name__)

REGISTER_URL = "https://agent.robinhood.com/oauth/trading/register"
AUTHORIZE_URL = "https://robinhood.com/oauth"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
SCOPE = "internal"
CLIENT_NAME = "swingbot"
REFRESH_SKEW_SEC = 3600.0
CREDENTIAL_FILENAME = "robinhood_mcp.cred.enc"


@dataclass(frozen=True)
class McpCredential:
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "McpCredential | None":
        try:
            d = json.loads(text)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        try:
            return cls(client_id=str(d["client_id"]), access_token=str(d["access_token"]),
                       refresh_token=str(d["refresh_token"]), expires_at=float(d["expires_at"]))
        except (KeyError, TypeError, ValueError):
            return None

    def seconds_to_expiry(self, now: float) -> float:
        return self.expires_at - now


class McpCredentialStore:
    """Fernet-encrypted credential file (mode 0600 in a 0700 directory). A key is mandatory: this token can place
    real orders, so it is never written in plaintext."""

    def __init__(self, session_dir: Path, enc_key: str | bytes | None, filename: str = CREDENTIAL_FILENAME):
        if not enc_key:
            raise AuthError("SESSION_ENC_KEY is required to store the Robinhood MCP credential")
        self.dir = Path(session_dir)
        self.path = self.dir / filename
        try:
            self._fernet = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key)
        except (ValueError, TypeError) as exc:
            raise AuthError("SESSION_ENC_KEY is not a valid Fernet key") from exc

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> McpCredential | None:
        if not self.path.exists():
            return None
        try:
            data = self._fernet.decrypt(self.path.read_bytes())
        except InvalidToken:
            log.error("stored Robinhood MCP credential could not be decrypted (SESSION_ENC_KEY changed?); ignoring it")
            return None
        cred = McpCredential.from_json(data.decode("utf-8", errors="replace"))
        if cred is None:
            log.error("stored Robinhood MCP credential is malformed; ignoring it")
        return cred

    def save(self, cred: McpCredential) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, stat.S_IRWXU)
        except OSError as exc:  # e.g. Windows
            log.debug("could not chmod session dir: %s", exc)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self._fernet.encrypt(cred.to_json().encode("utf-8")))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------ PKCE helpers
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` per RFC 7636 (S256)."""
    verifier = _b64url(pysecrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(client_id: str, redirect_uri: str, state: str, code_challenge: str,
                        authorize_url: str = AUTHORIZE_URL, scope: str = SCOPE) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorize_url}?{urlencode(params)}"


# ------------------------------------------------------------------------------------------------ loopback listener
class _CallbackServer:
    """Single-shot loopback HTTP listener for the authorization-code redirect."""

    def __init__(self, expected_state: str, host: str = "127.0.0.1", port: int = 0):
        self.expected_state = expected_state
        self.code: str | None = None
        self.error: str | None = None
        self.done = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # silence default stderr logging
                return

            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                url = urlparse(self.path)
                if url.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                q = parse_qs(url.query)
                state = (q.get("state") or [""])[0]
                if state != outer.expected_state:
                    self._reply(400, "state mismatch; close this tab and re-run swingbot auth")
                    return
                code = (q.get("code") or [""])[0]
                if not code:
                    outer.error = (q.get("error") or ["no authorization code in callback"])[0]
                    self._reply(400, f"sign-in failed: {outer.error}")
                    outer.done.set()
                    return
                outer.code = code
                self._reply(200, "swingbot is signed in to Robinhood. You can close this tab.")
                outer.done.set()

            def _reply(self, status: int, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer((host, port), Handler)
        self.redirect_uri = f"http://{host}:{self.server.server_address[1]}/callback"
        self._thread = threading.Thread(target=self.server.serve_forever, name="swingbot-oauth-callback", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout_sec: float) -> bool:
        return self.done.wait(timeout_sec)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ------------------------------------------------------------------------------------------------ OAuth client
class McpOAuth:
    """Mints, stores, refreshes and serves the bearer token for the hosted MCP server."""

    def __init__(
        self,
        store: McpCredentialStore,
        http: requests.Session | None = None,
        *,
        timeout_sec: float = 20.0,
        clock: Callable[[], float] = time.time,
        open_browser: Callable[[str], Any] = webbrowser.open,
        register_url: str = REGISTER_URL,
        authorize_url: str = AUTHORIZE_URL,
        token_url: str = TOKEN_URL,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
    ):
        self.store = store
        self.http = http or requests.Session()
        self.timeout = timeout_sec
        self.clock = clock
        self.open_browser = open_browser
        self.register_url = register_url
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ interactive sign-in
    def interactive_login(self, timeout_sec: float = 300.0, print_fn: Callable[[str], None] = print) -> McpCredential:
        """Run the browser flow once. Blocks until the account holder approves or ``timeout_sec`` elapses."""
        state = _b64url(pysecrets.token_bytes(16))
        verifier, challenge = pkce_pair()
        listener = _CallbackServer(state, self.listen_host, self.listen_port)
        listener.start()
        try:
            client_id = self.register_client(listener.redirect_uri)
            url = build_authorize_url(client_id, listener.redirect_uri, state, challenge, self.authorize_url)
            print_fn("Opening your browser for Robinhood sign-in. If nothing opens, visit this URL:\n  " + url)
            try:
                self.open_browser(url)
            except Exception as exc:  # the URL was printed; the user can open it by hand
                log.warning("could not open a browser automatically: %s", exc)
            if not listener.wait(timeout_sec):
                raise AuthError(f"sign-in timed out after {timeout_sec:.0f}s: the browser never returned to the callback")
            if listener.error or not listener.code:
                raise AuthError(f"sign-in failed: {listener.error or 'no authorization code'}")
            cred = self.exchange_code(client_id, listener.code, listener.redirect_uri, verifier)
        finally:
            listener.close()
        self.store.save(cred)
        log.info("robinhood mcp credential stored at %s", self.store.path)
        return cred

    def register_client(self, redirect_uri: str) -> str:
        payload = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
            "scope": SCOPE,
        }
        try:
            resp = self.http.post(self.register_url, json=payload, headers={"Accept": "application/json"},
                                  timeout=self.timeout)
        except Exception as exc:
            raise classify_exception(exc) from exc
        if resp.status_code >= 400:
            raise AuthError(f"client registration failed: HTTP {resp.status_code} {resp.text[:200]}",
                            status=resp.status_code)
        try:
            client_id = resp.json().get("client_id")
        except ValueError as exc:
            raise AuthError("client registration returned non-JSON") from exc
        if not isinstance(client_id, str) or not client_id:
            raise AuthError("client registration returned no client_id")
        return client_id

    def exchange_code(self, client_id: str, code: str, redirect_uri: str, verifier: str) -> McpCredential:
        body = self._token_grant({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                  "client_id": client_id, "code_verifier": verifier})
        if body is None:
            raise AuthError("authorization code exchange was rejected")
        return self._credential_from(client_id, body)

    # ------------------------------------------------------------------ token service
    def access_token(self) -> str:
        """A token valid for at least ``REFRESH_SKEW_SEC`` more seconds, refreshing (and rotating) if needed."""
        with self._lock:
            cred = self.store.load()
            if cred is None:
                raise AuthError("no stored Robinhood MCP credential; run `swingbot auth` once on this machine")
            if cred.seconds_to_expiry(self.clock()) > REFRESH_SKEW_SEC:
                return cred.access_token
            return self._refresh_or_reload(cred)

    def force_refresh(self) -> str:
        """Called after a 401: the token was rejected regardless of its nominal expiry."""
        with self._lock:
            cred = self.store.load()
            if cred is None:
                raise AuthError("no stored Robinhood MCP credential; run `swingbot auth` once on this machine")
            return self._refresh_or_reload(cred)

    def _refresh_or_reload(self, cred: McpCredential) -> str:
        fresh = self.refresh(cred)
        if fresh is not None:
            return fresh.access_token
        # The grant was rejected. Refresh tokens are single-use: another process may have rotated the pair.
        reloaded = self.store.load()
        if reloaded is not None and reloaded.refresh_token != cred.refresh_token:
            if reloaded.seconds_to_expiry(self.clock()) > REFRESH_SKEW_SEC:
                return reloaded.access_token
            again = self.refresh(reloaded)
            if again is not None:
                return again.access_token
        raise AuthError("Robinhood MCP token refresh was rejected; run `swingbot auth` again")

    def refresh(self, cred: McpCredential) -> McpCredential | None:
        """One refresh grant. Persists the rotated pair before returning it. ``None`` when the grant is rejected."""
        body = self._token_grant({"grant_type": "refresh_token", "refresh_token": cred.refresh_token,
                                  "client_id": cred.client_id})
        if body is None:
            return None
        fresh = self._credential_from(cred.client_id, body)
        self.store.save(fresh)
        log.info("robinhood mcp token refreshed; expires in %.0fh", fresh.seconds_to_expiry(self.clock()) / 3600)
        return fresh

    def _token_grant(self, form: dict[str, str]) -> dict[str, Any] | None:
        """POST to the token endpoint. The form carries credential material and is never logged."""
        try:
            resp = self.http.post(self.token_url, data=form,
                                  headers={"Accept": "application/json",
                                           "Content-Type": "application/x-www-form-urlencoded"},
                                  timeout=self.timeout)
        except Exception as exc:
            raise classify_exception(exc) from exc
        if 400 <= resp.status_code < 500:
            log.warning("token grant %s rejected: HTTP %d", form.get("grant_type"), resp.status_code)
            return None
        if resp.status_code >= 500:
            raise TransientError(f"token endpoint error {resp.status_code}", status=resp.status_code)
        try:
            body = resp.json()
        except ValueError as exc:
            raise AuthError("token endpoint returned non-JSON") from exc
        if not isinstance(body, dict):
            raise AuthError("token endpoint returned a non-object")
        return body

    def _credential_from(self, client_id: str, body: dict[str, Any]) -> McpCredential:
        access, refresh = body.get("access_token"), body.get("refresh_token")
        if not isinstance(access, str) or not isinstance(refresh, str) or not access or not refresh:
            raise AuthError("token response carried no access_token or refresh_token")
        try:
            expires_in = float(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0  # unknown lifetime: treat as stale so the next call refreshes
        return McpCredential(client_id=client_id, access_token=access, refresh_token=refresh,
                             expires_at=self.clock() + expires_in)
