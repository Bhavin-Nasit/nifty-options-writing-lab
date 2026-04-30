from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .charges import ZerodhaOptionCharges
from .data import OptionChainStore, load_options, load_spot
from .strategy import (
    StrategyConfig,
    build_iron_condor,
    build_iron_fly,
    choose_lots,
    close_legs,
    find_exit_timestamp,
    report_metrics,
    result_row,
)


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    metrics: dict[str, float]


def run_backtest(options_path: str | Path, spot_path: str | Path, cfg: StrategyConfig) -> BacktestResult:
    store = OptionChainStore(load_options(options_path), load_spot(spot_path))
    rows: list[dict[str, object]] = []
    builder = build_iron_fly if cfg.strategy == "iron_fly" else build_iron_condor

    for session in store.sessions:
        entry_ts = store.timestamp_for_session(session, cfg.entry_time)
        if entry_ts is None:
            continue
        expiry = store.expiry_for_dte(entry_ts, cfg.entry_dte)
        if expiry is None:
            continue

        actual_dte = (expiry - entry_ts.date()).days
        if actual_dte != cfg.entry_dte:
            continue

        lots, open_legs = choose_lots(store, entry_ts, expiry, cfg, builder)
        if lots <= 0 or open_legs is None:
            continue

        exit_ts, reason = find_exit_timestamp(store, entry_ts, expiry, open_legs, cfg)
        closed_legs = close_legs(store, exit_ts, expiry, open_legs, cfg)
        if closed_legs is None:
            continue

        charges = ZerodhaOptionCharges.for_trade_date(entry_ts.date())
        rows.append(result_row(entry_ts, exit_ts, expiry, closed_legs, reason, charges))

    trades = pd.DataFrame(rows)
    if not trades.empty:
        trades = trades.sort_values("entry_ts").reset_index(drop=True)
    return BacktestResult(trades=trades, metrics=report_metrics(trades, cfg.capital))


def write_report(result: BacktestResult, cfg: StrategyConfig, output_dir: str | Path) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    trades_path = out / f"{cfg.name}_trades.csv"
    metrics_path = out / f"{cfg.name}_metrics.csv"
    result.trades.to_csv(trades_path, index=False)
    pd.DataFrame([result.metrics]).to_csv(metrics_path, index=False)
    return trades_path, metrics_path
