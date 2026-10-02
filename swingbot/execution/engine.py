"""Execution engine: orchestrates reconciliation, screening, signals, risk, orders and position management.

Nothing here bypasses the risk layer: every entry goes through sizing + limits + pre-trade checks and writes a
``risk_decisions`` row; every sell goes through the PDT/GFV guards.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import pandas as pd

from swingbot.broker.interface import BrokerInterface
from swingbot.broker.retry import BrokerError, BrokerSchemaDrift, BrokerUnavailable, ClientError
from swingbot.calendar import TradingCalendar
from swingbot.data.service import BarsResult, MarketDataService
from swingbot.enums import (
    AlertSeverity,
    ExitReason,
    OrderStatus,
    OrderType,
    RunMode,
    Side,
    SignalOutcome,
    SignalType,
    Timeframe,
    TimeInForce,
)
from swingbot.execution.order_manager import OrderManager
from swingbot.execution.positions import PositionBook
from swingbot.execution.pricing import (
    PricingParams,
    entry_limit_price,
    exit_limit_price,
    gap_exit_price,
    price_within_signal,
    quote_is_usable,
    stop_prices,
    urgent_exit_price,
)
from swingbot.execution.reconciler import Reconciler
from swingbot.models import (
    AccountSnapshot,
    Holding,
    Order,
    OrderRequest,
    Position,
    Quote,
    RegimeResult,
    RiskCheck,
    RiskDecision,
    Signal,
    make_client_ref,
)
from swingbot.monitoring.alerts import AlertManager
from swingbot.risk.circuit_breaker import BreakerDecision, CircuitBreaker, kill_switch_engaged
from swingbot.risk.compliance import PDTGuard, SettledCashGuard
from swingbot.risk.limits import LimitsParams, PortfolioState, check_limits, first_failure
from swingbot.risk.sizing import SizingParams, compute_size
from swingbot.risk.stops import (
    StopParams,
    gap_below_stop,
    initial_hard_stop,
    pending_take_profits,
    time_stop_reason,
    update_trailing_stop,
)
from swingbot.settings import Settings
from swingbot.state.repository import Repository
from swingbot.strategy.base import InsufficientHistory, Strategy
from swingbot.strategy.indicators import atr as atr_indicator
from swingbot.strategy.regime import RegimeParams, classify_regime
from swingbot.universe.screener import Screener

log = logging.getLogger(__name__)


class CycleHalted(Exception):
    """Raised when the cycle must stop placing orders (kill switch, schema drift, broker unavailable)."""


@dataclass
class EngineDeps:
    settings: Settings
    cal: TradingCalendar
    repo: Repository
    broker: BrokerInterface
    alerts: AlertManager
    data: MarketDataService
    strategies: dict[str, Strategy]
    order_manager: OrderManager
    position_book: PositionBook
    reconciler: Reconciler
    breaker: CircuitBreaker
    pdt: PDTGuard
    settled: SettledCashGuard
    screener: Screener
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]


@dataclass
class ScanSummary:
    run_id: str
    as_of: date
    regime: RegimeResult | None = None
    eligible: list[str] = field(default_factory=list)
    excluded: dict[str, list[str]] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    halted: str | None = None
    insufficient_history: list[str] = field(default_factory=list)


@dataclass
class ManageSummary:
    run_id: str
    fills: int = 0
    stops_placed: int = 0
    stops_tightened: int = 0
    take_profits_placed: int = 0
    exits: list[str] = field(default_factory=list)
    expired_entries: int = 0
    pending_submitted: int = 0
    halted: str | None = None


class Engine:
    def __init__(self, deps: EngineDeps):
        self.d = deps
        s = deps.settings
        self.sizing = SizingParams(s.risk.risk_per_trade_pct, s.risk.max_position_pct, s.risk.cash_buffer_pct,
                                   s.risk.liquidity_cap_pct_of_adv, s.account.fractional_shares,
                                   s.risk.min_position_notional)
        self.limits = LimitsParams(s.risk.max_open_positions, s.risk.max_positions_per_strategy,
                                   s.risk.max_sector_exposure_pct, s.risk.correlation_lookback, s.risk.max_correlation,
                                   s.risk.max_correlated_holdings, s.risk.max_new_entries_per_day,
                                   s.risk.min_equity_to_trade)
        self.stops = StopParams(s.stops.atr_mult_initial, s.stops.hard_stop_pct, s.stops.stop_limit_offset_pct,
                                s.stops.trail_atr_mult, s.stops.trail_activation_r, s.stops.breakeven_r,
                                s.stops.breakeven_slippage_pct, tuple((t.r, t.fraction) for t in s.stops.take_profit),
                                s.stops.max_hold_days, s.stops.dead_money_sessions, s.stops.dead_money_r_band,
                                s.stops.gap_reprice_offset_pct, s.stops.gap_max_attempts)
        self.pricing = PricingParams(s.execution.entry_offset_pct, s.execution.max_chase_pct, s.execution.exit_offset_pct,
                                     s.execution.urgent_exit_offset_pct, s.execution.urgent_exit_max_offset_pct,
                                     s.execution.urgent_reprice_interval_sec, s.quotes.max_age_sec, s.quotes.max_spread_pct,
                                     s.execution.max_quote_vs_signal_close_pct)
        self.regime_params = RegimeParams(use_vix=s.regime.use_vix, vix_halt_level=s.regime.vix_halt_level,
                                          neutral_size_multiplier=s.regime.neutral_size_multiplier,
                                          neutral_min_score_bump=s.regime.neutral_min_score_bump)
        self._broker_open_orders: list[Order] | None = None
        self._bars_cache: dict[str, BarsResult] = {}

    # ================================================================== helpers
    @property
    def now(self) -> datetime:
        return self.d.clock()

    @property
    def mode(self) -> RunMode:
        return self.d.settings.mode

    def _warmup(self) -> int:
        return max((s.warmup_bars for s in self.d.strategies.values()), default=250)

    def _alert(self, sev: AlertSeverity, title: str, body: str = "") -> None:
        self.d.alerts.send(sev, title, body)

    def _check_kill_switch(self) -> None:
        if kill_switch_engaged(self.d.settings.paths.kill_switch_path):
            self._alert(AlertSeverity.WARNING, "kill switch engaged",
                        f"{self.d.settings.paths.kill_switch_path} exists; exiting after reconciliation without orders")
            raise CycleHalted("kill switch engaged")

    def _broker_open(self, refresh: bool = False) -> list[Order]:
        if self._broker_open_orders is None or refresh:
            self._broker_open_orders = self.d.broker.get_open_orders()
        return self._broker_open_orders

    def _quote(self, symbol: str) -> Quote | None:
        try:
            q = self.d.broker.get_quote(symbol)
        except BrokerError as exc:
            log.warning("%s: quote unavailable: %s", symbol, exc)
            return None
        self._paper_tick(symbol, q)
        return q

    def _paper_tick(self, symbol: str, quote: Quote) -> None:
        """In paper mode the simulated broker fills resting orders against each fresh quote."""
        proc = getattr(self.d.broker, "process_quote", None)
        if callable(proc):
            try:
                proc(symbol, quote)
            except Exception as exc:  # simulation bug must surface, not be swallowed
                log.error("paper fill simulation failed for %s: %s", symbol, exc, exc_info=True)
                raise

    def _bars(self, symbol: str, now: datetime) -> BarsResult:
        if symbol not in self._bars_cache:
            self._bars_cache[symbol] = self.d.data.load_bars(symbol, Timeframe.D1, self._warmup(), now)
        return self._bars_cache[symbol]

    def _current_atr(self, symbol: str, now: datetime, period: int = 14) -> float | None:
        res = self._bars(symbol, now)
        if res.df.empty or len(res.df) < period + 2:
            return None
        val = atr_indicator(res.df["high"], res.df["low"], res.df["close"], period).iloc[-1]
        return float(val) if pd.notna(val) else None

    def _reconcile_and_guard(self) -> AccountSnapshot:
        report = self.d.reconciler.reconcile()
        self._check_kill_switch()
        return report.account or self.d.broker.get_account()

    def _breaker(self, account: AccountSnapshot, now: datetime) -> BreakerDecision:
        decision = self.d.breaker.evaluate(account, now)
        if decision.liquidation_recommended:
            self._alert(AlertSeverity.CRITICAL, "max drawdown breached: liquidation recommended",
                        f"{decision.detail}\nRun `swingbot liquidate --confirm <N>` to flatten; entries are halted until `unhalt`.")
        elif not decision.entries_allowed:
            self._alert(AlertSeverity.WARNING, "circuit breaker: entries halted", decision.detail)
        return decision

    # ================================================================== reconcile
    def reconcile(self) -> Any:
        report = self.d.reconciler.reconcile()
        return report

    # ================================================================== scan
    def _reset_run_caches(self) -> None:
        self._broker_open_orders = None
        self._bars_cache = {}

    def _tick_open_order_symbols(self) -> None:
        """Fetch a quote for every symbol with a resting order (paper mode fills against it; live mode uses it
        for the gap check and logs it)."""
        for sym in sorted({o.symbol for o in self.d.repo.open_orders()}):
            self._quote(sym)

    def scan(self, run_id: str) -> ScanSummary:
        now = self.now
        self._reset_run_caches()
        as_of = self.d.cal.last_closed_session(now)
        summary = ScanSummary(run_id=run_id, as_of=as_of)
        try:
            account = self._reconcile_and_guard()
        except CycleHalted as exc:
            summary.halted = str(exc)
            return summary
        self.d.order_manager.sync_open_orders()
        decision = self._breaker(account, now)

        regime = self._regime(now)
        summary.regime = regime
        held = {p.symbol for p in self.d.repo.open_positions()}
        open_syms = {o.symbol for o in self._broker_open(refresh=True)} | {o.symbol for o in self.d.repo.open_orders()}
        universe = list(dict.fromkeys(self.d.settings.tradable_universe() + sorted(held)))
        recent = {r.signal.symbol for r in self.d.repo.signals_between(now - timedelta(days=10), now)}
        priority = sorted(held) + sorted(recent - held)
        results = self.d.data.load_many(universe, Timeframe.D1, self._warmup(), now, priority=priority)
        self._bars_cache.update(results)
        if any(r.halt for r in results.values()):
            bad = [s for s, r in results.items() if r.halt]
            self._alert(AlertSeverity.ERROR, "data quality halt", f"symbols with halt-level issues: {bad}")
            summary.halted = f"data quality halt: {bad}"
            return summary

        bars = {s: r.df for s, r in results.items()}
        issues = {s: r.issues for s, r in results.items()}
        allowed = set(self.d.settings.live_allowed_symbols) if self.d.settings.is_live else None
        screen = self.d.screener.run(bars, as_of, held, open_syms, issues, allowed)
        self.d.repo.save_screen(run_id, as_of, screen)
        summary.eligible, summary.excluded = list(screen.eligible), dict(screen.excluded)

        # ---- exits for held symbols (strategy exits) ---------------------------------------------------------
        for pos in self.d.repo.open_positions():
            res = results.get(pos.symbol)
            strat = self.d.strategies.get(pos.strategy_id)
            if res is None or strat is None or not res.usable:
                continue
            holding = Holding(entry_ts=pos.opened_at,
                              bars_held=self.d.cal.sessions_between(self.d.cal.session_date_of(pos.opened_at), as_of))
            try:
                sig = strat.generate_signals(res.df, holding=holding, as_of=now, cal=self.d.cal)[0]
            except (InsufficientHistory, AssertionError, ValueError) as exc:
                log.warning("%s: exit evaluation skipped: %s", pos.symbol, exc)
                continue
            if sig.type == SignalType.EXIT_LONG:
                self.d.repo.save_signal(sig, run_id)
                summary.signals.append(sig)
                order = self._execute_exit_signal(pos, sig, account, now)
                if order is not None:
                    summary.orders.append(order)
                else:
                    summary.deferred.append(pos.symbol)

        # ---- entries ----------------------------------------------------------------------------------------
        entries_today = len(self.d.repo.entry_orders_created_on(self.d.cal.session_date_of(now)))
        state = PortfolioState(equity=account.equity, open_positions=self.d.repo.open_positions(),
                               last_prices={}, entries_today=entries_today, sector_of=self.d.settings.sector_of,
                               returns={s: r.df["close"].pct_change() for s, r in results.items() if not r.df.empty})
        for p in state.open_positions:
            r = results.get(p.symbol)
            state.last_prices[p.symbol] = float(r.df["close"].iloc[-1]) if r is not None and not r.df.empty else p.avg_cost
        for symbol in screen.eligible:
            res = results[symbol]
            for strat in self.d.strategies.values():
                try:
                    sig = strat.generate_signals(res.df, min_score_bump=regime.min_score_bump, as_of=now, cal=self.d.cal)[0]
                except InsufficientHistory as exc:
                    log.info("INSUFFICIENT_HISTORY %s: %s", symbol, exc)
                    summary.insufficient_history.append(symbol)
                    continue
                except AssertionError as exc:
                    log.error("look-ahead guard tripped for %s: %s", symbol, exc)
                    continue
                if sig.type != SignalType.ENTRY_LONG:
                    continue
                existing = self.d.repo.get_signal(sig.id)
                if existing is not None and existing.outcome in (SignalOutcome.ORDERED, SignalOutcome.VETOED,
                                                                 SignalOutcome.SKIPPED_SIZE_ZERO):
                    log.info("%s: signal %s already processed (%s)", symbol, sig.id, existing.outcome.value)
                    continue
                self.d.repo.save_signal(sig, run_id)
                summary.signals.append(sig)
                if not regime.allow_new_entries:
                    self.d.repo.set_signal_outcome(sig.id, SignalOutcome.REGIME_BLOCKED, regime.detail)
                    continue
                if not decision.entries_allowed:
                    self.d.repo.set_signal_outcome(sig.id, SignalOutcome.HALTED, decision.detail)
                    continue
                order = self._execute_entry_signal(sig, res, account, regime, state, run_id, now)
                if order is not None:
                    summary.orders.append(order)
                elif self.d.repo.get_signal(sig.id).outcome == SignalOutcome.PENDING:
                    summary.deferred.append(symbol)
        self._sample_spreads(screen.eligible[:40], as_of)
        log.info("scan complete: %d eligible, %d signals, %d orders, %d deferred", len(summary.eligible),
                 len(summary.signals), len(summary.orders), len(summary.deferred))
        return summary

    def _regime(self, now: datetime) -> RegimeResult:
        rs = self.d.settings.regime
        spy = self.d.data.load_bars(rs.symbol, Timeframe.D1, 260, now)
        vix = None
        if rs.use_vix:
            v = self.d.data.load_bars(rs.vix_symbol, Timeframe.D1, 30, now)
            vix = v.df if not v.df.empty else None
        try:
            regime = classify_regime(spy.df, vix, self.regime_params, now)
        except ValueError as exc:
            self._alert(AlertSeverity.ERROR, "regime unavailable; treating as BEAR", str(exc))
            regime = RegimeResult(regime="BEAR", as_of=now, spy_close=0.0, ema50=0.0, ema200=0.0, size_multiplier=0.0,
                                  min_score_bump=1.0, allow_new_entries=False, detail=f"regime data error: {exc}")
        log.info("regime %s (%s) size x%.2f", regime.regime.value, regime.detail, regime.size_multiplier)
        self.d.repo.kv_set("last_regime", regime.model_dump_json())
        return regime

    def _sample_spreads(self, symbols: list[str], as_of: date) -> None:
        for s in symbols:
            q = self._quote(s)
            if q is not None and q.bid > 0 and q.ask > 0 and q.spread_pct < 1:
                self.d.repo.save_spread_sample(s, as_of, q.spread_pct)

    # ================================================================== entries
    def _execute_entry_signal(self, sig: Signal, res: BarsResult, account: AccountSnapshot, regime: RegimeResult,
                              state: PortfolioState, run_id: str, now: datetime) -> Order | None:
        decision = RiskDecision(signal_id=sig.id, symbol=sig.symbol, strategy_id=sig.strategy_id)
        stop = sig.suggested_stop if sig.suggested_stop else initial_hard_stop(sig.close, sig.atr, self.stops)
        stop = max(stop, initial_hard_stop(sig.close, sig.atr, self.stops))
        bp = self.d.settled.buying_power_for_entry(account, now)
        avg_vol = float(res.df["volume"].iloc[-20:].mean()) if not res.df.empty else 0.0
        sizing = compute_size(equity=account.equity, buying_power=bp, entry_price=sig.close, stop_price=stop,
                              avg_volume_20d=avg_vol, params=self.sizing, regime_multiplier=regime.size_multiplier)
        decision.sizing = sizing
        decision.checks.append(RiskCheck(name="sizing", passed=sizing.final_qty > 0,
                                         inputs={"binding": sizing.binding_constraint, "qty": sizing.final_qty}))
        if sizing.final_qty <= 0:
            decision.veto_reason = f"SKIPPED_SIZE_ZERO ({sizing.binding_constraint})"
            self.d.repo.save_risk_decision(decision, run_id)
            self.d.repo.set_signal_outcome(sig.id, SignalOutcome.SKIPPED_SIZE_ZERO, decision.veto_reason)
            return None
        notional = sizing.final_qty * sig.close
        checks = check_limits(sig.symbol, sig.strategy_id, notional, state, self.limits)
        decision.checks.extend(checks)
        failed = first_failure(checks)
        if failed:
            decision.veto_reason = f"limit:{failed}"
            self.d.repo.save_risk_decision(decision, run_id)
            self.d.repo.set_signal_outcome(sig.id, SignalOutcome.VETOED, decision.veto_reason)
            return None
        client_ref = make_client_ref(sig.symbol, sig.ts, Side.BUY, sig.strategy_id, "entry")
        pre = self._pre_trade_checks(sig.symbol, Side.BUY, client_ref, sig.close, now)
        decision.checks.extend(pre.checks)
        if pre.veto:
            if pre.deferrable:
                decision.veto_reason = None
                decision.final_qty = sizing.final_qty
                self.d.repo.save_risk_decision(decision, run_id)
                self.d.repo.set_signal_outcome(sig.id, SignalOutcome.PENDING, f"deferred to manage: {pre.veto}")
                self.d.repo.kv_set(f"pending_entry:{sig.id}", f"{sizing.final_qty}|{stop}")
                log.info("%s: entry deferred to manage pass (%s)", sig.symbol, pre.veto)
                return None
            decision.veto_reason = pre.veto
            self.d.repo.save_risk_decision(decision, run_id)
            self.d.repo.set_signal_outcome(sig.id, SignalOutcome.VETOED, pre.veto)
            return None
        limit, capped = entry_limit_price(pre.quote, sig.close, self.pricing)
        qty = sizing.final_qty
        if qty * limit > bp:
            qty = float(int(bp / limit)) if not self.d.settings.account.fractional_shares else bp / limit
        if qty <= 0 or qty * limit < self.sizing.min_position_notional:
            decision.veto_reason = "buying power at limit price below minimum notional"
            self.d.repo.save_risk_decision(decision, run_id)
            self.d.repo.set_signal_outcome(sig.id, SignalOutcome.SKIPPED_SIZE_ZERO, decision.veto_reason)
            return None
        decision.final_qty = qty
        self.d.repo.save_risk_decision(decision, run_id)
        req = OrderRequest(client_ref=client_ref, symbol=sig.symbol, side=Side.BUY, qty=qty, order_type=OrderType.LIMIT,
                           limit_price=limit, tif=TimeInForce.GFD, extended_hours=self.d.settings.execution.extended_hours,
                           reason=f"entry score={sig.score:.2f}{' chase-capped' if capped else ''}", signal_id=sig.id,
                           strategy_id=sig.strategy_id, purpose="entry")
        order = self._submit(req)
        if order is None:
            return None
        if order.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
            self.d.repo.set_signal_outcome(sig.id, SignalOutcome.VETOED, f"order {order.status.value}")
            return order
        self.d.repo.set_signal_outcome(sig.id, SignalOutcome.ORDERED, f"{order.client_ref} qty={qty} limit={limit}")
        settles = self.d.settled.funding_settles_at(account, qty * limit, now)
        if settles:
            self.d.repo.kv_set(f"settles:{client_ref}", settles.isoformat())
        state.register_pending(sig.symbol, sig.strategy_id, qty * limit)
        self._alert(AlertSeverity.INFO, f"entry queued {sig.symbol}",
                    f"qty {qty:g} limit {limit:.2f} stop {stop:.2f} score {sig.score:.2f} regime {regime.regime.value}")
        return order

    def _submit(self, req: OrderRequest, note: str = "") -> Order | None:
        try:
            return self.d.order_manager.submit(req, note)
        except BrokerSchemaDrift as exc:
            self._alert(AlertSeverity.ERROR, "schema drift: new orders halted this cycle", str(exc))
            raise CycleHalted(f"schema drift: {exc}") from exc
        except BrokerUnavailable as exc:
            self._alert(AlertSeverity.ERROR, "broker unavailable; cycle ended", str(exc))
            raise CycleHalted(str(exc)) from exc

    @dataclass
    class PreTrade:
        checks: list[RiskCheck]
        quote: Quote | None
        veto: str | None
        deferrable: bool = False

    def _pre_trade_checks(self, symbol: str, side: Side, client_ref: str, ref_price: float | None,
                          now: datetime, is_exit: bool = False) -> "Engine.PreTrade":
        checks: list[RiskCheck] = []
        ex = self.d.settings.execution
        today = self.d.cal.session_date_of(now)
        opens_soon = self.d.cal.opens_within(now, ex.queue_window_hours)
        next_open = self.d.cal.next_session_open(now) if not self.d.cal.is_open_at(now) else now
        target_day = self.d.cal.session_date_of(next_open)
        half_day = self.d.cal.is_early_close(target_day)
        ok_cal = opens_soon and (ex.trade_on_half_days or not half_day or is_exit)
        checks.append(RiskCheck(name="market_window", passed=ok_cal,
                                inputs={"opens_within_h": ex.queue_window_hours, "half_day": half_day, "today": str(today)}))
        broker_open = [o for o in self._broker_open() if o.symbol == symbol]
        if is_exit:
            broker_open = [o for o in broker_open if o.side == Side.BUY]
        checks.append(RiskCheck(name="no_open_order_at_broker", passed=not broker_open,
                                inputs={"open": [o.broker_id for o in broker_open]}))
        pos = self.d.repo.get_open_position(symbol)
        pos_ok = (pos is None) if side == Side.BUY else (pos is not None)
        checks.append(RiskCheck(name="position_state", passed=pos_ok, inputs={"has_position": pos is not None}))
        checks.append(RiskCheck(name="client_ref_unique", passed=not self.d.repo.has_active_ref(client_ref),
                                inputs={"client_ref": client_ref}))
        breaker_ok = not self.d.repo.get_breaker_state().halted or is_exit
        checks.append(RiskCheck(name="breakers_clear", passed=breaker_ok and not kill_switch_engaged(
            self.d.settings.paths.kill_switch_path), inputs={}))
        quote = self._quote(symbol)
        q_ok, q_reason = (quote_is_usable(quote, now, self.pricing) if quote else (False, "no quote"))
        if q_ok and ref_price and not is_exit:
            within, dev = price_within_signal(quote, ref_price, self.pricing)
            if not within:
                q_ok, q_reason = False, f"quote {dev:.2%} from signal close"
        checks.append(RiskCheck(name="quote_fresh_and_sane", passed=q_ok, inputs={"reason": q_reason}))
        veto = first_failure(checks)
        deferrable = veto == "quote_fresh_and_sane" and not self.d.cal.is_open_at(now)
        return Engine.PreTrade(checks, quote if q_ok else quote, veto, deferrable)

    # ================================================================== exits
    def _execute_exit_signal(self, pos: Position, sig: Signal, account: AccountSnapshot, now: datetime) -> Order | None:
        pdt = self.d.pdt.assess_sell(pos, account, now, broker_count=account.day_trades_used)
        gfv = self.d.settled.assess_sell(pos, account, now)
        if pdt.blocked or gfv.blocked:
            why = pdt.detail if pdt.blocked else gfv.detail
            self._alert(AlertSeverity.WARNING, f"exit blocked {pos.symbol}", why)
            self.d.repo.set_signal_outcome(sig.id, SignalOutcome.VETOED, why)
            return None
        client_ref = make_client_ref(pos.symbol, sig.ts, Side.SELL, pos.strategy_id, "exit")
        pre = self._pre_trade_checks(pos.symbol, Side.SELL, client_ref, None, now, is_exit=True)
        if pre.veto:
            if pre.deferrable or pre.veto == "quote_fresh_and_sane":
                self.d.repo.set_signal_outcome(sig.id, SignalOutcome.PENDING, f"exit deferred to manage: {pre.veto}")
                return None
            self.d.repo.set_signal_outcome(sig.id, SignalOutcome.VETOED, pre.veto)
            return None
        return self._place_exit(pos, pre.quote, ExitReason.STRATEGY_EXIT, "; ".join(sig.reasons), client_ref, sig.id,
                                urgent=False)

    def _place_exit(self, pos: Position, quote: Quote, reason: ExitReason, detail: str, client_ref: str,
                    signal_id: str | None, urgent: bool, attempt: int = 0) -> Order | None:
        """Cancel protective orders, then sell the full position at a (marketable) limit."""
        self._cancel_protective(pos, f"{reason.value}: exiting position")
        pos = self.d.repo.get_open_position(pos.symbol) or pos
        if pos.qty <= 0:
            return None
        if urgent:
            price, exhausted = urgent_exit_price(quote, attempt, self.pricing)
            if exhausted:
                return self._emergency_exit(pos, reason, detail)
        else:
            price = exit_limit_price(quote, self.pricing)
        req = OrderRequest(client_ref=client_ref, symbol=pos.symbol, side=Side.SELL, qty=pos.qty, order_type=OrderType.LIMIT,
                           limit_price=price, tif=TimeInForce.GFD, reason=f"{reason.value}: {detail} attempt={attempt}",
                           signal_id=signal_id, strategy_id=pos.strategy_id, purpose="exit")
        order = self._submit(req)
        if order is not None and order.status not in (OrderStatus.REJECTED, OrderStatus.FAILED):
            if signal_id:
                self.d.repo.set_signal_outcome(signal_id, SignalOutcome.ORDERED, order.client_ref)
            self._alert(AlertSeverity.INFO, f"exit order {pos.symbol} ({reason.value})",
                        f"qty {pos.qty:g} limit {price:.2f} urgent={urgent} attempt={attempt}: {detail}")
        return order

    def _emergency_exit(self, pos: Position, reason: ExitReason, detail: str) -> Order | None:
        self._alert(AlertSeverity.CRITICAL, f"EMERGENCY market exit {pos.symbol}", f"{reason.value}: {detail}")
        log.critical("emergency market order for %s qty %g (%s)", pos.symbol, pos.qty, reason.value)
        seq = len(self.d.repo.orders_for_symbol(pos.symbol, open_only=False))
        req = OrderRequest(client_ref=make_client_ref(pos.symbol, self.now, Side.SELL, pos.strategy_id, "emergency", seq),
                           symbol=pos.symbol, side=Side.SELL, qty=pos.qty, order_type=OrderType.MARKET, tif=TimeInForce.GFD,
                           reason=f"{reason.value}: EMERGENCY {detail}", strategy_id=pos.strategy_id, purpose="exit")
        return self._submit(req, note="EMERGENCY MARKET ORDER")

    def _cancel_protective(self, pos: Position, why: str) -> None:
        for ref in (pos.stop_order_ref, pos.tp_order_ref):
            if not ref:
                continue
            o = self.d.repo.get_order(ref)
            if o is not None and not o.status.is_terminal:
                self.d.order_manager.cancel(o, why)
        pos.stop_order_ref = None
        pos.tp_order_ref = None
        self.d.repo.save_position(pos)

    # ================================================================== manage
    def manage(self, run_id: str) -> ManageSummary:
        now = self.now
        self._reset_run_caches()
        summary = ManageSummary(run_id=run_id)
        try:
            account = self._reconcile_and_guard()
        except CycleHalted as exc:
            summary.halted = str(exc)
            return summary
        before = len(self.d.repo.fills_between(now - timedelta(days=30), now))
        if self.d.cal.is_open_at(now):
            self._tick_open_order_symbols()
        self.d.order_manager.sync_open_orders()
        summary.expired_entries = len(self.d.order_manager.expire_stale_entries())
        decision = self._breaker(account, now)
        market_open = self.d.cal.is_open_at(now)
        try:
            if market_open:
                summary.pending_submitted = self._submit_pending(account, decision, now)
            for pos in self.d.repo.open_positions():
                self._manage_position(pos, account, now, market_open, summary)
            self._reprice_urgent_exits(now, summary)
            self._close_fragments(account, now, market_open)
        except CycleHalted as exc:
            summary.halted = str(exc)
        self.d.order_manager.sync_open_orders()
        summary.fills = len(self.d.repo.fills_between(now - timedelta(days=30), now)) - before
        snap = self.d.broker.get_account()
        self.d.repo.save_snapshot(snap)
        log.info("manage complete: fills=%d stops placed=%d tightened=%d tp=%d exits=%s", summary.fills,
                 summary.stops_placed, summary.stops_tightened, summary.take_profits_placed, summary.exits)
        return summary

    def _submit_pending(self, account: AccountSnapshot, decision: BreakerDecision, now: datetime) -> int:
        """Entries/exits deferred from the scan because no usable quote existed after the close."""
        n = 0
        since = now - timedelta(days=4)
        regime_raw = self.d.repo.kv_get("last_regime")
        regime = RegimeResult.model_validate_json(regime_raw) if regime_raw else None
        for rec in self.d.repo.signals_between(since, now):
            if rec.outcome != SignalOutcome.PENDING:
                continue
            sig = rec.signal
            if sig.type == SignalType.ENTRY_LONG:
                if regime is None or not regime.allow_new_entries or not decision.entries_allowed:
                    self.d.repo.set_signal_outcome(sig.id, SignalOutcome.HALTED, "pending entry dropped: regime/breaker")
                    continue
                res = self._bars(sig.symbol, now)
                state = PortfolioState(equity=account.equity, open_positions=self.d.repo.open_positions(), last_prices={},
                                       entries_today=len(self.d.repo.entry_orders_created_on(self.d.cal.session_date_of(now))),
                                       sector_of=self.d.settings.sector_of)
                if self._execute_entry_signal(sig, res, account, regime, state, rec.run_id or "manage", now) is not None:
                    n += 1
            elif sig.type == SignalType.EXIT_LONG:
                pos = self.d.repo.get_open_position(sig.symbol)
                if pos is None:
                    self.d.repo.set_signal_outcome(sig.id, SignalOutcome.DUPLICATE, "position already closed")
                    continue
                if self._execute_exit_signal(pos, sig, account, now) is not None:
                    n += 1
        return n

    def _manage_position(self, pos: Position, account: AccountSnapshot, now: datetime, market_open: bool,
                         summary: ManageSummary) -> None:
        quote = self._quote(pos.symbol)
        if quote is None:
            return
        last = quote.last if quote.last > 0 else quote.mid
        pos = self.d.repo.get_open_position(pos.symbol) or pos  # paper fills may have changed it
        if pos.qty <= 0:
            return
        exit_open = [o for o in self.d.repo.orders_for_symbol(pos.symbol) if o.purpose == "exit" and o.side == Side.SELL]
        if exit_open:
            return  # an exit is already working; repricing handles it
        stop_order = self.d.repo.get_order(pos.stop_order_ref) if pos.stop_order_ref else None
        stop_live = stop_order is not None and not stop_order.status.is_terminal

        # ---- gap handling: market opened below the stop and the stop-limit did not fill ------------------------
        if market_open and stop_live and gap_below_stop(stop_order.stop_price, last):
            self._handle_gap(pos, stop_order, quote, summary)
            return

        # ---- time stop / dead money ---------------------------------------------------------------------------
        sessions_held = self.d.cal.sessions_between(self.d.cal.session_date_of(pos.opened_at), self.d.cal.session_date_of(now))
        reason = time_stop_reason(pos, now, sessions_held, pos.r_multiple(last), self.stops)
        if reason is not None and market_open:
            if self._sell_allowed(pos, account, now, protective=False):
                ref = make_client_ref(pos.symbol, now, Side.SELL, pos.strategy_id, reason.value.lower())
                if self._place_exit(pos, quote, reason, f"held {sessions_held} sessions", ref, None, urgent=True) is not None:
                    summary.exits.append(f"{pos.symbol}:{reason.value}")
            return

        # ---- trailing stop / break-even ratchet ---------------------------------------------------------------
        current_atr = self._current_atr(pos.symbol, now) or pos.atr_at_entry or 0.0
        if pos.hard_stop is None:  # adopted position without a stop
            pos.hard_stop = initial_hard_stop(pos.avg_cost, current_atr, self.stops)
            pos.initial_risk_per_share = max(pos.avg_cost - pos.hard_stop, 1e-6)
            pos.high_water_mark = pos.high_water_mark or max(pos.avg_cost, last)
        upd = update_trailing_stop(pos, last, current_atr, self.stops)
        if upd.changed:
            pos.high_water_mark = upd.new_high_water_mark
            if upd.new_stop is not None:
                pos.trailing_stop = upd.new_stop
                log.info("%s stop ratchet -> %.2f (%s)", pos.symbol, upd.new_stop, upd.reason)
            self.d.repo.save_position(pos)

        # ---- take-profit ladder -------------------------------------------------------------------------------
        tp_targets = pending_take_profits(pos, self.stops)
        tp_order = self.d.repo.get_order(pos.tp_order_ref) if pos.tp_order_ref else None
        tp_live = tp_order is not None and not tp_order.status.is_terminal
        tp_qty_reserved = tp_order.remaining_qty if tp_live else 0.0
        if tp_targets and not tp_live and tp_targets[0].qty < pos.qty - 1e-9 and self._sell_allowed(pos, account, now, protective=False, quiet=True):
            t = tp_targets[0]
            ref = make_client_ref(pos.symbol, pos.opened_at, Side.SELL, pos.strategy_id, f"tp{t.level}",
                                  len(self.d.repo.orders_for_symbol(pos.symbol, open_only=False)))
            req = OrderRequest(client_ref=ref, symbol=pos.symbol, side=Side.SELL, qty=t.qty, order_type=OrderType.LIMIT,
                               limit_price=max(t.price, exit_limit_price(quote, self.pricing)), tif=TimeInForce.GTC,
                               reason=f"TAKE_PROFIT: level={t.level} r={t.r_multiple}", strategy_id=pos.strategy_id,
                               purpose="take_profit")
            # the resting stop must shrink first so shares are not double-committed
            if stop_live and stop_order.remaining_qty > pos.qty - t.qty + 1e-9:
                self._replace_stop(pos, stop_order, pos.active_stop or stop_order.stop_price, pos.qty - t.qty, "shrink for take-profit")
                stop_order = self.d.repo.get_order(pos.stop_order_ref) if pos.stop_order_ref else None
                stop_live = stop_order is not None and not stop_order.status.is_terminal
            o = self._submit(req)
            if o is not None and o.status not in (OrderStatus.REJECTED, OrderStatus.FAILED):
                pos.tp_order_ref = o.client_ref
                self.d.repo.save_position(pos)
                tp_qty_reserved = t.qty
                summary.take_profits_placed += 1

        # ---- protective stop: ensure present and at the active level ------------------------------------------
        desired_stop = pos.active_stop
        protect_qty = max(0.0, pos.qty - tp_qty_reserved)
        if desired_stop is None or protect_qty <= 0:
            return
        if not stop_live:
            if self._place_stop(pos, desired_stop, protect_qty):
                summary.stops_placed += 1
        else:
            cur_stop = stop_order.stop_price or 0.0
            if desired_stop > cur_stop + 1e-6 or abs(stop_order.remaining_qty - protect_qty) > 1e-6:
                if self._replace_stop(pos, stop_order, desired_stop, protect_qty, upd.reason if upd.new_stop else "qty sync"):
                    summary.stops_tightened += 1

    def _sell_allowed(self, pos: Position, account: AccountSnapshot, now: datetime, protective: bool,
                      quiet: bool = False) -> bool:
        pdt = self.d.pdt.assess_sell(pos, account, now, broker_count=account.day_trades_used, is_protective_stop=protective)
        gfv = self.d.settled.assess_sell(pos, account, now)
        if pdt.would_be_day_trade and protective and pdt.day_trades_used >= self.d.pdt.max_day_trades:
            self._alert(AlertSeverity.WARNING, f"PDT: protective stop on {pos.symbol} may count as a day trade", pdt.detail)
        if pdt.blocked or gfv.blocked:
            if not quiet:
                self._alert(AlertSeverity.WARNING, f"sell blocked {pos.symbol}", pdt.detail if pdt.blocked else gfv.detail)
            return False
        return True

    def _place_stop(self, pos: Position, stop: float, qty: float) -> bool:
        trigger, limit = stop_prices(stop, self.stops.stop_limit_offset_pct)
        seq = len([o for o in self.d.repo.orders_for_symbol(pos.symbol, open_only=False) if o.purpose == "stop"])
        ref = make_client_ref(pos.symbol, pos.opened_at, Side.SELL, pos.strategy_id, "stop", seq)
        req = OrderRequest(client_ref=ref, symbol=pos.symbol, side=Side.SELL, qty=qty, order_type=OrderType.STOP_LIMIT,
                           stop_price=trigger, limit_price=limit, tif=TimeInForce.GTC,
                           reason=f"{'TRAILING_STOP' if pos.trailing_stop else 'HARD_STOP'}: protective stop",
                           strategy_id=pos.strategy_id, purpose="stop")
        retries = self.d.settings.execution.stop_placement_retries
        for attempt in range(retries):
            o = self._submit(req)
            if o is not None and o.status not in (OrderStatus.REJECTED, OrderStatus.FAILED, OrderStatus.UNKNOWN):
                pos.stop_order_ref = o.client_ref
                pos.stop_placement_failures = 0
                self.d.repo.save_position(pos)
                log.info("%s protective stop placed: trigger %.2f limit %.2f qty %g", pos.symbol, trigger, limit, qty)
                return True
            if o is not None and o.status == OrderStatus.UNKNOWN:
                pos.stop_order_ref = o.client_ref
                self.d.repo.save_position(pos)
                return False
            req = req.model_copy(update={"client_ref": make_client_ref(pos.symbol, pos.opened_at, Side.SELL, pos.strategy_id,
                                                                       "stop", seq + attempt + 1)})
        pos.stop_placement_failures += 1
        self.d.repo.save_position(pos)
        self._alert(AlertSeverity.CRITICAL, f"stop placement failed for {pos.symbol}",
                    f"{retries} attempts failed; exiting position at marketable limit")
        q = self._quote(pos.symbol)
        if q is not None:
            self._place_exit(pos, q, ExitReason.STOP_PLACEMENT_FAILED, "could not place protective stop",
                             make_client_ref(pos.symbol, self.now, Side.SELL, pos.strategy_id, "nostop"), None, urgent=True)
        return False

    def _replace_stop(self, pos: Position, old: Order, stop: float, qty: float, why: str) -> bool:
        trigger, limit = stop_prices(stop, self.stops.stop_limit_offset_pct)
        seq = len([o for o in self.d.repo.orders_for_symbol(pos.symbol, open_only=False) if o.purpose == "stop"])
        new_req = OrderRequest(client_ref=make_client_ref(pos.symbol, pos.opened_at, Side.SELL, pos.strategy_id, "stop", seq),
                               symbol=pos.symbol, side=Side.SELL, qty=qty, order_type=OrderType.STOP_LIMIT, stop_price=trigger,
                               limit_price=limit, tif=TimeInForce.GTC, reason=f"TRAILING_STOP: {why}",
                               strategy_id=pos.strategy_id, purpose="stop")
        try:
            cancelled, new = self.d.order_manager.cancel_replace(old, new_req, why)
        except (BrokerSchemaDrift, BrokerUnavailable) as exc:
            raise CycleHalted(str(exc)) from exc
        if new is None:
            if cancelled.status == OrderStatus.FILLED:
                log.warning("%s: stop filled during replace; position closing via fills", pos.symbol)
            else:
                pos.stop_order_ref = cancelled.client_ref if not cancelled.status.is_terminal else None
                self.d.repo.save_position(pos)
            return False
        pos.stop_order_ref = new.client_ref
        self.d.repo.save_position(pos)
        return new.status not in (OrderStatus.REJECTED, OrderStatus.FAILED)

    def _handle_gap(self, pos: Position, stop_order: Order, quote: Quote, summary: ManageSummary) -> None:
        pos.gap_attempts += 1
        self.d.repo.save_position(pos)
        self._alert(AlertSeverity.WARNING, f"gap below stop {pos.symbol}",
                    f"last {quote.last:.2f} < stop {stop_order.stop_price:.2f}; attempt {pos.gap_attempts}")
        self.d.order_manager.cancel(stop_order, "gap below stop: converting to marketable limit")
        pos.stop_order_ref = None
        self.d.repo.save_position(pos)
        if pos.gap_attempts > self.stops.gap_max_attempts:
            self._emergency_exit(pos, ExitReason.GAP_STOP, f"{pos.gap_attempts - 1} marketable-limit attempts failed")
            summary.exits.append(f"{pos.symbol}:GAP_EMERGENCY")
            return
        price = gap_exit_price(quote, self.stops.gap_reprice_offset_pct)
        ref = make_client_ref(pos.symbol, self.now, Side.SELL, pos.strategy_id, "gap", pos.gap_attempts)
        req = OrderRequest(client_ref=ref, symbol=pos.symbol, side=Side.SELL, qty=pos.qty, order_type=OrderType.LIMIT,
                           limit_price=price, tif=TimeInForce.GFD, reason=f"GAP_STOP: gap below stop attempt={pos.gap_attempts - 1}",
                           strategy_id=pos.strategy_id, purpose="exit")
        if self._submit(req) is not None:
            summary.exits.append(f"{pos.symbol}:GAP_STOP")

    def _reprice_urgent_exits(self, now: datetime, summary: ManageSummary) -> None:
        interval = timedelta(seconds=self.pricing.urgent_reprice_interval_sec)
        for o in self.d.repo.open_orders():
            if o.purpose != "exit" or o.order_type != OrderType.LIMIT or "attempt=" not in (o.reason or ""):
                continue
            if o.submitted_at is None or now - o.submitted_at < interval or not self.d.cal.is_open_at(now):
                continue
            o = self.d.order_manager.refresh(o)
            if o.status.is_terminal:
                continue
            pos = self.d.repo.get_open_position(o.symbol)
            quote = self._quote(o.symbol)
            if pos is None or quote is None:
                continue
            attempt = int(o.reason.split("attempt=")[1].split()[0]) + 1
            head = o.reason.split(":")[0]
            reason = ExitReason(head) if head in ExitReason.__members__ else ExitReason.MANUAL
            price, exhausted = urgent_exit_price(quote, attempt, self.pricing)
            if exhausted:
                self.d.order_manager.cancel(o, "urgent exit exhausted; escalating")
                self._emergency_exit(pos, reason, "urgent limit repricing exhausted")
                summary.exits.append(f"{o.symbol}:EMERGENCY")
                continue
            new_req = OrderRequest(client_ref=make_client_ref(o.symbol, now, Side.SELL, pos.strategy_id, "exit", attempt),
                                   symbol=o.symbol, side=Side.SELL, qty=o.remaining_qty, order_type=OrderType.LIMIT,
                                   limit_price=price, tif=TimeInForce.GFD,
                                   reason=f"{reason.value}: reprice attempt={attempt}", strategy_id=pos.strategy_id, purpose="exit")
            self.d.order_manager.cancel_replace(o, new_req, f"urgent reprice #{attempt}")

    def _close_fragments(self, account: AccountSnapshot, now: datetime, market_open: bool) -> None:
        if not market_open:
            return
        for pos in self.d.repo.open_positions():
            quote = self._quote(pos.symbol)
            if quote is None:
                continue
            notional = pos.qty * quote.last
            has_buy = any(o.side == Side.BUY for o in self.d.repo.orders_for_symbol(pos.symbol))
            if notional < self.sizing.min_position_notional and not has_buy and pos.qty > 0:
                if self._sell_allowed(pos, account, now, protective=False):
                    ref = make_client_ref(pos.symbol, now, Side.SELL, pos.strategy_id, "fragment")
                    self._place_exit(pos, quote, ExitReason.FRAGMENT_CLOSE, f"notional {notional:.2f} below minimum", ref,
                                     None, urgent=True)

    # ================================================================== liquidate
    def liquidate(self, confirm_count: int) -> list[Order]:
        now = self.now
        self._reset_run_caches()
        account = self.d.reconciler.reconcile().account
        positions = self.d.broker.get_positions()
        if confirm_count != len(positions):
            raise ValueError(f"--confirm {confirm_count} does not match live position count {len(positions)}")
        self._alert(AlertSeverity.CRITICAL, "LIQUIDATION STARTED", f"{len(positions)} positions, equity {account.equity if account else 'n/a'}")
        log.critical("liquidation started for %d positions", len(positions))
        for o in self.d.order_manager.sync_open_orders():
            if not o.status.is_terminal:
                self.d.order_manager.cancel(o, "liquidation", wait=True)
        orders: list[Order] = []
        for bp in positions:
            pos = self.d.repo.get_open_position(bp.symbol) or Position(symbol=bp.symbol, qty=bp.qty, avg_cost=bp.avg_cost,
                                                                        opened_at=bp.opened_at, strategy_id="external")
            pos.qty = bp.qty
            quote = self._quote(bp.symbol)
            order: Order | None = None
            for attempt in range(2):
                if quote is None:
                    break
                price, _ = urgent_exit_price(quote, attempt, self.pricing)
                req = OrderRequest(client_ref=make_client_ref(bp.symbol, now, Side.SELL, pos.strategy_id, "liquidate", attempt),
                                   symbol=bp.symbol, side=Side.SELL, qty=bp.qty, order_type=OrderType.LIMIT, limit_price=price,
                                   tif=TimeInForce.GFD, reason=f"LIQUIDATE: attempt={attempt}", strategy_id=pos.strategy_id,
                                   purpose="liquidate")
                order = self._submit(req, note="liquidation")
                if order is None or order.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
                    continue
                self.d.sleep(self.d.settings.execution.fill_poll_interval_sec)
                order = self.d.order_manager.refresh(order)
                if order.status == OrderStatus.FILLED:
                    break
                self.d.order_manager.cancel(order, "liquidation reprice")
                quote = self._quote(bp.symbol) or quote
            if order is None or order.status != OrderStatus.FILLED:
                order = self._emergency_exit(pos, ExitReason.LIQUIDATE, "marketable limits did not fill")
            if order is not None:
                orders.append(order)
        self._alert(AlertSeverity.CRITICAL, "LIQUIDATION SUBMITTED", "\n".join(f"{o.symbol} {o.status.value}" for o in orders))
        return orders
