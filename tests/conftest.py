"""Shared fixtures: calendar, synthetic bars, temp DB/repo, temp config directory."""
from __future__ import annotations

import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from swingbot.calendar import TradingCalendar
from swingbot.enums import RunMode
from swingbot.state.db import Database
from swingbot.state.repository import Repository

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


@pytest.fixture(scope="session")
def cal() -> TradingCalendar:
    return TradingCalendar(start_year=2019, end_year=2027)


def make_daily_bars(cal: TradingCalendar, start: date, end: date, seed: int = 0, start_price: float = 100.0,
                    drift: float = 0.0003, vol: float = 0.012, volume: float = 3_000_000.0,
                    closes: np.ndarray | None = None) -> pd.DataFrame:
    dates = cal.sessions_in_range(start, end)
    idx = pd.DatetimeIndex([cal.session_open(d) for d in dates], name="ts")
    rng = np.random.default_rng(seed)
    n = len(idx)
    if closes is None:
        closes = start_price * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    closes = np.asarray(closes, dtype=float)[:n]
    n = len(closes)
    idx = idx[:n]
    opens = closes * (1 + rng.normal(0, 0.002, n))
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0.001, 0.01, n))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0.001, 0.01, n))
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes,
                       "volume": rng.integers(int(volume * 0.7), int(volume * 1.3), n).astype(float)}, index=idx)
    return df


@pytest.fixture
def daily_bars(cal) -> pd.DataFrame:
    df = make_daily_bars(cal, date(2024, 1, 2), date(2026, 10, 1), seed=1)
    df.attrs["symbol"] = "SYN"
    return df


@pytest.fixture
def repo(tmp_path) -> Repository:
    db = Database(tmp_path / "test.sqlite3")
    db.init_schema()
    r = Repository(db, RunMode.PAPER)
    yield r
    db.close()


def make_config_dir(tmp_path: Path, watchlist: list[str], **settings_overrides) -> Path:
    """Copy the repo config into tmp and override the universe + paths so tests are hermetic."""
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)
    uni = yaml.safe_load((cfg / "universe.yaml").read_text())
    uni["watchlist"] = watchlist
    (cfg / "universe.yaml").write_text(yaml.safe_dump(uni))
    st = yaml.safe_load((cfg / "settings.yaml").read_text())
    st["paths"] = {k: str(tmp_path / "var" / Path(v).name) for k, v in st["paths"].items()}
    st["alerts"]["min_severity"] = "INFO"
    for k, v in settings_overrides.items():
        if isinstance(v, dict) and isinstance(st.get(k), dict):
            st[k].update(v)
        else:
            st[k] = v
    (cfg / "settings.yaml").write_text(yaml.safe_dump(st))
    return cfg


@pytest.fixture
def config_dir(tmp_path) -> Path:
    return make_config_dir(tmp_path, ["AAPL", "MSFT", "SPY"])


UTC = timezone.utc


def dt(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)
