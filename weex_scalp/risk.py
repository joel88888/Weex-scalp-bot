"""Hard risk controls the strategy cannot bypass."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

from weex_scalp.config import Settings
from weex_scalp.models import OrderIntent, Position, Quote

log = logging.getLogger("weex_scalp.risk")


class Halted(RuntimeError):
    """Trading loop must stop."""


@dataclass
class RiskSnapshot:
    halted: bool
    halt_reason: str
    daily_pnl: float
    open_position: bool
    orders_last_minute: int


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.halted = False
        self.halt_reason = ""
        self.daily_pnl = 0.0
        self._order_times: deque[float] = deque()
        self._cooldown_until = 0.0
        self.day_key = _london_day_key(settings.timezone)

    def snapshot(self, position: Position | None) -> RiskSnapshot:
        self._expire_orders()
        return RiskSnapshot(
            halted=self.halted,
            halt_reason=self.halt_reason,
            daily_pnl=self.daily_pnl,
            open_position=position is not None,
            orders_last_minute=len(self._order_times),
        )

    def halt(self, reason: str) -> None:
        if not self.halted:
            log.error("HALT %s", reason)
        self.halted = True
        self.halt_reason = reason

    def raise_if_halted(self) -> None:
        if self.halted:
            raise Halted(self.halt_reason)

    def on_stop_hit(self, now: float | None = None) -> None:
        until = (now or time.time()) + self.settings.cooldown_after_stop_seconds
        self._cooldown_until = max(self._cooldown_until, until)
        log.info("cooldown_until=%.0f after stop", self._cooldown_until)

    def record_realized_pnl(self, pnl: float) -> None:
        self._roll_day_if_needed()
        self.daily_pnl += pnl
        log.info("daily_pnl=%.6f after realized %+0.6f", self.daily_pnl, pnl)
        if self.daily_pnl <= -abs(self.settings.max_daily_loss_usdt):
            self.halt(
                f"max daily loss reached pnl={self.daily_pnl:.4f} "
                f"limit=-{self.settings.max_daily_loss_usdt}"
            )

    def allow_entry(self, intent: OrderIntent, quote: Quote, position: Position | None) -> str | None:
        """Return a reject reason, or None if the order may proceed."""
        self._roll_day_if_needed()
        if self.halted:
            return f"halted: {self.halt_reason}"
        if position is not None:
            return "max 1 open position"
        now = time.time()
        if now < self._cooldown_until:
            return f"cooldown {self._cooldown_until - now:.0f}s remaining"
        if intent.qty <= 0:
            return "qty must be > 0"
        if intent.qty - self.settings.max_position_qty > 1e-12:
            return f"qty {intent.qty} exceeds max_position_qty {self.settings.max_position_qty}"
        ref = quote.ask if intent.position_side.value == "LONG" else quote.bid
        notional = intent.qty * ref
        if notional > self.settings.max_notional_usdt + 1e-9:
            return f"notional {notional:.4f} exceeds max {self.settings.max_notional_usdt}"
        self._expire_orders(now)
        if len(self._order_times) >= self.settings.max_orders_per_minute:
            return (
                f"order rate {len(self._order_times)}/"
                f"{self.settings.max_orders_per_minute} per minute"
            )
        return None

    def allow_reduce(self, intent: OrderIntent, position: Position | None) -> str | None:
        if self.halted and intent.reason not in {"flatten", "halt_flatten"}:
            return f"halted: {self.halt_reason}"
        if position is None:
            return "no position to reduce"
        if intent.qty - position.qty > 1e-12:
            return "reduce qty larger than position"
        self._expire_orders()
        if len(self._order_times) >= self.settings.max_orders_per_minute:
            # Flatten on halt is allowed to exceed the soft cap so we can get flat.
            if intent.reason not in {"flatten", "halt_flatten"}:
                return "order rate limit"
        return None

    def record_order_attempt(self) -> None:
        self._order_times.append(time.time())

    def _expire_orders(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - 60.0
        while self._order_times and self._order_times[0] < cutoff:
            self._order_times.popleft()

    def _roll_day_if_needed(self) -> None:
        today = _london_day_key(self.settings.timezone)
        if today != self.day_key:
            log.info("new trading day %s; resetting daily pnl", today)
            self.day_key = today
            self.daily_pnl = 0.0


def _london_day_key(timezone: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("Europe/London")
    return datetime.now(tz).date().isoformat()
