from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd


OPTION_COLUMNS = {
    "timestamp",
    "expiry",
    "strike",
    "option_type",
    "open",
    "high",
    "low",
    "close",
}

SPOT_COLUMNS = {"timestamp", "open", "high", "low", "close"}


def _read_csv(path: str | Path) -> pd.DataFrame:
    return pd.read_csv(path)


def load_options(path: str | Path) -> pd.DataFrame:
    df = _read_csv(path)
    missing = OPTION_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Options CSV is missing required columns: {sorted(missing)}")

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["strike"] = df["strike"].astype(float)
    df["option_type"] = df["option_type"].str.upper().str.strip()
    if "lot_size" not in df.columns:
        df["lot_size"] = 50
    if "volume" not in df.columns:
        df["volume"] = 0
    if "oi" not in df.columns:
        df["oi"] = 0
    if "tradingsymbol" not in df.columns:
        df["tradingsymbol"] = ""

    return df.sort_values(["timestamp", "expiry", "strike", "option_type"]).reset_index(drop=True)


def load_spot(path: str | Path) -> pd.DataFrame:
    df = _read_csv(path)
    missing = SPOT_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Spot CSV is missing required columns: {sorted(missing)}")

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if "volume" not in df.columns:
        df["volume"] = 0
    return df.sort_values("timestamp").reset_index(drop=True)


def combine_date_time(day: date, value: str | time) -> datetime:
    if isinstance(value, time):
        parsed = value
    else:
        hour, minute = value.split(":")[:2]
        parsed = time(int(hour), int(minute))
    return datetime.combine(day, parsed)


def nearest_strike(value: float, step: int) -> float:
    return round(float(value) / step) * step


@dataclass
class OptionChainStore:
    options: pd.DataFrame
    spot: pd.DataFrame

    def __post_init__(self) -> None:
        self.options = self.options.copy()
        self.spot = self.spot.copy()
        self.options["_session"] = self.options["timestamp"].dt.date
        self.spot["_session"] = self.spot["timestamp"].dt.date

    @property
    def sessions(self) -> list[date]:
        return sorted(set(self.spot["_session"]))

    def spot_at(self, timestamp: datetime) -> float | None:
        exact = self.spot[self.spot["timestamp"] == timestamp]
        if not exact.empty:
            return float(exact.iloc[-1]["close"])

        session_rows = self.spot[self.spot["_session"] == timestamp.date()]
        if session_rows.empty:
            return None
        before = session_rows[session_rows["timestamp"] <= timestamp]
        if before.empty:
            return float(session_rows.iloc[0]["open"])
        return float(before.iloc[-1]["close"])

    def available_expiries(self, timestamp: datetime) -> list[date]:
        session_rows = self.options[self.options["timestamp"] == timestamp]
        if session_rows.empty:
            session_rows = self.options[self.options["_session"] == timestamp.date()]
        expiries = sorted({value for value in session_rows["expiry"] if value >= timestamp.date()})
        return expiries

    def expiry_for_dte(self, timestamp: datetime, dte: int) -> date | None:
        target = timestamp.date() + timedelta(days=int(dte))
        expiries = self.available_expiries(timestamp)
        if not expiries:
            return None
        same_or_after = [expiry for expiry in expiries if expiry >= target]
        return same_or_after[0] if same_or_after else None

    def timestamp_for_session(self, session: date, clock: str) -> datetime | None:
        target = combine_date_time(session, clock)
        spot_rows = self.spot[self.spot["_session"] == session]
        if spot_rows.empty:
            return None
        exact = spot_rows[spot_rows["timestamp"] == target]
        if not exact.empty:
            return target
        after = spot_rows[spot_rows["timestamp"] >= target]
        if not after.empty:
            return after.iloc[0]["timestamp"].to_pydatetime()
        return spot_rows.iloc[-1]["timestamp"].to_pydatetime()

    def option_bar(
        self,
        timestamp: datetime,
        expiry: date,
        strike: float,
        option_type: str,
    ) -> pd.Series | None:
        rows = self.options[
            (self.options["timestamp"] == timestamp)
            & (self.options["expiry"] == expiry)
            & (self.options["strike"] == float(strike))
            & (self.options["option_type"] == option_type.upper())
        ]
        if rows.empty:
            return None
        return rows.iloc[-1]

    def option_close(
        self,
        timestamp: datetime,
        expiry: date,
        strike: float,
        option_type: str,
        fallback_spot: float | None = None,
    ) -> float | None:
        bar = self.option_bar(timestamp, expiry, strike, option_type)
        if bar is not None:
            return float(bar["close"])

        if fallback_spot is None:
            fallback_spot = self.spot_at(timestamp)
        if fallback_spot is None:
            return None

        if timestamp.date() >= expiry:
            if option_type.upper() == "CE":
                return max(0.0, fallback_spot - float(strike))
            return max(0.0, float(strike) - fallback_spot)
        return None

    def bars_between(self, start: datetime, end: datetime, expiry: date, strikes: set[float]) -> pd.DataFrame:
        return self.options[
            (self.options["timestamp"] >= start)
            & (self.options["timestamp"] <= end)
            & (self.options["expiry"] == expiry)
            & (self.options["strike"].isin(strikes))
        ].copy()
