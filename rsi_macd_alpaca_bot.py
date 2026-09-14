"""
RSI + MACD stocks bot for Alpaca (paper trading) -- deployment-ready.

Strategy (your confirmed logic):
    BUY  when RSI < 35 AND MACD histogram > 0
    SELL when RSI > 65 AND MACD histogram < 0

Deployment features:
    - market-clock aware: skips trading when the exchange is closed,
      sleeps until near the next open instead of hammering the API
    - rotating daily logs (stdout for journalctl + bot.log, 14 days kept)
    - CLI args so systemd can pass --symbol / --qty / --poll

Broker layer is isolated in AlpacaBroker -- swap that one class to move
to IBKR (ib_insync) for live EU trading, without touching the strategy.

Setup:
    pip install alpaca-py pandas
    export ALPACA_API_KEY="..."      # paper keys from app.alpaca.markets
    export ALPACA_SECRET_KEY="..."
    python rsi_macd_alpaca_bot.py --symbol AAPL --qty 1
"""

import os
import time
import json
import logging
import urllib.request
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler

import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# ----------------------------------------------------------------------
# Logging: stdout (journalctl) + rotating daily file
# ----------------------------------------------------------------------
def setup_logging(logfile: str = "bot.log") -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = TimedRotatingFileHandler(logfile, when="midnight", backupCount=14)
    fh.setFormatter(fmt)
    log.addHandler(sh)
    log.addHandler(fh)
    return log


log = setup_logging()


# ----------------------------------------------------------------------
# Discord notifications (via webhook URL in env). No-op if not configured.
# ----------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


def notify(msg: str):
    """Post a message to Discord. Never crashes the bot if it fails."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        data = json.dumps({"content": msg}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=data,
            headers={
                "Content-Type": "application/json",
                # default urllib UA gets 403'd by Cloudflare; use a normal one
                "User-Agent": "Mozilla/5.0 (compatible; stocks-bot/1.0)",
            },
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.error(f"discord notify failed: {e}")


def portfolio_msg(acct) -> str:
    """Build a Discord block with budget, total invested and today's P&L."""
    if acct is None:
        return ""
    daily_pl = acct["equity"] - acct["last_equity"]
    daily_pct = (daily_pl / acct["last_equity"]) if acct["last_equity"] else 0.0
    trend = "📈" if daily_pl >= 0 else "📉"
    return (
        f"💰 Cash ${acct['cash']:,.2f} · 📦 Investit ${acct['invested']:,.2f} · "
        f"💵 Total ${acct['equity']:,.2f}\n"
        f"{trend} Azi: {daily_pl:+,.2f} ({daily_pct:+.2%})"
    )


# ----------------------------------------------------------------------
# 1. Indicators -- pure pandas
# ----------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


# ----------------------------------------------------------------------
# 2. Strategy -- pure logic
# ----------------------------------------------------------------------
def generate_signal(df: pd.DataFrame, rsi_buy: float = 35, rsi_sell: float = 65):
    """Return (signal, rsi_value, macd_hist_value). Values are None if not ready."""
    df = df.copy()
    df["rsi"] = rsi(df["close"])
    _, _, df["macd_hist"] = macd(df["close"])

    last = df.iloc[-1]
    rsi_val = None if pd.isna(last["rsi"]) else float(last["rsi"])
    hist_val = None if pd.isna(last["macd_hist"]) else float(last["macd_hist"])

    if rsi_val is None or hist_val is None:
        return "HOLD", rsi_val, hist_val

    if rsi_val < rsi_buy and hist_val > 0:
        return "BUY", rsi_val, hist_val
    if rsi_val > rsi_sell and hist_val < 0:
        return "SELL", rsi_val, hist_val
    return "HOLD", rsi_val, hist_val


