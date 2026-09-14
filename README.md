# Stocks Trading Bot

An RSI+MACD trading bot that watches a basket of stocks, opens/closes paper-trading positions on Alpaca, and reports what it's doing over Discord. Runs as a long-lived systemd service.

## Strategy

Pure `pandas`-based technical analysis, no ML:

- **Buy** when RSI < 35 **and** MACD histogram > 0
- **Sell** when RSI > 65 **and** MACD histogram < 0
- Hard stop-loss at **-5%** per position

## Architecture

The broker integration is isolated behind a single `AlpacaBroker` class. Everything else (strategy, position sizing, logging, notifications) is broker-agnostic — swapping to Interactive Brokers for live EU trading means rewriting that one class, not the rest of the bot.

Other design points:
- Market-clock aware — only trades while the market is open
- Budget-based whole-share position sizing, capped at 3 concurrent positions
- 14-day rotating logs
- Discord webhook notifications (per-symbol price/RSI/position, cash/invested/total, daily P&L)

## Why paper trading

Alpaca's live trading isn't available in Romania/EU, so this currently runs against Alpaca's paper trading API. Interactive Brokers (via `ib_insync`) is the confirmed path for real EU live trading later, without changing the strategy code — see Architecture above.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your own keys
```

Required environment variables (see `.env.example`):

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Paper trading key from [app.alpaca.markets](https://app.alpaca.markets) |
| `ALPACA_SECRET_KEY` | Paper trading secret |
| `DISCORD_WEBHOOK_URL` | Optional — leave empty to disable notifications |

## Usage

```bash
python rsi_macd_alpaca_bot.py --symbols AAPL,MSFT,NVDA,AMD,META,TSLA --budget 2000 --max-positions 3 --poll 3600
```

| Flag | Meaning |
|---|---|
| `--symbols` | Comma-separated tickers to watch |
| `--budget` | Total cash allocated across positions |
| `--max-positions` | Max concurrent open positions |
| `--poll` | Seconds between checks |
| `--stop-loss` | Stop-loss percentage (default -5%) |

## Running as a service

`setup_acer_stocks.sh` sets this up as a systemd service (`stocks-bot`, `Restart=always`) so it survives reboots and crashes.

## Disclaimer

This is a personal project for learning and experimentation. It currently trades on a paper account only. Nothing here is financial advice, and past backtest/paper performance is not indicative of future results.
