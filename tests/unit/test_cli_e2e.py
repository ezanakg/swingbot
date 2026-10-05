"""End-to-end CLI runs through ``cli.main`` -> settings -> lock -> build_app -> _handlers -> _run_mode.

The two bugs the live operator hit (auth pre-login, cache depth) lived in paths the handler-level tests never ran.
These tests drive every command the way cron does, in paper mode with synthetic data and in live mode against the
fake MCP server, and check exit codes, the ``runs`` table, the heartbeat file and the broker calls made.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from filelock import FileLock

import swingbot.broker.robinhood_mcp as rm
import swingbot.cli as cli
from swingbot.broker import mcp_auth as ma
from swingbot.enums import RunKind, RunMode, Timeframe
from swingbot.settings import load_settings
from swingbot.state.db import Database
from swingbot.state.repository import Repository
from swingbot.strategy.registry import build as build_strategy
from tests.conftest import make_config_dir, make_daily_bars
from tests.integration.test_paper_cycle import Clock, Quotes, SynthProvider, _find_entry_day
from tests.unit.test_robinhood_mcp import KEY, FakeMcp, default_tools, ok

SYMS = ["ALFA", "BRVO"]
ENV_KEYS = ("MODE", "LIVE_TRADING_ACK", "LIVE_ALLOWED_SYMBOLS", "SESSION_ENC_KEY", "RH_SESSION_DIR", "RH_ADAPTER",
            "SWINGBOT_CONFIG_DIR", "SWINGBOT_RISK_PROFILE", "RH_AGENTIC_ACCOUNT", "KILL_SWITCH_PATH")


@pytest.fixture
def world(tmp_path, cal, monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = make_config_dir(tmp_path, SYMS + ["SPY"], regime={"use_vix": False}, data={"providers": {"1d": "yfinance"}})
    bars = {s: make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=20 + k, drift=0.0006, vol=0.013)
            for k, s in enumerate(SYMS)}
    bars["SPY"] = make_daily_bars(cal, date(2023, 1, 3), date(2026, 10, 1), seed=7, drift=0.001, vol=0.006)
    settings = load_settings(cfg, env={})
    strat = build_strategy(settings.strategy_params["ema_rsi_macd"])
    alfa = bars["ALFA"]
    alfa.attrs["symbol"] = "ALFA"
    entry_day = _find_entry_day(cal, strat, alfa)
    clock = Clock(cal.session_close(entry_day) + timedelta(minutes=15))
    prices = {s: float(bars[s].loc[: pd.Timestamp(clock.now), "close"].iloc[-1]) for s in bars}
    quotes = Quotes(clock, prices)
    provider = SynthProvider(bars)
    real_build = cli.build_app
    built = []

    def offline_build(settings, **kw):
        app = real_build(settings, providers={Timeframe.D1: provider}, fallback_provider=provider, quote_fn=quotes,
                         earnings_fn=lambda s: [], clock=clock, sleep=lambda s: None, alert_channels=[], calendar=cal,
                         **kw)
        built.append(app)
        return app

    monkeypatch.setattr(cli, "build_app", offline_build)
    return SimpleNamespace(cfg=cfg, clock=clock, quotes=quotes, bars=bars, entry_day=entry_day, built=built,
                           settings=settings, cal=cal)


def run(cfg, *argv) -> int:
    return cli.main(["--config", str(cfg), "--no-lock", *argv])


def last_runs(settings, mode=RunMode.PAPER) -> dict[str, str]:
    db = Database(settings.paths.db_path)
    try:
        repo = Repository(db, mode)
        out = {}
        for kind in RunKind:
            lr = repo.last_run(kind)
            if lr:
                out[kind.value] = lr["status"]
        return out
    finally:
        db.close()


def test_paper_commands_end_to_end(world, capsys):
    cfg, cal, clock, quotes = world.cfg, world.cal, world.clock, world.quotes
    assert run(cfg, "status") == cli.EXIT_OK and "mode=paper" in capsys.readouterr().out
    assert run(cfg, "auth") == cli.EXIT_OK and "authenticated=True" in capsys.readouterr().out
    assert run(cfg, "auth", "--port", "8765", "--no-browser") == cli.EXIT_OK  # paper broker ignores the extras
    assert run(cfg, "liquidate", "--confirm", "0") == cli.EXIT_OK  # nothing held: a no-op, not a crash
    assert run(cfg, "liquidate", "--confirm", "3") == cli.EXIT_ERROR  # wrong confirm count is refused
    assert run(cfg, "halt", "--reason", "test") == cli.EXIT_OK
    assert run(cfg, "status") == cli.EXIT_OK and "halted=True" in capsys.readouterr().out
    assert run(cfg, "unhalt", "--reason", "done") == cli.EXIT_OK
    assert run(cfg, "reconcile") == cli.EXIT_OK and "reconcile clean=" in capsys.readouterr().out
    assert run(cfg, "scan") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "regime=" in out and "orders=" in out
    # next session: the resting entry fills and the protective stop goes in
    d1 = cal.next_session(world.entry_day)
    clock.now = cal.session_open(d1) + timedelta(minutes=5)
    quotes.prices["ALFA"] *= 0.999
    assert run(cfg, "manage") == cli.EXIT_OK and "manage: fills=" in capsys.readouterr().out
    assert run(cfg, "report") == cli.EXIT_OK
    assert run(cfg, "preflight") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "preflight complete: no orders were placed" in out and "risk_profile=standard" in out
    assert run(cfg, "suggest-allowlist", "--equity", "63.85") == cli.EXIT_OK
    assert "no watchlist symbol is affordable" in capsys.readouterr().out
    assert run(cfg, "suggest-allowlist", "--equity", "100000") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "LIVE_ALLOWED_SYMBOLS=" in out and "take-profit ladder possible" in out
    assert run(cfg, "backtest", "--start", "2025-01-02", "--end", "2025-06-30", "--symbols", "ALFA") == cli.EXIT_OK
    # every command recorded a run of its own kind, and all succeeded
    runs = last_runs(world.settings)
    expected = {"status", "auth", "liquidate", "halt", "unhalt", "reconcile", "scan", "manage", "report", "preflight",
                "allowlist", "backtest"}
    assert expected <= set(runs) and runs["liquidate"] == "FAILED"  # the last liquidate was the refused one
    assert all(v == "OK" for k, v in runs.items() if k != "liquidate"), runs
    hb = json.loads(world.settings.paths.heartbeat_path.read_text())
    assert {"scan", "manage", "auth", "status"} <= set(hb["runs"]) and hb["runs"]["scan"]["status"] == "OK"


def test_config_error_lock_and_usage_exit_codes(world, monkeypatch, capsys):
    cfg = world.cfg
    monkeypatch.setenv("MODE", "live")  # no acknowledgement, no allowlist, no key
    assert run(cfg, "status") == cli.EXIT_CONFIG and "CONFIG ERROR" in capsys.readouterr().err
    monkeypatch.delenv("MODE")
    lock_dir = world.settings.paths.lock_dir
    lock_dir.mkdir(parents=True, exist_ok=True)
    held = FileLock(str(lock_dir / "swingbot.lock"))
    held.acquire()
    try:
        assert cli.main(["--config", str(cfg), "status"]) == cli.EXIT_LOCKED
    finally:
        held.release()
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "--no-lock", "liquidate"])  # --confirm is mandatory


def test_login_first_rule_is_explicit():
    """The pre-login set is a named constant: commands that create the credential or never touch the broker are
    not in it, the trading cycle is."""
    assert cli.BROKER_LOGIN_KINDS == {RunKind.SCAN, RunKind.MANAGE, RunKind.RECONCILE, RunKind.LIQUIDATE}
    assert RunKind.AUTH not in cli.BROKER_LOGIN_KINDS and RunKind.PREFLIGHT not in cli.BROKER_LOGIN_KINDS


@pytest.fixture
def live(world, monkeypatch, tmp_path):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_THE_RISKS")
    monkeypatch.setenv("LIVE_ALLOWED_SYMBOLS", "ALFA")
    monkeypatch.setenv("SESSION_ENC_KEY", KEY)
    monkeypatch.setenv("RH_SESSION_DIR", str(tmp_path / "sess"))
    tools = default_tools()
    tools["get_equity_positions"] = lambda a: ok({"positions": [], "next": ""})
    tools["get_equity_orders"] = lambda a: ok({"orders": [], "next": ""})
    fake = FakeMcp(tools)
    monkeypatch.setattr(rm, "SESSION", fake)
    interactive = []

    def fake_interactive_login(self, timeout_sec=300.0, print_fn=print):
        interactive.append((timeout_sec, self.listen_port, self.open_browser("http://example.invalid") is None))
        cred = ma.McpCredential("cid", "at-browser", "rt-browser", self.clock() + 30 * 86400)
        self.store.save(cred)
        return cred

    monkeypatch.setattr(ma.McpOAuth, "interactive_login", fake_interactive_login)
    return SimpleNamespace(fake=fake, tools=tools, interactive=interactive)


def test_live_auth_then_trading_commands_end_to_end(world, live, capsys):
    cfg, fake = world.cfg, live.fake
    # a trading command before any credential exists fails cleanly with the auth exit code: no browser, no MCP
    assert run(cfg, "scan") == cli.EXIT_AUTH
    assert live.interactive == [] and fake.initialized == 0
    runs = last_runs(world.settings, RunMode.LIVE)
    assert runs["scan"] == "AUTH_FAILED"
    # `auth` runs the browser flow WITHOUT a non-interactive login first (the operator's regression), then
    # verifies the session and the agentic account
    assert run(cfg, "auth", "--port", "8765", "--no-browser") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "authenticated=True" in out and "ssh -L 8765:127.0.0.1:8765" in out
    assert live.interactive == [(300.0, 8765, True)]  # pinned port, browser launch suppressed
    assert fake.initialized == 1 and fake.token_forms == []  # no refresh attempted before the browser flow
    assert fake.headers_seen[-1]["Authorization"] == "Bearer at-browser"
    assert last_runs(world.settings, RunMode.LIVE)["auth"] == "OK"
    # read-only first contact
    assert run(cfg, "preflight", "--review", "ALFA") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "••••2222" in out and "preflight complete: no orders were placed" in out
    assert "broker tools: all present" in out and "review (dry run, nothing placed)" in out
    assert not any(n in ("place_equity_order", "cancel_equity_order") for n, _ in fake.calls)
    # the trading cycle logs in first and pins every account-scoped call to the agentic account
    assert run(cfg, "reconcile") == cli.EXIT_OK
    assert run(cfg, "scan") == cli.EXIT_OK
    assert run(cfg, "status") == cli.EXIT_OK and "mode=live" in capsys.readouterr().out
    names = [n for n, _ in fake.calls]
    assert "get_equity_positions" in names and "get_equity_orders" in names and "get_portfolio" in names
    assert all(a["account_number"] == "5AG22222" for n, a in fake.calls if "account_number" in a)
    for n, a in fake.calls:
        if n == "place_equity_order":  # whether or not the synthetic series signalled today, any order is well-formed
            assert a["symbol"] == "ALFA" and a["type"] == "limit" and a["ref_id"] and a["quantity"].isdigit()
    runs = last_runs(world.settings, RunMode.LIVE)
    assert runs["reconcile"] == "OK" and runs["scan"] == "OK" and runs["preflight"] == "OK"
    # the live DB never mixed with paper rows
    assert "scan" not in last_runs(world.settings, RunMode.PAPER)
