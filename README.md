# WEEX BTCUSDT scalping scaffold

A small Python bot that Joel Burpitt can run locally against [WEEX](https://www.weex.com/) futures.

It talks to WEEX V3, **starts in DEMO / dry-run**, logs every decision, and **cannot place a live order** unless you set two explicit flags.

This is a **starting scaffold**, not a promised edge. Short-horizon mean-reversion on a 15-second window usually loses to fees. Demo and dry-run have no real queue or slippage, so their PnL is **not** a live forecast.

Leverage is **hard-capped at 1x in code**. The bot never requests more, even if the account allows 400x.

## What the modes do

| Mode | Market data | Orders | Money |
| --- | --- | --- | --- |
| **demo** (default when keys exist) | Live public WS (`BTCUSDT`) | Signed REST on `/capi/v3/sim/` using `BTCSUSDT` / SUSDT | Paper only |
| **dry_run** (default when keys are missing) | Live public WS, or a recorded fixture | None. Local taker fills at the visible bid/ask | None |
| **live** | Live public WS | Signed REST on `/capi/v3/order` | **Real** — double-gated, off by default |

Live mode starts only if `LIVE=true` **and** `I_UNDERSTAND_LIVE=true`. Either flag missing → the process refuses to start.

Demo uses the same API key as live. With `LIVE=false` (the default) the bot still only hits `/capi/v3/sim/` even if keys are present.

## Honest warning

- Scalping a 0.12% target against a ~0.06% taker fee each way often loses before the market does anything clever.
- Demo fills are exchange paper trades. Dry-run fills are the top of the public book. Neither models queue position, partials, or a thin book walking through your size.
- Demo PnL is not a live forecast.
- Crypto futures can go to zero. You can lose more than you expect if you later raise size or turn live on. This bot will not protect you from a bad decision to go live.
- UK user: this is not financial advice. Check your own tax and regulatory position.

## Create a WEEX API key

1. Log in on the web → **Account → API Management**.
2. Create a key with **Read + Futures** only.
3. **Do not enable withdrawals.** Trade-only keys.
4. Passphrase: **alphanumeric only** (no special characters). You cannot recover it later.
5. Wait **about 15 minutes** after creating or editing a key before you debug auth errors (`-1049` is often “key not propagated yet”).
6. When you eventually go live, **bind an IP whitelist**. An unbound trade key works from anywhere on the internet.
7. The pair must be API-enabled. The bot checks `GET /capi/v3/market/apiTradingSymbols` before live orders. `BTCUSDT` is the live perpetual; demo orders use `BTCSUSDT` (documented on the official demo Place Order page as of August 2026).

## Install and run

You need Python 3.11+ (3.12 is fine).

```bash
cd Weex-scalp-bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` only if you have keys. For a first run you can leave keys empty.

### First run — local dry-run (no keys, no orders)

```bash
MODE=dry_run python -m weex_scalp
```

What you should see:

- A line `start mode=dry_run symbol=BTCUSDT ...`
- Status lines every few seconds, London time:

  `[2026-08-29 18:15:03 BST] mode=dry_run mid=65012.40 spread=0.31bps signal=NONE reason=inside_band pos=FLAT daily_pnl=0.0000`

- Occasional `decision` / `dry_fill` / `opened` / `exit` lines if the 15-second window stretches far enough.
- Files under `logs/`: rotating `bot.log` and `trades.csv`.

Stop with **Ctrl+C**. The bot cancels what it can, flattens a local position at the last bid/ask, and exits. SIGTERM does the same.

Leave it running for a minute so the moving-average window can warm up. The first ~15 seconds print `warming_up` and do nothing.

### Offline / CI (no WEEX network)

```bash
MODE=dry_run python -m weex_scalp --fixture tests/fixtures/market_replay.jsonl
```

This replays a recorded public book. It never opens a WebSocket and never places an order.

### Demo (paper SUSDT, needs keys)

```bash
# After filling WEEX_API_KEY / SECRET / PASSPHRASE in .env
MODE=demo python -m weex_scalp
```

The bot:

1. Syncs clock via `GET /capi/v3/market/time` (signing window is 30 seconds).
2. Streams live `wss://ws-contract.weex.com/v3/ws/public` for `BTCUSDT@ticker` and `BTCUSDT@depth15` (User-Agent is required or the handshake 403s). Futures has no `bookTicker` channel — bid/ask come from depth, last from ticker. The bot also seeds the book from public REST `bookTicker` once at startup.
3. Places paper orders on `POST /capi/v3/sim/order` with symbol `BTCSUSDT`.
4. Reads `GET /capi/v3/sim/balance` and `.../sim/position/allPosition`.

Official demo endpoints (WEEX FAQ, April 2026 / Place Order Demo, checked August 2026) are only:

- `GET /capi/v3/sim/balance`
- `GET /capi/v3/sim/position/allPosition`
- `POST /capi/v3/sim/order`
- `GET /capi/v3/sim/order/history`

There is **no documented sim cancel or closePositions**. Flatten is an opposite `MARKET` order. HTTP 200 can still be `{success: false}` — the client always checks `success`.

If demo auth is missing, the process falls back to dry-run and says so in the log.

### Live (real money) — do not do this until you understand the above

```bash
# Both flags required. MODE=live alone is refused.
LIVE=true
I_UNDERSTAND_LIVE=true
MODE=live
```

Live path is `POST /capi/v3/order` on `BTCUSDT`. The bot sets leverage to 1x (`POST /capi/v3/account/leverage`) and checks the API symbol whitelist first.

## How to read the log

Stdout and `logs/bot.log` share the same lines. Useful fields:

| Field | Meaning |
| --- | --- |
| `mode=` | `dry_run` / `demo` / `live` |
| `mid=` | Mid of best bid/ask |
| `signal=` | `NONE`, `LONG`, `SHORT`, `EXIT_TP`, `EXIT_SL`, `FLATTEN` |
| `order=` / `id=` | Exchange order id, or `dry-...` in dry-run |
| `fill_px=` | Confirmed fill price. `0` means “accepted, not confirmed yet” — **we do not invent a fill** |
| `pnl=` | Realized PnL after fees on a close |
| `HALT` | Risk engine stopped the bot |

`logs/trades.csv` is one row per fill or flatten: timestamp (Europe/London), mode, signal, side, qty, price, fee, pnl, order ids.

## The strategy (scaffold)

1. Keep the last `WINDOW_SECONDS` of mid prices.
2. Compare the current mid to that simple moving average.
3. If the mid is at least `ENTRY_DEVIATION_BPS` below the average **and** the spread is under `MAX_SPREAD_BPS`, go **long** (buy the ask).
4. If stretched above, go **short** (sell the bid).
5. Take profit / stop loss are percent from the entry. Max **one** open position.
6. After a stop, wait `COOLDOWN_AFTER_STOP_SECONDS`.
7. Fees are modelled as taker (`TAKER_FEE_RATE`) on both sides unless you later switch to `POST_ONLY` (live futures only; demo Place Order docs do not list `POST_ONLY`).

No grid, no martingale, no copy-trading, no 400x.

## Risk controls the strategy cannot bypass

- Max position quantity and max notional per order (sized as **1x**; higher account leverage is ignored).
- Max daily loss → flatten-and-halt (London calendar day).
- Max orders per minute (default 8; WEEX futures allow 300).
- Public WebSocket disconnect → flatten-and-halt (no silent reconnect-and-trade).
- Unhandled exception → flatten-and-halt.
- Ctrl+C / SIGTERM → cancel open orders if the API allows, flatten, stop.

## WEEX V3 facts this repo was built against (August 2026)

Re-check the live docs before you change paths. They move.

| Item | Value |
| --- | --- |
| Spot REST | `https://api-spot.weex.com` `/api/v3/` |
| Futures REST | `https://api-contract.weex.com` `/capi/v3/` |
| Futures demo | same host, `/capi/v3/sim/` (SUSDT, e.g. `BTCSUSDT`) |
| Public WS | `wss://ws-contract.weex.com/v3/ws/public` (`BTCUSDT@ticker`, `BTCUSDT@depth15`) |
| Private WS | `wss://ws-contract.weex.com/v3/ws/private` |
| Auth headers | `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-PASSPHRASE`, `ACCESS-TIMESTAMP` (ms) |
| Sign | HMAC-SHA256 then Base64 of `timestamp + METHOD + requestPath [?query] [body]` |
| Clock | Must be within 30s of `GET /capi/v3/market/time` |
| WS | **User-Agent required** or handshake 403 |
| Futures orders | `BUY`/`SELL`, `LIMIT`/`MARKET`, required `positionSide` `LONG`/`SHORT`, required `newClientOrderId` |
| Symbols | Uppercase only (`BTCUSDT`). `-1121` if lowercase |
| Success | Always read `success` on HTTP 200 |
| Rate limits | 500 weight / 10s / IP; 300 orders / min / account |
| No official standalone `weex-python-sdk` | This repo uses a thin signed client. Do not pull a random unofficial SDK. |

ccxt has discussed WEEX sandbox via `/capi/v3/sim/`; this bot does **not** depend on ccxt.

## Tests

```bash
pytest
```

Coverage: signing, order payload, live-mode gates, risk halt, fake WebSocket fixture. CI should not need WEEX.

Optional live-network smoke (public data only):

```bash
MODE=dry_run python -m weex_scalp --duration 20
```

## Project layout

```
weex_scalp/          # the bot
  signing.py         # HMAC + compact JSON
  rest.py            # thin V3 REST client
  ws.py              # public WS + fixture replay
  strategy.py        # mean-reversion scaffold
  risk.py            # hard gates
  execution.py       # dry-run broker + demo/live broker
  bot.py             # loop, signals, flatten
tests/               # no live orders
.env.example         # copy to .env
```

## If something breaks

| Symptom | Likely cause |
| --- | --- |
| WS 403 | Missing User-Agent (this client always sends one) |
| `-1047` | Signed body ≠ transmitted body, or wrong path prefix |
| `-1046` | Clock drift — bot syncs server time on start |
| `-1049` | Key too new, or passphrase has special characters |
| `-1052` | Futures permission not ticked |
| `-1058` | Pair not on `apiTradingSymbols` |
| `-1121` | Lowercase symbol |
| HTTP 429 | Rate limit; WEEX then bans for ~10s |

Timezone for logs and the daily-loss reset is `Europe/London` (GMT/BST).