# ----------------------------------------------------------------------
# 3. Broker adapter (Alpaca paper) -- the ONLY broker-specific part
# ----------------------------------------------------------------------
class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)

    def clock(self):
        return self.trading.get_clock()

    def get_bars(self, symbol: str, limit: int = 200,
                 lookback_days: int = 30) -> pd.DataFrame:
        # Request an explicit time window so we always get enough bars for
        # RSI(14)/MACD(26) warm-up. Without `start`, the free feed can return
        # just the current session's few bars, leaving RSI as NaN.
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start,
            limit=limit,
        )
        bars = self.data.get_stock_bars(req).df
        if bars.empty:
            return bars
        return bars.reset_index(level=0, drop=True)

    def position(self, symbol: str) -> dict:
        """Return position details, or zeros if no open position."""
        try:
            p = self.trading.get_open_position(symbol)
            return {
                "qty": float(p.qty),
                "avg_price": float(p.avg_entry_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
        except Exception:
            return {"qty": 0.0, "avg_price": 0.0, "market_value": 0.0,
                    "unrealized_pl": 0.0, "unrealized_plpc": 0.0}

    def open_position_count(self) -> int:
        """How many positions are currently open (across all symbols)."""
        try:
            return len(self.trading.get_all_positions())
        except Exception as e:
            log.error(f"open_position_count failed: {e}")
            return 0

    def account_summary(self):
        """Return dict with budget/equity numbers, or None on failure."""
        try:
            a = self.trading.get_account()
            return {
                "cash": float(a.cash),
                "equity": float(a.equity),
                "last_equity": float(a.last_equity),
                "invested": float(a.long_market_value or 0),
            }
        except Exception as e:
            log.error(f"account_summary failed: {e}")
            return None

    def market_order(self, symbol: str, side: str, qty: float):
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return self.trading.submit_order(order)


# ----------------------------------------------------------------------
# 4. Main loop -- market-clock aware
# ----------------------------------------------------------------------
def run(symbols=("AAPL",), budget: float = 2000, poll_seconds: int = 3600,
        stop_loss_pct: float = -0.05, max_positions: int = 3):
    broker = AlpacaBroker(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=True,
    )
    syms = list(symbols)
    log.info(f"Started. symbols={','.join(syms)} budget=${budget:,.0f}/poz "
             f"max_pos={max_positions} poll={poll_seconds}s "
             f"stop_loss={stop_loss_pct:.0%} paper=True")
    notify(f"🟢 **stocks-bot pornit** — {len(syms)} simboluri: "
           f"{', '.join(syms)}\n"
           f"Buget ${budget:,.0f}/poziție · max {max_positions} poziții · "
           f"stop-loss {stop_loss_pct:.0%} (paper)\n"
           + portfolio_msg(broker.account_summary()))

    while True:
        try:
            clock = broker.clock()
            if not clock.is_open:
                wait = (clock.next_open - datetime.now(timezone.utc)).total_seconds()
                wait = max(60, min(wait, 3600))  # cap at 1h so we re-check
                log.info(f"Market closed. Next open {clock.next_open}. "
                         f"Sleeping {int(wait)}s.")
                time.sleep(wait)
                continue

            open_count = broker.open_position_count()
            rows = []  # one summary line per symbol, sent as ONE message

            for symbol in syms:
                try:
                    df = broker.get_bars(symbol)
                    if df.empty:
                        rows.append(f"`{symbol:5}` ⚠️ fără date")
                        continue
                    price = float(df["close"].iloc[-1])
                    pos = broker.position(symbol)
                    held, avg_price = pos["qty"], pos["avg_price"]

                    # --- stop-loss FIRST (safety takes priority) ---
                    if held > 0 and avg_price > 0:
                        pnl_pct = (price - avg_price) / avg_price
                        if pnl_pct <= stop_loss_pct:
                            broker.market_order(symbol, "SELL", held)
                            open_count -= 1
                            log.warning(f"STOP-LOSS {symbol}: price={price:.2f} "
                                        f"avg={avg_price:.2f} pnl={pnl_pct:.2%}")
                            notify(f"⛔ **STOP-LOSS {symbol}** @ ${price:.2f} "
                                   f"(intrare ${avg_price:.2f}, {pnl_pct:+.2%}) "
                                   f"— vândut {held:g}")
                            rows.append(f"`{symbol:5}` ⛔ STOP-LOSS ${price:.2f}")
                            continue

                    signal, rsi_val, hist_val = generate_signal(df)
                    rsi_s = f"{rsi_val:.1f}" if rsi_val is not None else "n/a"
                    hist_s = f"{hist_val:+.3f}" if hist_val is not None else "n/a"
                    log.info(f"{symbol} signal={signal} held={held} "
                             f"price={price:.2f} RSI={rsi_s} MACD_hist={hist_s}")

                    note = ""
                    if signal == "BUY" and held == 0:
                        if open_count >= max_positions:
                            note = f" (limită {max_positions} poz.)"
                            log.info(f"{symbol} BUY skipped: position limit")
                        else:
                            n = int(budget // price)  # whole shares only
                            if n < 1:
                                note = " (buget prea mic)"
                            else:
                                broker.market_order(symbol, "BUY", n)
                                open_count += 1
                                log.info(f"submitted BUY {n} {symbol}")
                                notify(f"🟢 **BUY {n} {symbol}** @ ${price:.2f} "
                                       f"(~${n*price:,.0f} · RSI={rsi_s} "
                                       f"MACD={hist_s})")
                                note = f" → cumpărat {n}"
                    elif signal == "SELL" and held > 0:
                        broker.market_order(symbol, "SELL", held)
                        open_count -= 1
                        log.info(f"submitted SELL {held} {symbol}")
                        notify(f"🔴 **SELL {held:g} {symbol}** @ ${price:.2f} "
                               f"(P&L {pos['unrealized_pl']:+,.2f} · "
                               f"RSI={rsi_s} MACD={hist_s})")
                        note = f" → vândut {held:g}"

                    hold_s = ""
                    if held > 0:
                        hold_s = f" · {held:g} buc {pos['unrealized_plpc']:+.1%}"
                    rows.append(f"`{symbol:5}` {signal:4} ${price:7.2f} · "
                                f"RSI {rsi_s:>4}{hold_s}{note}")

                except Exception as e:
                    log.error(f"{symbol} error: {e}")
                    rows.append(f"`{symbol:5}` ⚠️ eroare")

            # ONE consolidated status message per cycle
            notify("📊 **Status**\n" + "\n".join(rows) + "\n"
                   + portfolio_msg(broker.account_summary()))

        except Exception as e:
            log.error(f"cycle error: {e}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="AAPL",
                   help="comma-separated, e.g. AAPL,MSFT,NVDA")
    p.add_argument("--budget", type=float, default=2000,
                   help="USD per position (whole shares only)")
    p.add_argument("--max-positions", type=int, default=3, dest="max_positions",
                   help="max concurrent open positions")
    p.add_argument("--poll", type=int, default=3600, help="seconds between cycles")
    p.add_argument("--stop-loss", type=float, default=-0.05, dest="stop_loss",
                   help="stop-loss fraction, e.g. -0.05 for -5%%")
    a = p.parse_args()
    run(symbols=[s.strip().upper() for s in a.symbols.split(",") if s.strip()],
        budget=a.budget, poll_seconds=a.poll, stop_loss_pct=a.stop_loss,
        max_positions=a.max_positions)
