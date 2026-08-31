from __future__ import annotations

from pathlib import Path

import pytest

from weex_scalp.config import Mode, Settings

FIXTURE = Path(__file__).parent / "fixtures" / "market_replay.jsonl"


def make_settings(**overrides) -> Settings:
    values = dict(
        mode=Mode.DRY_RUN,
        live_flag=False,
        i_understand_live=False,
        api_key="",
        api_secret="",
        api_passphrase="",
        symbol="BTCUSDT",
        demo_symbol="BTCSUSDT",
        quantity=0.001,
        max_notional_usdt=80.0,
        take_profit_pct=0.20,
        stop_loss_pct=0.10,
        max_daily_loss_usdt=15.0,
        max_spread_bps=4.0,
        window_seconds=15.0,
        entry_deviation_bps=6.0,
        cooldown_after_stop_seconds=300.0,
        stop_circuit_after=3,
        stop_circuit_window_seconds=1800.0,
        stop_circuit_pause_seconds=900.0,
        taker_fee_rate=0.0006,
        tp_fee_buffer=0.0002,
        max_orders_per_minute=8,
        max_position_qty=0.001,
        timezone="Europe/London",
        log_dir=Path("logs"),
        rest_base="https://api-contract.weex.com",
        public_ws_url="wss://ws-contract.weex.com/v3/ws/public",
        user_agent="weex-scalp-bot-test/0.1",
        duration_seconds=0.0,
        fixture_path=FIXTURE,
        status_every_seconds=100.0,
        quote_stall_seconds=90.0,
        ws_reconnect_delay_seconds=2.0,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(log_dir=tmp_path / "logs", fixture_path=FIXTURE)
