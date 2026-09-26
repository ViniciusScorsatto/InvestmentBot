# Swing Lab Auto

Local-first swing-trading simulator built with FastAPI, deterministic strategies, Telegram alerts, and scheduled market scans.

## Local Setup

```bash
cd swing-lab
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Set the required database connection:

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@HOST:PORT/DBNAME"
```

Optional Telegram alerts:

```bash
export SWING_LAB_TELEGRAM_BOT_TOKEN="your_bot_token"
export SWING_LAB_TELEGRAM_CHAT_ID="your_chat_id"
```

Run the app:

```bash
python3 main.py
```

## Railway

- Create a Railway Postgres service.
- Create a Railway web service from this GitHub repo.
- Set `DATABASE_URL` from the Railway Postgres service.
- Set `SWING_LAB_TELEGRAM_BOT_TOKEN` and `SWING_LAB_TELEGRAM_CHAT_ID` if you want alerts.
- Use a single replica only so the in-process scheduler runs once.
- Railway can use the root `Procfile` start command automatically.

Health check endpoint:

```text
/healthz
```

## Simulation and result integrity

The `portfolio-v1-signals-v1` ruleset uses a consistent fill ledger for R and
normalized $100-notional P/L. Historical closed results are preserved; their dollar
P/L is derived from recorded R instead of an observed price beyond the exit level.
R totals assume equal initial risk per trade and are not a portfolio percentage return.

- **Signals:** only completed candles. Crypto four-hour buckets start at 00:00,
  04:00, etc. UTC. Equities use the XNYS regular-session calendar, including DST,
  holidays and early closes; the final session bucket may be shorter than four hours.
  Missing constituent bars invalidate a signal bucket. Cached data from the old
  format is invalidated. On provider failure, cached bars are used only for execution
  replay, never for new signals.
- **Entry:** a selected setup is pending until the first whole hourly bar starting
  at or after selection. Its open, plus adverse slippage, becomes the entry fill.
  Stop and target levels remain those of the signal; achieved R/R is recomputed.
  A gap beyond either level cancels the unfilled setup, which is excluded from
  win/loss statistics. The dashboard identifies pending and cancelled entries.
- **Exits:** updates replay completed hourly OHLC bars in order, including bars that
  arrive after the equity close. Intrabar touches count even if the close recovers.
  A gap through a stop fills at the opening price with adverse slippage. Targets
  and partial-profit limits fill at their limit without assumed price improvement.
  When the OHLC path is ambiguous, the existing stop wins; after +1R, a touched
  breakeven stop wins over a target. An opening gap establishes ordering first.
  Breakouts retain full size after +1R; other setups sell half at +1R. Timed exits
  use a fresh processed bar at/after expiry, not the wall clock with a stale price.
  Intrabar event times are recorded as the bar end, not an invented exact fill time.
- **Costs:** defaults are **5 basis points per fill in fees** and **5 basis points
  adverse slippage on market fills**. These are illustrative simulation assumptions,
  not venue quotations. Configure `SWING_LAB_SIM_FEE_BPS` and
  `SWING_LAB_SIM_SLIPPAGE_BPS` (set both to `0` for a gross-cost experiment).
  Costs and duration rules are frozen per trade; initial and partial fees are
  included exactly once. Open P/L estimates liquidation at the marked price with
  exit costs. A breakeven-price stop can therefore have negative net R.
- **Recovery:** state, fills and replay cursor are written together with an
  optimistic concurrency check. Repeated updates do not process a bar twice.
  Missing execution bars pause replay and show `Data Gap: Awaiting Missing Bars`;
  replay resumes if those bars return. Bars containing pre-entry price history are
  skipped. Legacy open trades start replay at upgrade time and retain prior partials
  and stops; they are excluded from current-version training. Closed trades are
  never silently re-simulated. Execution state remains in `metadata_json`; the
  additive portfolio/signal/outbox migration below runs at startup.

