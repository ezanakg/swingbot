"""Typed settings: YAML tunables + environment secrets, validated with pydantic. Fails fast on invalid config.

Secrets are never part of ``repr``/``str``/model dumps of ``Settings`` and are read from the environment only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from swingbot.enums import AccountType, AlertSeverity, IssueAction, RunMode, Timeframe

LIVE_ACK_PHRASE = "I_UNDERSTAND_THE_RISKS"
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^]{1,10}$")


class ConfigError(ValueError):
    """Raised for any invalid or unsafe configuration. The process must not start when this is raised."""


def _check_pct(name: str, v: float, lo: float = 0.0, hi: float = 1.0, allow_zero: bool = False) -> float:
    if (v < lo) or (v > hi) or (not allow_zero and v == 0):
        raise ValueError(f"{name} must be in ({lo}, {hi}]; got {v}")
    return v


class AccountConfig(BaseModel):
    type: AccountType = AccountType.MARGIN
    fractional_shares: bool = False
    pdt_equity_threshold: float = 25000.0


class PathsConfig(BaseModel):
    data_dir: Path = Path("var/data")
    db_path: Path = Path("var/swingbot.sqlite3")
    log_dir: Path = Path("var/logs")
    lock_dir: Path = Path("var/locks")
    heartbeat_path: Path = Path("var/heartbeat.json")
    kill_switch_path: Path = Path("var/KILL_SWITCH")
    session_dir: Path = Path("var/session")

    def resolved(self, root: Path) -> "PathsConfig":
        def r(p: Path) -> Path:
            return p if p.is_absolute() else (root / p)

        return PathsConfig(
            data_dir=r(self.data_dir),
            db_path=r(self.db_path),
            log_dir=r(self.log_dir),
            lock_dir=r(self.lock_dir),
            heartbeat_path=r(self.heartbeat_path),
            kill_switch_path=r(self.kill_switch_path),
            session_dir=r(self.session_dir),
        )


class QualityActions(BaseModel):
    missing_sessions: IssueAction = IssueAction.SKIP_SYMBOL
    nan_values: IssueAction = IssueAction.SKIP_SYMBOL
    ohlc_sanity: IssueAction = IssueAction.SKIP_SYMBOL
    stale_data: IssueAction = IssueAction.SKIP_SYMBOL
    split_detected: IssueAction = IssueAction.SKIP_SYMBOL
    duplicates: IssueAction = IssueAction.WARN


class QualityConfig(BaseModel):
    max_missing_sessions_60: int = 2
    max_zero_volume_pct: float = 0.01
    stale_sessions: int = 2
    split_move_pct: float = 0.40
    actions: QualityActions = Field(default_factory=QualityActions)


class DataConfig(BaseModel):
    providers: dict[str, str] = Field(default_factory=lambda: {"1d": "yfinance", "1h": "yfinance"})
    fallback_provider: str = "yfinance"
    lookback_buffer: int = 30
    cache_ttl_daily_after_et: str = "16:10"
    cache_ttl_4h_minutes: int = 15
    overlap_revalidate_bars: int = 5
    quality: QualityConfig = Field(default_factory=QualityConfig)

    @field_validator("providers")
    @classmethod
    def _providers(cls, v: dict[str, str]) -> dict[str, str]:
        for tf, name in v.items():
            Timeframe(tf)
            if name not in ("robinhood", "yfinance"):
                raise ValueError(f"unknown data provider '{name}' for timeframe {tf}")
        return v

    @field_validator("cache_ttl_daily_after_et")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError("cache_ttl_daily_after_et must be HH:MM")
        return v


class QuotesConfig(BaseModel):
    max_age_sec: int = 60
    max_spread_pct: float = 0.005


class ScreeningConfig(BaseModel):
    min_avg_dollar_volume: float = 20_000_000
    min_price: float = 5.0
    max_price: float = 2000.0
    max_median_spread_pct: float = 0.0025
    atr_pct_min: float = 0.01
    atr_pct_max: float = 0.08
    earnings_sessions_before: int = 5
    earnings_sessions_after: int = 1
    exclude_leveraged_etfs: bool = True
    spread_history_days: int = 20

    @model_validator(mode="after")
    def _ranges(self) -> "ScreeningConfig":
        if self.min_price >= self.max_price:
            raise ValueError("screening.min_price must be < max_price")
        if self.atr_pct_min >= self.atr_pct_max:
            raise ValueError("screening.atr_pct_min must be < atr_pct_max")
        return self


class RegimeConfig(BaseModel):
    symbol: str = "SPY"
    vix_symbol: str = "^VIX"
    use_vix: bool = True
    vix_halt_level: float = 35.0
    neutral_size_multiplier: float = 0.5
    neutral_min_score_bump: float = 0.1


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.08
    cash_buffer_pct: float = 0.10
    liquidity_cap_pct_of_adv: float = 0.005
    max_open_positions: int = 6
    max_positions_per_strategy: int = 4
    max_sector_exposure_pct: float = 0.30
    correlation_lookback: int = 60
    max_correlation: float = 0.80
    max_correlated_holdings: int = 2
    max_new_entries_per_day: int = 2
    min_equity_to_trade: float = 500.0
    min_position_notional: float = 200.0

    @model_validator(mode="after")
    def _pcts(self) -> "RiskConfig":
        _check_pct("risk.risk_per_trade_pct", self.risk_per_trade_pct, hi=0.10)
        _check_pct("risk.max_position_pct", self.max_position_pct)
        _check_pct("risk.cash_buffer_pct", self.cash_buffer_pct, allow_zero=True)
        _check_pct("risk.liquidity_cap_pct_of_adv", self.liquidity_cap_pct_of_adv)
        _check_pct("risk.max_sector_exposure_pct", self.max_sector_exposure_pct)
        if self.max_open_positions < 1 or self.max_positions_per_strategy < 1:
            raise ValueError("position caps must be >= 1")
        return self


class TakeProfitLevel(BaseModel):
    r: float = Field(gt=0)
    fraction: float = Field(gt=0, le=1)


class StopsConfig(BaseModel):
    atr_mult_initial: float = 2.0
    hard_stop_pct: float = 0.06
    stop_limit_offset_pct: float = 0.005
    trail_atr_mult: float = 2.5
    trail_activation_r: float = 1.0
    breakeven_r: float = 1.5
    breakeven_slippage_pct: float = 0.002
    take_profit: list[TakeProfitLevel] = Field(default_factory=lambda: [TakeProfitLevel(r=2.0, fraction=0.5)])
    max_hold_days: int = 30
    dead_money_sessions: int = 15
    dead_money_r_band: float = 0.5
    gap_reprice_offset_pct: float = 0.003
    gap_max_attempts: int = 2

    @model_validator(mode="after")
    def _tp(self) -> "StopsConfig":
        if sum(level.fraction for level in self.take_profit) > 1.0 + 1e-9:
            raise ValueError("stops.take_profit fractions must sum to <= 1")
        return self


class CircuitBreakerConfig(BaseModel):
    daily_loss_halt_pct: float = 0.03
    weekly_loss_halt_pct: float = 0.06
    weekly_halt_sessions: int = 5
    consecutive_loss_halt: int = 4
    max_drawdown_halt_pct: float = 0.15


class ExecutionConfig(BaseModel):
    entry_offset_pct: float = 0.001
    max_chase_pct: float = 0.01
    exit_offset_pct: float = 0.001
    urgent_exit_offset_pct: float = 0.003
    urgent_exit_max_offset_pct: float = 0.01
    urgent_reprice_interval_sec: int = 120
    entry_ttl_minutes: int = 90
    queue_window_hours: float = 20.0
    trade_on_half_days: bool = False
    extended_hours: bool = False
    max_quote_vs_signal_close_pct: float = 0.03
    fill_poll_interval_sec: int = 60
    manage_poll_duration_sec: int = 0
    cancel_confirm_timeout_sec: int = 30
    stop_placement_retries: int = 3
    submitting_adoption_window_min: int = 10


class RetryConfig(BaseModel):
    base_sec: float = 1.0
    factor: float = 2.0
    max_attempts: int = 5
    cap_sec: float = 60.0


class AdapterBreakerConfig(BaseModel):
    failures: int = 10
    window_sec: int = 300
    open_sec: int = 600


class BrokerConfig(BaseModel):
    rate_limit_rps: float = 1.5
    rate_limit_burst: int = 10
    cost_weights: dict[str, int] = Field(
        default_factory=lambda: {"historicals": 2, "quotes": 1, "orders": 1, "account": 1}
    )
    retry: RetryConfig = Field(default_factory=RetryConfig)
    breaker: AdapterBreakerConfig = Field(default_factory=AdapterBreakerConfig)
    auth_approval_timeout_sec: int = 120
    request_timeout_sec: int = 20


class PaperConfig(BaseModel):
    starting_cash: float = 10_000.0
    slippage_bps: float = 5.0
    partial_fill_prob: float = 0.10
    seed: int = 42


class TelegramConfig(BaseModel):
    enabled: bool = False


class DiscordConfig(BaseModel):
    enabled: bool = False


class SmtpConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 587
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    use_tls: bool = True


class AlertsConfig(BaseModel):
    min_severity: AlertSeverity = AlertSeverity.WARNING
    dedupe_window_sec: int = 900
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    level: str = "INFO"
    json_format: bool = Field(default=True, alias="json")
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 10


class HeartbeatConfig(BaseModel):
    max_age_hours: float = 30.0


class UniverseConfig(BaseModel):
    watchlist: list[str]
    robinhood_watchlist: str | None = None
    leveraged_etfs: list[str] = Field(default_factory=list)
    leveraged_etf_whitelist: list[str] = Field(default_factory=list)
    sectors: dict[str, str] = Field(default_factory=dict)

    @field_validator("watchlist", "leveraged_etfs", "leveraged_etf_whitelist")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            s2 = str(s).strip().upper()
            if not _SYMBOL_RE.match(s2):
                raise ValueError(f"invalid symbol '{s}'")
            if s2 not in out:
                out.append(s2)
        return out

    @model_validator(mode="after")
    def _nonempty(self) -> "UniverseConfig":
        if not self.watchlist:
            raise ValueError("universe.watchlist must not be empty")
        return self


class Secrets(BaseModel):
    """Environment-only secrets. ``repr`` is redacted; never log this object."""

    model_config = ConfigDict(frozen=True)

    rh_username: str | None = None
    rh_password: str | None = None
    rh_totp_secret: str | None = None
    session_enc_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        present = [k for k, v in self.__dict__.items() if v]
        return f"Secrets(present={present})"

    __str__ = __repr__

    def values_to_redact(self) -> list[str]:
        return [v for v in self.__dict__.values() if isinstance(v, str) and len(v) >= 4]


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    mode: RunMode = RunMode.PAPER
    config_dir: Path
    root_dir: Path
    account: AccountConfig = Field(default_factory=AccountConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    quotes: QuotesConfig = Field(default_factory=QuotesConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    stops: StopsConfig = Field(default_factory=StopsConfig)
    circuit_breakers: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    strategies: list[str] = Field(default_factory=lambda: ["ema_rsi_macd"])
    strategy_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    universe: UniverseConfig
    secrets: Secrets = Field(default_factory=Secrets, exclude=True, repr=False)
    live_allowed_symbols: list[str] = Field(default_factory=list)
    live_ack: bool = False

    @property
    def is_live(self) -> bool:
        return self.mode == RunMode.LIVE

    def tradable_universe(self) -> list[str]:
        """Watchlist restricted to the live allowlist when live."""
        if self.is_live:
            return [s for s in self.universe.watchlist if s in set(self.live_allowed_symbols)]
        return list(self.universe.watchlist)

    def sector_of(self, symbol: str) -> str | None:
        return self.universe.sectors.get(symbol.upper())

    @model_validator(mode="after")
    def _live_guards(self) -> "Settings":
        if self.mode == RunMode.LIVE:
            if not self.live_ack:
                raise ValueError(
                    f"MODE=live requires LIVE_TRADING_ACK={LIVE_ACK_PHRASE} in the environment; refusing to start"
                )
            if not self.live_allowed_symbols:
                raise ValueError("MODE=live requires a non-empty LIVE_ALLOWED_SYMBOLS list; refusing to start")
            missing = [s for s in self.live_allowed_symbols if s not in set(self.universe.watchlist)]
            if missing:
                raise ValueError(f"LIVE_ALLOWED_SYMBOLS not in universe watchlist: {missing}; refusing to start")
            if not (self.secrets.rh_username and self.secrets.rh_password and self.secrets.rh_totp_secret):
                raise ValueError("MODE=live requires RH_USERNAME, RH_PASSWORD and RH_TOTP_SECRET; refusing to start")
        if self.strategies and any(s not in self.strategy_params for s in self.strategies):
            missing = [s for s in self.strategies if s not in self.strategy_params]
            raise ValueError(f"strategy config file(s) missing under config/strategies/: {missing}")
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _env(env: Mapping[str, str], key: str) -> str | None:
    v = env.get(key)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _parse_symbol_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[,\s]+", raw.strip()):
        if part:
            p = part.upper()
            if p not in out:
                out.append(p)
    return out


def load_settings(
    config_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    dotenv_path: Path | str | None = None,
) -> Settings:
    """Load + validate settings.

    Precedence: process environment > .env file > YAML. Raises ``ConfigError`` on any problem.
    """
    base_env: dict[str, str] = dict(os.environ if env is None else env)
    cfg_dir = Path(config_dir or base_env.get("SWINGBOT_CONFIG_DIR") or "config")
    if not cfg_dir.is_absolute():
        cfg_dir = Path.cwd() / cfg_dir
    root = cfg_dir.parent
    dot = Path(dotenv_path) if dotenv_path else root / ".env"
    if dot.exists():
        for k, v in dotenv_values(dot).items():
            if v is not None and k not in base_env:
                base_env[k] = v

    try:
        raw = _read_yaml(cfg_dir / "settings.yaml")
        universe_raw = _read_yaml(cfg_dir / "universe.yaml")
        sectors_path = cfg_dir / "sectors.yaml"
        sectors_raw = _read_yaml(sectors_path).get("sectors", {}) if sectors_path.exists() else {}
        universe_raw.setdefault("sectors", {})
        universe_raw["sectors"] = {str(k).upper(): str(v) for k, v in {**sectors_raw, **universe_raw["sectors"]}.items()}

        strat_dir = cfg_dir / "strategies"
        strategy_params: dict[str, dict[str, Any]] = {}
        if strat_dir.exists():
            for f in sorted(strat_dir.glob("*.yaml")):
                params = _read_yaml(f)
                params.setdefault("type", f.stem)
                params.setdefault("id", f"{f.stem}_v1")
                strategy_params[f.stem] = params

        mode = RunMode((_env(base_env, "MODE") or raw.get("mode") or "paper").lower())
        secrets = Secrets(
            rh_username=_env(base_env, "RH_USERNAME"),
            rh_password=_env(base_env, "RH_PASSWORD"),
            rh_totp_secret=_env(base_env, "RH_TOTP_SECRET"),
            session_enc_key=_env(base_env, "SESSION_ENC_KEY"),
            telegram_bot_token=_env(base_env, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env(base_env, "TELEGRAM_CHAT_ID"),
            discord_webhook_url=_env(base_env, "DISCORD_WEBHOOK_URL"),
            smtp_username=_env(base_env, "SMTP_USERNAME"),
            smtp_password=_env(base_env, "SMTP_PASSWORD"),
        )

        paths_raw = dict(raw.get("paths") or {})
        for env_key, field in (
            ("SWINGBOT_DB_PATH", "db_path"),
            ("SWINGBOT_DATA_DIR", "data_dir"),
            ("SWINGBOT_LOG_DIR", "log_dir"),
            ("KILL_SWITCH_PATH", "kill_switch_path"),
            ("RH_SESSION_DIR", "session_dir"),
        ):
            v = _env(base_env, env_key)
            if v:
                paths_raw[field] = v
        paths = PathsConfig(**paths_raw).resolved(root)

        broker_raw = dict(raw.get("broker") or {})
        approval = _env(base_env, "AUTH_APPROVAL_TIMEOUT_SEC")
        if approval:
            broker_raw["auth_approval_timeout_sec"] = int(approval)

        logging_raw = dict(raw.get("logging") or {})
        lvl = _env(base_env, "LOG_LEVEL")
        if lvl:
            logging_raw["level"] = lvl.upper()

        settings = Settings(
            mode=mode,
            config_dir=cfg_dir,
            root_dir=root,
            account=AccountConfig(**(raw.get("account") or {})),
            paths=paths,
            data=DataConfig(**(raw.get("data") or {})),
            quotes=QuotesConfig(**(raw.get("quotes") or {})),
            screening=ScreeningConfig(**(raw.get("screening") or {})),
            regime=RegimeConfig(**(raw.get("regime") or {})),
            risk=RiskConfig(**(raw.get("risk") or {})),
            stops=StopsConfig(**(raw.get("stops") or {})),
            circuit_breakers=CircuitBreakerConfig(**(raw.get("circuit_breakers") or {})),
            execution=ExecutionConfig(**(raw.get("execution") or {})),
            broker=BrokerConfig(**broker_raw),
            paper=PaperConfig(**(raw.get("paper") or {})),
            alerts=AlertsConfig(**(raw.get("alerts") or {})),
            logging=LoggingConfig(**logging_raw),
            heartbeat=HeartbeatConfig(**(raw.get("heartbeat") or {})),
            strategies=list(raw.get("strategies") or ["ema_rsi_macd"]),
            strategy_params=strategy_params,
            universe=UniverseConfig(**universe_raw),
            secrets=secrets,
            live_allowed_symbols=_parse_symbol_list(_env(base_env, "LIVE_ALLOWED_SYMBOLS")),
            live_ack=(_env(base_env, "LIVE_TRADING_ACK") == LIVE_ACK_PHRASE),
        )
    except ConfigError:
        raise
    except (ValueError, TypeError) as exc:  # pydantic ValidationError is a ValueError
        raise ConfigError(f"invalid configuration: {exc}") from exc
    return settings
