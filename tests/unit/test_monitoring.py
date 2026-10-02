import io
import json
import logging
from datetime import datetime, timedelta, timezone

from swingbot.enums import AlertSeverity
from swingbot.monitoring.alerts import Alert, AlertManager
from swingbot.monitoring.heartbeat import heartbeat_age_hours, heartbeat_missed, write_heartbeat
from swingbot.monitoring.logging_setup import redact_text, setup_logging


def test_redaction_patterns():
    txt = ("login {'username': 'me@example.com', 'password': 'hunter2secret', 'mfa_code': '123456'} "
           "Authorization: Bearer abc.def account_number=5PY12345 https://api.robinhood.com/accounts/5PY12345/ "
           '{"access_token": "eyJ", "refresh_token": "zzz"}')
    out = redact_text(txt, ["hunter2secret"])
    for secret in ("hunter2secret", "me@example.com", "abc.def", "5PY12345", "eyJ", "zzz", "123456"):
        assert secret not in out
    assert "[REDACTED]" in out


def test_json_logging_with_redaction_and_extras():
    buf = io.StringIO()
    setup_logging(None, "INFO", True, secrets=["s3cretvalue"], run_id="r1", mode="paper", stream=buf)
    logging.getLogger("t").info("token=s3cretvalue for %s", "AAPL", extra={"symbol": "AAPL"})
    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert "s3cretvalue" not in rec["msg"] and rec["symbol"] == "AAPL" and rec["run_id"] == "r1" and rec["level"] == "INFO"


class Rec:
    name = "rec"

    def __init__(self):
        self.got = []

    def send(self, a, t):
        self.got.append(t)


class Bad:
    name = "bad"

    def send(self, a, t):
        raise RuntimeError("down")


def test_alert_routing_threshold_dedupe_force():
    r, t = Rec(), [0.0]
    am = AlertManager([r, Bad()], AlertSeverity.WARNING, 900, mode="paper", clock=lambda: t[0])
    assert am.info("hello") is False and r.got == []
    assert am.warning("stop hit", "AAPL") is True
    assert am.warning("stop hit", "AAPL") is False and am.suppressed == 1
    assert am.critical("crit") and am.critical("crit")  # critical never de-duplicated
    t[0] = 1000
    assert am.warning("stop hit", "AAPL") is True
    assert am.emit(Alert(severity=AlertSeverity.INFO, title="report", body="x"), force=True) is True
    assert "[swingbot/paper] WARNING: stop hit" in r.got[0]


def test_heartbeat(tmp_path):
    p = tmp_path / "hb.json"
    now = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
    assert heartbeat_missed(p, 30, "scan", now)
    write_heartbeat(p, "scan", "run1", "OK", now)
    assert heartbeat_age_hours(p, "scan", now + timedelta(hours=2)) == 2.0
    assert not heartbeat_missed(p, 30, "scan", now + timedelta(hours=2))
    assert heartbeat_missed(p, 30, "scan", now + timedelta(hours=31))
    assert heartbeat_missed(p, 30, "manage", now)
