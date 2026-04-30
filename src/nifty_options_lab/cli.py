from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .backtest import run_backtest, write_report
from .instruments import classify_expiries, filter_nifty_options, save_snapshot
from .institutional import build_feature_table
from .kite import (
    NIFTY_50_INSTRUMENT_TOKEN,
    KiteRestClient,
    fetch_history_chunked,
    load_env,
)
from .strategy import StrategyConfig


LAB_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = LAB_ROOT / "data" / "raw"
PROCESSED_DIR = LAB_ROOT / "data" / "processed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NIFTY option-writing research tools")
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot-instruments", help="Download current Kite instrument dump")
    snap.add_argument("--env", default=str(LAB_ROOT / ".env"))
    snap.add_argument("--out", default=str(RAW_DIR / "instruments"))

    spot = sub.add_parser("fetch-spot", help="Fetch NIFTY 50 spot/index candles from Kite")
    spot.add_argument("--env", default=str(LAB_ROOT / ".env"))
    spot.add_argument("--from", dest="from_date", required=True)
    spot.add_argument("--to", dest="to_date", required=True)
    spot.add_argument("--interval", default="5minute")
    spot.add_argument("--chunk-days", type=int, default=30)
    spot.add_argument("--out", default=str(PROCESSED_DIR / "nifty_spot.csv"))

    active = sub.add_parser("fetch-active-options", help="Fetch currently active NIFTY option candles from Kite")
    active.add_argument("--env", default=str(LAB_ROOT / ".env"))
    active.add_argument("--from", dest="from_date", required=True)
    active.add_argument("--to", dest="to_date", required=True)
    active.add_argument("--interval", default="5minute")
    active.add_argument("--chunk-days", type=int, default=7)
    active.add_argument("--max-contracts", type=int, default=80)
    active.add_argument("--center-strike", type=float)
    active.add_argument("--strike-window", type=int, default=600)
    active.add_argument("--expiries", type=int, default=2)
    active.add_argument("--out", default=str(PROCESSED_DIR / "nifty_active_options.csv"))

    bt = sub.add_parser("backtest", help="Run a strategy backtest from normalized CSVs")
    bt.add_argument("--options", required=True)
    bt.add_argument("--spot", required=True)
    bt.add_argument("--config", required=True)
    bt.add_argument("--out", default=str(LAB_ROOT / "reports"))

    features = sub.add_parser("build-features", help="Build daily institutional/volatility filter table")
    features.add_argument("--spot", required=True)
    features.add_argument("--participant-oi")
    features.add_argument("--fii-derivatives")
    features.add_argument("--vix")
    features.add_argument("--out", default=str(PROCESSED_DIR / "daily_features.csv"))

    return parser.parse_args()


def parse_from_to(from_date: str, to_date: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(from_date)
    end = datetime.fromisoformat(to_date)
    if len(to_date) == 10:
        end = end.replace(hour=15, minute=30)
    if len(from_date) == 10:
        start = start.replace(hour=9, minute=15)
    return start, end


def command_snapshot(args: argparse.Namespace) -> None:
    load_env(args.env)
    client = KiteRestClient()
    instruments = client.instruments()
    path = save_snapshot(instruments, args.out)
    nifty = classify_expiries(filter_nifty_options(instruments))
    nifty_path = Path(args.out) / f"{path.stem}_nifty_options.csv"
    nifty.to_csv(nifty_path, index=False)
    print(f"Saved full instrument snapshot: {path}")
    print(f"Saved active NIFTY option contracts: {nifty_path}")


def command_fetch_spot(args: argparse.Namespace) -> None:
    load_env(args.env)
    client = KiteRestClient()
    start, end = parse_from_to(args.from_date, args.to_date)
    df = fetch_history_chunked(
        client,
        NIFTY_50_INSTRUMENT_TOKEN,
        args.interval,
        start,
        end,
        chunk_days=args.chunk_days,
        oi=False,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"Saved {len(df)} NIFTY spot candles: {out}")


def command_fetch_active_options(args: argparse.Namespace) -> None:
    load_env(args.env)
    client = KiteRestClient()
    start, end = parse_from_to(args.from_date, args.to_date)
    instruments = classify_expiries(filter_nifty_options(client.instruments("NFO")))
    expiries = sorted(set(instruments["expiry"]))[: args.expiries]
    center = args.center_strike
    if center is None:
        quote = client.ltp(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {})
        center = float(quote.get("last_price", instruments["strike"].median()))
    instruments = instruments[
        (instruments["expiry"].isin(expiries))
        & (instruments["strike"].between(center - args.strike_window, center + args.strike_window))
    ].head(args.max_contracts)
    frames = []
    for row in instruments.itertuples(index=False):
        candles = fetch_history_chunked(
            client,
            int(row.instrument_token),
            args.interval,
            start,
            end,
            chunk_days=args.chunk_days,
            oi=True,
        )
        if candles.empty:
            continue
        candles["expiry"] = row.expiry
        candles["strike"] = row.strike
        candles["option_type"] = row.instrument_type
        candles["tradingsymbol"] = row.tradingsymbol
        candles["lot_size"] = row.lot_size
        frames.append(candles)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if frames:
        pd.concat(frames, ignore_index=True).to_csv(out, index=False)
    else:
        pd.DataFrame().to_csv(out, index=False)
    print(f"Saved active option candle file: {out}")


def command_backtest(args: argparse.Namespace) -> None:
    config = StrategyConfig.from_dict(json.loads(Path(args.config).read_text(encoding="utf-8")))
    result = run_backtest(args.options, args.spot, config)
    trades_path, metrics_path = write_report(result, config, args.out)
    print(f"Trades: {trades_path}")
    print(f"Metrics: {metrics_path}")
    print(json.dumps(result.metrics, indent=2, default=str))


def command_build_features(args: argparse.Namespace) -> None:
    features = build_feature_table(
        args.spot,
        participant_oi_path=args.participant_oi,
        fii_derivatives_path=args.fii_derivatives,
        vix_path=args.vix,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    features.to_csv(out, index=False)
    print(f"Saved {len(features)} daily feature rows: {out}")


def main() -> None:
    args = parse_args()
    if args.command == "snapshot-instruments":
        command_snapshot(args)
    elif args.command == "fetch-spot":
        command_fetch_spot(args)
    elif args.command == "fetch-active-options":
        command_fetch_active_options(args)
    elif args.command == "backtest":
        command_backtest(args)
    elif args.command == "build-features":
        command_build_features(args)
    else:
        raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
