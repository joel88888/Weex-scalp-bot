"""Thin signed WEEX futures V3 REST client.

We own this client. ccxt may list WEEX, but sandbox `/capi/v3/sim/` support is
not something we depend on. Official standalone weex-python-sdk does not exist.

HTTP 200 can still be `{success: false}` — always check `success`.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from weex_scalp.config import LEVERAGE_HARD_CAP, Settings, USER_AGENT
from weex_scalp.models import OrderIntent, OrderType
from weex_scalp.signing import compact_json, sign_request

log = logging.getLogger("weex_scalp.rest")

CLIENT_ORDER_ID_RE = r"^[.A-Z:/a-z0-9_-]{1,36}$"


class WeexApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class Clock:
    """Keep local clock within WEEX's 30-second signing window."""

    def __init__(self) -> None:
        self.offset_ms = 0

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def sync_from_server(self, server_ms: int) -> None:
        local = int(time.time() * 1000)
        self.offset_ms = int(server_ms) - local
        log.info("clock_sync server_ms=%s offset_ms=%s", server_ms, self.offset_ms)


def build_order_payload(
    *,
    symbol: str,
    intent: OrderIntent,
    attach_tp_sl: bool = True,
) -> dict[str, Any]:
    """Build a futures (or sim) Place Order body. Fields are uppercase as required."""
    if not intent.client_order_id:
        raise ValueError("newClientOrderId is required on WEEX futures orders")
    if len(intent.client_order_id) > 36:
        raise ValueError("newClientOrderId must be 1-36 characters")

    payload: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": intent.side.value,
        "positionSide": intent.position_side.value,
        "type": intent.order_type.value,
        "quantity": _qty_str(intent.qty),
        "newClientOrderId": intent.client_order_id,
    }
    if intent.order_type is OrderType.LIMIT:
        if intent.price is None:
            raise ValueError("LIMIT orders require a price")
        payload["timeInForce"] = "GTC"
        payload["price"] = _price_str(intent.price)
    if attach_tp_sl and intent.tp_price:
        payload["tpTriggerPrice"] = _price_str(intent.tp_price)
        payload["TpWorkingType"] = "MARK_PRICE"
    if attach_tp_sl and intent.sl_price:
        payload["slTriggerPrice"] = _price_str(intent.sl_price)
        payload["SlWorkingType"] = "MARK_PRICE"
    return payload


def _qty_str(qty: float) -> str:
    text = f"{qty:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _price_str(price: float) -> str:
    text = f"{price:.2f}"
    return text


def new_client_order_id(prefix: str = "w") -> str:
    """WEEX: 1-36 chars, charset ^[.A-Z:/a-z0-9_-]{1,36}$."""
    # 1 + 13 + 8 = 22 chars. Milliseconds + 4 hex bytes.
    import secrets

    return f"{prefix}{int(time.time() * 1000)}{secrets.token_hex(4)}"[:36]


