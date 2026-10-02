"""SQLite connection management (WAL mode), schema bootstrap and ordered migrations."""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIG_RE = re.compile(r"^(\d{3})_.*\.sql$")


class Database:
    def __init__(self, path: Path | str, timeout_sec: float = 5.0):
        self.path = Path(path) if str(path) != ":memory:" else path
        self.timeout_sec = timeout_sec
        self._local = threading.local()
        self._memory_conn: sqlite3.Connection | None = None
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ connections
    def connect(self) -> sqlite3.Connection:
        if not isinstance(self.path, Path):  # ":memory:" shares a single connection
            if self._memory_conn is None:
                self._memory_conn = self._open()
            return self._memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.timeout_sec, isolation_level=None,
                               check_same_thread=False, detect_types=0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout_sec * 1000)}")
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT/ROLLBACK. Nested use joins the outer transaction."""
        conn = self.connect()
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------ helpers
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self.connect().executemany(sql, rows)

    def fetchone(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    # ------------------------------------------------------------------ schema
    def init_schema(self) -> int:
        """Create tables and apply pending migrations. Returns the schema version in effect."""
        conn = self.connect()
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = int(row["v"]) if row and row["v"] is not None else 0
        if current == 0:
            conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (1, ?)",
                         (datetime.now(timezone.utc).isoformat(),))
            current = 1
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            m = _MIG_RE.match(path.name)
            if not m:
                continue
            version = int(m.group(1))
            if version <= current:
                continue
            log.info("applying migration %s", path.name)
            with self.transaction() as tx:
                tx.executescript(path.read_text(encoding="utf-8"))
                tx.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                           (version, datetime.now(timezone.utc).isoformat()))
            current = version
        return current
