from __future__ import annotations

from dataclasses import dataclass
from datetime import date


BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    transaction: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.transaction + self.sebi + self.stamp + self.gst


@dataclass(frozen=True)
class ZerodhaOptionCharges:
    """Approximate Zerodha F&O option charges.

    The 1 April 2026 STT change is handled explicitly because it materially
    changes historical option-writing costs. Exchange fee revisions can be
    extended here if you want contract-note-exact historical modelling.
    """

    brokerage_per_order: float = 20.0
    stt_sell_premium_rate: float = 0.0015
    transaction_rate: float = 0.0003553
    sebi_rate: float = 10.0 / 10_000_000.0
    stamp_buy_rate: float = 0.00003
    gst_rate: float = 0.18

    @classmethod
    def for_trade_date(cls, trade_date: date) -> "ZerodhaOptionCharges":
        if trade_date < date(2026, 4, 1):
            return cls(
                stt_sell_premium_rate=0.001,
                transaction_rate=0.0003503,
            )
        return cls()

    def for_order(self, premium: float, quantity: int, side: str) -> ChargeBreakdown:
        turnover = abs(float(premium) * int(quantity))
        if turnover <= 0:
            return ChargeBreakdown(0, 0, 0, 0, 0, 0)

        side = side.upper()
        brokerage = self.brokerage_per_order
        stt = turnover * self.stt_sell_premium_rate if side == SELL else 0.0
        transaction = turnover * self.transaction_rate
        sebi = turnover * self.sebi_rate
        stamp = turnover * self.stamp_buy_rate if side == BUY else 0.0
        gst = (brokerage + transaction + sebi) * self.gst_rate
        return ChargeBreakdown(brokerage, stt, transaction, sebi, stamp, gst)

    def round_trip_for_leg(self, entry_price: float, exit_price: float, signed_quantity: int) -> float:
        entry_side = BUY if signed_quantity > 0 else SELL
        exit_side = SELL if signed_quantity > 0 else BUY
        quantity = abs(signed_quantity)
        return (
            self.for_order(entry_price, quantity, entry_side).total
            + self.for_order(exit_price, quantity, exit_side).total
        )
