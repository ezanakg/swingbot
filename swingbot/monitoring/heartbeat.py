"""Heartbeat file: last-run timestamp per mode, used by the report mode and external monitors."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def write_heartbeat(path: Path, kind: str, run_id: str, status: str, now: datetime | None = None,
                    extra: dict[str, Any] | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_heartbeat(path) or {}
    data.setdefault("runs", {})
    data["runs"][kind] = {"ts": now.isoformat(), "run_id": run_id, "status": status, **(extra or {})}
    data["last"] = {"kind": kind, "ts": now.isoformat(), "run_id": run_id, "status": status}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("heartbeat unreadable: %s", exc)
        return None


def heartbeat_age_hours(path: Path, kind: str | None = None, now: datetime | None = None) -> float | None:
    data = read_heartbeat(path)
    if not data:
        return None
    entry = data["runs"].get(kind) if kind else data.get("last")
    if not entry:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(entry["ts"])).total_seconds() / 3600.0


def heartbeat_missed(path: Path, max_age_hours: float, kind: str | None = None,
                     now: datetime | None = None) -> bool:
    age = heartbeat_age_hours(path, kind, now)
    return age is None or age > max_age_hours
