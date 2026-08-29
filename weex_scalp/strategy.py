"""Short-horizon mean-reversion scaffold.

This is a starting template, not a promised edge. Scalping often loses to
fees. Demo and dry-run fills have no real queue or slippage.

Idea: compare the current mid to a moving average of the last N seconds.
If price is stretched below the average and the spread is tight, fade it
(go long). If stretched above, go short. Exit on take-profit or stop-loss.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from weex_scalp.config import Settings
from weex_scalp.models import Decision, OrderIntent, Position, PositionSide, Quote, Side, Signal


@dataclass
class StrategyState:
    last_signal: Signal = Signal.NONE
    last_reason: str = "idle"
    sma: float | None = None
    deviation_bps: float | None = None


class MeanReversionStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._window: deque[tuple[int, float]] = deque()
        self.state = StrategyState()

    def on_quote(self, quote: Quote, position: Position | None) -> Decision:
        self._remember(quote)
        sma = self._sma()
        self.state.sma = sma
        extras = {
            "mid": quote.mid,
            "spread_bps": quote.spread_bps,
            "sma": sma,
            "samples": len(self._window),
        }

        if not quote.has_book:
            self.state.last_signal = Signal.NONE
            self.state.last_reason = "waiting_for_book"
            return Decision(Signal.NONE, "waiting_for_book", quote, extras=extras)

        if sma is None:
            self.state.last_signal = Signal.NONE
            self.state.last_reason = "warming_up"
            return Decision(Signal.NONE, "warming_up", quote, extras=extras)

        deviation_bps = ((quote.mid - sma) / sma) * 10_000.0
        self.state.deviation_bps = deviation_bps
        extras["deviation_bps"] = deviation_bps

        if position is not None:
            return self._manage_open(quote, position, extras)

        if quote.spread_bps > self.settings.max_spread_bps:
            reason = f"spread {quote.spread_bps:.2f}bps > max {self.settings.max_spread_bps}"
            self.state.last_signal = Signal.NONE
            self.state.last_reason = reason
            return Decision(Signal.NONE, reason, quote, extras=extras)

        threshold = self.settings.entry_deviation_bps
        if deviation_bps <= -threshold:
            intent = self._entry(PositionSide.LONG, quote, reason="below_sma")
            self.state.last_signal = Signal.LONG
            self.state.last_reason = "below_sma"
            return Decision(Signal.LONG, "below_sma", quote, intent, extras)
        if deviation_bps >= threshold:
            intent = self._entry(PositionSide.SHORT, quote, reason="above_sma")
            self.state.last_signal = Signal.SHORT
            self.state.last_reason = "above_sma"
            return Decision(Signal.SHORT, "above_sma", quote, intent, extras)

        self.state.last_signal = Signal.NONE
        self.state.last_reason = "inside_band"
        return Decision(Signal.NONE, "inside_band", quote, extras=extras)

    def _manage_open(self, quote: Quote, position: Position, extras: dict) -> Decision:
        extras["position_side"] = position.side.value
        extras["entry"] = position.entry_price
        if position.side is PositionSide.LONG:
            if quote.bid >= position.tp_price:
                return self._exit(quote, position, Signal.EXIT_TP, "take_profit", extras)
            if quote.bid <= position.sl_price:
                return self._exit(quote, position, Signal.EXIT_SL, "stop_loss", extras)
        else:
            if quote.ask <= position.tp_price:
                return self._exit(quote, position, Signal.EXIT_TP, "take_profit", extras)
            if quote.ask >= position.sl_price:
                return self._exit(quote, position, Signal.EXIT_SL, "stop_loss", extras)
        self.state.last_signal = Signal.NONE
        self.state.last_reason = "holding"
        return Decision(Signal.NONE, "holding", quote, extras=extras)

    def _entry(self, side: PositionSide, quote: Quote, reason: str) -> OrderIntent:
        qty = self.settings.quantity
        if side is PositionSide.LONG:
            entry = quote.ask
            tp = entry * (1.0 + self.settings.take_profit_pct / 100.0)
            sl = entry * (1.0 - self.settings.stop_loss_pct / 100.0)
            order_side = Side.BUY
        else:
            entry = quote.bid
            tp = entry * (1.0 - self.settings.take_profit_pct / 100.0)
            sl = entry * (1.0 + self.settings.stop_loss_pct / 100.0)
            order_side = Side.SELL
        return OrderIntent(
            side=order_side,
            position_side=side,
            qty=qty,
            reduce_only=False,
            tp_price=tp,
            sl_price=sl,
            reason=reason,
        )

    def _exit(
        self,
        quote: Quote,
        position: Position,
        signal: Signal,
        reason: str,
        extras: dict,
    ) -> Decision:
        order_side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        intent = OrderIntent(
            side=order_side,
            position_side=position.side,
            qty=position.qty,
            reduce_only=True,
            reason=reason,
        )
        self.state.last_signal = signal
        self.state.last_reason = reason
        return Decision(signal, reason, quote, intent, extras)

    def flatten_intent(self, position: Position) -> OrderIntent:
        order_side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        return OrderIntent(
            side=order_side,
            position_side=position.side,
            qty=position.qty,
            reduce_only=True,
            reason="flatten",
        )

    def _remember(self, quote: Quote) -> None:
        self._window.append((quote.ts_ms, quote.mid))
        cutoff = quote.ts_ms - int(self.settings.window_seconds * 1000)
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _sma(self) -> float | None:
        if len(self._window) < 5:
            return None
        span = self._window[-1][0] - self._window[0][0]
        if span < int(self.settings.window_seconds * 1000 * 0.6):
            return None
        return sum(mid for _, mid in self._window) / len(self._window)
