"""WEEX public futures WebSocket.

The handshake MUST send a User-Agent or the firewall returns 403.
Public ping: {"event":"ping","time":"..."}  ->  {"method":"PONG","id":1}

Futures public channels (V3 docs, August 2026) are not the spot ones:
- `BTCUSDT@ticker` — last / mark / index. `d` is an array. No bid/ask.
- `BTCUSDT@depth15` — book updates. `d` is SNAPSHOT/CHANGED; `b`/`a` are [px, qty].
Spot-style `bookTicker` is still parsed so recorded fixtures and mixed feeds work.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from weex_scalp.config import USER_AGENT
from weex_scalp.models import Quote

log = logging.getLogger("weex_scalp.ws")

PONG = {"method": "PONG", "id": 1}
FUTURES_CHANNELS = ("ticker", "depth15")


def is_ping(message: dict[str, Any]) -> bool:
    return message.get("event") == "ping" or message.get("type") == "ping"


def subscribe_message(symbol: str, channels: list[str] | None = None) -> dict[str, Any]:
    symbol = symbol.upper()
    channels = channels or list(FUTURES_CHANNELS)
    params = [f"{symbol}@{ch}" for ch in channels]
    return {"method": "SUBSCRIBE", "params": params, "id": 1}


def parse_quote(message: dict[str, Any]) -> Quote | None:
    """Parse a single message that already has bid/ask (spot bookTicker / 24hrTicker)."""
    if not isinstance(message, dict):
        return None
    event = str(message.get("e") or message.get("event") or "")
    data = message.get("d")
    if isinstance(data, dict):
        body = data
    else:
        body = message

    symbol = str(message.get("s") or body.get("s") or body.get("symbol") or "")
    ts = _int(message.get("E") or message.get("time") or body.get("time"))

    bid = _first_float(body, ("b", "bidPrice", "bid"))
    ask = _first_float(body, ("a", "askPrice", "ask"))
    last = _first_float(body, ("c", "lastPrice", "last", "close"))

    if event in {"bookTicker", "24hrTicker"} or (bid is not None and ask is not None and event != "depth"):
        if bid is None or ask is None:
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            log.warning("rejecting crossed/invalid book bid=%s ask=%s", bid, ask)
            return None
        return Quote(
            ts_ms=ts,
            symbol=symbol.upper(),
            bid=bid,
            ask=ask,
            last=last,
            bid_qty=_first_float(body, ("B", "bidQty")),
            ask_qty=_first_float(body, ("A", "askQty")),
            has_book=True,
        )
    return None


def _ticker_last(message: dict[str, Any]) -> tuple[str, int, float] | None:
    """Futures `ticker` payload: d is a list of stats objects with last price `c`."""
    if str(message.get("e") or "") != "ticker":
        return None
    data = message.get("d")
    row: dict[str, Any]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]
    elif isinstance(data, dict):
        row = data
    else:
        return None
    last = _first_float(row, ("c", "lastPrice", "last"))
    if last is None or last <= 0:
        return None
    symbol = str(message.get("s") or row.get("s") or "")
    ts = _int(message.get("E") or row.get("C") or row.get("T"))
    return symbol.upper(), ts, last


class LocalBook:
    """Top-of-book from futures depth + optional last trade."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last: float | None = None
        self.symbol = ""
        self.ts_ms = 0

    def seed(self, quote: Quote) -> None:
        self.symbol = quote.symbol
        self.ts_ms = quote.ts_ms
        self.last = quote.last
        if quote.has_book and quote.bid > 0 and quote.ask > 0:
            self.bids = {quote.bid: quote.bid_qty or 0.0}
            self.asks = {quote.ask: quote.ask_qty or 0.0}

    def apply(self, message: dict[str, Any]) -> Quote | None:
        event = str(message.get("e") or message.get("event") or "")
        if message.get("result") is not None and event == "":
            log.info("ws_ack %s", message)
            return None

        parsed = parse_quote(message)
        if parsed is not None:
            self.seed(parsed)
            return parsed

        last_info = _ticker_last(message)
        if last_info is not None:
            symbol, ts, last = last_info
            self.symbol = symbol or self.symbol
            self.ts_ms = ts or self.ts_ms
            self.last = last
            return self.quote()

        if event == "depth":
            self._apply_depth(message)
            return self.quote()

        return None

    def _apply_depth(self, message: dict[str, Any]) -> None:
        kind = message.get("d")
        if isinstance(kind, str) and kind.upper() == "SNAPSHOT":
            self.bids.clear()
            self.asks.clear()
        symbol = str(message.get("s") or self.symbol)
        if symbol:
            self.symbol = symbol.upper()
        self.ts_ms = _int(message.get("E")) or self.ts_ms
        _merge_levels(self.bids, message.get("b"))
        _merge_levels(self.asks, message.get("a"))

    def quote(self) -> Quote | None:
        if self.bids and self.asks:
            bid = max(self.bids)
            ask = min(self.asks)
            if bid <= 0 or ask <= 0 or ask < bid:
                return None
            return Quote(
                ts_ms=self.ts_ms,
                symbol=self.symbol,
                bid=bid,
                ask=ask,
                last=self.last,
                bid_qty=self.bids.get(bid),
                ask_qty=self.asks.get(ask),
                has_book=True,
            )
        if self.last:
            return Quote(
                ts_ms=self.ts_ms,
                symbol=self.symbol,
                bid=self.last,
                ask=self.last,
                last=self.last,
                has_book=False,
            )
        return None


