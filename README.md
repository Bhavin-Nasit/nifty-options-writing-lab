# NIFTY Options Writing Research Lab

This repository contains a research workflow for NIFTY option-writing strategies using Zerodha/Kite data where possible, plus normalized CSV inputs for older expired option chains.

It is built for research, not live trading. No strategy here should be treated as a sure-shot signal. Option writing can show a high win rate while hiding rare, large losses, so the reports focus on drawdown, tail loss, realistic charges, and position sizing.

## Important Kite Limitation

Kite historical candles are fetched by `instrument_token`. NFO option contracts expire, and Kite's live instrument dump only contains currently tradable contracts. Zerodha's own docs state that instrument tokens change by expiry and the instrument master only returns live contracts; continuous historical data is for futures day candles, not old option chains.

That means a true 5-year NIFTY options backtest needs one of these:

1. Your own archived option instrument dumps and historical option candles.
2. A paid vendor export containing historical NIFTY option chains.
3. NSE F&O bhavcopy/UDiFF archives for EOD-level testing, with intraday tests limited to the period where you have intraday options data.

Kite can still fetch:

- NIFTY spot/index candles.
- Current live NIFTY option contracts.
- Recent/active contract candles while the contracts are still available.

## Setup

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally. Do not paste API secrets into chat.

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

Expected normalized institutional columns are lowercase snake case. Useful examples:

```text
date,fii_index_options_long,fii_index_options_short,pro_index_options_long,pro_index_options_short,client_index_options_long,client_index_options_short
date,index_futures_long,index_futures_short
date,close
```

## Strategy Defaults

- Intraday expiry capital: `1800000`
- Positional capital: `400000`
- Default product: defined-risk spreads only
- Costs: Zerodha F&O options brokerage, STT on sell premium, NSE transaction charges, SEBI charges, stamp duty on buy side, GST. The model uses pre/post 1 April 2026 option STT rates.
- Sizing: lots are limited by configured risk per trade and `max_lots`

## Institutional Data To Add

For a more institutional-style model, add daily CSVs for:

- Participant-wise OI: Client, FII, DII, Pro long/short positions in index futures/options.
- FII derivatives statistics.
- India VIX.
- NIFTY futures basis.
- Expiry calendar and holiday-adjusted expiry dates.

These are best used as filters: avoid short-vol trades when VIX is expanding, when FII futures shorts are rising sharply, or when Pro/FII option shorts are unwinding into expiry.

Read `STRATEGY_NOTES.md` for the research assumptions behind weekly, monthly, and longer-dated writing.
