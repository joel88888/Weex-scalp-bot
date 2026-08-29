"""Order execution: local dry-run simulation, or signed WEEX demo/live orders.

Dry-run fills use the visible bid/ask from the public book (taker). That is
not an invented print — it is the top of book at decision time. Demo/live
fills come from the exchange response / order history only.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from weex_scalp.config import Mode, Settings
from weex_scalp.models import Fill, OrderIntent, Position, PositionSide, Quote, Side
from weex_scalp.rest import WeexRest, build_order_payload, new_client_order_id
from weex_scalp.risk import RiskEngine

log = logging.getLogger("weex_scalp.execution")


class Broker:
    def submit(self, intent: OrderIntent, quote: Quote) -> Fill:
        raise NotImplementedError

    def confirm_fill(self, fill: Fill, quote: Quote) -> Fill:
        return fill

    def flatten(self, position: Position, quote: Quote, reason: str) -> Fill:
        raise NotImplementedError

    def cancel_open(self) -> None:
        return None

    def sync_position(self) -> Position | None:
        return None


class DryRunBroker(Broker):
    """Local taker fills at the visible bid/ask. Never talks to private REST."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_client_id = ""

    def submit(self, intent: OrderIntent, quote: Quote) -> Fill:
        price = _taker_price(intent.side, quote)
        fee = abs(price * intent.qty) * self.settings.taker_fee_rate
        cid = intent.client_order_id or new_client_order_id("d")
        self.last_client_id = cid
        fill = Fill(
            ts_ms=quote.ts_ms or int(time.time() * 1000),
            client_order_id=cid,
            exchange_order_id=f"dry-{cid}",
            side=intent.side,
            position_side=intent.position_side,
            qty=intent.qty,
            price=price,
            fee=fee,
            reducing=intent.reduce_only,
            note=f"dry_run taker at {'ask' if intent.side is Side.BUY else 'bid'} {intent.reason}",
            simulated=True,
        )
        log.info(
            "dry_fill id=%s side=%s pos=%s qty=%s px=%s fee=%s",
            fill.exchange_order_id,
            fill.side.value,
            fill.position_side.value,
            fill.qty,
            fill.price,
            fill.fee,
        )
        return fill

    def flatten(self, position: Position, quote: Quote, reason: str) -> Fill:
        side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        intent = OrderIntent(
            side=side,
            position_side=position.side,
            qty=position.qty,
            reduce_only=True,
            reason=reason,
        )
        return self.submit(intent, quote)