def _merge_levels(book: dict[float, float], rows: Any) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = float(row[0])
            qty = float(row[1])
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            book.pop(price, None)
        else:
            book[price] = qty


def _first_float(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class QuoteFeed:
    """Async quote source used by the bot. Live WS or a recorded fixture."""

    async def quotes(self) -> AsyncIterator[Quote]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def close(self) -> None:
        return None


class PublicWsFeed(QuoteFeed):
    def __init__(
        self,
        url: str,
        symbol: str,
        user_agent: str = USER_AGENT,
        on_disconnect: Callable[[], None] | None = None,
        seed: Quote | None = None,
    ) -> None:
        self.url = url
        self.symbol = symbol.upper()
        self.user_agent = user_agent
        self.on_disconnect = on_disconnect
        self.seed = seed
        self._stop = asyncio.Event()
        self.book = LocalBook()
        if seed is not None:
            self.book.seed(seed)

    def stop(self) -> None:
        self._stop.set()

    async def quotes(self) -> AsyncIterator[Quote]:
        import websockets
        from websockets.exceptions import ConnectionClosed

        headers = {"User-Agent": self.user_agent}
        log.info("ws_connect url=%s ua=%s", self.url, self.user_agent)
        if self.seed is not None:
            yield self.seed
        try:
            async with websockets.connect(
                self.url,
                additional_headers=headers,
                ping_interval=None,
                open_timeout=15,
                close_timeout=5,
            ) as ws:
                sub = subscribe_message(self.symbol)
                await ws.send(json.dumps(sub, separators=(",", ":")))
                log.info("ws_subscribed %s", sub["params"])
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("ws_non_json %s", raw[:200])
                        continue
                    if not isinstance(message, dict):
                        continue
                    if is_ping(message):
                        await ws.send(json.dumps(PONG, separators=(",", ":")))
                        continue
                    quote = self.book.apply(message)
                    if quote is not None:
                        yield quote
        except ConnectionClosed:
            log.error("ws_disconnected")
            if self.on_disconnect:
                self.on_disconnect()
        except Exception:
            log.exception("ws_error")
            if self.on_disconnect:
                self.on_disconnect()
            raise


class FixtureFeed(QuoteFeed):
    """Replay a JSONL file of raw WEEX WS messages. Used by tests and offline CI."""

    def __init__(self, path: Path, pace_seconds: float = 0.0) -> None:
        self.path = path
        self.pace_seconds = pace_seconds
        self.book = LocalBook()

    async def quotes(self) -> AsyncIterator[Quote]:
        text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            message = json.loads(line)
            if is_ping(message):
                continue
            quote = self.book.apply(message)
            if quote is None:
                continue
            if self.pace_seconds:
                await asyncio.sleep(self.pace_seconds)
            yield quote
