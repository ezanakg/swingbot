# swingbot — context for Claude sessions

Swing trading bot for a Robinhood account, built from `docs/ENGINEERING_PROMPT.md` (the spec, sections 0–7).
Full history of the first build session: `docs/session-1-transcript.md`. README.md describes behaviour and ops.
`docs/GO_LIVE.md` is the runbook for taking it live on the user's own machine.

## State as of 2026-10-02 (end of session 3)
- All spec modules implemented; no stubs. 103 tests pass offline: `pytest`.
- Session 2 added a second live adapter for Robinhood's official agentic-trading surface (hosted Trading MCP
  server + OAuth, orders reach the dedicated Agentic account only). It is the default (`broker.adapter:
  robinhood_mcp`); the robin-stocks adapter remains as `robin_stocks`. Also added `swingbot preflight`
  (read-only first-contact check) and the go-live runbook.
- A separate LOCAL operator session runs the bot against the real Agentic account (••••2155, limited_margin,
  ~$64 equity) and commits small fixes; cloud sessions review them and build the larger changes. Preflight and
  auth have run for real; no live order has been placed yet as of this session.
- Session 3: reviewed the operator's fixes (auth run kind, cache backfill), hardened the cache (backfill merges
  instead of replacing; malformed metadata tolerated), added honest run kinds for every command and the explicit
  `BROKER_LOGIN_KINDS` set, end-to-end CLI tests through `cli.main` in paper and live (fake MCP) modes, the
  `small_account` risk profile (`config/profiles/`, `risk_profile:` / `SWINGBOT_RISK_PROFILE`) and
  `swingbot suggest-allowlist`.
- Tool/field names for the MCP adapter come from a captured `tools/list` dated 2026-09-28 and have now been
  confirmed by the operator's preflight (`broker tools: all present`).
- Live data comes from Robinhood: quotes always via the broker; bars via `data.providers: robinhood` (default
  since session 3), with yfinance as the automatic fallback (index symbols such as `^VIX`, unknown tickers, broker
  errors on the data path raise `ProviderError`, never a bare `BrokerError`). Paper mode uses yfinance.
- Default is paper mode. Never set MODE=live, never add credentials, never place real orders from a cloud session.

## Setup
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
pytest
```

## Architecture rules (keep them)
- `swingbot/broker/robinhood.py` is the ONLY module that imports `robin_stocks`.
- `swingbot/broker/robinhood_mcp.py` is the ONLY module that speaks MCP or names `agent.robinhood.com` tools;
  `swingbot/broker/mcp_auth.py` is the only module that knows the OAuth endpoints.
- Dependencies point inward: strategy/data/risk never import execution or broker; only `cli.py` and `app.py` import execution.
- Strategies are pure (no I/O, no clock); signals use closed bars only (`assert_last_bar_closed`).
- Every order gets a deterministic client_ref written to SQLite before submission (idempotency); on Robinhood the
  same value seeds the server-side `ref_id` (uuid5) in both adapters.
- No bare `except:`, no market orders outside the emergency path, secrets never logged.

## Deliberate deviations from the spec (documented in README)
- Queue window ignores weekends/holidays so Friday scans can queue Monday entries.
- Protective stop covers `qty - take-profit qty` while a take-profit rests (Robinhood reserves shares per sell order).
- Entries/exits without a usable after-hours quote are deferred to the 09:35 manage pass.
- The MCP surface has no PDT counter; the adapter approximates day trades from filled orders and the PDT guard
  takes the larger of that and its own fill-based count.

## Conventions added in session 3
- Every CLI command has its own `RunKind` (the `runs` table and heartbeat record what actually ran). Commands that
  need a broker session before their handler are listed in `cli.BROKER_LOGIN_KINDS`; `auth` must never be in it.
- Risk profiles are overlays in `config/profiles/<name>.yaml` applied by `load_settings`; keep `settings.yaml` as
  the standard account and put size-specific tuning in a profile.
- New CLI behaviour gets a test through `cli.main` (see `tests/unit/test_cli_e2e.py`), not only a handler test.
- Symbols are yfinance/universe.yaml form everywhere in the bot (share classes with a dash: BRK-B). Only the MCP
  adapter converts to and from Robinhood's dot form (BRK.B) at the wire via `to_broker_symbol`/`from_broker_symbol`.

## Ideas for next sessions
- Watch the operator's first live fills: confirm the stop quantity and the `ref_id` idempotency on the real server.
- Run `swingbot backtest` on real yfinance history and tune parameters with `--walk-forward`.
- Add a mypy/ruff pass and CI (GitHub Actions running pytest).
- Paper-trade for several weeks via the cron schedule in `ops/` before any live use.
