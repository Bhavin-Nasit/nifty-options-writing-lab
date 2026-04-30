from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import pandas as pd

from .charges import ZerodhaOptionCharges
from .data import OptionChainStore, nearest_strike


StrategyName = Literal["iron_fly", "iron_condor"]


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    strategy: StrategyName
    capital: float
    risk_per_trade_pct: float
    max_lots: int
    lot_size: int
    entry_dte: int
    entry_time: str
    exit_time: str
    strike_step: int = 50
    wing_width: int = 300
    short_distance: int = 250
    exit_dte: int = 0
    min_net_credit: float = 0.0
    stop_loss_close_value_multiple: float = 1.8
    profit_target_capture_pct: float = 0.55
    slippage_bps: float = 5.0

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "StrategyConfig":
        return cls(**raw)


@dataclass(frozen=True)
class Leg:
    option_type: str
    strike: float
    signed_quantity: int
    entry_price: float
    exit_price: float | None = None


def apply_slippage(price: float, signed_quantity: int, bps: float, is_entry: bool) -> float:
    """Move fills against the strategy: buys pay more and sells receive less."""

    side_is_buy = signed_quantity > 0 if is_entry else signed_quantity < 0
    direction = 1 if side_is_buy else -1
    return max(0.0, price * (1 + direction * bps / 10_000.0))


def build_iron_fly(
    store: OptionChainStore,
    entry_ts: datetime,
    expiry: date,
    cfg: StrategyConfig,
    lots: int,
) -> list[Leg] | None:
    spot = store.spot_at(entry_ts)
    if spot is None:
        return None
    atm = nearest_strike(spot, cfg.strike_step)
    strikes = {
        ("CE", atm): -1,
        ("PE", atm): -1,
        ("CE", atm + cfg.wing_width): 1,
        ("PE", atm - cfg.wing_width): 1,
    }
    return _build_legs(store, entry_ts, expiry, strikes, cfg, lots)


def build_iron_condor(
    store: OptionChainStore,
    entry_ts: datetime,
    expiry: date,
    cfg: StrategyConfig,
    lots: int,
) -> list[Leg] | None:
    spot = store.spot_at(entry_ts)
    if spot is None:
        return None
    atm = nearest_strike(spot, cfg.strike_step)
    put_short = atm - cfg.short_distance
    call_short = atm + cfg.short_distance
    strikes = {
        ("PE", put_short): -1,
        ("CE", call_short): -1,
        ("PE", put_short - cfg.wing_width): 1,
        ("CE", call_short + cfg.wing_width): 1,
    }
    return _build_legs(store, entry_ts, expiry, strikes, cfg, lots)


def _build_legs(
    store: OptionChainStore,
    entry_ts: datetime,
    expiry: date,
    strikes: dict[tuple[str, float], int],
    cfg: StrategyConfig,
    lots: int = 1,
) -> list[Leg] | None:
    legs: list[Leg] = []
    for (option_type, strike), direction in strikes.items():
        bar = store.option_bar(entry_ts, expiry, strike, option_type)
        if bar is None:
            return None
        price = float(bar["close"])
        if price <= 0:
            return None
        lot_size = int(bar.get("lot_size", cfg.lot_size) or cfg.lot_size)
        signed_quantity = direction * lots * lot_size
        fill = apply_slippage(price, signed_quantity, cfg.slippage_bps, is_entry=True)
        legs.append(Leg(option_type, float(strike), signed_quantity, fill))
    return legs


def net_credit(legs: list[Leg]) -> float:
    return -sum(leg.signed_quantity * leg.entry_price for leg in legs) / abs(legs[0].signed_quantity)


def max_loss_per_unit(legs: list[Leg], cfg: StrategyConfig) -> float:
    credit = net_credit(legs)
    return max(0.0, cfg.wing_width - credit)


