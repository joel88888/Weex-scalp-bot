"""Environment-driven settings. Live trading is double-gated and off by default."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

# Hard cap. Never send a higher leverage request. Ignore account settings above this.
LEVERAGE_HARD_CAP = 1

USER_AGENT = "weex-scalp-bot/0.1 (+https://github.com/joel88888/Weex-scalp-bot)"

FUTURES_REST = "https://api-contract.weex.com"
FUTURES_PUBLIC_WS = "wss://ws-contract.weex.com/v3/ws/public"
FUTURES_PRIVATE_WS = "wss://ws-contract.weex.com/v3/ws/private"
SPOT_REST = "https://api-spot.weex.com"


class Mode(str, Enum):
    DRY_RUN = "dry_run"
    DEMO = "demo"
    LIVE = "live"


class LiveModeError(RuntimeError):
    """Raised when live trading is requested without both confirm flags."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def demo_symbol_for(market_symbol: str, override: str = "") -> str:
    """Map a live USDT-M symbol to the paper-trading id (BTCUSDT -> BTCSUSDT).

    Official demo Place Order docs (Aug 2026) use BTCSUSDT. Balances are SUSDT.
    Live public market data still uses BTCUSDT.
    """
    if override:
        return override.upper()
    symbol = market_symbol.upper()
    if symbol.endswith("SUSDT"):
        return symbol
    if symbol.endswith("USDT"):
        return symbol[: -len("USDT")] + "SUSDT"
    return symbol


