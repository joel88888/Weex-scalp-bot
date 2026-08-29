from __future__ import annotations

import asyncio
import json
from pathlib import Path

from weex_scalp.ws import FixtureFeed, LocalBook, is_ping, parse_quote, subscribe_message

FIXTURE = Path(__file__).parent / "fixtures" / "market_replay.jsonl"


def test_parse_book_ticker():
    raw = {
        "e": "bookTicker",
        "E": 1672515782136,
        "s": "BNBUSDT",
        "d": {"u": 400900217, "b": "25.35190000", "B": "31.21", "a": "25.36520000", "A": "40.66"},
    }
    quote = parse_quote(raw)
    assert quote is not None
    assert quote.symbol == "BNBUSDT"
    assert quote.bid == 25.3519
    assert quote.ask == 25.3652
    assert abs(quote.mid - (25.3519 + 25.3652) / 2) < 1e-9
    assert quote.spread_bps > 0


def test_parse_24hr_ticker():
    raw = {
        "e": "24hrTicker",
        "E": 1672515782136,
        "s": "BTCUSDT",
        "d": {"c": "65000.2", "b": "65000.0", "B": "1", "a": "65000.4", "A": "1"},
    }
    quote = parse_quote(raw)
    assert quote is not None
    assert quote.last == 65000.2
    assert quote.bid == 65000.0
    assert quote.ask == 65000.4


def test_reject_crossed_book():
    raw = {"e": "bookTicker", "E": 1, "s": "BTCUSDT", "d": {"b": "100", "a": "99"}}
    assert parse_quote(raw) is None


def test_ping_detection_and_subscribe_shape():
    assert is_ping({"event": "ping", "time": "1"})
    assert is_ping({"type": "ping", "time": "1"})
    assert not is_ping({"e": "bookTicker"})
    msg = subscribe_message("btcusdt")
    assert msg["method"] == "SUBSCRIBE"
    assert "BTCUSDT@ticker" in msg["params"]
    assert "BTCUSDT@depth15" in msg["params"]


def test_fixture_feed_yields_quotes_without_network():
    async def _run() -> int:
        feed = FixtureFeed(FIXTURE)
        quotes = [q async for q in feed.quotes()]
        return len(quotes)

    count = asyncio.run(_run())
    assert count >= 18
    # Sanity: fixture file itself is offline JSONL, no hostnames required at parse time.
    assert "wss://" not in FIXTURE.read_text()


def test_fixture_lines_are_valid_json():
    for line in FIXTURE.read_text().splitlines():
        if line.strip():
            json.loads(line)


def test_futures_ticker_array_has_last_but_no_book_until_depth():
    book = LocalBook()
    quote = book.apply(
        {
            "e": "ticker",
            "E": 1773295738939,
            "s": "BTCUSDT",
            "d": [{"c": "102623.90", "m": "102620.00", "i": "102615.50"}],
        }
    )
    assert quote is not None
    assert quote.last == 102623.90
    assert quote.has_book is False
    quote = book.apply(
        {
            "e": "depth",
            "E": 1773295701456,
            "s": "BTCUSDT",
            "d": "SNAPSHOT",
            "b": [["102623.80", "2.1"], ["102623.70", "1.0"]],
            "a": [["102624.00", "1.2"], ["102624.10", "3.0"]],
        }
    )
    assert quote is not None
    assert quote.has_book is True
    assert quote.bid == 102623.80
    assert quote.ask == 102624.00
    assert quote.last == 102623.90


def test_depth_size_zero_removes_level():
    book = LocalBook()
    book.apply(
        {
            "e": "depth",
            "E": 1,
            "s": "BTCUSDT",
            "d": "SNAPSHOT",
            "b": [["100", "1"], ["99", "1"]],
            "a": [["101", "1"]],
        }
    )
    quote = book.apply(
        {
            "e": "depth",
            "E": 2,
            "s": "BTCUSDT",
            "d": "CHANGED",
            "b": [["100", "0"]],
            "a": [],
        }
    )
    assert quote is not None
    assert quote.bid == 99.0
