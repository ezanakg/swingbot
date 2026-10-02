"""Command-line entry points. Every mode takes a file lock, reconciles first, and records a ``runs`` row."""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock, Timeout

from swingbot import __version__
from swingbot.app import App, build_app
from swingbot.backtest.engine import BacktestConfig, Backtester
from swingbot.backtest.metrics import format_metrics
from swingbot.backtest.walkforward import WalkForwardConfig, format_walkforward, walk_forward
from swingbot.broker.retry import AuthError, BrokerError, MfaRequired
from swingbot.enums import AlertSeverity, OrderType, RunKind, Side, Timeframe
from swingbot.execution.engine import CycleHalted
from swingbot.models import OrderRequest
from swingbot.monitoring.heartbeat import heartbeat_missed, write_heartbeat
from swingbot.monitoring.logging_setup import setup_logging
from swingbot.risk.limits import LimitsParams
from swingbot.risk.sizing import SizingParams
from swingbot.settings import ConfigError, Settings, load_settings
from swingbot.strategy.registry import build as build_strategy

log = logging.getLogger("swingbot.cli")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_AUTH, EXIT_HALTED, EXIT_LOCKED, EXIT_USAGE = 0, 1, 2, 3, 4, 5, 6


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swingbot", description=f"swingbot {__version__}: swing trading bot for Robinhood")
    p.add_argument("--config", default=None, help="config directory (default: $SWINGBOT_CONFIG_DIR or ./config)")
    p.add_argument("--log-level", default=None)
    p.add_argument("--no-lock", action="store_true", help="do not take the instance lock (tests only)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("reconcile", help="pull broker truth, repair the local DB, report mismatches")
    sub.add_parser("scan", help="after the close: refresh data, screen, signal, size, queue entries")
    sub.add_parser("manage", help="during the session: fills, stops, take-profits, time stops, exits")
    r = sub.add_parser("report", help="daily/weekly summary")
    r.add_argument("--weekly", action="store_true")
    b = sub.add_parser("backtest", help="run a strategy over history with the same signal + risk code")
    b.add_argument("--start", required=True, type=date.fromisoformat)
    b.add_argument("--end", default=None, type=date.fromisoformat)
    b.add_argument("--symbols", default=None, help="comma-separated; default = universe watchlist")
    b.add_argument("--cash", type=float, default=None)
    b.add_argument("--strategy", default=None, help="strategy file stem (default: all enabled)")
    b.add_argument("--no-regime", action="store_true")
    b.add_argument("--walk-forward", action="store_true")
    b.add_argument("--train", type=int, default=504)
    b.add_argument("--test", type=int, default=126)
    b.add_argument("--step", type=int, default=126)
    b.add_argument("--grid", action="append", default=[], help="param=v1,v2,... (repeatable)")
    liq = sub.add_parser("liquidate", help="MANUAL ONLY: cancel everything and close all positions")
    liq.add_argument("--confirm", type=int, required=True, help="must equal the live position count")
    u = sub.add_parser("unhalt", help="clear a manual-clear circuit breaker")
    u.add_argument("--reason", required=True)
    h = sub.add_parser("halt", help="engage a manual circuit breaker (no new entries)")
    h.add_argument("--reason", required=True)
    sub.add_parser("auth", help="live only: interactive login to register this device with Robinhood")
    sub.add_parser("status", help="print positions, open orders, breaker state and last runs")
    pf = sub.add_parser("preflight", help="read-only first-contact check: login, account, positions, orders, quotes, "
                                          "bars, earnings. Places NO orders.")
    pf.add_argument("--review", default=None, metavar="SYMBOL",
                    help="also ask the broker to simulate a 1-share limit buy of SYMBOL (a dry run; places nothing)")
    return p


def _backtest(app: App, args: argparse.Namespace) -> str:
    s = app.settings
    symbols = [x.strip().upper() for x in args.symbols.split(",")] if args.symbols else list(s.universe.watchlist)
    stems = [args.strategy] if args.strategy else list(s.strategies)
    strategies = [build_strategy(s.strategy_params[stem]) for stem in stems]
    warm = max(st.warmup_bars for st in strategies)
    end = args.end or app.cal.last_closed_session(app.clock())
    now = app.clock()
    fetch_now = min(now, app.cal.session_close(end) + timedelta(hours=1))
    bars = {}
    for sym in symbols:
        res = app.data.load_bars(sym, Timeframe.D1, warm + app.cal.sessions_between(args.start, end) + 10, now=fetch_now)
        if res.df.empty:
            log.warning("%s: no data; skipped", sym)
            continue
        bars[sym] = res.df
    spy = app.data.load_bars(s.regime.symbol, Timeframe.D1, warm + app.cal.sessions_between(args.start, end) + 10, now=fetch_now).df
    cfg = BacktestConfig(
        start=args.start, end=end, starting_cash=args.cash or s.paper.starting_cash, use_regime=not args.no_regime,
        regime_symbol=s.regime.symbol,
        sizing=SizingParams(s.risk.risk_per_trade_pct, s.risk.max_position_pct, s.risk.cash_buffer_pct,
                            s.risk.liquidity_cap_pct_of_adv, s.account.fractional_shares, s.risk.min_position_notional),
        limits=LimitsParams(s.risk.max_open_positions, s.risk.max_positions_per_strategy, s.risk.max_sector_exposure_pct,
                            s.risk.correlation_lookback, s.risk.max_correlation, s.risk.max_correlated_holdings,
                            s.risk.max_new_entries_per_day, s.risk.min_equity_to_trade),
        stops=app.engine.stops, regime_params=app.engine.regime_params, slippage_bps=s.paper.slippage_bps,
        partial_fill_prob=s.paper.partial_fill_prob, seed=s.paper.seed, entry_offset_pct=s.execution.entry_offset_pct,
        max_chase_pct=s.execution.max_chase_pct, exit_offset_pct=s.execution.exit_offset_pct,
    )
    if args.walk_forward:
        grid: dict[str, list[Any]] = {}
        for g in args.grid:
            k, v = g.split("=", 1)
            grid[k] = [_coerce(x) for x in v.split(",")]
        base = dict(s.strategy_params[stems[0]])
        wf = walk_forward(bars, build_strategy, base, app.cal, WalkForwardConfig(args.train, args.test, args.step, grid), cfg,
                          spy_bars=spy if not spy.empty else None)
        return format_walkforward(wf)
    res = Backtester(bars, strategies, app.cal, cfg, sector_of=s.sector_of, spy_bars=spy if not spy.empty else None).run()
    from collections import Counter

    reasons = Counter(t.exit_reason.value if t.exit_reason else "?" for t in res.trades)
    return (f"symbols={len(bars)} signals={res.n_signals} orders={res.n_orders}\n{format_metrics(res.metrics)}\n"
            f"exit reasons: {dict(reasons)}")


def _coerce(x: str) -> Any:
    for cast in (int, float):
        try:
            return cast(x)
        except ValueError:
            continue
    return x


def _status(app: App) -> str:
    repo = app.repo
    now = app.clock()
    lines = [f"swingbot {__version__} mode={app.settings.mode.value} db={app.settings.paths.db_path}"]
    snap = repo.latest_snapshot()
    if snap:
        lines.append(f"last snapshot {snap.ts.isoformat()} equity {snap.equity:,.2f} cash {snap.cash:,.2f} bp {snap.buying_power:,.2f}")
    st = repo.get_breaker_state()
    lines.append(f"breaker halted={st.halted} reasons={[r.value for r in st.reasons]} manual_clear={st.requires_manual_clear} {st.detail}")
    lines.append(app.reporter.positions_table(None, now))
    oo = repo.open_orders()
    lines.append(f"open orders ({len(oo)}):")
    for o in oo:
        lines.append(f"  {o.client_ref} {o.symbol} {o.side.value} {o.qty:g} {o.order_type.value} limit={o.limit_price} stop={o.stop_price} {o.status.value} {o.purpose}")
    for kind in RunKind:
        lr = repo.last_run(kind)
        if lr:
            lines.append(f"last {kind.value}: {lr['started_at']} {lr['status']} {lr['detail'][:80]}")
    return "\n".join(lines)


def _preflight(app: App, args: argparse.Namespace) -> str:
    """Exercise every read path of the configured broker and print what the bot would see. Never submits."""
    s = app.settings
    now = app.clock()
    broker = app.broker
    lines = [f"swingbot {__version__} preflight: mode={s.mode.value} broker={broker.name} adapter={s.broker.adapter}"]
    if s.is_live:
        lines.append(f"live gates: ack=ok allowlist={s.live_allowed_symbols} max_open_positions={s.risk.max_open_positions}")
    ks = s.paths.kill_switch_path
    lines.append(f"kill switch: {'PRESENT (no orders would be placed)' if ks.exists() else f'absent ({ks})'}")

    broker.login()
    lines.append(f"login: ok (authenticated={broker.is_authenticated()})")
    summary = getattr(broker, "account_summary", None)
    if callable(summary):
        lines.append(f"account: {summary()}")
    required = getattr(broker, "required_tools", None)
    live_tools = getattr(broker, "tool_names", None)
    if callable(required) and callable(live_tools):
        missing = sorted(set(required()) - set(live_tools()))
        lines.append("broker tools: all present" if not missing else f"broker tools MISSING (schema drift): {missing}")

    acct = broker.get_account()
    lines.append(f"account snapshot: type={acct.account_type.value} equity={acct.equity:,.2f} cash={acct.cash:,.2f} "
                 f"settled={acct.settled_cash:,.2f} buying_power={acct.buying_power:,.2f} "
                 f"day_trades_used={acct.day_trades_used} unrealized={acct.unrealized_pl:,.2f}")
    positions = broker.get_positions()
    lines.append(f"broker positions ({len(positions)}):")
    lines.extend(f"  {p.symbol} qty={p.qty:g} avg={p.avg_cost:.2f}" for p in positions)
    orders = broker.get_open_orders()
    lines.append(f"broker open orders ({len(orders)}):")
    lines.extend(f"  {o.broker_id} {o.symbol} {o.side.value} {o.qty:g} {o.order_type.value} limit={o.limit_price} "
                 f"stop={o.stop_price} {o.status.value}" for o in orders)

    symbols = s.tradable_universe()[:10]
    lines.append(f"quotes ({len(symbols)} of {len(s.tradable_universe())} tradable symbols):")
    for sym in symbols:
        try:
            q = broker.get_quote(sym)
        except BrokerError as exc:
            lines.append(f"  {sym}: ERROR {exc}")
            continue
        age = q.age_seconds(now)
        spread = q.spread_pct
        usable = age <= s.quotes.max_age_sec and q.last > 0 and (spread == float("inf") or spread <= s.quotes.max_spread_pct)
        lines.append(f"  {sym}: bid={q.bid:.2f} ask={q.ask:.2f} last={q.last:.2f} age={age:.0f}s "
                     f"spread={'n/a' if spread == float('inf') else f'{spread:.3%}'} source={q.source} "
                     f"{'usable' if usable else 'NOT usable now (deferred until the open)'}")

    if symbols:
        sym = symbols[0]
        res = app.data.load_bars(sym, Timeframe.D1, 40, now=now)
        df = res.df
        last = df.index[-1].date().isoformat() if len(df) else "n/a"
        lines.append(f"bars: {sym} daily rows={len(df)} last_closed={last} provider={df.attrs.get('provider', '?')}")
        try:
            dates = broker.get_earnings(sym)
            upcoming = [d.isoformat() for d in dates if d >= now.date()][:2]
            lines.append(f"earnings: {sym} known={len(dates)} next={upcoming or 'none scheduled'}")
        except BrokerError as exc:
            lines.append(f"earnings: {sym} ERROR {exc}")

    if args.review:
        review = getattr(broker, "review_order", None)
        sym = args.review.upper()
        if not callable(review):
            lines.append(f"review: broker {broker.name} has no pre-trade simulation; skipped")
        else:
            q = broker.get_quote(sym)
            price = q.bid if q.bid > 0 else q.last
            req = OrderRequest(client_ref=f"preflight-review-{sym}", symbol=sym, side=Side.BUY, qty=1,
                               order_type=OrderType.LIMIT, limit_price=round(price, 2), reason="preflight dry run")
            r = review(req)
            lines.append(f"review (dry run, nothing placed): buy 1 {sym} limit {req.limit_price:.2f} -> "
                         f"order_checks={r.get('order_checks')} quote={ {k: r.get('quote_data', {}).get(k) for k in ('bid_price', 'ask_price', 'last_trade_price')} if isinstance(r.get('quote_data'), dict) else None}")

    snap = app.repo.latest_snapshot()
    lines.append(f"local db: last snapshot {snap.ts.isoformat() if snap else 'none'}; "
                 f"local open orders={len(app.repo.open_orders())}")
    lines.append(app.reporter.positions_table(None, now))
    lines.append("preflight complete: no orders were placed")
    return "\n".join(lines)


def _run_mode(app: App, kind: RunKind, fn: Callable[[str], str]) -> int:
    run_id = app.repo.start_run(kind, now=app.clock())
    status, detail, code = "OK", "", EXIT_OK
    try:
        if kind in (RunKind.SCAN, RunKind.MANAGE, RunKind.RECONCILE, RunKind.LIQUIDATE):
            app.broker.login()
        detail = fn(run_id)
    except CycleHalted as exc:
        status, detail, code = "HALTED", str(exc), EXIT_HALTED
        log.warning("cycle halted: %s", exc)
    except (MfaRequired, AuthError) as exc:
        status, detail, code = "AUTH_FAILED", str(exc), EXIT_AUTH
        app.alerts.send(AlertSeverity.ERROR, f"{kind.value}: authentication failed", str(exc))
    except BrokerError as exc:
        status, detail, code = "BROKER_ERROR", str(exc), EXIT_ERROR
        app.alerts.send(AlertSeverity.ERROR, f"{kind.value}: broker error", str(exc))
    except Exception as exc:  # last line of defence: classify as fatal, alert, never trade blind
        status, detail, code = "FAILED", f"{type(exc).__name__}: {exc}", EXIT_ERROR
        log.exception("unhandled error in %s", kind.value)
        app.alerts.send(AlertSeverity.CRITICAL, f"{kind.value}: unhandled exception", detail)
    finally:
        app.repo.finish_run(run_id, status, detail, now=app.clock())
        write_heartbeat(app.settings.paths.heartbeat_path, kind.value, run_id, status, app.clock())
    if detail and code == EXIT_OK:
        print(detail)
    return code


def _handlers(app: App, args: argparse.Namespace) -> tuple[RunKind, Callable[[str], str]]:
    e = app.engine
    if args.command == "reconcile":
        return RunKind.RECONCILE, lambda rid: f"reconcile clean={e.reconcile().clean}"
    if args.command == "scan":
        def scan(rid: str) -> str:
            s = e.scan(rid)
            if s.halted:
                raise CycleHalted(s.halted)
            return (f"scan {s.as_of}: regime={s.regime.regime.value if s.regime else 'n/a'} eligible={len(s.eligible)} "
                    f"signals={len(s.signals)} orders={len(s.orders)} deferred={s.deferred} "
                    f"insufficient_history={len(s.insufficient_history)}")
        return RunKind.SCAN, scan
    if args.command == "manage":
        def manage(rid: str) -> str:
            m = e.manage(rid)
            if m.halted:
                raise CycleHalted(m.halted)
            return (f"manage: fills={m.fills} stops_placed={m.stops_placed} tightened={m.stops_tightened} "
                    f"tp={m.take_profits_placed} exits={m.exits} expired={m.expired_entries} pending={m.pending_submitted}")
        return RunKind.MANAGE, manage
    if args.command == "report":
        def report(rid: str) -> str:
            now = app.clock()
            text = app.reporter.daily_summary(now)
            app.reporter.publish("daily summary", text)
            if args.weekly or app.cal.session_date_of(now).weekday() == 4:
                wk = app.reporter.weekly_summary(now)
                app.reporter.publish("weekly performance", wk)
                text += "\n\n" + wk
            hb = app.settings.paths.heartbeat_path
            if heartbeat_missed(hb, app.settings.heartbeat.max_age_hours, "scan", now):
                app.alerts.send(AlertSeverity.ERROR, "heartbeat missed", f"no successful scan within {app.settings.heartbeat.max_age_hours}h")
            return text
        return RunKind.REPORT, report
    if args.command == "backtest":
        return RunKind.BACKTEST, lambda rid: _backtest(app, args)
    if args.command == "liquidate":
        def liquidate(rid: str) -> str:
            orders = e.liquidate(args.confirm)
            return "liquidation orders:\n" + "\n".join(f"  {o.symbol} {o.status.value} {o.client_ref}" for o in orders)
        return RunKind.LIQUIDATE, liquidate
    if args.command == "unhalt":
        return RunKind.UNHALT, lambda rid: f"breaker cleared: {app.engine.d.breaker.clear_manual(args.reason, app.clock()).detail}"
    if args.command == "halt":
        return RunKind.UNHALT, lambda rid: f"breaker engaged: {app.engine.d.breaker.halt_manually(args.reason, app.clock()).detail}"
    if args.command == "status":
        return RunKind.REPORT, lambda rid: _status(app)
    if args.command == "preflight":
        return RunKind.REPORT, lambda rid: _preflight(app, args)
    if args.command == "auth":
        def auth(rid: str) -> str:
            login = getattr(app.broker, "login")
            try:
                login(interactive=True)
            except TypeError:
                login()
            return f"authenticated={app.broker.is_authenticated()}"
        return RunKind.RECONCILE, auth
    raise SystemExit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    lg = settings.logging
    setup_logging(settings.paths.log_dir, args.log_level or lg.level, lg.json_format, lg.max_bytes, lg.backup_count,
                  secrets=settings.secrets.values_to_redact(), mode=settings.mode.value)
    log.info("swingbot %s starting: command=%s mode=%s", __version__, args.command, settings.mode.value)
    lock = None
    if not args.no_lock:
        settings.paths.lock_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(settings.paths.lock_dir / "swingbot.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            print("another swingbot instance holds the lock; exiting", file=sys.stderr)
            return EXIT_LOCKED
    try:
        app = build_app(settings)
    except Exception as exc:
        log.exception("startup failed")
        print(f"STARTUP ERROR: {exc}", file=sys.stderr)
        if lock:
            lock.release()
        return EXIT_ERROR
    try:
        kind, fn = _handlers(app, args)
        return _run_mode(app, kind, fn)
    finally:
        app.close()
        if lock:
            lock.release()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