Hourly replay has up to one bar of detection latency, plus polling/provider delay.
It cannot recover unknown intrabar ordering or missing provider history. It is a
research simulation, not an order-execution service.

## Learning and evaluation

The learning rank is based on shrinkage-adjusted average **net R**, with win rate
reported descriptively. It uses one cohort for ranking instead of treating
multiple overlapping slices as independent observations. Warm-up/unavailable
feedback has zero ranking weight. Specific negative-expectancy slices can still
veto candidates after the configured minimum sample.

Training is restricted to the current strategy version and matching cost settings.
The default analytics view also isolates the current version; an explicit date
range can inspect older trades. Empty periods remain empty in every breakdown.
Unresolved closed outcomes and cancelled entries are not counted as losses.
Summary calculations include profit factor and closed-trade R drawdown (not
mark-to-market portfolio drawdown).

`GET /analytics/learning/evaluation` returns a walk-forward report. Each decision
uses only trades closed **before** that setup was selected, excluding future and
overlapping outcomes. It compares the observed baseline with accepted/rejected
subsets, reporting sample size, net R, win rate, profit factor and closed-trade
drawdown. This evaluates the filter on recorded trades only: historical rejected
candidates and alternate portfolio selections are unavailable, so the report
explicitly does **not** claim an unbiased full-strategy performance improvement.

Run regressions without connecting to production or sending alerts:

```bash
python scripts/test_unit.py
```


## Calendar-aware scanning and unique signals

The scheduler checks for recently completed candles every minute, with a two-minute
provider publication grace period and a one-hour maximum catch-up window. Crypto
signals are evaluated at UTC four-hour/day closes. Stock/ETF signals are evaluated
after session-aligned four-hour and daily closes, including early closes and DST;
holidays generate no equity scan windows. A provider must actually return the
expected completed candle before that symbol/window is marked evaluated. Progress
is stored per asset/timeframe and survives restarts. No old weekend signals are
backfilled as new entries.

Signals have a stable ID derived from asset class, symbol, strategy, timeframe,
candle end and strategy version. The first observation freezes its decision and
simulation settings. A rescan cannot re-enter the same signal even after its trade
closes. Database unique indexes also enforce at most one open/pending trade per
asset/timeframe. The scanner retains the full ranked pool; portfolio eligibility
is checked before the final selection limit, so an unavailable or correlated
candidate cannot crowd out a lower-ranked eligible setup.

## Funded paper portfolio

A persistent cash account starts with `SWING_LAB_INITIAL_CASH` (default **$10,000**).
Changing that setting later does not reset an existing account. All reservations,
entries, partial exits and final exits are applied transactionally. Fractional units
are permitted. Short positions, if enabled, are cash-secured with entry notional
held as collateral; this is not a margin/broker model.

Default allocation limits, expressed as fractions of current net liquidation equity:

| Setting | Default | Meaning |
|---|---:|---|
| `SWING_LAB_RISK_PER_TRADE` | 0.01 | Initial stop risk budget per trade, before gap losses/costs |
| `SWING_LAB_MAX_PORTFOLIO_RISK` | 0.05 | Total allocated initial stop risk |
| `SWING_LAB_MAX_GROSS_EXPOSURE` | 0.80 | Total gross exposure, including pending reservations |
| `SWING_LAB_MAX_POSITION_EXPOSURE` | 0.20 | Maximum allocation to one position |
| `SWING_LAB_MAX_GROUP_EXPOSURE` | 0.30 | Maximum allocation to one correlation group |
| `SWING_LAB_MAX_GROUP_POSITIONS` | 1 | Maximum open/pending positions in each group |

These are configurable simulation defaults, not personalized investment allocations.
Exposure checks include **all** open positions, including those opened on prior days.
The old one-per-group-per-day heuristic is replaced by limits on concurrent positions.
Pending entries reserve cash, fees and risk. Actual entry quantities can only shrink
if a gap would exceed the reserved cash or risk budget. Existing positions can later
move beyond exposure limits as prices change; the simulator blocks further allocation
rather than forcing liquidation. Missing/stale marks suspend new allocations.

