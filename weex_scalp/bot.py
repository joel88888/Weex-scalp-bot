"""Main loop: quotes in, risk check, optional order, flatten on halt/signal."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from weex_scalp.config import Mode, Settings
from weex_scalp.execution import (
    Broker,
    DryRunBroker,
    WeexBroker,
    apply_fill,
    realized_pnl,
    size_intent,
)
from weex_scalp.logging_setup import TradeLog, setup_logging
from weex_scalp.models import Decision, Fill, Position, Quote, Signal
from weex_scalp.rest import Clock, WeexRest
from weex_scalp.risk import Halted, RiskEngine
from weex_scalp.strategy import MeanReversionStrategy
from weex_scalp.ws import FixtureFeed, PublicWsFeed, QuoteFeed

log = logging.getLogger("weex_scalp.bot")


class Bot:
    def __init__(self, settings: Settings, rest: WeexRest | None = None) -> None:
        self.settings = settings
        self.rest = rest
        self.risk = RiskEngine(settings)
        self.strategy = MeanReversionStrategy(settings)
        self.broker: Broker = self._make_broker()
        self.position: Position | None = None
        self.last_quote: Quote | None = None
        self.trade_log = TradeLog(settings.log_dir / "trades.csv", settings.timezone)
        self._stop = asyncio.Event()
        self._started = time.time()
        self._last_status = 0.0
        self._feed: QuoteFeed | None = None

    def _make_broker(self) -> Broker:
        if self.settings.mode is Mode.DRY_RUN:
            return DryRunBroker(self.settings)
        if self.rest is None:
            raise RuntimeError("demo/live require a REST client")
        return WeexBroker(self.settings, self.rest)

    def request_stop(self, reason: str) -> None:
        log.info("stop requested: %s", reason)
        self._stop.set()
        if self._feed is not None and hasattr(self._feed, "stop"):
            self._feed.stop()

    def _on_ws_disconnect(self) -> None:
        self.risk.halt("public websocket disconnected")
        self._stop.set()

    async def run(self) -> int:
        setup_logging(self.settings)
        self._install_signals()
        self._announce()
        duration_task = None
        try:
            seed = await self._startup_exchange()
            self._feed = self._make_feed(seed)
            if self.settings.duration_seconds:
                duration_task = asyncio.create_task(self._stop_after(self.settings.duration_seconds))
            async for quote in self._feed.quotes():
                if self._stop.is_set() or self.risk.halted:
                    break
                self._on_quote(quote)
            if self.risk.halted:
                return 2
            return 0
        except Halted as exc:
            log.error("halted: %s", exc)
            return 2
        except Exception:
            log.exception("unhandled error")
            self.risk.halt("unhandled error")
            return 1
        finally:
            if duration_task is not None:
                duration_task.cancel()
            await self._shutdown()

    async def _stop_after(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            log.info("duration reached; stopping")
            self.request_stop("duration")
        except asyncio.CancelledError:
            return

    def _announce(self) -> None:
        s = self.settings
        log.info(
            "start mode=%s symbol=%s order_symbol=%s leverage_cap=%s qty=%s "
            "max_notional=%s tp=%s%% sl=%s%% daily_loss=%s live=%s understand=%s fixture=%s",
            s.mode.value,
            s.symbol,
            s.order_symbol,
            1,
            s.quantity,
            s.max_notional_usdt,
            s.take_profit_pct,
            s.stop_loss_pct,
            s.max_daily_loss_usdt,
            s.live_flag,
            s.i_understand_live,
            s.fixture_path,
        )
        if s.mode is Mode.DRY_RUN and not s.has_api_keys:
            log.info(
                "no API keys; DRY_RUN will use public market data only and simulate taker fills locally"
            )
        if s.mode is Mode.DEMO:
            log.info(
                "DEMO uses /capi/v3/sim/ (SUSDT). Public WS still streams %s. "
                "Demo orders use %s. Official sim surface is balance, positions, place, history.",
                s.symbol,
                s.order_symbol,
            )
        if s.mode is Mode.LIVE:
            log.warning("LIVE MODE is enabled. Real orders can spend real money.")

    async def _startup_exchange(self) -> Quote | None:
        if self.settings.fixture_path:
            return None
        if self.rest is None:
            # Dry-run without keys: still try a public clock/book check if we can.
            self.rest = WeexRest(self.settings, Clock())
        try:
            self.rest.sync_clock()
        except Exception:
            if self.settings.mode is Mode.DRY_RUN:
                log.warning("could not sync WEEX server time; continuing in dry-run")
            else:
                raise
        if self.settings.mode is Mode.LIVE:
            self.rest.assert_symbol_api_enabled(self.settings.symbol)
            self.rest.set_leverage_1x(self.settings.symbol)
        if self.settings.mode is Mode.DEMO and self.settings.has_api_keys:
            try:
                bals = self.rest.balance()
                log.info("demo_balance %s", bals)
            except Exception:
                log.exception("demo balance read failed (key not live yet? wait ~15 min)")
        return self._seed_book()

    def _seed_book(self) -> Quote | None:
        if self.rest is None:
            return None
        try:
            row = self.rest.book_ticker(self.settings.symbol)
            bid = float(row["bidPrice"])
            ask = float(row["askPrice"])
            quote = Quote(
                ts_ms=int(row.get("time") or time.time() * 1000),
                symbol=str(row.get("symbol") or self.settings.symbol).upper(),
                bid=bid,
                ask=ask,
                bid_qty=float(row.get("bidQty") or 0) or None,
                ask_qty=float(row.get("askQty") or 0) or None,
                has_book=True,
            )
            log.info("rest_book_seed bid=%s ask=%s mid=%.2f", bid, ask, quote.mid)
            return quote
        except Exception:
            log.warning("could not seed book from REST bookTicker", exc_info=True)
            return None

    def _make_feed(self, seed: Quote | None = None) -> QuoteFeed:
        if self.settings.fixture_path:
            log.info("using fixture %s", self.settings.fixture_path)
            return FixtureFeed(self.settings.fixture_path)
        return PublicWsFeed(
            url=self.settings.public_ws_url,
            symbol=self.settings.symbol,
            user_agent=self.settings.user_agent,
            on_disconnect=self._on_ws_disconnect,
            seed=seed,
        )

    def _on_quote(self, quote: Quote) -> None:
        self.last_quote = quote
        decision = self.strategy.on_quote(quote, self.position)
        self._maybe_status(quote, decision)

        if decision.signal in {Signal.LONG, Signal.SHORT}:
            self._try_entry(decision)
        elif decision.signal in {Signal.EXIT_TP, Signal.EXIT_SL, Signal.FLATTEN}:
            self._try_exit(decision)

        if self.risk.halted:
            self._stop.set()

    def _try_entry(self, decision: Decision) -> None:
        assert decision.intent is not None
        intent = size_intent(self.settings, decision.intent, decision.quote, self.risk)
        reject = self.risk.allow_entry(intent, decision.quote, self.position)
        if reject:
            log.info(
                "skip_entry signal=%s mid=%.2f reason=%s reject=%s",
                decision.signal.value,
                decision.quote.mid,
                decision.reason,
                reject,
            )
            return
        self.risk.record_order_attempt()
        fill = self.broker.submit(intent, decision.quote)
        self._log_decision(decision, fill, pnl=None)
        position, _ = apply_fill(self.position, fill)
        if position is not None:
            position.tp_price = intent.tp_price or 0.0
            position.sl_price = intent.sl_price or 0.0
            self.position = position
            log.info(
                "opened %s qty=%s entry=%s tp=%s sl=%s id=%s",
                position.side.value,
                position.qty,
                position.entry_price,
                position.tp_price,
                position.sl_price,
                fill.exchange_order_id,
            )
        else:
            log.warning(
                "order accepted but fill not confirmed (price=0). "
                "Not marking a local position. id=%s",
                fill.exchange_order_id,
            )

    def _try_exit(self, decision: Decision) -> None:
        if self.position is None or decision.intent is None:
            return
        reject = self.risk.allow_reduce(decision.intent, self.position)
        if reject:
            log.info("skip_exit reject=%s", reject)
            return
        self.risk.record_order_attempt()
        fill = self.broker.submit(decision.intent, decision.quote)
        pnl = None
        if fill.price > 0:
            pnl = realized_pnl(self.position, fill)
            self.risk.record_realized_pnl(pnl)
            if decision.signal is Signal.EXIT_SL:
                self.risk.on_stop_hit()
            self.position = None
        self._log_decision(decision, fill, pnl=pnl)
        log.info(
            "exit signal=%s px=%s pnl=%s daily=%s id=%s",
            decision.signal.value,
            fill.price,
            pnl,
            self.risk.daily_pnl,
            fill.exchange_order_id,
        )

    def _log_decision(self, decision: Decision, fill: Fill, pnl: float | None) -> None:
        log.info(
            "decision mode=%s mid=%.4f signal=%s order=%s fill_px=%s pnl=%s note=%s",
            self.settings.mode.value,
            decision.quote.mid,
            decision.signal.value,
            fill.exchange_order_id or fill.client_order_id,
            fill.price,
            pnl,
            fill.note,
        )
        self.trade_log.write(
            mode=self.settings.mode.value,
            symbol=self.settings.symbol,
            signal=decision.signal,
            fill=fill,
            pnl=pnl,
            mid=decision.quote.mid,
        )

    def _maybe_status(self, quote: Quote, decision: Decision) -> None:
        now = time.time()
        if now - self._last_status < self.settings.status_every_seconds:
            return
        self._last_status = now
        pos = "FLAT" if self.position is None else f"{self.position.side.value}@{self.position.entry_price:.2f}"
        when = _local_now(self.settings.timezone)
        print(
            f"[{when}] mode={self.settings.mode.value} mid={quote.mid:.2f} "
            f"spread={quote.spread_bps:.2f}bps signal={decision.signal.value} "
            f"reason={decision.reason} pos={pos} daily_pnl={self.risk.daily_pnl:.4f}",
            flush=True,
        )

    async def _shutdown(self) -> None:
        log.info("shutdown begin halted=%s reason=%s", self.risk.halted, self.risk.halt_reason)
        quote = self.last_quote
        try:
            self.broker.cancel_open()
        except Exception:
            log.exception("cancel_open during shutdown")
        if self.position is not None and quote is not None:
            try:
                log.warning("flattening open position on shutdown")
                fill = self.broker.flatten(self.position, quote, "halt_flatten")
                pnl = realized_pnl(self.position, fill) if fill.price > 0 else None
                if pnl is not None:
                    self.risk.record_realized_pnl(pnl)
                self.trade_log.write(
                    mode=self.settings.mode.value,
                    symbol=self.settings.symbol,
                    signal=Signal.FLATTEN,
                    fill=fill,
                    pnl=pnl,
                    mid=quote.mid,
                )
                self.position = None
            except Exception:
                log.exception("flatten failed — check the exchange UI")
        if self._feed is not None:
            try:
                await self._feed.close()
            except Exception:
                pass
        if self.rest is not None:
            self.rest.close()
        log.info("shutdown complete")

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()

        def _handle(signum: int) -> None:
            name = signal.Signals(signum).name
            self.risk.halt(f"received {name}")
            self.request_stop(name)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle, sig)
            except NotImplementedError:
                signal.signal(sig, lambda s, _f: _handle(s))


def _local_now(timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("Europe/London")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
