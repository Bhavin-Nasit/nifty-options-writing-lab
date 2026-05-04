# NIFTY Options Writing Research Lab

This repository contains a hosted NIFTY strategy engine plus research scripts for option-writing backtests. The Render dashboard uses public NSE data and does not require daily Kite token updates.

It is built for research and trade preparation, not as a guaranteed signal engine. Option writing can show a high win rate while hiding rare, large losses.

## Hosted Strategy Engine

The dashboard is now a trade-prep cockpit:

- Market regime: range, bullish range, bearish range, expiry pin risk, mixed, or no trade.
- Writer map: PE support/writer zones and CE resistance/writer zones from OI, OI change, volume, premium, and option price change.
- Strategy selector: bull put spread, bear call spread, weekly iron condor, expiry intraday iron fly, or monthly wide iron condor.
- Recommended trade card: exact legs, credit, estimated max risk, target, stop reference, confidence, entry rule, and invalidation.
- Alternative strategy table: compares rejected and alternate structures.
- Participant bias: FII, PRO, Client, and DII index derivatives positioning when the NSE participant OI archive is available.

Data flow:

1. Try live NSE option-chain data.
2. If live NSE returns no usable rows, fall back to latest real NSE F&O bhavcopy EOD data.
3. If both fail, show no trade. The app does not generate sample trades.

When source is `NSE_EOD`, treat the trade card as an opening-plan candidate only. Verify live Zerodha LTP, bid-ask spread, margin, and execution price before placing any order.

Optional Render environment variables:

```text
NSE_CACHE_SECONDS=900
NIFTY_LOT_SIZE=65
```

## Data Reality

Strike-level FII or hedge-fund short positioning is not published live in India. The closest public proxy is aggregate option-chain or bhavcopy OI and OI change by strike. NSE participant OI is category-level and EOD, not strike-specific.

## Render Deployment

Use **New > Web Service** if you want the same flow as your other dashboards.

Manual web service deploy:

1. Open Render and choose **New > Web Service**.
2. Connect `Bhavin-Nasit/nifty-options-writing-lab`.
3. Runtime: Python.
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app`
6. Health check path: `/healthz`
7. Optional env vars: `NSE_CACHE_SECONDS=900`, `NIFTY_LOT_SIZE=65`.

The deployed app exposes:

- `/` strategy engine dashboard
- `/api/action-plan` JSON strategy engine output
- `/api/strategy-configs` strategy config JSON
- `/healthz` Render health check

## Research Data Limitation

The hosted dashboard is for current trade preparation. A true 5-year options backtest still needs archived options candles or vendor data. Kite can still be used locally for historical or recent candles, but it is not required for the hosted dashboard.

## Strategy Defaults

- Intraday expiry capital model: `1800000`
- Positional capital model: `400000`
- Hosted dashboard: defined-risk structures only
- Research backtester costs: Zerodha F&O options brokerage, STT on sell premium, NSE transaction charges, SEBI charges, stamp duty on buy side, and GST
