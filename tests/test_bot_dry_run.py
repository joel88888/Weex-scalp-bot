from __future__ import annotations

import asyncio
from pathlib import Path

from weex_scalp.bot import Bot
from weex_scalp.config import Mode
from weex_scalp.models import PositionSide

FIXTURE = Path(__file__).parent / "fixtures" / "market_replay.jsonl"


def test_bot_dry_run_against_fixture_never_hits_live_paths(settings, monkeypatch):
    placed: list[str] = []

    def _fail_if_called(*_a, **_k):
        raise AssertionError("REST must not be used in fixture dry-run")

    settings = settings
    assert settings.mode is Mode.DRY_RUN
    bot = Bot(settings, rest=None)
    # Guard: DryRunBroker.submit is local; WeexRest must stay unused.
    if bot.rest is not None:
        monkeypatch.setattr(bot.rest, "place_order", _fail_if_called)
        monkeypatch.setattr(bot.rest, "private", _fail_if_called)

    code = asyncio.run(bot.run())
    assert code == 0
    log_text = (settings.log_dir / "bot.log").read_text(encoding="utf-8")
    assert "mode=dry_run" in log_text
    assert "place_order mode=live" not in log_text
    trades = (settings.log_dir / "trades.csv").read_text(encoding="utf-8")
    assert "timestamp" in trades
    # The replay stretches below the SMA — expect a simulated local fill, never a live path.
    assert "dry_run" in trades
    assert "dry-" in trades
    assert placed == []


def test_flatten_on_halt_closes_local_position(settings):
    from weex_scalp.execution import DryRunBroker
    from weex_scalp.models import Position, Quote

    bot = Bot(settings, rest=None)
    bot.broker = DryRunBroker(settings)
    bot.position = Position(
        side=PositionSide.LONG,
        qty=0.001,
        entry_price=65000.0,
        entry_fee=0.04,
        opened_ts_ms=1,
        client_order_id="open1",
        tp_price=65100,
        sl_price=64900,
    )
    bot.last_quote = Quote(ts_ms=2, symbol="BTCUSDT", bid=64990.0, ask=64990.4)
    bot.risk.halt("test halt")
    asyncio.run(bot._shutdown())
    assert bot.position is None
    trades = (settings.log_dir / "trades.csv").read_text(encoding="utf-8")
    assert "FLATTEN" in trades
