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

## Model challenger (shadow only)

`ridge-entry-v2` adds model research without changing funded trade selection,
strategy rules, stops, fees, or the existing learning model. It has a separate
model version so this addition does not discard compatible strategy history.
There is no automatic promotion switch.

### Training data and time boundaries

The challenger uses one row per qualifying **baseline signal**, including model
rejections and signals omitted by portfolio limits. It never duplicates a selected
signal by adding its funded trade, and never trains on experimental stop/exit
variants. Training requires matching strategy version, execution version, fees,
slippage, minimum entry payoff, breakeven rule and maximum duration. It uses
completed filled-trade net R; cancelled entries are zero-return opportunities in
comparisons and negative labels for the entry model, not return-regression targets. Missing features retain the outcome for the
hierarchy but exclude it from the feature fit.

`signals.label_available_at` records when a terminal baseline outcome is first
persisted, rather than assuming the closing candle was immediately available.
Features come from the original immutable signal setup, including its original
entry and stop, never later fills or exit information. Each fit requires both
observation and label availability **strictly before** its cutoff. Unresolved,
nonfinite or incompatible labels are excluded. Existing terminal signals without
availability timestamps become available at migration time and are never backdated.

### What the challenger estimates

A ridge regressor predicts a correction to a hierarchically pooled net-R estimate.
The fixed features are RSI, log(1 + relative volume), distance from EMA20 as a price
fraction, EMA20–EMA50 gap as a price fraction, ATR/price and initial stop distance/ATR.
Two fixed nonlinear terms add RSI curvature `((RSI - 50) / 50)^2` and
EMA gap × log(1 + relative volume).
A weighted mean and scale are fitted only on training features; standardized inputs
are clipped at ±5 for numerical stability. The L2 penalty is fixed at 10, rather
than searched across test results. NumPy provides the linear algebra.

The hierarchy is global → strategy → strategy/asset-class →
strategy/asset-class/timeframe. Each level blends its observations with its parent's
estimate using a fixed 20-unit prior strength, smoothly borrowing evidence for
small/unseen groups. This is empirical pooling, not an exact Bayesian posterior.
To limit repeated correlated evidence, each correlation group contributes a total
training weight of at most one per UTC decision date. A fixed 90-day half-life
then reduces weight with age, measured from observation to the fit cutoff. This
applies to pooling, feature fitting and entry fitting; validation outcomes receive
cluster weights without age decay. No half-life search is performed. Feature-fit residuals use a hierarchy
that excludes the observation's entire correlated date/group cluster.

Feature fitting needs at least 30 complete feature rows across four observation
weeks and ten age-weighted evidence units. Feature influence is scaled by effective
training weight and candidate extrapolation/leverage. It is zero unless earlier
validation shows both lower mean absolute error and better top-three selections.
Validation tests the same confidence-weighted correction used for predictions.

A separate L2-regularized logistic model estimates entry probability from the same
signal-time features. It trains on resolved fills and cancellations, respecting the
same arrival cutoffs. It requires four weeks, 30 feature rows, ten evidence units,
and five weighted units of each class. Sparse, missing-feature or unproven cases
use a pooled fill probability with a fixed Beta(10, 10) prior. Feature-based entry
probabilities require lower held-out Brier loss and improved selection outcomes.
Reliability bins report predicted versus observed fill frequencies; passing the
gates does not establish perfect calibration.

The ranking quantity is **estimated net R per signal opportunity**:
`entry_probability × conditional_net_R`. Predictions retain both components for
audit. Cancelled entries have zero opportunity return. The conditional estimate is
the pooled return plus its validated feature correction. All four changes remain
in the shadow challenger; they do not alter funded selections.

For initial safety, challenger rankings fall back to the current model until
there are at least 30 outcomes across four weeks and ten effective evidence units.
After that, the hierarchy can rank while the feature correction remains unproven.
Missing features use the hierarchy alone. Read/fit failures are recorded as
unavailable and fall back to current-model ranks without changing funded decisions.

### Validation and comparisons

Every new qualifying scan records an immutable model snapshot and per-signal
predictions alongside both models' ranks and top-three choices. Snapshots include
the training cutoff, training IDs and hash, model definition, execution contract, fitted hierarchy,
scalers, return coefficients, entry classifier, recency settings and validation diagnostics. Signal, experiment, model and
portfolio writes remain transactional. Repeated scans cannot replace predictions.