The analytics page shows equity, available/reserved cash, gross/risk exposure and
maximum sampled drawdown. `GET /analytics/portfolio` exposes these figures plus the
latest 500 snapshots. Cash and a per-event ledger are persistent. Equity includes
all simultaneous positions, estimated exit fees/slippage and secured short
collateral; drawdown is measured on update snapshots, not unknown intrabar lows.
The trade date filter does not change the portfolio account/history.

Existing open positions are adopted once using the old $100 entry-notional
convention (half the units if a partial was already taken), with adoption-time
marks as the portfolio cost basis. Pending legacy setups reserve the corresponding
amount. This creates no claim about pre-upgrade portfolio performance. Initial
cash must cover adoption; legacy positions and results remain identifiable.

## Prospective shadow comparison

Every rule/score/R-qualified signal is saved, including model rejections and setups
not selected because of cash, exposure, correlation or daily limits. Shadow trades
use the same frozen execution engine, costs and observation time as funded trades;
they never reserve cash or enqueue trade notifications. Their outcomes do not enter
the live learning training set automatically.

The analytics page and `GET /analytics/shadow` compare all qualifying signals,
model-approved signals, model-rejected signals and selected signals. Each group
reports closed sample size, net R, mean R and win rate. Open/cancelled signals and
missing-data counts are shown separately. This is a prospective **per-signal**
comparison, not two independent funded portfolios; overlapping signals and different
sample sizes must not be interpreted as portfolio return uplift. Historical rejected
signals cannot be reconstructed. The older walk-forward endpoint remains available
for retrospective checks on recorded executed trades.

## Database and notification safeguards

Startup runs an additive migration under a PostgreSQL advisory lock. It creates
signal/progress, portfolio/ledger/snapshot and notification-outbox tables, adds
`trades.signal_id`, and creates active-position/signal uniqueness indexes. It is
idempotent and preserves historical closed results. Existing duplicate open
asset/timeframe positions cause an explicit migration failure listing their IDs;
they must be reconciled deliberately instead of silently deleting trade history.

The account row lock serializes cash reservations and daily-limit checks across
concurrent scans. Trade updates, ledger entries and notification events commit in
one transaction. Failed transactions leave all three unchanged. Notifications have
unique event keys and are sent by a separate worker using expiring leases and
`SKIP LOCKED`. Network errors and unsuccessful Telegram responses retry with bounded
exponential backoff; rate-limit retry hints are respected. Credentials and request
URLs are never saved in delivery errors. Unconfigured Telegram leaves events queued.
Delivery is **at least once**: a crash after Telegram accepts a message but before
the acknowledgement is saved can produce a duplicate delivery.

## CI and deployment checks

`.github/workflows/test.yml` runs unit regressions and real PostgreSQL migration,
ledger, rollback, uniqueness, outbox and concurrency tests on pushes and pull
requests. Each database test uses a fresh isolated schema in the test service.
`railway.json` runs `python scripts/test_unit.py` as a pre-deploy command; a nonzero
exit blocks that deployment. The wrapper removes production database credentials
and simulation overrides from the test subprocess. The complete PostgreSQL suite
runs in GitHub CI; requiring that check for merges is a repository setting.

From the repository root:

```bash
python scripts/test_unit.py
# Use an isolated test database; never use production credentials here.
TEST_DATABASE_URL=postgresql://USER:PASSWORD@localhost/investmentbot_test python scripts/test_postgres.py
```

For a temporary local test server, install the development-only
`pixeltable-pgserver` package in your test virtual environment, then run
`python scripts/test_postgres.py` without `TEST_DATABASE_URL`. It starts and cleans
up its own temporary database; it is not a production dependency.
