"""Structured JSON logging with rotation and secret redaction.

The redaction filter runs on every handler so that tokens, usernames, passwords and account numbers never reach
a log line, even when they are embedded in an exception message or a broker payload dump.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([\"']?(?:access_token|refresh_token|token|password|mfa_code|totp|secret|authorization|"
                r"api[_-]?key|webhook_url|device_token)[\"']?\s*[:=]\s*[\"']?)([^\"',\s}]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(account(?:_number|_id)?[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9\-]{6,})"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(/accounts/)([A-Za-z0-9\-]{6,})"), r"\1[REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?i)(username[\"']?\s*[:=]\s*[\"']?)([^\"',\s}]+)"), r"\1[REDACTED]"),
]


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    if not text:
        return text
    out = text
    for value in secrets:
        if value and len(value) >= 4:
            out = out.replace(value, "[REDACTED]")
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


class RedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def add_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets.extend(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):
            msg = str(record.msg)
        record.msg = redact_text(msg, self._secrets)
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            if exc.args:
                exc.args = tuple(redact_text(str(a), self._secrets) for a in exc.args)
        for key in ("payload", "raw", "detail"):
            if hasattr(record, key):
                setattr(record, key, redact_text(str(getattr(record, key)), self._secrets))
        return True


_STD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, run_id: str | None = None, mode: str | None = None):
        super().__init__()
        self.run_id = run_id
        self.mode = mode

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.mode:
            payload["mode"] = self.mode
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                payload[k] = v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)
        if record.exc_info and record.exc_info[1] is not None:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")


_FILTER: RedactionFilter | None = None


def setup_logging(
    log_dir: Path | None,
    level: str = "INFO",
    json_format: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
    secrets: Iterable[str] = (),
    run_id: str | None = None,
    mode: str | None = None,
    stream: Any = None,
) -> RedactionFilter:
    """Configure the root logger. Idempotent: existing swingbot handlers are replaced."""
    global _FILTER
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        if getattr(h, "_swingbot", False):
            root.removeHandler(h)
            h.close()
    flt = RedactionFilter(secrets)
    _FILTER = flt
    fmt: logging.Formatter = JsonFormatter(run_id, mode) if json_format else HumanFormatter()
    handlers: list[logging.Handler] = []
    sh = logging.StreamHandler(stream or sys.stderr)
    handlers.append(sh)
    if log_dir is not None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            Path(log_dir) / "swingbot.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handlers.append(fh)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(flt)
        h._swingbot = True  # type: ignore[attr-defined]
        root.addHandler(h)
    for noisy in ("urllib3", "yfinance", "peewee", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return flt


def add_secrets(secrets: Iterable[str]) -> None:
    if _FILTER is not None:
        _FILTER.add_secrets(secrets)
