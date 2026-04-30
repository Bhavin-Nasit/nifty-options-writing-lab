from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_daily_csv(path: str | Path, prefix: str | None = None) -> pd.DataFrame:
    df = pd.read_csv(path)
    date_col = "date" if "date" in df.columns else "timestamp"
    if date_col not in df.columns:
        raise ValueError(f"{path} must contain a date or timestamp column")
    df = df.copy()
    df["date"] = pd.to_datetime(df[date_col]).dt.date
    df = df.drop(columns=[date_col]) if date_col != "date" else df
    if prefix:
        rename = {column: f"{prefix}_{column}" for column in df.columns if column != "date"}
        df = df.rename(columns=rename)
    return df.sort_values("date").reset_index(drop=True)


def spot_daily_features(spot_path: str | Path) -> pd.DataFrame:
    spot = pd.read_csv(spot_path)
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(spot.columns)
    if missing:
        raise ValueError(f"Spot CSV missing columns for feature build: {sorted(missing)}")

    spot["timestamp"] = pd.to_datetime(spot["timestamp"])
    spot["date"] = spot["timestamp"].dt.date
    daily = (
        spot.groupby("date")
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        .reset_index()
        .sort_values("date")
    )
    daily["return_1d"] = daily["close"].pct_change()
    daily["gap_pct"] = daily["open"] / daily["close"].shift(1) - 1
    daily["range_pct"] = (daily["high"] - daily["low"]) / daily["close"].shift(1)
    daily["realized_vol_5d"] = daily["return_1d"].rolling(5).std() * (252**0.5)
    daily["realized_vol_20d"] = daily["return_1d"].rolling(20).std() * (252**0.5)
    return daily


def add_positioning_ratios(features: pd.DataFrame) -> pd.DataFrame:
    df = features.copy()
    pairs = [
        ("participant_fii_index_options_long", "participant_fii_index_options_short", "fii_option_short_ratio"),
        ("participant_pro_index_options_long", "participant_pro_index_options_short", "pro_option_short_ratio"),
        ("participant_client_index_options_long", "participant_client_index_options_short", "client_option_short_ratio"),
        ("fii_index_futures_long", "fii_index_futures_short", "fii_future_short_ratio"),
    ]
    for long_col, short_col, output_col in pairs:
        if long_col in df.columns and short_col in df.columns:
            denominator = df[long_col].abs() + df[short_col].abs()
            df[output_col] = df[short_col] / denominator.replace(0, pd.NA)
            df[f"{output_col}_change_5d"] = df[output_col].diff(5)
    if "vix_close" in df.columns:
        df["vix_change_1d"] = df["vix_close"].pct_change()
        df["vix_percentile_252d"] = df["vix_close"].rolling(252).rank(pct=True)
    return df


def build_feature_table(
    spot_path: str | Path,
    *,
    participant_oi_path: str | Path | None = None,
    fii_derivatives_path: str | Path | None = None,
    vix_path: str | Path | None = None,
) -> pd.DataFrame:
    features = spot_daily_features(spot_path)
    joins = []
    if participant_oi_path:
        joins.append(load_daily_csv(participant_oi_path, "participant"))
    if fii_derivatives_path:
        joins.append(load_daily_csv(fii_derivatives_path, "fii"))
    if vix_path:
        joins.append(load_daily_csv(vix_path, "vix"))

    for table in joins:
        features = features.merge(table, on="date", how="left")
    return add_positioning_ratios(features)
