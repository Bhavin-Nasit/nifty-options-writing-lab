from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd


def filter_nifty_options(instruments: pd.DataFrame) -> pd.DataFrame:
    required = {"tradingsymbol", "exchange", "segment", "instrument_type", "expiry", "strike"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"Instrument dump missing columns: {sorted(missing)}")

    df = instruments.copy()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    name_filter = True
    if "name" in df:
        name = df["name"].fillna("").astype(str).str.upper()
        name_filter = name.isin(["", "NIFTY"])
    return df[
        (df["exchange"] == "NFO")
        & (df["segment"] == "NFO-OPT")
        & (df["instrument_type"].isin(["CE", "PE"]))
        & (df["tradingsymbol"].astype(str).str.startswith("NIFTY"))
        & name_filter
        & df["expiry"].notna()
        & df["strike"].notna()
    ].sort_values(["expiry", "strike", "instrument_type"]).reset_index(drop=True)


def classify_expiries(option_instruments: pd.DataFrame) -> pd.DataFrame:
    df = option_instruments.copy()
    expiries = sorted(set(df["expiry"]))
    last_by_month: dict[tuple[int, int], date] = {}
    for expiry in expiries:
        last_by_month[(expiry.year, expiry.month)] = max(last_by_month.get((expiry.year, expiry.month), expiry), expiry)
    df["expiry_type"] = df["expiry"].map(
        lambda expiry: "monthly" if last_by_month[(expiry.year, expiry.month)] == expiry else "weekly"
    )
    return df


def save_snapshot(instruments: pd.DataFrame, output_dir: str | Path) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = output / f"kite_instruments_{today}.csv"
    instruments.to_csv(path, index=False)
    return path