@dataclass(frozen=True)
class Settings:
    mode: Mode
    live_flag: bool
    i_understand_live: bool
    api_key: str
    api_secret: str
    api_passphrase: str
    symbol: str
    demo_symbol: str
    quantity: float
    max_notional_usdt: float
    take_profit_pct: float
    stop_loss_pct: float
    max_daily_loss_usdt: float
    max_spread_bps: float
    window_seconds: float
    entry_deviation_bps: float
    cooldown_after_stop_seconds: float
    stop_circuit_after: int
    stop_circuit_window_seconds: float
    stop_circuit_pause_seconds: float
    taker_fee_rate: float
    tp_fee_buffer: float
    max_orders_per_minute: int
    max_position_qty: float
    timezone: str
    log_dir: Path
    rest_base: str
    public_ws_url: str
    user_agent: str
    duration_seconds: float
    fixture_path: Path | None
    status_every_seconds: float
    quote_stall_seconds: float
    ws_reconnect_delay_seconds: float

    @property
    def round_trip_taker_fee(self) -> float:
        """Two taker fills, as a fraction (0.0006 * 2 = 0.0012)."""
        return 2.0 * self.taker_fee_rate

    @property
    def min_take_profit_pct(self) -> float:
        """Smallest TAKE_PROFIT_PCT that still nets more than round-trip fees + buffer.

        TAKE_PROFIT_PCT is percent (0.20 = 0.20%). Fee rate and buffer are fractions.
        """
        return (self.round_trip_taker_fee + self.tp_fee_buffer) * 100.0

    def take_profit_covers_fees(self) -> bool:
        """True when configured TP is strictly above round-trip taker fees plus buffer."""
        return (self.take_profit_pct / 100.0) > (self.round_trip_taker_fee + self.tp_fee_buffer)

    @property
    def order_symbol(self) -> str:
        """Symbol used on signed order/position calls."""
        if self.mode is Mode.DEMO:
            return self.demo_symbol
        return self.symbol

    @property
    def has_api_keys(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    @property
    def uses_exchange_orders(self) -> bool:
        return self.mode in {Mode.DEMO, Mode.LIVE}

    @property
    def private_prefix(self) -> str:
        if self.mode is Mode.DEMO:
            return "/capi/v3/sim"
        return "/capi/v3"


def resolve_mode(
    *,
    mode_raw: str,
    live_flag: bool,
    i_understand_live: bool,
    has_keys: bool,
) -> Mode:
    """Resolve operating mode. Live requires BOTH confirm flags."""
    requested = (mode_raw or "demo").strip().lower().replace("-", "_")
    if requested in {"live"} or live_flag:
        if not (live_flag and i_understand_live):
            raise LiveModeError(
                "Live trading is double-gated. Set LIVE=true AND I_UNDERSTAND_LIVE=true "
                "in .env (and MODE=live). Both default to false. Refusing to start."
            )
        return Mode.LIVE
    if requested in {"dry_run", "dryrun", "paper_local"}:
        return Mode.DRY_RUN
    # Default: demo. Fall back to local dry-run if keys are missing.
    if requested in {"demo", "sim", ""}:
        if has_keys:
            return Mode.DEMO
        return Mode.DRY_RUN
    raise ValueError(f"Unknown MODE={mode_raw!r}. Use demo, dry_run, or live.")


def load_settings(
    env_file: str | Path | None = ".env",
    *,
    duration_seconds: float | None = None,
    fixture_path: str | Path | None = None,
) -> Settings:
    if env_file:
        load_dotenv(env_file)

    api_key = os.getenv("WEEX_API_KEY", "").strip()
    api_secret = os.getenv("WEEX_API_SECRET", "").strip()
    api_passphrase = os.getenv("WEEX_API_PASSPHRASE", "").strip()
    live_flag = _env_bool("LIVE", False)
    i_understand = _env_bool("I_UNDERSTAND_LIVE", False)
    mode = resolve_mode(
        mode_raw=os.getenv("MODE", "demo"),
        live_flag=live_flag,
        i_understand_live=i_understand,
        has_keys=bool(api_key and api_secret and api_passphrase),
    )

    symbol = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
    demo_symbol = demo_symbol_for(symbol, os.getenv("DEMO_SYMBOL", "").strip())
    fixture = fixture_path or os.getenv("WS_FIXTURE", "").strip() or None

    return Settings(
        mode=mode,
        live_flag=live_flag,
        i_understand_live=i_understand,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        symbol=symbol,
        demo_symbol=demo_symbol,
        quantity=_env_float("QUANTITY", 0.001),
        max_notional_usdt=_env_float("MAX_NOTIONAL_USDT", 80.0),
        # 0.20% TP > 2 * 0.0006 taker + 2 bps buffer. 0.12% was fee-neutral.
        take_profit_pct=_env_float("TAKE_PROFIT_PCT", 0.20),
        stop_loss_pct=_env_float("STOP_LOSS_PCT", 0.10),
        max_daily_loss_usdt=_env_float("MAX_DAILY_LOSS_USDT", 15.0),
        max_spread_bps=_env_float("MAX_SPREAD_BPS", 4.0),
        window_seconds=_env_float("WINDOW_SECONDS", 15.0),
        entry_deviation_bps=_env_float("ENTRY_DEVIATION_BPS", 6.0),
        cooldown_after_stop_seconds=_env_float("COOLDOWN_AFTER_STOP_SECONDS", 300.0),
        stop_circuit_after=_env_int("STOP_CIRCUIT_AFTER", 3),
        stop_circuit_window_seconds=_env_float("STOP_CIRCUIT_WINDOW_SECONDS", 1800.0),
        stop_circuit_pause_seconds=_env_float("STOP_CIRCUIT_PAUSE_SECONDS", 900.0),
        taker_fee_rate=_env_float("TAKER_FEE_RATE", 0.0006),
        tp_fee_buffer=_env_float("TP_FEE_BUFFER", 0.0002),
        max_orders_per_minute=_env_int("MAX_ORDERS_PER_MINUTE", 8),
        max_position_qty=_env_float("MAX_POSITION_QTY", 0.001),
        timezone=os.getenv("TIMEZONE", "Europe/London"),
        log_dir=Path(os.getenv("LOG_DIR", "logs")),
        rest_base=os.getenv("WEEX_REST_BASE", FUTURES_REST).rstrip("/"),
        public_ws_url=os.getenv("WEEX_PUBLIC_WS", FUTURES_PUBLIC_WS),
        user_agent=os.getenv("WEEX_USER_AGENT", USER_AGENT),
        duration_seconds=(
            duration_seconds
            if duration_seconds is not None
            else _env_float("DURATION_SECONDS", 0.0)
        ),
        fixture_path=Path(fixture) if fixture else None,
        status_every_seconds=_env_float("STATUS_EVERY_SECONDS", 5.0),
        quote_stall_seconds=_env_float("QUOTE_STALL_SECONDS", 90.0),
        ws_reconnect_delay_seconds=_env_float("WS_RECONNECT_DELAY_SECONDS", 2.0),
    )