The inner validation uses four fixed calendar weeks ending at least 17 days before
the fit cutoff. Each week refits from labels available strictly before that week,
and a week with any unresolved signal supplies no evidence. Feature influence
requires at least two completed validation weeks and 20 effective validation units,
as well as a lower weighted MAE. On the same complete scan batches, the fixed
positive-return top-three policy must also improve mean net R per batch without
worsening the worst weekly total. The return correction is checked against pooling;
the entry classifier is checked against pooled entry probability using the validated
return policy. Ties use the same score and signal-ID ordering as prospective ranks.
These are nested development gates; outer evaluation and future frozen predictions
remain separate. They are not significance tests, and four weeks can be noisy.
These conservative checks are evidence controls,
not statistical guarantees of future profitability.

The model panel in Analytics links to:

- `GET /analytics/model`: frozen **prospective** predictions, training coverage,
  latest snapshot diagnostics and matched ranking outcomes.
- `GET /analytics/model/evaluation`: **retrospective** weekly expanding-window
  evaluation. Every weekly fit recomputes preprocessing, pooling, ridge and inner
  validation using earlier data only. Incomplete weeks are listed but excluded
  from aggregate results. The existing `/analytics/learning/evaluation` remains
  the older funded-trade-only evaluation of the current model.

Both models see the same candidate batches. The current model uses frozen approval
and combined score; the challenger ranks positive estimated net R. Ties resolve
by score and signal identity. Cold/unavailable batches explicitly fall back to the
current model and are not described as active challenger decisions. The comparison
selects up to three signals per model, without portfolio funding, exposure or
correlation limits; it is not a funded portfolio backtest. Entire unresolved
batches are excluded so early-closing winners cannot make a comparison look better.
Reports include opportunities, filled sample size, cancellations, net R, win rate,
closed-trade R drawdown, mean delta per matched batch and opportunity prediction MAE (including cancellations).

No unseen historical signals are reconstructed. Calendar-week validation observes
temporal ordering but cannot make correlated markets independent. Review several
later completed periods and prospective results before promoting a model. A
synthetic regression test demonstrates recovery of a known feature/return
relationship; it does not measure investment performance.

## Downloading an analysis report

On **Analytics**, click **Download analysis report**. The browser downloads a ZIP
from `GET /analytics/export` containing nine UTF-8 CSV files plus `metadata.json`
and `README.txt` with joins and metric definitions:

- `signals.csv`: original features, approval/selection, latest baseline outcomes,
  signal IDs, observation times and label arrival times.
- `model_predictions.csv` and `model_snapshots.csv`: frozen rankings, probabilities,
  expected returns, versions, training cutoffs and complete fitted artifacts.
- `trades.csv`: simulated funded trades, partial exits, net R and frozen costs/fills.
- `portfolio_accounts.csv`, `portfolio_positions.csv`, `portfolio_ledger.csv`,
  `portfolio_snapshots.csv`: balances, quantities, cash flows and sampled equity.
- `signal_experiments.csv`: separately identified experimental variants.

The archive always includes **all recorded dates, strategy/model versions and
statuses**. Page date filters are recorded as context in metadata, not applied to
archive rows. This keeps comparison batches and portfolio cash history complete.
Filter the CSVs after download, using the documented timestamps and version keys.
No historical signals or missing fills are reconstructed. Pending results stay
blank; cancelled entries are zero opportunities but are not filled-trade losses.
Raw nested records are preserved in JSON columns for detailed analysis.

A read-only PostgreSQL repeatable-read transaction keeps all files consistent even
while scans update the app. Rows are read using server cursors; archives larger
than 8 MiB spool to temporary disk. Downloads do not refit models, fetch prices,
change account state or send notifications. No credentials or notification data
are included. CSV text is escaped against spreadsheet formulas while negative
numeric returns retain their numeric representation. HTTP responses use
`Cache-Control: no-store`. Empty datasets still include column headers. Failed
exports return a retryable error instead of a partial archive.

Upload the ZIP for analysis so predictions, outcomes and portfolio records can be
joined by their stable IDs. Avoid summing trade results with baseline signal
results: selected trades and signals represent overlapping opportunities.

## Database and notification safeguards

Startup runs an additive migration under a PostgreSQL advisory lock. It creates
signal/progress/experiment/model, portfolio/ledger/snapshot and notification-outbox tables, adds
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
