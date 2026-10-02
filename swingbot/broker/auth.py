"""Authentication helpers: TOTP generation, encrypted session store, non-interactive guard, approval polling.

Everything robin_stocks-specific (the actual OAuth calls) lives in ``broker/robinhood.py``; this module is pure
plumbing so it can be unit-tested without the library.
"""
from __future__ import annotations

import builtins
import getpass
import logging
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from cryptography.fernet import Fernet, InvalidToken

from swingbot.broker.retry import AuthError, MfaRequired

log = logging.getLogger(__name__)


def totp_now(secret: str) -> str:
    import pyotp

    return pyotp.TOTP(secret.replace(" ", "").strip()).now()


class SessionStore:
    """Owns the robin_stocks session pickle: plaintext only while in use, Fernet-encrypted at rest."""

    def __init__(self, session_dir: Path, enc_key: str | None, pickle_name: str = ""):
        self.dir = Path(session_dir)
        self.pickle_name = pickle_name
        self.plaintext_path = self.dir / f"robinhood{pickle_name}.pickle"
        self.encrypted_path = self.dir / f"robinhood{pickle_name}.pickle.enc"
        self._fernet = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key) if enc_key else None
        if self._fernet is None:
            log.warning("SESSION_ENC_KEY not set: session pickle will be stored unencrypted (mode 0600)")

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, stat.S_IRWXU)
        except OSError as exc:  # e.g. Windows
            log.debug("could not chmod session dir: %s", exc)

    def restore_plaintext(self) -> bool:
        """Materialise the plaintext pickle for robin_stocks to load. Returns True if a session exists."""
        self.ensure_dir()
        if self._fernet is not None and self.encrypted_path.exists():
            try:
                data = self._fernet.decrypt(self.encrypted_path.read_bytes())
            except InvalidToken:
                log.error("stored session could not be decrypted (key changed?); discarding it")
                self.encrypted_path.unlink(missing_ok=True)
                return False
            self._write_private(self.plaintext_path, data)
            return True
        if self.plaintext_path.exists():
            if self._fernet is not None:
                log.warning("found legacy unencrypted session pickle; it will be sealed after this login")
            return True
        return False

    def seal(self) -> None:
        """Encrypt the plaintext pickle (if present) and remove it."""
        if not self.plaintext_path.exists():
            return
        data = self.plaintext_path.read_bytes()
        if self._fernet is None:
            os.chmod(self.plaintext_path, stat.S_IRUSR | stat.S_IWUSR)
            return
        self._write_private(self.encrypted_path, self._fernet.encrypt(data))
        self.plaintext_path.unlink(missing_ok=True)

    def clear(self) -> None:
        for p in (self.plaintext_path, self.encrypted_path):
            p.unlink(missing_ok=True)

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)


@contextmanager
def no_interactive_input(reason: str = "scheduled mode") -> Iterator[None]:
    """Make any ``input()``/``getpass()`` call raise ``MfaRequired`` instead of blocking a cron job forever."""

    def _blocked(*_args: object, **_kwargs: object) -> str:
        raise MfaRequired(f"interactive prompt requested during {reason}; MFA could not be satisfied non-interactively")

    orig_input, orig_getpass = builtins.input, getpass.getpass
    builtins.input = _blocked  # type: ignore[assignment]
    getpass.getpass = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        builtins.input = orig_input
        getpass.getpass = orig_getpass


def poll_until(check: Callable[[], bool], timeout_sec: float, interval_sec: float = 5.0,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
               on_wait: Callable[[float], None] | None = None) -> bool:
    """Poll ``check`` until it returns True or ``timeout_sec`` elapses. ``on_wait(elapsed)`` is called each loop
    so the caller can alert the user (e.g. "approve the login on your phone")."""
    start = clock()
    while True:
        if check():
            return True
        elapsed = clock() - start
        if elapsed >= timeout_sec:
            return False
        if on_wait is not None:
            on_wait(elapsed)
        sleep(min(interval_sec, max(0.0, timeout_sec - elapsed)))


def require_credentials(username: str | None, password: str | None, totp_secret: str | None) -> None:
    missing = [n for n, v in (("RH_USERNAME", username), ("RH_PASSWORD", password), ("RH_TOTP_SECRET", totp_secret))
               if not v]
    if missing:
        raise AuthError(f"missing credentials in environment: {missing}")
