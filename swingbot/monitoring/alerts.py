"""Alert routing to Telegram, Discord and SMTP with severity thresholds and de-duplication.

Alert delivery must never take the bot down: every channel failure is logged and swallowed *here* (this is
the one place where that is the correct behaviour, because the alternative is losing the trading cycle).
"""
from __future__ import annotations

import logging
import smtplib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Callable, Protocol

import requests

from swingbot.enums import AlertSeverity
from swingbot.settings import AlertsConfig, Secrets

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    severity: AlertSeverity
    title: str
    body: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    key: str | None = None  # de-dup key; defaults to severity+title

    @property
    def dedupe_key(self) -> str:
        return self.key or f"{self.severity.value}|{self.title}"

    def text(self, mode: str | None = None) -> str:
        prefix = f"[swingbot{'/' + mode if mode else ''}] {self.severity.value}: {self.title}"
        return prefix if not self.body else f"{prefix}\n{self.body}"


class AlertChannel(Protocol):
    name: str

    def send(self, alert: Alert, text: str) -> None:
        """Deliver. Raise on failure; the manager handles logging."""


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0,
                 post: Callable[..., requests.Response] = requests.post):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout
        self._post = post

    def send(self, alert: Alert, text: str) -> None:
        resp = self._post(self._url, json={"chat_id": self._chat_id, "text": text[:4000]}, timeout=self._timeout)
        resp.raise_for_status()


class DiscordChannel:
    name = "discord"

    def __init__(self, webhook_url: str, timeout: float = 10.0,
                 post: Callable[..., requests.Response] = requests.post):
        self._url = webhook_url
        self._timeout = timeout
        self._post = post

    def send(self, alert: Alert, text: str) -> None:
        resp = self._post(self._url, json={"content": text[:1900]}, timeout=self._timeout)
        resp.raise_for_status()


class SmtpChannel:
    name = "smtp"

    def __init__(self, host: str, port: int, from_addr: str, to_addrs: list[str], username: str | None,
                 password: str | None, use_tls: bool = True, timeout: float = 15.0):
        self.host, self.port, self.from_addr, self.to_addrs = host, port, from_addr, to_addrs
        self.username, self.password, self.use_tls, self.timeout = username, password, use_tls, timeout

    def send(self, alert: Alert, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[swingbot] {alert.severity.value}: {alert.title}"[:200]
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)


class AlertManager:
    def __init__(self, channels: list[AlertChannel], min_severity: AlertSeverity = AlertSeverity.WARNING,
                 dedupe_window_sec: float = 900.0, mode: str | None = None,
                 sink: Callable[[Alert, list[str], bool], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.channels = channels
        self.min_severity = min_severity
        self.dedupe_window_sec = dedupe_window_sec
        self.mode = mode
        self._sink = sink
        self._clock = clock
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self.sent: list[Alert] = []
        self.suppressed = 0

    def send(self, severity: AlertSeverity, title: str, body: str = "", key: str | None = None) -> bool:
        return self.emit(Alert(severity=severity, title=title, body=body, key=key))

    def info(self, title: str, body: str = "", key: str | None = None) -> bool:
        return self.send(AlertSeverity.INFO, title, body, key)

    def warning(self, title: str, body: str = "", key: str | None = None) -> bool:
        return self.send(AlertSeverity.WARNING, title, body, key)

    def error(self, title: str, body: str = "", key: str | None = None) -> bool:
        return self.send(AlertSeverity.ERROR, title, body, key)

    def critical(self, title: str, body: str = "", key: str | None = None) -> bool:
        return self.send(AlertSeverity.CRITICAL, title, body, key)

    def emit(self, alert: Alert, force: bool = False) -> bool:
        """Route an alert. Returns True if at least one channel accepted it (or none are configured).
        ``force`` bypasses the severity threshold and de-duplication (used for scheduled reports)."""
        level = logging.CRITICAL if alert.severity == AlertSeverity.CRITICAL else (
            logging.ERROR if alert.severity == AlertSeverity.ERROR else (
                logging.WARNING if alert.severity == AlertSeverity.WARNING else logging.INFO))
        log.log(level, "ALERT %s: %s %s", alert.severity.value, alert.title, alert.body[:500].replace("\n", " | "))
        if alert.severity.rank < self.min_severity.rank and not force:
            return False
        with self._lock:
            last = self._last_sent.get(alert.dedupe_key)
            now = self._clock()
            if (last is not None and now - last < self.dedupe_window_sec and alert.severity != AlertSeverity.CRITICAL
                    and not force):
                self.suppressed += 1
                log.debug("alert suppressed (dedupe): %s", alert.dedupe_key)
                return False
            self._last_sent[alert.dedupe_key] = now
        text = alert.text(self.mode)
        delivered: list[str] = []
        for ch in self.channels:
            try:
                ch.send(alert, text)
                delivered.append(ch.name)
            except Exception as exc:  # channel failure must not break the trading cycle
                log.error("alert channel %s failed: %s", ch.name, exc)
        self.sent.append(alert)
        if self._sink is not None:
            try:
                self._sink(alert, delivered, bool(delivered) or not self.channels)
            except Exception as exc:  # persistence failure must not break alerting
                log.error("alert sink failed: %s", exc)
        return bool(delivered) or not self.channels


def build_alert_manager(cfg: AlertsConfig, secrets: Secrets, mode: str,
                        sink: Callable[[Alert, list[str], bool], None] | None = None) -> AlertManager:
    channels: list[AlertChannel] = []
    # A chat channel is on when settings.yaml enables it OR its secrets are present in the environment, so an
    # operator can turn alerts on from .env alone without committing account-specific config.
    telegram_on = cfg.telegram.enabled or bool(secrets.telegram_bot_token and secrets.telegram_chat_id)
    discord_on = cfg.discord.enabled or bool(secrets.discord_webhook_url)
    if telegram_on:
        if secrets.telegram_bot_token and secrets.telegram_chat_id:
            channels.append(TelegramChannel(secrets.telegram_bot_token, secrets.telegram_chat_id))
        else:
            log.warning("telegram alerts enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing; channel disabled")
    if discord_on:
        if secrets.discord_webhook_url:
            channels.append(DiscordChannel(secrets.discord_webhook_url))
        else:
            log.warning("discord alerts enabled but DISCORD_WEBHOOK_URL missing; channel disabled")
    if cfg.smtp.enabled:
        if cfg.smtp.host and cfg.smtp.from_addr and cfg.smtp.to_addrs:
            channels.append(SmtpChannel(cfg.smtp.host, cfg.smtp.port, cfg.smtp.from_addr, cfg.smtp.to_addrs,
                                        secrets.smtp_username, secrets.smtp_password, cfg.smtp.use_tls))
        else:
            log.warning("smtp alerts enabled but host/from/to incomplete; channel disabled")
    return AlertManager(channels, cfg.min_severity, cfg.dedupe_window_sec, mode=mode, sink=sink)
