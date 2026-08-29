"""python -m weex_scalp"""

from __future__ import annotations

import argparse
import asyncio
import sys

from weex_scalp.bot import Bot
from weex_scalp.config import LiveModeError, load_settings
from weex_scalp.logging_setup import setup_logging
from weex_scalp.rest import Clock, WeexRest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WEEX BTCUSDT perpetual scalping scaffold (demo/dry-run by default)."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after N seconds (useful for a smoke test).",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="Replay a JSONL WebSocket fixture instead of connecting to WEEX.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env (default: .env). Missing file is fine.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(
            args.env_file,
            duration_seconds=args.duration,
            fixture_path=args.fixture,
        )
    except LiveModeError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    setup_logging(settings)
    rest = None
    if settings.uses_exchange_orders or not settings.fixture_path:
        rest = WeexRest(settings, Clock())
    bot = Bot(settings, rest=rest)
    return asyncio.run(bot.run())


if __name__ == "__main__":
    raise SystemExit(main())
