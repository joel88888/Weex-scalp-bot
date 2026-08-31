from __future__ import annotations

import re

import pytest

from weex_scalp.config import LEVERAGE_HARD_CAP, Mode, demo_symbol_for, resolve_mode, LiveModeError
from weex_scalp.models import OrderIntent, OrderType, PositionSide, Side
from weex_scalp.rest import build_order_payload, new_client_order_id
from weex_scalp.signing import CLIENT_ORDER_ID_PATTERN, compact_json


def test_futures_payload_is_uppercase_and_requires_client_id():
    intent = OrderIntent(
        side=Side.BUY,
        position_side=PositionSide.LONG,
        qty=0.001,
        order_type=OrderType.MARKET,
        tp_price=65100.0,
        sl_price=64900.0,
        client_order_id="w1788023457abcd1234",
        reason="below_sma",
    )
    payload = build_order_payload(symbol="btcusdt", intent=intent)
    assert payload["symbol"] == "BTCUSDT"
    assert payload["side"] == "BUY"
    assert payload["positionSide"] == "LONG"
    assert payload["type"] == "MARKET"
    assert payload["quantity"] == "0.001"
    assert payload["newClientOrderId"] == "w1788023457abcd1234"
    assert payload["tpTriggerPrice"] == "65100.00"
    assert payload["slTriggerPrice"] == "64900.00"
    assert payload["TpWorkingType"] == "MARK_PRICE"
    assert payload["SlWorkingType"] == "MARK_PRICE"
    body = compact_json(payload)
    assert " " not in body


def test_limit_payload_includes_price_and_tif():
    intent = OrderIntent(
        side=Side.SELL,
        position_side=PositionSide.SHORT,
        qty=0.01,
        order_type=OrderType.LIMIT,
        price=66000.5,
        client_order_id="my-order-0001",
    )
    payload = build_order_payload(symbol="BTCUSDT", intent=intent, attach_tp_sl=False)
    assert payload["type"] == "LIMIT"
    assert payload["timeInForce"] == "GTC"
    assert payload["price"] == "66000.50"
    assert "tpTriggerPrice" not in payload


def test_missing_client_id_is_rejected():
    intent = OrderIntent(
        side=Side.BUY,
        position_side=PositionSide.LONG,
        qty=0.001,
        client_order_id="",
    )
    with pytest.raises(ValueError, match="newClientOrderId"):
        build_order_payload(symbol="BTCUSDT", intent=intent)


def test_client_order_id_matches_weex_charset():
    cid = new_client_order_id()
    assert re.match(CLIENT_ORDER_ID_PATTERN, cid)
    assert 1 <= len(cid) <= 36


def test_demo_symbol_mapping():
    assert demo_symbol_for("BTCUSDT") == "BTCSUSDT"
    assert demo_symbol_for("ETHUSDT") == "ETHSUSDT"
    assert demo_symbol_for("BTCSUSDT") == "BTCSUSDT"
    assert demo_symbol_for("BTCUSDT", override="BTCSUSDT") == "BTCSUSDT"


def test_leverage_is_hard_capped_at_one():
    assert LEVERAGE_HARD_CAP == 1


def test_live_mode_requires_both_flags():
    with pytest.raises(LiveModeError):
        resolve_mode(mode_raw="live", live_flag=False, i_understand_live=False, has_keys=True)
    with pytest.raises(LiveModeError):
        resolve_mode(mode_raw="live", live_flag=True, i_understand_live=False, has_keys=True)
    with pytest.raises(LiveModeError):
        resolve_mode(mode_raw="demo", live_flag=True, i_understand_live=False, has_keys=True)
    assert (
        resolve_mode(mode_raw="live", live_flag=True, i_understand_live=True, has_keys=True)
        is Mode.LIVE
    )


def test_demo_falls_back_to_dry_run_without_keys():
    assert resolve_mode(mode_raw="demo", live_flag=False, i_understand_live=False, has_keys=False) is Mode.DRY_RUN
    assert resolve_mode(mode_raw="demo", live_flag=False, i_understand_live=False, has_keys=True) is Mode.DEMO


def test_load_settings_paper_safe_defaults(monkeypatch, tmp_path):
    from weex_scalp.config import load_settings

    for key in (
        "LIVE",
        "I_UNDERSTAND_LIVE",
        "MODE",
        "TAKE_PROFIT_PCT",
        "TAKER_FEE_RATE",
        "TP_FEE_BUFFER",
        "COOLDOWN_AFTER_STOP_SECONDS",
        "STOP_CIRCUIT_AFTER",
        "STOP_CIRCUIT_WINDOW_SECONDS",
        "STOP_CIRCUIT_PAUSE_SECONDS",
        "QUOTE_STALL_SECONDS",
        "WS_RECONNECT_DELAY_SECONDS",
        "WEEX_API_KEY",
        "WEEX_API_SECRET",
        "WEEX_API_PASSPHRASE",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.live_flag is False
    assert settings.i_understand_live is False
    assert settings.mode is Mode.DRY_RUN
    assert settings.take_profit_pct == 0.20
    assert settings.taker_fee_rate == 0.0006
    assert settings.tp_fee_buffer == 0.0002
    assert settings.take_profit_covers_fees() is True
    assert settings.min_take_profit_pct == pytest.approx(0.14)
    assert settings.cooldown_after_stop_seconds == 300.0
    assert settings.stop_circuit_after == 3
    assert settings.stop_circuit_window_seconds == 1800.0
    assert settings.stop_circuit_pause_seconds == 900.0
    assert settings.quote_stall_seconds == 90.0
    assert settings.ws_reconnect_delay_seconds == 2.0


def test_private_prefix_for_modes(settings):
    from dataclasses import replace

    assert replace(settings, mode=Mode.DEMO).private_prefix == "/capi/v3/sim"
    assert replace(settings, mode=Mode.LIVE).private_prefix == "/capi/v3"
    assert replace(settings, mode=Mode.DEMO).order_symbol == "BTCSUSDT"
    assert replace(settings, mode=Mode.LIVE).order_symbol == "BTCUSDT"
