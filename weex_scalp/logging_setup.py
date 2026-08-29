"""Stdout + rotating file logs, plus a simple trades.csv."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from weex_scalp.config import Settings
from weex_scalp.models import Fill, Signal


def setup_logging(settings: Settings) -> logging.Logger:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("weex_scalp")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    file_handler = RotatingFileHandler(
        settings.log_dir / "bot.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


class TradeLog:
    def __init__(self, path: Path, timezone_name: str) -> None:
        self.path = path
        self.timezone_name = timezone_name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(
                    [
                        "timestamp",
                        "mode",
                        "symbol",
                        "signal",
                        "side",
                        "position_side",
                        "qty",
                        "price",
                        "fee",
                        "pnl",
                        "order_id",
                        "client_order_id",
                        "mid",
                        "note",
                        "simulated",
                    ]
                )

    def write(
        self,
        *,
        mode: str,
        symbol: str,
        signal: Signal | str,
        fill: Fill,
        pnl: float | None,
        mid: float | None,
    ) -> None:
        ts = _format_ts(fill.ts_ms, self.timezone_name)
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(
                [
                    ts,
                    mode,
                    symbol,
                    signal if isinstance(signal, str) else signal.value,
                    fill.side.value,
                    fill.position_side.value,
                    f"{fill.qty:.8f}",
                    f"{fill.price:.4f}",
                    f"{fill.fee:.6f}",
                    "" if pnl is None else f"{pnl:.6f}",
                    fill.exchange_order_id,
                    fill.client_order_id,
                    "" if mid is None else f"{mid:.4f}",
                    fill.note,
                    "1" if fill.simulated else "0",
                ]
            )


def _format_ts(ts_ms: int, timezone_name: str) -> str:
    if ts_ms <= 0:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    try:
        dt = dt.astimezone(ZoneInfo(timezone_name))
    except Exception:
        dt = dt.astimezone(ZoneInfo("Europe/London"))
    return dt.isoformat()
