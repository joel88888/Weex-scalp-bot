"""Shared value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Signal(str, Enum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT_TP = "EXIT_TP"
    EXIT_SL = "EXIT_SL"
    FLATTEN = "FLATTEN"


@dataclass(frozen=True)
class Quote:
    ts_ms: int
    symbol: str
    bid: float
    ask: float
    last: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    has_book: bool = True

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0:
            return float("inf")
        return (self.spread / mid) * 10_000.0


@dataclass
class Position:
    side: PositionSide
    qty: float
    entry_price: float
    entry_fee: float
    opened_ts_ms: int
    client_order_id: str
    exchange_order_id: str = ""
    tp_price: float = 0.0
    sl_price: float = 0.0


@dataclass
class Fill:
    ts_ms: int
    client_order_id: str
    exchange_order_id: str
    side: Side
    position_side: PositionSide
    qty: float
    price: float
    fee: float
    reducing: bool
    note: str = ""
    simulated: bool = False


@dataclass
class OrderIntent:
    side: Side
    position_side: PositionSide
    qty: float
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    reduce_only: bool = False
    tp_price: float | None = None
    sl_price: float | None = None
    client_order_id: str = ""
    reason: str = ""


@dataclass
class Decision:
    signal: Signal
    reason: str
    quote: Quote
    intent: OrderIntent | None = None
    extras: dict = field(default_factory=dict)
