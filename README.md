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
or statistically uncertain feedback has zero ranking weight. Ranking requires at
least 30 closed outcomes across four ISO closing weeks and an approximate 95%
mean-R interval excluding zero. Intervals cluster outcomes by closing week, use
conservative Student-t critical values and a one-R standard-error floor; they
are an uncertainty safeguard, not a guarantee about independent observations.
Only the predeclared scoring cohort can veto: at least 60 observations, at least
30 in each chronological half, at least two weeks per half, and a negative upper
interval bound in both halves. Feature buckets remain descriptive, eliminating
independent vetoes from many overlapping buckets. The thresholds can be raised
with `SWING_LAB_LEARNING_MODEL_MIN_SAMPLE` and
`SWING_LAB_LEARNING_MODEL_BLOCK_MIN_SAMPLE`; values below 30/60 are rejected.

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

## Entry quality and controlled experiments

Version `quality-v2-experiments-v1` applies these changes prospectively. Existing
positions retain their frozen execution contracts; no historical outcome is
rewritten and older signals are not retroactively assigned experiments.

- **Entry payoff check:** at the next eligible hourly open, include entry slippage,
  both fees and estimated stop slippage in the reward/risk ratio. Cancel unfilled
  entries below `SWING_LAB_MIN_ENTRY_NET_R` (default **1.8**) and release reservations
  atomically. This is deliberately below the 2R gross setup threshold to leave room
  for costs on nominal 2R pullbacks. Record the computed ratio and cancellation
  reason. The check estimates full-target payoff; it is not a win-probability or
  expectancy estimate and does not promise stop execution at the estimated price.
- **Comparable volume:** exclude the signal candle from the average. Stocks/ETFs
  compare only bars with the same offset from the exchange open and the same
  duration, including daily bars. Use up to 20 prior matching observations and
  require at least 10; missing timestamps/history, zero baselines and invalid
  volumes reject a setup. Early-close segments without comparable history are
  deliberately skipped. Crypto uses the preceding 20 bars.
- **Learning safeguards:** the larger, time-diversified evidence requirements above
  replace the previous 8/16-trade thresholds. Learning uses funded trades only.

The analytics page and `GET /analytics/experiments` compare five frozen variants
on every newly recorded qualifying signal, including model-rejected signals:

| Variant | Single change from baseline |
| --- | --- |
| Baseline | Current funded strategy rules |
| ATR stop | Widen the structural stop by 0.5 signal-timeframe Wilder ATR(14); preserve target and recheck entry payoff |
| Delayed breakeven | Raise stop to entry at +1.5R; preserve any half exit at +1R |
| ATR trailing | After +1R, trail by one frozen signal-timeframe ATR from each completed hourly close; apply only to the next bar and never loosen |
| Earnings blackout | Skip individual-stock entries on a report date or the two preceding calendar days |

ATR is measured using completed bars available at signal time, including price
gaps, and remains frozen. Invalid/missing ATR is unavailable, not a fabricated
zero. These variants never reserve cash, send trade notifications, train the
model or change funded strategy rules. Definitions and states are stored
transactionally with the signal, with unique `(signal_id, variant)` keys and
compare-and-swap replay. A new definition requires a new version.

Comparisons include only pairs where both baseline and variant have resolved.
Skipped/cancelled opportunities contribute zero to mean delta R; unavailable
observations are excluded. Win rate, average R and closed-R drawdown apply to
filled trades in those matched pairs. Open, unavailable, skipped and cancelled
counts are exposed separately in JSON. R assumes the same initial risk budget
per filled variant, so wider stops imply fewer units. This is not a simulation of
separately funded portfolios, and closed-R drawdown is not concurrent equity
drawdown. Compare the baseline subset shown for each variant, not unmatched totals.

Stopped trades continue observing subsequent complete bars until their original
expiry, counting later touches of the original target. The stopping bar is
excluded because intrabar ordering is ambiguous. Missing bars keep followups
unresolved; wall-clock expiry never fabricates a negative observation. Reports
show resolved and pending followups separately.

Do not promote a variant based on one favorable average. Keep definitions frozen,
collect outcomes across separate market periods, compare net R, drawdown, win
rate and opportunity count, and confirm any proposed change in a later untouched
period. No variant is promoted automatically.

### Earnings calendar setup

The earnings test is **shadow-only**. Supply either:

- `SWING_LAB_EARNINGS_API_KEY`: an Alpha Vantage key. The app requests the documented
  [EARNINGS_CALENDAR](https://www.alphavantage.co/documentation/#earnings-calendar)
  CSV endpoint with a three-month horizon, caches successful responses for six
  hours, backs off 15 minutes after failures, and never logs the key or response.
- `SWING_LAB_EARNINGS_CALENDAR_PATH`: a local JSON snapshot from a trusted calendar
  (takes precedence over the API). Format:

```json
{
  "source": "Your calendar provider",
  "fetched_at": "2026-09-27T00:00:00+00:00",
  "events": {"AAPL": ["2026-10-29"], "MSFT": ["2026-10-28"]}
}
```

The dates above illustrate the format; they are not verified earnings dates.
Freshness must be within 24 hours, and never in the future. Each candidate freezes
its source, fetch time and next report date. The actual next-open date is checked
again using only that frozen calendar; a now-stale snapshot is unavailable.
Dates use New York calendar days; without a reliable release time the whole
report date is blocked. A missing symbol, old date, bad response or unconfigured
provider is **unknown**, never “no earnings.” ETFs and crypto are not applicable.
Unknown observations are excluded from the earnings comparison and visibly counted
as unavailable; the funded portfolio remains on baseline rules. Calendar revisions
cannot rewrite already-recorded decisions.

## Database and notification safeguards

Startup runs an additive migration under a PostgreSQL advisory lock. It creates
signal/progress/experiment, portfolio/ledger/snapshot and notification-outbox tables, adds
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