class WeexBroker(Broker):
    """Places orders on /capi/v3/sim/ (demo) or /capi/v3/ (live)."""

    def __init__(self, settings: Settings, rest: WeexRest) -> None:
        self.settings = settings
        self.rest = rest
        self._open_client_ids: list[str] = []

    def submit(self, intent: OrderIntent, quote: Quote) -> Fill:
        cid = intent.client_order_id or new_client_order_id("w")
        intent.client_order_id = cid
        payload = build_order_payload(
            symbol=self.settings.order_symbol,
            intent=intent,
            attach_tp_sl=not intent.reduce_only,
        )
        log.info(
            "place_order mode=%s path=%s/order payload=%s",
            self.settings.mode.value,
            self.settings.private_prefix,
            payload,
        )
        data = self.rest.place_order(payload)
        self._open_client_ids.append(cid)
        exchange_id = str(data.get("orderId") or "")
        # Do not invent a fill price. Record a submitted order; confirm via history.
        submitted = Fill(
            ts_ms=quote.ts_ms or int(time.time() * 1000),
            client_order_id=cid,
            exchange_order_id=exchange_id,
            side=intent.side,
            position_side=intent.position_side,
            qty=intent.qty,
            price=0.0,
            fee=0.0,
            reducing=intent.reduce_only,
            note="accepted success=true; awaiting fill confirmation",
            simulated=False,
        )
        confirmed = self.confirm_fill(submitted, quote)
        return confirmed

    def confirm_fill(self, fill: Fill, quote: Quote) -> Fill:
        """Look up order history. If not filled yet, leave price at 0 and say so."""
        try:
            history = self.rest.order_history(self.settings.order_symbol, limit=50)
        except Exception:
            log.exception("order_history failed; not inventing a fill")
            return fill
        match = _find_history(history, fill)
        if match is None:
            log.info("no history row yet for %s", fill.client_order_id)
            return fill
        avg = _float(match.get("avgPrice") or match.get("price"))
        qty = _float(match.get("executedQty") or match.get("origQty")) or fill.qty
        status = str(match.get("status") or "")
        if avg <= 0 or qty <= 0:
            log.info("history row not filled status=%s row=%s", status, match)
            return fill
        fee = abs(avg * qty) * self.settings.taker_fee_rate
        return Fill(
            ts_ms=_int(match.get("updateTime") or match.get("time")) or fill.ts_ms,
            client_order_id=str(match.get("clientOrderId") or fill.client_order_id),
            exchange_order_id=str(match.get("orderId") or fill.exchange_order_id),
            side=fill.side,
            position_side=fill.position_side,
            qty=qty,
            price=avg,
            fee=fee,
            reducing=fill.reducing,
            note=f"history status={status}",
            simulated=False,
        )

    def flatten(self, position: Position, quote: Quote, reason: str) -> Fill:
        side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        intent = OrderIntent(
            side=side,
            position_side=position.side,
            qty=position.qty,
            reduce_only=True,
            reason=reason,
        )
        if self.settings.mode is Mode.LIVE:
            try:
                data = self.rest.close_positions(self.settings.order_symbol)
                log.info("closePositions %s", data)
            except Exception:
                log.exception("closePositions failed; falling back to opposite MARKET")
        return self.submit(intent, quote)

    def cancel_open(self) -> None:
        # Demo official catalog (Aug 2026) does not document sim cancel.
        for cid in list(self._open_client_ids):
            try:
                self.rest.cancel_order(client_order_id=cid)
            except Exception:
                log.warning("cancel %s failed (demo may not expose cancel)", cid, exc_info=True)
        self._open_client_ids.clear()

    def sync_position(self) -> Position | None:
        rows = self.rest.all_positions()
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in {self.settings.order_symbol, self.settings.symbol}:
                continue
            size = abs(_float(row.get("size")))
            if size <= 0:
                continue
            side_raw = str(row.get("side") or row.get("positionSide") or "").upper()
            side = PositionSide.LONG if side_raw == "LONG" else PositionSide.SHORT
            open_value = _float(row.get("openValue"))
            entry = (open_value / size) if open_value and size else 0.0
            return Position(
                side=side,
                qty=size,
                entry_price=entry,
                entry_fee=_float(row.get("openFee")),
                opened_ts_ms=_int(row.get("updatedTime") or row.get("createdTime")),
                client_order_id="",
                exchange_order_id=str(row.get("id") or ""),
            )
        return None


def apply_fill(position: Position | None, fill: Fill) -> tuple[Position | None, float | None]:
    """Update local position. Returns (new_position, realized_pnl_or_None)."""
    if not fill.reducing:
        if fill.price <= 0:
            # Accepted but unconfirmed — do not pretend we are in a position at $0.
            return position, None
        side = fill.position_side
        tp, sl = _tp_sl_from_fill(fill)
        return (
            Position(
                side=side,
                qty=fill.qty,
                entry_price=fill.price,
                entry_fee=fill.fee,
                opened_ts_ms=fill.ts_ms,
                client_order_id=fill.client_order_id,
                exchange_order_id=fill.exchange_order_id,
                tp_price=tp,
                sl_price=sl,
            ),
            None,
        )

    if position is None or fill.price <= 0:
        return position, None
    pnl = realized_pnl(position, fill)
    return None, pnl


def realized_pnl(position: Position, exit_fill: Fill) -> float:
    if position.side is PositionSide.LONG:
        gross = (exit_fill.price - position.entry_price) * position.qty
    else:
        gross = (position.entry_price - exit_fill.price) * position.qty
    return gross - position.entry_fee - exit_fill.fee


def _tp_sl_from_fill(fill: Fill) -> tuple[float, float]:
    # Placeholders; strategy sets tp/sl on the intent and bot copies them after.
    return 0.0, 0.0


def _taker_price(side: Side, quote: Quote) -> float:
    return quote.ask if side is Side.BUY else quote.bid


def _find_history(rows: list[dict[str, Any]], fill: Fill) -> dict[str, Any] | None:
    for row in rows:
        cid = str(row.get("clientOrderId") or "")
        oid = str(row.get("orderId") or "")
        if cid and cid == fill.client_order_id:
            return row
        if fill.exchange_order_id and oid == str(fill.exchange_order_id):
            return row
    return None


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def size_intent(settings: Settings, intent: OrderIntent, quote: Quote, risk: RiskEngine) -> OrderIntent:
    """Cap quantity by max position and max notional. Leverage is always 1x."""
    ref = quote.ask if intent.position_side is PositionSide.LONG else quote.bid
    qty = min(intent.qty, settings.max_position_qty, settings.quantity)
    if ref > 0:
        max_qty = settings.max_notional_usdt / ref
        qty = min(qty, max_qty)
    intent.qty = float(f"{qty:.8f}")
    return intent
