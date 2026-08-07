# Alpaca 24/7 Crypto Trading Bot

A production-grade algorithmic crypto trading bot built on the official
[`alpaca-py`](https://github.com/alpacahq/alpaca-py) SDK. It trades a
trend-following pullback strategy on 15-minute bars against **Alpaca Paper
Trading** (`https://paper-api.alpaca.markets`), enforces take-profit and
stop-loss in Python, persists state across restarts, and reconciles against the
broker on every cycle.

> **This is educational software, not financial advice.** Run it on paper only
> until you have read every line, run the backtest, and understand the risks
> documented at the bottom of this file.

---

## Strategy

| Component | Rule |
|---|---|
| Universe | `BTC/USD`, `ETH/USD`, `SOL/USD` (configurable) |
| Timeframe | 15-minute bars, UTC |
| Trend filter | `close > SMA(200)` |
| Pullback entry | `RSI(14) < 42` **AND** `abs(close - EMA(20)) / EMA(20) <= 0.5%` |
| Direction | Long only |
| Take profit | `+5.0%` from the actual average fill price |
| Stop loss | `-2.0%` from the actual average fill price |
| Max concurrent positions | `2` |
| Risk per trade | `1%` of capital, sized off the 2% stop distance |
| Max total exposure | `20%` of capital across all open positions |
| Minimum order | `$10` notional (smaller sizes are skipped, not submitted) |

Both entry conditions must be true on the most recent closed bar. Exits are
evaluated every cycle against the latest price.

---

## Architecture

```
config.py             Env/dotenv config + boot-time validation
logger.py             Console + rotating file logging (crypto_bot.log)
state_manager.py      Atomic positions.json read/write + broker reconciliation
market_data.py        CryptoHistoricalDataClient -> clean pandas DataFrames
strategy.py           SMA/EMA/RSI indicators + BUY / NEUTRAL signal
risk_engine.py        Position sizing, exposure cap, min-notional, precision
alpaca_execution.py   TradingClient: account, positions, market orders, fill polling
main.py               24/7 loop: reconcile -> manage exits -> scan entries -> sleep
backtest.py           Standalone walk-forward backtest of the same strategy

.github/workflows/    Free scheduled execution via GitHub Actions cron
render.yaml           Render Worker blueprint (paid host)
```


Data flow per cycle:

```
broker positions ──► reconcile ──► positions.json
                                        │
latest prices ──────────────────────────┼──► TP/SL check ──► market SELL ──► update state
                                        │
15-min bars ──► indicators ──► signal ──┴──► risk sizing ──► market BUY ──► save state
```

---

## Setup

### 1. Clone / open the project and create a virtual environment

Windows (cmd):

```bat
cd "f:\user\Documents\Project\crypto trading"
python -m venv venv
venv\Scripts\activate
```

macOS / Linux:

```bash
cd "/path/to/crypto trading"
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

`pandas-ta` is **not** a dependency, deliberately. It has been withdrawn from
PyPI (`pip install pandas-ta` now fails with "No matching distribution found"),
and the last release imported `numpy.NaN`, which numpy 2.x removed. Neither
matters: `strategy.py` detects whether pandas-ta is importable and otherwise
uses equivalent built-in pandas implementations — SMA, EMA and a proper
Wilder-smoothed RSI — producing the same values. If you want pandas-ta anyway,
install it yourself with `pip install "numpy<2" pandas-ta`.

`pandas` is pinned to `<3.0` because pandas 3.x is a breaking major release
that has not been validated against this code.


### 3. Configure credentials

```bat
copy .env.example .env        :: Windows
```

```bash
cp .env.example .env          # macOS/Linux
```

Then open `.env` and paste your **paper** keys from
<https://app.alpaca.markets/paper/dashboard/overview>:

```
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
```

`.env` is already in `.gitignore`. Never commit it. If you ever paste a key into
a file that is not `.env`, rotate the key immediately.

---

## Step 1 (required): Backtest before you run anything live

**Do not skip this.** Validate the strategy on historical data before letting
it touch an account, even a paper one.

```bash
python backtest.py --start 2024-01-01 --end 2025-01-01
```

Useful variations:

```bash
# Single symbol, show every trade
python backtest.py --start 2024-01-01 --end 2025-01-01 --symbols BTC/USD --show-trades

# Different timeframe and starting capital
python backtest.py --start 2024-06-01 --end 2024-12-01 --timeframe 1Hour --capital 25000

# Zero-cost run to isolate strategy edge from fees
python backtest.py --start 2024-01-01 --end 2025-01-01 --fee-pct 0 --slippage-pct 0
```

The backtester walks forward bar by bar, feeding the strategy only the data
available at that point (no look-ahead), and applies the same +5% / -2% exits.
It prints a per-symbol table and an aggregate summary: trade count, win rate,
total return, profit factor, and max drawdown.

Backtest assumptions, stated plainly:

- Entries fill at the signal bar's close plus slippage.
- Exits fill exactly at the TP/SL level, minus slippage.
- When a single bar touches both the stop and the target, the **stop** is
  assumed to trigger first (worst case).
- Default costs: 0.25% fee per side, 0.05% slippage per side.

Review the output. **If the backtest shows zero trades, a negative return, or a
drawdown you would not tolerate, fix the strategy before continuing.** A common
outcome is very few trades: requiring price within 0.5% of the EMA(20) *and*
RSI below 42 *while* above the SMA(200) is a narrow filter.

---

## Step 2: Run the bot on paper

```bash
python main.py
```

What happens on startup:

1. Config is validated. Missing API keys raise an explicit `RuntimeError`.
2. `positions.json` is reconciled against `TradingClient.get_all_positions()`.
   **Alpaca is always the source of truth**: positions open at the broker but
   missing locally are adopted (with TP/SL derived from the broker's average
   entry), and local records with no matching broker position are dropped. Every
   mismatch is logged as a warning.
3. The 15-minute loop begins.

Each cycle:

- Heartbeat log with cash, equity, deployed capital and position count.
- Reconcile with the broker again (not just at boot).
- Check every open position for TP/SL, selling with a plain GTC market order.
- Scan the watchlist for entries while under both the position count cap and
  the exposure cap.
- Sleep the remainder of the interval. Any exception is logged and the loop
  keeps running.

Stop it with `Ctrl+C`. The bot finishes the current cycle, then exits. **It does
not liquidate on shutdown** - open positions stay open at the broker and stay
recorded in `positions.json`, ready to be picked up on the next start.

### Watching it

```bash
# Windows
type crypto_bot.log
powershell -Command "Get-Content crypto_bot.log -Wait -Tail 40"

# macOS/Linux
tail -f crypto_bot.log
```

Set `LOG_LEVEL=DEBUG` in `.env` for indicator-level detail.

---

## Configuration reference

Every value below can be overridden in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `ALPACA_API_KEY` | – | **Required.** Paper API key ID |
| `ALPACA_SECRET_KEY` | – | **Required.** Paper API secret |
| `ALPACA_PAPER` | `true` | `false` points at live money. Leave it `true` |
| `WATCHLIST` | `BTC/USD,ETH/USD,SOL/USD` | Comma-separated pairs |
| `BAR_TIMEFRAME` | `15Min` | Bar size |
| `LOOP_INTERVAL_SECONDS` | `900` | Cycle interval |
| `BAR_LIMIT` | `250` | Bars fetched per analysis (must exceed `SMA_PERIOD`) |
| `SMA_PERIOD` | `200` | Trend filter length |
| `EMA_PERIOD` | `20` | Pullback anchor length |
| `RSI_PERIOD` | `14` | RSI length |
| `RSI_BUY_THRESHOLD` | `42` | RSI must be below this |
| `EMA_PROXIMITY_PCT` | `0.005` | Max distance from EMA(20) |
| `TAKE_PROFIT_PCT` | `0.05` | +5% target |
| `STOP_LOSS_PCT` | `0.02` | -2% stop |
| `MAX_POSITIONS` | `2` | Concurrent position cap |
| `MAX_RISK_PER_TRADE` | `0.01` | 1% of capital risked per trade |
| `MAX_TOTAL_EXPOSURE_PCT` | `0.20` | 20% total deployed capital cap |
| `MIN_NOTIONAL_USD` | `10` | Skip anything smaller |
| `QTY_PRECISION` | `4` | Decimal places for quantities |
| `ORDER_POLL_TIMEOUT_SECONDS` | `60` | How long to wait for a fill |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### How sizing actually works

With `$10,000` cash, a 1% risk budget is `$100`. Divided by the 2% stop distance
that implies a `$5,000` notional - half the account. The exposure cap is what
keeps that sane: total deployed capital may never exceed 20% (`$2,000`), so the
first position is capped at `$2,000` and the second gets whatever headroom is
left. Order size is then capped again by real available cash (with a 0.5%
buffer) and finally rounded to 4 decimals. If what remains is under `$10`, the
trade is skipped with a logged reason.

---

## Git & GitHub

The repository is safe to publish: `.env`, `positions.json`, `*.log`,
`__pycache__/` and `venv/` are all ignored.

> **This project must be its own repository, with these files at the root.**
> GitHub Actions only runs workflows found at `.github/workflows/` **at the
> repository root**. If this folder is a subdirectory of some other repo, the
> workflow path becomes `<subfolder>/.github/workflows/trade.yml` and Actions
> will silently never run it — no error, just nothing happening.
>
> Check where you are first:
>
> ```bash
> git rev-parse --show-toplevel   # should print THIS folder
> git rev-parse --show-prefix     # should print nothing (empty = you're at the root)
> ```
>
> If `--show-toplevel` prints a parent directory, move this folder somewhere
> outside that repo before running the commands below.

```bash
git init
git add .
git status                 # CONFIRM .env and positions.json are NOT listed
                           # and that .github/workflows/trade.yml IS listed
git commit -m "Add Alpaca 24/7 crypto trading bot"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

After pushing, confirm on GitHub that `.github/workflows/trade.yml` appears at
the top level of the file listing, not nested inside another folder.

If a secret ever does land in a commit, rotate the key in the Alpaca dashboard
first; rewriting history is not enough on its own, because the value has
already been exposed.


---

## Deployment (24/7 operation)

Something has to run the bot on a schedule, because exits are only checked
while it is running. Two shapes work:

- **Always-on process** (`python main.py`) — a real loop that sleeps between
  cycles. Needs a host that stays up: VPS, Render Worker, your own PC.
- **Scheduled one-shot** (`python main.py --once`) — a single cycle per
  invocation, driven by an external cron. **This is the free route**, via
  GitHub Actions. Jump to
  [Free option: GitHub Actions](#free-option-github-actions-no-host-no-cost).

If you have no budget, use the GitHub Actions option — it is genuinely free and
already wired up.

### Linux (systemd)

`/etc/systemd/system/crypto-bot.service`:


```ini
[Unit]
Description=Alpaca Crypto Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/crypto-trading
ExecStart=/opt/crypto-trading/venv/bin/python /opt/crypto-trading/main.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-bot
sudo journalctl -u crypto-bot -f
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-u", "main.py"]
```

```bash
docker build -t crypto-bot .
docker run -d --name crypto-bot --restart unless-stopped \
  --env-file .env -v "$(pwd)/positions.json:/app/positions.json" crypto-bot
```

Mount `positions.json` so state survives container restarts. Pass secrets with
`--env-file`; do not bake them into the image.

### Windows

```bat
venv\Scripts\python.exe main.py
```

For unattended operation, register a Task Scheduler task with trigger "At
startup", action `venv\Scripts\python.exe main.py`, working directory set to the
project folder, and "Restart if the task fails" enabled.

### Free option: GitHub Actions (no host, no cost)

**This is the recommended path if you don't want to pay for anything.**

The bot supports `--once`, which runs a single cycle and exits. Because every
run reconciles against Alpaca first (the broker is the source of truth), it
needs no long-lived process and no persistent disk — so a free scheduler can
drive it. `.github/workflows/trade.yml` is included and ready:

1. Push the repo to GitHub.
2. **Settings → Secrets and variables → Actions → New repository secret**, and
   add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. Secrets are encrypted and are
   never visible in the repo, logs, or to forks.
3. **Actions** tab → enable workflows. The workflow fires every 30 minutes; each
   delivered run executes **6 cycles at 5-minute intervals** internally, so
   in-run coverage is 25 of the 30 minutes rather than 1 check per half hour.
4. Hit **Run workflow** once manually to confirm it works before trusting the
   schedule.

Run it locally the same way:

```bash
python main.py --once          # one cycle, then exit
python main.py --cycles 6      # six cycles, then exit
python main.py --interval 300  # 5-minute gap between cycles
```

The honest caveats:

- **Keep the repo public**, or enlarge the cron. Public repos get unlimited
  free Actions minutes. A 30-minute schedule on a private repo still exceeds
  the 2,000-minute limit, so if you need this setup on a private repo set the
  cron to hourly or wider.
- **GitHub cron is best-effort.** Runs are routinely delayed 5–15+ minutes and
  can be skipped entirely when the platform is under load.
- **The `*/15` slot is the single most oversubscribed schedule on GitHub and
  most of those slots are silently dropped.** That is why the workflow now uses
  `*/30` with internal batching — you get far more *actual* deliveries from a
  30-minute slot than by asking for 15. Each delivered run covers the gap with
  multiple cycles. If a run is dropped, the gap is 60 minutes worst-case
  instead of 240 minutes under the old config.
- **`internal server error` / `job was not acquired by a runner`** are
  GitHub-side capacity failures, not bugs in this code. The install step retries
  three times to absorb the common flavour; a whole-job failure just means that
  cycle did not happen. Re-run it from the Actions tab if you want.
- **Scheduled workflows auto-disable after 60 days** of repo inactivity. GitHub
  emails you; re-enable from the Actions tab.
- State is carried between runs via the Actions cache on a best-effort basis.
  If it is evicted, nothing breaks — the bot rebuilds TP/SL from Alpaca's
  average entry price on the next cycle.


Other genuinely free options: **Oracle Cloud Always Free** gives you a real
always-on VM (the best free choice, but sign-up is picky), and **your own PC**
works if it truly never sleeps. Note that Render, Railway and Fly no longer
offer a free tier that suits an always-on worker.

### Render (paid, ~$7/mo)

A `render.yaml` blueprint is included. Push to GitHub, then in Render:

**New → Blueprint → select the repo**, and set `ALPACA_API_KEY` /
`ALPACA_SECRET_KEY` in the dashboard when prompted (they are marked
`sync: false` so they are never stored in the repo).

Render works, with caveats you should understand before choosing it:

- **It must be a Worker, not a Web Service.** This bot has no HTTP server. If
  you deploy it as a web service, Render's health check finds no open port and
  restarts it forever.
- **The free tier is not usable for this.** Free plans do not include
  background workers, and free web services sleep when idle. A sleeping bot is
  a bot that is not watching your stop-losses. Starter (~$7/mo) is the
  realistic minimum.
- **The filesystem is ephemeral.** Without the mounted disk in `render.yaml`,
  `positions.json` is wiped on every deploy. The bot survives this because it
  reconciles against Alpaca at boot, but the original TP/SL anchors are lost
  and rebuilt from the broker's average entry price. The blueprint mounts a
  1 GB disk at `/var/data` and points `POSITIONS_FILE` there.
- **`autoDeploy` is off by default** in the blueprint. You do not want a
  routine `git push` to restart a process that is currently holding positions.
- Render sends `SIGTERM` on shutdown; `main.py` handles it and exits cleanly
  between cycles rather than mid-order.

### Honest comparison

| Option | Cost | Verdict |
|---|---|---|
| **GitHub Actions + `--once`** | **free** | **Best free choice, already configured.** Cron is best-effort so timing drifts. Fine for paper |
| Oracle Cloud Always Free VM | free | A real always-on VM with a real disk. Best free option technically; sign-up can be a hassle |
| Your own PC | free | Only if it genuinely never sleeps. Laptop sleep = unmonitored stops |
| Small VPS (Hetzner/DigitalOcean) + systemd | ~$4-6/mo | Best value once you are paying. Real disk, no cold starts, full control |
| Render Worker (Starter) | ~$7/mo | Easiest git-push deploy with managed restarts. Slightly more than a VPS for less control |
| Railway / Fly.io | ~$5/mo | Equivalent to Render; Fly needs a volume for state |
| AWS Lambda / EventBridge | ~free | Would work with `--once`, but needs packaging and external state. GitHub Actions gets you there with no effort |

Short version: with no budget, use GitHub Actions — it is set up and costs
nothing. If you later want punctual 15-minute cycles, a cheap VPS is the best
value. What matters more than the host is that something keeps running: the
stop-loss only exists while this code executes.


---

## Known risks and limitations

Read this section. It is the honest part.

### 1. The stop-loss is not guaranteed to trigger at -2%

Alpaca **rejects `OrderClass.BRACKET`, OCO and OTO for crypto**, so there is no
resting stop order at the exchange. This bot monitors price in Python and sends
a market sell once the level is breached.

Consequences:

- The check happens **once every 15 minutes**. Between checks, price can move
  arbitrarily far. A gap or fast dump can leave you exiting at -8% or worse when
  the intended stop was -2%.
- The exit is a **market order**, so the fill is whatever the book gives you,
  not the stop price.
- If the process is stopped, crashed, offline, or the machine sleeps, **no stops
  are being monitored at all**.
- Alpaca API outages or market-data gaps can cause a cycle to skip a symbol. The
  bot logs it and moves on rather than dying, which means a missed check.
- **On GitHub Actions this is worse.** Cron there is best-effort: cycles are
  routinely delayed 5–15+ minutes and are occasionally skipped, so the real gap
  between stop-loss checks can stretch well past 15 minutes.

The `-2%` figure is an intention, not a guarantee. Shorten
`LOOP_INTERVAL_SECONDS` for tighter monitoring (at the cost of more API calls),
and run on infrastructure that restarts the process automatically. This is one
of several reasons to keep this bot on paper trading.


### 2. Small-account behavior

With the 20% exposure cap, small balances produce order sizes below the `$10`
minimum notional, and the bot will skip trades and log why. Example: a `$100`
account has a `$20` exposure budget total, so a second position often cannot be
opened at all. Very high-priced assets can also round to a zero quantity at 4
decimals. Neither case is a bug - it is the risk limits doing their job. Fund
the paper account with enough simulated capital (`$10,000`+) to see meaningful
behavior.

### 3. Partial fills

Market orders can fill partially. The bot polls order status after submission
and records the **actual** filled quantity and average fill price, so TP/SL
levels are computed from what really happened. If an exit fills partially, the
remaining quantity stays tracked and is retried on the next cycle.

### 4. Fees are not modelled live

The backtester applies a configurable fee and slippage per side. The live bot
does not deduct fees from its P&L logging, so realised numbers will read
slightly better in the log than they are in the account.

### 5. Strategy risk

Long-only trend-pullback logic with a fixed 5:2 reward-to-risk ratio needs
roughly a 29% win rate just to break even before costs, and it has no regime
detection. It will keep buying pullbacks in a market that has rolled over as
long as price is above the SMA(200). Past backtest performance says nothing
about future results.

### 6. Reconciliation caveat

Manual trades in the same account will be adopted by the bot on the next cycle
and managed under its TP/SL rules. If you want to hold something manually, use
a separate account.

**Adopted positions get TP/SL measured from the broker's average entry price,
not from where the bot would have entered.** A position you opened yourself at
a bad price is inherited with its stop already close to being hit — or already
past it, in which case the bot sells on the very next cycle. Check what the
account is holding before you start it.

Quantities from the broker are floored, never rounded, to 4 decimals. Rounding
to nearest could round *up* past the real balance (0.078593517 → 0.0786) and
the exit order would be rejected for insufficient funds, leaving the stop-loss
unable to fire. Flooring leaves a negligible dust remainder instead, which is
the safe direction to err.


---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `RuntimeError: Missing required environment variable(s)` | `.env` missing or keys blank. Copy `.env.example` and fill it in |
| `ERROR: No matching distribution found for pandas-ta` | pandas-ta was withdrawn from PyPI. It is no longer in `requirements.txt` and is not needed - `strategy.py` uses built-in pandas indicators |
| `ModuleNotFoundError: pandas_ta` | Harmless. The bot falls back to built-in indicators automatically |
| `AttributeError: module 'numpy' has no attribute 'NaN'` | pandas-ta vs numpy 2.x. Harmless (fallback engages) or pin `numpy<2` |
| No bars returned | Symbol format must include the slash (`BTC/USD`, not `BTCUSD`) |
| `insufficient history for indicator warm-up` | Widen the backtest window: SMA(200) on 15-min bars needs ~2+ days of warm-up |
| Bot reports positions you did not open | It adopted pre-existing positions in the account during reconciliation. Expected - see "Reconciliation caveat" |
| `Position cap reached (2/2 open)` | `MAX_POSITIONS` is 2. Close a position or raise the cap |
| Every symbol logs `NEUTRAL` | Expected. The entry filter is narrow; verify with the backtest |
| Orders rejected for buying power | Crypto is cash-only on Alpaca. Check `cash`, not margin buying power |
| `403 forbidden` | Crypto trading not enabled on the account, or live keys used with `ALPACA_PAPER=true` |
| Scheduled runs arrive every ~2 hours instead of on schedule | GitHub silently drops oversubscribed cron slots, `*/15` worst of all. Fixed by using `*/30` + multiple cycles per run |
| `The job was not acquired by a runner` / `internal server error` | GitHub-side capacity failure, not your code. That cycle is simply skipped; re-run from the Actions tab |



---

## Disclaimer

This software is provided for educational purposes, as-is and without warranty
of any kind. Algorithmic trading carries substantial risk of loss. The authors
accept no liability for financial losses incurred through its use. Do not point
this at a live account with real money unless you fully understand the code and
accept the consequences.
