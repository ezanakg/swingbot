# Going live: swingbot on a Robinhood Agentic account

This is the runbook for taking swingbot from paper to real money. Every step below runs **on your own
machine**, not in a cloud session: live mode needs a browser sign-in, an encrypted credential on local disk,
and a scheduler that survives longer than a container. The project rule in `CLAUDE.md` stands: cloud sessions
never set `MODE=live`, never hold credentials, never place orders.

Read "What has and has not been verified" before you fund anything.

## 1. Which Robinhood product this uses

Robinhood ships two agent features in 2026. They are different products and this bot uses the first one.

| Product | What it is | How swingbot relates to it |
| --- | --- | --- |
| **Agentic Trading** (third-party agents) | Robinhood's official, supported surface for external agents: a hosted Trading MCP server at `agent.robinhood.com/mcp/trading`, OAuth sign-in in your browser, and a dedicated **Agentic brokerage account** that is the only account the agent may trade. Launched May 2026 for stocks and ETFs; crypto added August 2026. | swingbot's default live adapter (`broker.adapter: robinhood_mcp`, module `swingbot/broker/robinhood_mcp.py`). swingbot is the agent: it does the screening, signals, sizing, stops and reconciliation, and uses the MCP server purely as the execution venue and data source. |
| **Robinhood Agents** (in-app) | A native feature announced September 2026: you pick an LLM inside the Robinhood app, give it a mandate, and it researches and trades for you. | Separate from swingbot. If you also enable it, give it its **own** Agentic account (Robinhood allows up to ten). Two decision-makers on one account defeat swingbot's risk limits and its reconciler will keep adopting the other agent's orders as "external". |

The older adapter (`broker.adapter: robin_stocks`) uses the unofficial web API with your username, password
and TOTP seed. Keep it as a fallback only: it is not a surface Robinhood supports for agents.

## 2. What the Agentic account gives you (and what it costs)

* **Ring-fenced budget.** You choose how much cash to move into the Agentic account. swingbot sizes against
  that account's equity and buying power only; it reads your other accounts but cannot trade them.
* **Trade approvals.** The account has an in-app setting that makes the agent wait for your tap on every order.
  It defaults to **on**. swingbot cannot run unattended with it on: entries would expire at their TTL and,
  more importantly, protective stops would sit unapproved while the position is unprotected. Turn approvals
  **off** for this agent once preflight is clean, and compensate with a small budget and a short allowlist.
* **Notifications.** Robinhood notifies you of every order the agent places. Keep them on; they are your
  independent audit trail of what the bot did.
* **Disconnect.** You can revoke the agent's access in the app at any time. swingbot's next run then fails
  authentication (exit code 3) and alerts; nothing else happens.
* **Risk is yours.** Robinhood's terms put the outcome of agent trades on the account holder.

## 3. Prerequisites

1. A Robinhood individual brokerage account in good standing with agentic trading available (it rolled out
   in beta; not every account has it yet). Setup needs a desktop browser.
2. Python 3.11 on the machine that will run the schedule (a Mac mini, a home server, a small VPS). It needs a
   browser **once**, for the sign-in. Headless servers: sign in on a laptop, then copy `var/session/` and the
   same `SESSION_ENC_KEY` to the server. Only one machine may use the credential at a time (refresh tokens are
   single-use; two users poison each other).
3. This repository checked out on that machine, with the tests passing:

   ```bash
   git clone <your fork> swingbot && cd swingbot
   python3.11 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt && pip install -e .
   pytest
   ```

## 4. Open and fund the Agentic account

In the Robinhood app: open an Agentic account, name the agent (for example `swingbot`), move a **small**
amount of cash into it (your first-week budget; the bot refuses to trade below `risk.min_equity_to_trade`,
500 USD by default), and leave trade approvals on for now. Note the last four digits of the new account
number; you will see them again in preflight.

