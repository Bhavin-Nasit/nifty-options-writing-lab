from __future__ import annotations

import os
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests


KITE_BASE_URL = "https://api.kite.trade"
NIFTY_50_INSTRUMENT_TOKEN = 256265


def load_env(path: str | Path | None) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(f"Env file not found: {env_path}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class KiteRestClient:
    def __init__(self, api_key: str | None = None, access_token: str | None = None) -> None:
        self.api_key = api_key or os.getenv("KITE_API_KEY")
        self.access_token = access_token or os.getenv("KITE_ACCESS_TOKEN")
        if not self.api_key or not self.access_token:
            raise ValueError("Set KITE_API_KEY and KITE_ACCESS_TOKEN in nifty_options_lab/.env")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{self.access_token}",
        }

    def get(self, path: str, params: dict[str, object] | list[tuple[str, object]] | None = None) -> requests.Response:
        response = requests.get(f"{KITE_BASE_URL}{path}", headers=self.headers, params=params, timeout=30)
        response.raise_for_status()
        return response

    def instruments(self, exchange: str | None = None) -> pd.DataFrame:
        path = "/instruments" if exchange is None else f"/instruments/{exchange}"
        response = self.get(path)
        return pd.read_csv(StringIO(response.text))

    def ltp(self, instruments: list[str]) -> dict[str, dict[str, float]]:
        response = self.get("/quote/ltp", params=[("i", instrument) for instrument in instruments])
        return response.json().get("data", {})

    def historical(
        self,
        instrument_token: int,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
        *,
        oi: bool = True,
        continuous: bool = False,
    ) -> pd.DataFrame:
        params = {
            "from": from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "to": to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "oi": 1 if oi else 0,
            "continuous": 1 if continuous else 0,
        }
        response = self.get(f"/instruments/historical/{instrument_token}/{interval}", params=params)
        candles = response.json().get("data", {}).get("candles", [])
        columns = ["timestamp", "open", "high", "low", "close", "volume"]
        if candles and len(candles[0]) == 7:
            columns.append("oi")
        df = pd.DataFrame(candles, columns=columns)
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df


def chunk_ranges(from_dt: datetime, to_dt: datetime, chunk_days: int) -> Iterable[tuple[datetime, datetime]]:
    current = from_dt
    delta = timedelta(days=chunk_days)
    while current <= to_dt:
        chunk_end = min(to_dt, current + delta)
        yield current, chunk_end
        current = chunk_end + timedelta(seconds=1)


def fetch_history_chunked(
    client: KiteRestClient,
    instrument_token: int,
    interval: str,
    from_dt: datetime,
    to_dt: datetime,
    *,
    chunk_days: int = 30,
    oi: bool = True,
    continuous: bool = False,
) -> pd.DataFrame:
    frames = []
    for start, end in chunk_ranges(from_dt, to_dt, chunk_days):
        frames.append(
            client.historical(
                instrument_token,
                interval,
                start,
                end,
                oi=oi,
                continuous=continuous,
            )
        )
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
