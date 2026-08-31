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
        self._circuit_until = 0.0
        self._stop_times: deque[float] = deque()
        self._consecutive_stops = 0
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
        now = now or time.time()
        until = now + self.settings.cooldown_after_stop_seconds
        self._cooldown_until = max(self._cooldown_until, until)
        self._consecutive_stops += 1
        self._stop_times.append(now)
        window = self.settings.stop_circuit_window_seconds
        cutoff = now - window
        while self._stop_times and self._stop_times[0] < cutoff:
            self._stop_times.popleft()
        log.info(
            "cooldown_until=%.0f after stop consecutive=%s windowed=%s",
            self._cooldown_until,
            self._consecutive_stops,
            len(self._stop_times),
        )
        n = self.settings.stop_circuit_after
        if n > 0 and self._consecutive_stops >= n and len(self._stop_times) >= n:
            pause = self.settings.stop_circuit_pause_seconds
            self._circuit_until = max(self._circuit_until, now + pause)
            log.warning(
                "stop_circuit_breaker after %s consecutive stops in %.0fs; "
                "pausing entries %.0fs until=%.0f",
                n,
                window,
                pause,
                self._circuit_until,
            )
            self._consecutive_stops = 0
            self._stop_times.clear()

    def on_take_profit(self) -> None:
        self._consecutive_stops = 0

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
        if now < self._circuit_until:
            return f"stop_circuit {self._circuit_until - now:.0f}s remaining"
        if now < self._cooldown_until:
            return f"cooldown {self._cooldown_until - now:.0f}s remaining"
        fee_reject = _fee_aware_tp_reject(self.settings)
        if fee_reject:
            return fee_reject
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


def _fee_aware_tp_reject(settings: Settings) -> str | None:
    if settings.take_profit_covers_fees():
        return None
    round_trip_pct = settings.round_trip_taker_fee * 100.0
    buffer_pct = settings.tp_fee_buffer * 100.0
    return (
        f"take_profit {settings.take_profit_pct:g}% cannot cover "
        f"round-trip taker fees {round_trip_pct:g}% + buffer {buffer_pct:g}%"
    )
