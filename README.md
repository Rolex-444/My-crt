# CRT Telegram Alert Bot (Delta Exchange India)

Alert-only scanner for Delta Exchange India perpetual futures. It does **not** place, edit, or close orders.

## Current strategy implementation
- 4H CRT candle: body must be greater than upper wick + lower wick.
- The next completed 4H candle must sweep CRT high or low and close back inside the CRT range.
- Key-level gate: at least one 4H level must overlap either CRT candle or its second candle: previous completed 4H candle high/low (Old High/Old Low), 3-candle FVG, simplified OB, IFVG, or rejection block.
- 15M swing pivots use 2 candles left + 2 candles right; pivots are only confirmed after the right-side candles close.
- Turtle Body Soup: for SELL, 15M candle body crosses above a confirmed swing high and candle closes below it. BUY is reversed at a swing low.
- The TBS candle becomes Model #1. Entry alert only after a later 15M candle closes beyond Model #1 low (SELL) / high (BUY).
- SMT is informational only and does not block an otherwise valid entry. BTC vs ETH, ETH vs BTC, XRP vs BTC.
- At CRT midpoint: partial-profit / move-SL-to-breakeven reminder. At opposite CRT boundary: full-exit reminder.

## Important interpretation notes
OB and rejection-block language can vary between traders. This first implementation makes them deterministic for code: OB = last opposite-colour candle before a 4H displacement candle whose body is at least 1.5x the median body of the prior 20 candles; RB = confirmed 4H pivot sweep with close back through the pivot. These are configurable approximations, not a claim that every discretionary chart mark can be reproduced exactly. IFVG is created when a candle closes through an existing FVG. Only completed candles are used.

## Deploy on Koyeb
Create a **Web Service** from this GitHub repository (not a Worker). Build command: `pip install -r requirements.txt`. Run command is read from `Procfile`.

Set environment variables in Koyeb:
- `TELEGRAM_BOT_TOKEN` = token from BotFather
- `TELEGRAM_CHAT_ID` = your Telegram chat/group ID
- `SYMBOLS` = `BTCUSD,ETHUSD,XRPUSD` (confirm all three are live in your Delta India account; change a symbol if Delta reports it is unavailable)
- `POLL_SECONDS` = `60`
- `STATE_FILE` = `/tmp/crt_state.json`

No Delta API key is needed: the bot uses public OHLC candles. Never add your Telegram token to GitHub or paste it in chat.

After deploy, open `https://YOUR-APP.koyeb.app/health`. Configure UptimeRobot to monitor that URL every 5 minutes if the plan allows. This is only a keep-awake attempt; Koyeb Free can scale to zero/restart and is not guaranteed continuous. Missed alerts are possible.

## First run
1. Add secrets in Koyeb and deploy.
2. Open `/health`; `telegram_configured` should be true.
3. The bot sends a startup message to Telegram.
4. Let it run in alert-only / paper mode and compare every signal against TradingView before relying on it.

## Risk / limitations
- Signals are rule-based alerts, not financial advice and not proof of profitability.
- Alerts are generated only after candle close; no intrabar prediction.
- The API may return unavailable symbols or transient errors. Check logs and Delta's live product list.
- No order execution, account access, leverage, balance, or API trading keys are used.
