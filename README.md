# NIFTY Options Writing Research Lab

This repository contains a hosted NIFTY options action board plus research scripts for option-writing backtests. The Render dashboard now uses public NSE option-chain data and does not require daily Kite token updates.

It is built for research, not live trading. No strategy here should be treated as a sure-shot signal. Option writing can show a high win rate while hiding rare, large losses, so the reports focus on drawdown, tail loss, realistic charges, and position sizing.

## Hosted Action Board

The dashboard derives weekly and monthly writing plans from the public NSE NIFTY option chain:

- NIFTY spot from the option-chain payload.
- Current weekly and monthly expiries from NSE expiry dates.
- PE/CE short strikes selected using OI, positive OI change, traded volume, premium, and distance buffer.
- Hedge strikes added automatically to form defined-risk iron condors.
- Net credit, approximate max risk, target, stop reference, OI, volume, and IV shown on screen.

No broker token is required. The app caches NSE data server-side for `NSE_CACHE_SECONDS`, default `900` seconds.

Optional Render environment variables:

```text
NSE_CACHE_SECONDS=900
NIFTY_LOT_SIZE=65
```

If NSE blocks or rate-limits the hosted request and no cached board exists, the app clearly switches to SAMPLE mode. Do not trade from SAMPLE mode.

## Important Data Limitation

The hosted dashboard is for planning weekly/monthly writing candidates from currently available option-chain data. It is not a full institutional backtest by itself. A true 5-year NIFTY options backtest still needs one of these:

1. Your own archived option instrument dumps and historical option candles.
2. A paid vendor export containing historical NIFTY option chains.
3. NSE F&O bhavcopy/UDiFF archives for EOD-level testing, with intraday tests limited to the period where you have intraday options data.

Kite can still be used locally for historical/recent candles, but it is not required for the hosted dashboard.

## Render Deployment

Use New > Web Service if you want the same flow as your other dashboards.

Manual web service deploy:

1. Open Render and choose New > Web Service.
2. Connect `Bhavin-Nasit/nifty-options-writing-lab`.
3. Runtime: Python.
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app`
6. Health check path: `/healthz`
7. Optional env vars: `NSE_CACHE_SECONDS=900`, `NIFTY_LOT_SIZE=65`.

Blueprint deploy is also supported through `render.yaml`, but it is optional.

The deployed app exposes:

- `/` action board
- `/api/action-plan` JSON action plan
- `/api/strategy-configs` strategy config JSON
- `/healthz` Render health check

## Local Setup

For the web dashboard only:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8050`.

For research and optional Kite data capture:

```powershell
pip install -r requirements-research.txt
Copy-Item .env.example .env
```

Edit `.env` locally only if you are running Kite capture scripts.

## Capture Data

Snapshot the current Kite instrument dump:

```powershell
python run.py snapshot-instruments --env .env
```

Fetch NIFTY spot candles:

```powershell
python run.py fetch-spot --env .env --from 2024-01-01 --to 2024-12-31 --interval 5minute
```

Fetch currently active NIFTY option candles:

```powershell
python run.py fetch-active-options --env .env --from 2026-04-01 --to 2026-04-29 --interval 5minute --max-contracts 40
```

For old 5-year option backtests, place a normalized options CSV at:

```text
data\processed\nifty_options.csv
```

Expected columns:

```text
timestamp,expiry,strike,option_type,open,high,low,close,volume,oi,tradingsymbol,lot_size
```

Place NIFTY spot candles at:

```text
data\processed\nifty_spot.csv
```

Expected columns:

```text
timestamp,open,high,low,close,volume
```

## Run Backtests

Expiry-day defined-risk iron fly:

```powershell
python run.py backtest --options data\processed\nifty_options.csv --spot data\processed\nifty_spot.csv --config configs\expiry_intraday_iron_fly.json --out reports
```

Weekly positional iron condor:

```powershell
python run.py backtest --options data\processed\nifty_options.csv --spot data\processed\nifty_spot.csv --config configs\weekly_positional_iron_condor.json --out reports
```

Build daily institutional/volatility features:

```powershell
python run.py build-features --spot data\processed\nifty_spot.csv --participant-oi data\processed\participant_oi.csv --fii-derivatives data\processed\fii_derivatives.csv --vix data\processed\india_vix.csv
```

## Strategy Defaults

- Intraday expiry capital: `1800000`
- Positional capital: `400000`
- Hosted action board: defined-risk iron condor candidates only
- Costs in the research backtester: Zerodha F&O options brokerage, STT on sell premium, NSE transaction charges, SEBI charges, stamp duty on buy side, GST

Read `STRATEGY_NOTES.md` for the research assumptions behind weekly, monthly, and longer-dated writing.