class WeexRest:
    def __init__(self, settings: Settings, clock: Clock | None = None) -> None:
        self.settings = settings
        self.clock = clock or Clock()
        self._client = httpx.Client(
            base_url=settings.rest_base,
            timeout=15.0,
            headers={"User-Agent": settings.user_agent or USER_AGENT},
        )

    def close(self) -> None:
        self._client.close()

    def sync_clock(self) -> int:
        data = self.public_get("/capi/v3/market/time")
        server = _extract_server_time(data)
        self.clock.sync_from_server(server)
        return server

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode(params or {}, doseq=True)
        url = path if not query else f"{path}?{query}"
        response = self._client.get(url)
        return self._parse(response, path)

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        data = self.public_get("/capi/v3/market/ticker/bookTicker", {"symbol": symbol.upper()})
        if isinstance(data, list):
            if not data:
                raise WeexApiError("empty bookTicker response")
            return data[0]
        return data

    def api_trading_symbols(self) -> list[str]:
        data = self.public_get("/capi/v3/market/apiTradingSymbols")
        return _normalize_symbol_list(data)

    def assert_symbol_api_enabled(self, symbol: str) -> None:
        symbols = self.api_trading_symbols()
        if symbol.upper() not in {s.upper() for s in symbols}:
            raise WeexApiError(
                f"{symbol} is not on GET /capi/v3/market/apiTradingSymbols. "
                "Enable the pair for API trading in WEEX before going live."
            )

    def private(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.settings.has_api_keys:
            raise WeexApiError("API keys are required for private endpoints")
        query = urlencode(sorted((params or {}).items()), doseq=True)
        body = compact_json(json_body) if json_body is not None else ""
        timestamp = str(self.clock.now_ms())
        sign = sign_request(
            self.settings.api_secret,
            timestamp,
            method,
            path,
            query=query,
            body=body,
        )
        headers = {
            "ACCESS-KEY": self.settings.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-PASSPHRASE": self.settings.api_passphrase,
            "ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
            "User-Agent": self.settings.user_agent or USER_AGENT,
        }
        url = path if not query else f"{path}?{query}"
        response = self._client.request(
            method.upper(),
            url,
            headers=headers,
            content=body.encode("utf-8") if body else None,
        )
        return self._parse(response, path)

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = f"{self.settings.private_prefix}/order"
        data = self.private("POST", path, json_body=payload)
        _require_success(data, action="place_order")
        return data

    def cancel_order(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        path = f"{self.settings.private_prefix}/order"
        params: dict[str, Any] = {}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        if not params:
            raise ValueError("cancel_order requires orderId or origClientOrderId")
        data = self.private("DELETE", path, params=params)
        # Official demo catalog (Aug 2026) lists place/history but not cancel.
        # Treat missing-order codes as already-gone rather than a crash.
        if isinstance(data, dict) and data.get("success") is False:
            code = str(data.get("errorCode") or "")
            if code in {"-1054", "-3200", "43001", "43004"}:
                log.warning("cancel_order already gone code=%s body=%s", code, data)
                return data
        _require_success(data, action="cancel_order")
        return data

    def order_history(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        path = f"{self.settings.private_prefix}/order/history"
        data = self.private("GET", path, params={"symbol": symbol.upper(), "limit": limit})
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    def all_positions(self) -> list[dict[str, Any]]:
        if self.settings.mode.value == "demo":
            path = "/capi/v3/sim/position/allPosition"
        else:
            path = "/capi/v3/account/position/allPosition"
        data = self.private("GET", path)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    def balance(self) -> list[dict[str, Any]]:
        if self.settings.mode.value == "demo":
            path = "/capi/v3/sim/balance"
        else:
            path = "/capi/v3/account/balance"
        data = self.private("GET", path)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    def set_leverage_1x(self, symbol: str) -> dict[str, Any] | None:
        """Force 1x. Demo has no leverage endpoint in the official sim catalog."""
        if self.settings.mode.value == "demo":
            log.info("demo mode: no official sim leverage endpoint; sizing as %sx", LEVERAGE_HARD_CAP)
            return None
        payload = {
            "symbol": symbol.upper(),
            "marginType": "CROSSED",
            "crossLeverage": str(LEVERAGE_HARD_CAP),
            "isolatedLongLeverage": str(LEVERAGE_HARD_CAP),
            "isolatedShortLeverage": str(LEVERAGE_HARD_CAP),
        }
        return self.private("POST", "/capi/v3/account/leverage", json_body=payload)

    def close_positions(self, symbol: str) -> Any:
        if self.settings.mode.value == "demo":
            raise WeexApiError(
                "Official demo catalog has no /capi/v3/sim/closePositions. "
                "Flatten with an opposite MARKET order instead."
            )
        return self.private("POST", "/capi/v3/closePositions", json_body={"symbol": symbol.upper()})

    def _parse(self, response: httpx.Response, path: str) -> Any:
        text = response.text
        try:
            data = response.json() if text else {}
        except ValueError as exc:
            raise WeexApiError(
                f"non-JSON response status={response.status_code} path={path} body={text[:200]}",
                status=response.status_code,
                body=text,
            ) from exc
        if response.status_code == 429:
            raise WeexApiError("HTTP 429 rate limited; back off at least 10s", status=429, body=data)
        if response.status_code >= 400:
            raise WeexApiError(
                f"HTTP {response.status_code} path={path} body={data}",
                status=response.status_code,
                body=data,
            )
        return data


def _extract_server_time(data: Any) -> int:
    if isinstance(data, dict):
        if "serverTime" in data:
            return int(data["serverTime"])
        inner = data.get("data")
        if isinstance(inner, dict) and "serverTime" in inner:
            return int(inner["serverTime"])
        if isinstance(inner, (int, float, str)):
            return int(inner)
    raise WeexApiError(f"unexpected server time payload: {data!r}")


def _normalize_symbol_list(data: Any) -> list[str]:
    if isinstance(data, list):
        out: list[str] = []
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and "symbol" in item:
                out.append(str(item["symbol"]))
        return out
    if isinstance(data, dict):
        inner = data.get("data") or data.get("symbols") or data.get("list")
        return _normalize_symbol_list(inner) if inner is not None else []
    return []


def _require_success(data: Any, *, action: str) -> None:
    if isinstance(data, dict) and "success" in data and data.get("success") is not True:
        raise WeexApiError(
            f"{action} rejected success=false errorCode={data.get('errorCode')} "
            f"errorMessage={data.get('errorMessage')} body={data}"
        )