## 5. Configure

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # -> SESSION_ENC_KEY
```

Edit `.env`:

```
MODE=paper                                   # stays paper until step 8
SESSION_ENC_KEY=<the key you just generated>
RH_SESSION_DIR=var/session
RH_ADAPTER=robinhood_mcp                     # default; shown for clarity
# RH_AGENTIC_ACCOUNT=1234                    # only if you have more than one agentic account
LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS      # required by `swingbot auth` and by live mode
LIVE_ALLOWED_SYMBOLS=AAPL,MSFT               # start with one or two liquid names from config/universe.yaml
```

No username, password or TOTP seed is needed for the MCP adapter. In `config/settings.yaml` set
`risk.max_open_positions: 1` and `risk.max_new_entries_per_day: 1` for the first week.

## 6. Sign in once (`swingbot auth`)

`swingbot auth` runs the browser flow: it registers swingbot as an OAuth client with Robinhood, opens your
browser at Robinhood's sign-in, and waits on a loopback listener for the redirect. Approve the agent
connection in the browser. The resulting token pair is stored, Fernet-encrypted, under `RH_SESSION_DIR`.

```bash
MODE=live swingbot auth
```

Scheduled runs refresh the token themselves (it rotates on every refresh and lives for days). If the bot
sits idle longer than the refresh lifetime, or you revoke the agent in the app, the next run exits with code
3 and the alert says to re-run `swingbot auth`.

## 7. First contact: `swingbot preflight`

Preflight logs in non-interactively and exercises every **read** path the bot uses, then stops. It never
submits or cancels anything.

```bash
MODE=live swingbot preflight --review AAPL
```

Check the output line by line:

* `account:` shows `••••` plus the last four digits of your **Agentic** account and `agentic_allowed: True`.
  If it names another account, stop and set `RH_AGENTIC_ACCOUNT`.
* `broker tools: all present`. If tools are missing, Robinhood renamed something; do not go live, open an
  issue with the line.
* `account snapshot:` equity and buying power match the app.
* `quotes:` each symbol has a bid, ask and last. After the close they are usually "NOT usable now"; that is
  expected and the bot defers to the 09:35 manage pass.
* `bars:` and `earnings:` return rows.
* `review (dry run, nothing placed):` Robinhood's own pre-trade simulation of a one-share limit buy.
  `order_checks={}` means no warnings; anything else (buying power, PDT, instrument restrictions) is printed.

Run preflight again during market hours so you see usable quotes at least once.

## 8. Go live with a small budget

1. Set `MODE=live` in `.env`. The bot refuses to start without the acknowledgement phrase, a non-empty
   allowlist that is a subset of the watchlist, and `SESSION_ENC_KEY`.
2. In the app, turn trade approvals **off** for the swingbot agent.
3. Run the cycle by hand once before scheduling it:

   ```bash
   swingbot reconcile      # broker is truth: pulls positions, open orders, balances into the local DB
   swingbot scan           # after the close: screens, signals, sizes, queues entries for the next open
   swingbot manage         # during the next session: fills, stops, take-profits, exits
   swingbot status
   swingbot report
   ```

4. The first real order is the one thing offline tests cannot prove. When `manage` reports a fill, open the
   app and confirm three things: the position is in the Agentic account, a stop-limit sell is resting for it,
   and `swingbot status` shows the same quantities. If any of these differ, create the kill-switch file
   (`touch var/KILL_SWITCH`), run `swingbot reconcile`, and read the log before continuing.
5. Install the schedule from `ops/crontab.example` (or the systemd timer / Windows task). The cron lines run
   `scan` after the close and `manage` through the session, each under the instance lock.

## 9. Operating it

| Need | Do |
| --- | --- |
| Stop all new orders immediately | `touch var/KILL_SWITCH` (every mode exits after reconciliation) |
| Pause entries, keep managing exits | `swingbot halt --reason "..."`; resume with `swingbot unhalt --reason "..."` |
| Flatten everything (manual, emergency) | `swingbot liquidate --confirm` |
| See what the bot thinks | `swingbot status`; `swingbot report` |
| Re-check the broker connection | `swingbot preflight` |
| Re-authenticate | `swingbot auth` (needed after revoking the agent or a long idle gap) |
| Grow the budget | Move more cash into the Agentic account in the app, then widen `LIVE_ALLOWED_SYMBOLS` and `risk.max_open_positions` one step at a time |

Circuit breakers (daily loss, five-session drawdown, consecutive losers, peak-to-trough drawdown) persist in
the database and halt entries on their own; the consecutive-loser and drawdown breakers need `swingbot
unhalt` to clear.

## 10. What has and has not been verified

Verified offline, in this repository's tests:

* The MCP adapter's request shapes, argument mapping (limit, stop-limit, market, time in force, session,
  whole-share quantities, deterministic `ref_id`), response parsing, state mapping, pagination, after-hours
  quote selection, earnings parsing, and the day-trade approximation.
* The transport's handling of throttling, token refresh on 401, session loss (404), and ambiguous
  failures on order placement (which land the order in `UNKNOWN` for the reconciler, never in a retry loop).
* The OAuth flow end to end against fake endpoints: registration, PKCE, loopback callback, state check,
  code exchange, refresh rotation, rejected refresh, and the encrypted store.
* The settings gates and that `swingbot preflight` places nothing.

Not verified, because no cloud session may hold credentials or place orders:

* Any call against the real `agent.robinhood.com` server. The tool names and field names come from the
  server's own `tools/list` as captured by a third-party client on 2026-09-28, and from Robinhood's
  published description of the OAuth flow. Preflight's tool check and the adapter's field validation are
  the guards: anything renamed fails loudly before an order is placed.
* The exact lifetime of access and refresh tokens (reported as several days). The adapter refreshes within
  an hour of nominal expiry and on any 401, so a shorter lifetime only costs a refresh.
* Whether Robinhood's server honours `ref_id` idempotency across retries exactly as documented. The order
  manager's `UNKNOWN` state plus reconciliation covers the case where it does not.
* Fill quality. Paper fills are simulated; expect live fills to be worse by the spread.

The honest expectation for the first live week: the bot places a handful of small limit orders, you check
each one in the app, and you keep the kill switch one keystroke away.

## Sources

* [Robinhood: "Robinhood is Now Open to Agents"](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/)
* [CNBC, 27 May 2026: AI agents can now trade for you on Robinhood](https://www.cnbc.com/2026/05/27/your-ai-agent-can-now-trade-for-you-on-robinhood-and-buy-stuff-with-your-credit-card-too.html)
* [CNBC, 30 Sep 2026: Robinhood unveils weekend hours and in-app AI agents](https://www.cnbc.com/2026/09/30/robinhood-unveils-weekend-hours-ai-agents-to-allow-users-to-trade-nonstop.html)
* [PYMNTS: Robinhood lets AI agents trade without customer sign-off](https://www.pymnts.com/news/investment-tracker/2026/robinhood-lets-ai-agents-trade-without-customer-sign-off/)
* [Finder: Robinhood agentic accounts](https://www.finder.com/stock-trading/robinhood-agentic-accounts)
* [robinhood-for-agents (third-party client whose captured `tools/list` and OAuth client informed this adapter)](https://github.com/kevin1chun/robinhood-for-agents)
