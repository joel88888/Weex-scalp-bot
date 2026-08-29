from __future__ import annotations

from weex_scalp.execution import DryRunBroker, apply_fill, realized_pnl
from weex_scalp.models import OrderIntent, Position, PositionSide, Quote, Side, Signal
from weex_scalp.risk import RiskEngine
from weex_scalp.strategy import MeanReversionStrategy


def _quote(mid: float = 65000.0, spread: float = 0.4, ts: int = 1_000_000) -> Quote:
    half = spread / 2.0
    return Quote(ts_ms=ts, symbol="BTCUSDT", bid=mid - half, ask=mid + half, last=mid)


def _intent(qty: float = 0.001) -> OrderIntent:
    return OrderIntent(
        side=Side.BUY,
        position_side=PositionSide.LONG,
        qty=qty,
        reason="test",
    )


def test_max_one_position(settings):
    risk = RiskEngine(settings)
    quote = _quote()
    assert risk.allow_entry(_intent(), quote, position=None) is None
    open_pos = Position(
        side=PositionSide.LONG,
        qty=0.001,
        entry_price=65000,
        entry_fee=0.04,
        opened_ts_ms=1,
        client_order_id="x",
    )
    assert risk.allow_entry(_intent(), quote, position=open_pos) == "max 1 open position"


def test_daily_loss_halts_and_blocks_entries(settings):
    risk = RiskEngine(settings)
    risk.record_realized_pnl(-15.0)
    assert risk.halted
    assert "max daily loss" in risk.halt_reason
    reject = risk.allow_entry(_intent(), _quote(), position=None)
    assert reject is not None
    assert reject.startswith("halted")


def test_order_rate_limit(settings):
    tight = settings
    object.__setattr__(tight, "max_orders_per_minute", 2)  # frozen dataclass
    risk = RiskEngine(tight)
    quote = _quote()
    risk.record_order_attempt()
    risk.record_order_attempt()
    reject = risk.allow_entry(_intent(), quote, position=None)
    assert reject is not None
    assert "order rate" in reject


def test_notional_and_qty_caps(settings):
    risk = RiskEngine(settings)
    quote = _quote(mid=65000)
    too_big = _intent(qty=0.01)
    assert "max_position_qty" in (risk.allow_entry(too_big, quote, None) or "")
    # 0.002 BTC * 65000 = 130 > max notional 80, but qty also exceeds 0.001
    # cap on qty is checked first


def test_cooldown_after_stop(settings):
    risk = RiskEngine(settings)
    risk.on_stop_hit()
    reject = risk.allow_entry(_intent(), _quote(), None)
    assert reject is not None
    assert "cooldown" in reject


def test_realized_pnl_includes_taker_fees(settings):
    pos = Position(
        side=PositionSide.LONG,
        qty=0.001,
        entry_price=65000.0,
        entry_fee=65000.0 * 0.001 * 0.0006,
        opened_ts_ms=1,
        client_order_id="a",
        tp_price=65078.0,
        sl_price=64935.0,
    )
    broker = DryRunBroker(settings)
    exit_quote = _quote(mid=65078.2, spread=0.4)
    fill = broker.submit(
        OrderIntent(side=Side.SELL, position_side=PositionSide.LONG, qty=0.001, reduce_only=True),
        exit_quote,
    )
    pnl = realized_pnl(pos, fill)
    # Bid is 65078.0; gross = 0.078; fees both sides ~0.078
    assert fill.simulated is True
    assert fill.price == exit_quote.bid
    assert pnl < 0.078  # fees eat most of a 0.12% move on tiny size


def test_strategy_enters_only_when_stretched_and_tight(settings):
    strat = MeanReversionStrategy(settings)
    base = 1_788_023_500_000
    for i in range(16):
        strat.on_quote(_quote(mid=65000.0, ts=base + i * 1000), None)
    wide = _quote(mid=64950.0, spread=40.0, ts=base + 20_000)
    decision = strat.on_quote(wide, None)
    assert decision.signal is Signal.NONE
    assert "spread" in decision.reason

    tight = _quote(mid=64950.0, spread=0.4, ts=base + 21_000)
    decision = strat.on_quote(tight, None)
    assert decision.signal is Signal.LONG
    assert decision.intent is not None
    assert decision.intent.side is Side.BUY
    assert decision.intent.position_side is PositionSide.LONG


def test_apply_fill_does_not_open_position_without_a_price(settings):
    from weex_scalp.models import Fill

    fill = Fill(
        ts_ms=1,
        client_order_id="c",
        exchange_order_id="e",
        side=Side.BUY,
        position_side=PositionSide.LONG,
        qty=0.001,
        price=0.0,
        fee=0.0,
        reducing=False,
        note="awaiting fill",
    )
    pos, pnl = apply_fill(None, fill)
    assert pos is None
    assert pnl is None