def choose_lots(
    store: OptionChainStore,
    entry_ts: datetime,
    expiry: date,
    cfg: StrategyConfig,
    builder,
) -> tuple[int, list[Leg] | None]:
    one_lot_legs = builder(store, entry_ts, expiry, cfg, 1)
    if one_lot_legs is None:
        return 0, None
    credit = net_credit(one_lot_legs)
    if credit < cfg.min_net_credit:
        return 0, None
    max_loss = max_loss_per_unit(one_lot_legs, cfg) * abs(one_lot_legs[0].signed_quantity)
    if max_loss <= 0:
        return 0, None
    risk_budget = cfg.capital * cfg.risk_per_trade_pct
    lots = max(0, min(cfg.max_lots, int(risk_budget // max_loss)))
    if lots == 0:
        return 0, None
    return lots, builder(store, entry_ts, expiry, cfg, lots)


def mark_close_value(
    store: OptionChainStore,
    timestamp: datetime,
    expiry: date,
    legs: list[Leg],
) -> float | None:
    spot = store.spot_at(timestamp)
    value = 0.0
    for leg in legs:
        price = store.option_close(timestamp, expiry, leg.strike, leg.option_type, fallback_spot=spot)
        if price is None:
            return None
        value += -leg.signed_quantity * price
    return value / abs(legs[0].signed_quantity)


def close_legs(
    store: OptionChainStore,
    timestamp: datetime,
    expiry: date,
    legs: list[Leg],
    cfg: StrategyConfig,
) -> list[Leg] | None:
    spot = store.spot_at(timestamp)
    closed: list[Leg] = []
    for leg in legs:
        price = store.option_close(timestamp, expiry, leg.strike, leg.option_type, fallback_spot=spot)
        if price is None:
            return None
        fill = apply_slippage(price, leg.signed_quantity, cfg.slippage_bps, is_entry=False)
        closed.append(
            Leg(
                option_type=leg.option_type,
                strike=leg.strike,
                signed_quantity=leg.signed_quantity,
                entry_price=leg.entry_price,
                exit_price=fill,
            )
        )
    return closed


def find_exit_timestamp(
    store: OptionChainStore,
    entry_ts: datetime,
    expiry: date,
    legs: list[Leg],
    cfg: StrategyConfig,
) -> tuple[datetime, str]:
    target_exit_day = expiry - timedelta(days=cfg.exit_dte)
    scheduled_exit = store.timestamp_for_session(target_exit_day, cfg.exit_time)
    if scheduled_exit is None or scheduled_exit <= entry_ts:
        scheduled_exit = store.timestamp_for_session(entry_ts.date(), cfg.exit_time) or entry_ts

    credit = net_credit(legs)
    stop_close_value = credit * cfg.stop_loss_close_value_multiple
    target_close_value = credit * (1.0 - cfg.profit_target_capture_pct)
    candidate_times = sorted(
        set(
            store.options[
                (store.options["timestamp"] > entry_ts)
                & (store.options["timestamp"] <= scheduled_exit)
                & (store.options["expiry"] == expiry)
            ]["timestamp"].dt.to_pydatetime()
        )
    )
    for ts in candidate_times:
        close_value = mark_close_value(store, ts, expiry, legs)
        if close_value is None:
            continue
        if close_value >= stop_close_value:
            return ts, "stop_loss"
        if close_value <= target_close_value:
            return ts, "profit_target"
    return scheduled_exit, "scheduled_exit"


def summarize_legs(legs: list[Leg]) -> str:
    pieces = []
    for leg in sorted(legs, key=lambda item: (item.strike, item.option_type, item.signed_quantity)):
        side = "BUY" if leg.signed_quantity > 0 else "SELL"
        pieces.append(f"{side} {abs(leg.signed_quantity)} {int(leg.strike)}{leg.option_type}@{leg.entry_price:.2f}")
    return " | ".join(pieces)


def leg_pnl(leg: Leg) -> float:
    if leg.exit_price is None:
        raise ValueError("Cannot compute P&L for an open leg")
    return leg.signed_quantity * (leg.exit_price - leg.entry_price)


def charges_for_legs(legs: list[Leg], model: ZerodhaOptionCharges) -> float:
    total = 0.0
    for leg in legs:
        if leg.exit_price is None:
            raise ValueError("Cannot compute charges for an open leg")
        total += model.round_trip_for_leg(leg.entry_price, leg.exit_price, leg.signed_quantity)
    return total


def result_row(
    entry_ts: datetime,
    exit_ts: datetime,
    expiry: date,
    legs: list[Leg],
    reason: str,
    charges: ZerodhaOptionCharges,
) -> dict[str, object]:
    gross = sum(leg_pnl(leg) for leg in legs)
    total_charges = charges_for_legs(legs, charges)
    return {
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "expiry": expiry,
        "exit_reason": reason,
        "legs": summarize_legs(legs),
        "net_credit_per_unit": net_credit(legs),
        "gross_pnl": gross,
        "charges": total_charges,
        "net_pnl": gross - total_charges,
    }


def report_metrics(trades: pd.DataFrame, capital: float) -> dict[str, float]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "avg_trade": 0.0,
        }
    pnl = trades["net_pnl"].astype(float)
    equity = capital + pnl.cumsum()
    running_peak = equity.cummax()
    drawdown = equity - running_peak
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    return {
        "trades": float(len(trades)),
        "win_rate": float((pnl > 0).mean()),
        "total_pnl": float(pnl.sum()),
        "return_pct": float(pnl.sum() / capital),
        "max_drawdown": float(drawdown.min()),
        "max_drawdown_pct": float(drawdown.min() / capital),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if not losses.empty else float("inf"),
        "avg_trade": float(pnl.mean()),
        "median_trade": float(pnl.median()),
        "worst_trade": float(pnl.min()),
        "best_trade": float(pnl.max()),
    }
