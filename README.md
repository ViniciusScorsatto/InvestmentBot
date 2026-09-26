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

The `execution-v2-expectancy-v1` ruleset uses a consistent fill ledger for R and
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
  never silently re-simulated. No database schema migration is needed: version,
  cost and execution state use the existing `metadata_json` field.

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
python -m unittest discover -s swing-lab -v
```
