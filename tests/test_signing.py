from __future__ import annotations

import base64
import hashlib
import hmac

from weex_scalp.signing import build_sign_message, compact_json, sign_request, ws_private_sign


def test_sign_message_matches_documented_rule():
    timestamp = "1659076670000"
    method = "POST"
    path = "/capi/v3/order"
    body = compact_json(
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "positionSide": "LONG",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": "0.01",
            "price": "60000",
            "newClientOrderId": "my-order-0001",
        }
    )
    secret = "testsecret"
    message = build_sign_message(timestamp, method, path, body=body)
    assert message == timestamp + "POST" + path + body
    assert " " not in body
    expected = base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    assert sign_request(secret, timestamp, method, path, body=body) == expected


def test_query_string_is_prefixed_with_question_mark():
    message = build_sign_message(
        "1659076670000",
        "GET",
        "/capi/v3/sim/order/history",
        query="symbol=BTCSUSDT&limit=50",
    )
    assert message == (
        "1659076670000GET/capi/v3/sim/order/history?symbol=BTCSUSDT&limit=50"
    )


def test_no_query_and_no_body_has_no_extra_chars():
    message = build_sign_message("1", "GET", "/capi/v3/sim/balance")
    assert message == "1GET/capi/v3/sim/balance"


def test_private_ws_sign_is_timestamp_plus_path_only():
    secret = "s"
    timestamp = "1690000000000"
    message = timestamp + "/v3/ws/private"
    expected = base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    assert ws_private_sign(secret, timestamp) == expected
