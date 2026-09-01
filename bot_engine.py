import os
import sys
import json
import uuid
from datetime import datetime, timezone
import ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# --- File Paths ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "settings.json")
BOT_ENABLED_PATH = os.path.join(BASE_DIR, "config", "bot_enabled.txt")
OPEN_SIGNALS_PATH = os.path.join(BASE_DIR, "signals", "open.json")
CLOSED_SIGNALS_PATH = os.path.join(BASE_DIR, "signals", "closed.json")
STATS_PATH = os.path.join(BASE_DIR, "signals", "stats.json")


def is_bot_active() -> bool:
    """Graceful exit if the master switch in bot_enabled.txt is 0."""
    if not os.path.exists(BOT_ENABLED_PATH):
        return True
    try:
        with open(BOT_ENABLED_PATH, "r", encoding="utf-8") as f:
            status = f.read().strip()
            return status == "1"
    except Exception as e:
        print(f"[WARN] Failed reading switch status: {e}. Defaulting to active.")
        return True


def load_json(file_path: str, default_val):
    if not os.path.exists(file_path):
        return default_val
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_val


def save_json(file_path: str, data):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def init_exchange(settings: dict):
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret_key = os.getenv("BINANCE_SECRET_KEY", "")
    is_testnet = settings.get("testnet", True)

    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": secret_key,
        "enableRateLimit": True,
        "options": {
            "defaultType": "future" if is_testnet else "spot"
        }
    })
    if is_testnet:
        exchange.set_sandbox_mode(True)
    return exchange


# --- Strategy Indicators ---
def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))


def calculate_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = series.ewm(span=fast, adjust=False).mean()
    slow_ema = series.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def calculate_bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    return sma + (std * std_dev), sma, sma - (std * std_dev)


# --- Bot Evaluation Logic (5 Strategies) ---
def evaluate_strategy(bot_name: str, df: pd.DataFrame, cfg: dict) -> str:
    close = df['close']
    volume = df['volume']
    high = df['high']
    low = df['low']
    i = len(df) - 1

    if bot_name == "RSI_Bollinger":
        rsi = calculate_rsi(close, cfg.get("rsi_period", 14))
        upper_bb, _, lower_bb = calculate_bollinger_bands(close, cfg.get("bb_period", 20), cfg.get("bb_std", 2.0))
        if rsi.iloc[i] <= cfg.get("rsi_oversold", 30) and close.iloc[i] <= lower_bb.iloc[i]:
            return "BUY"
        elif rsi.iloc[i] >= cfg.get("rsi_overbought", 70) and close.iloc[i] >= upper_bb.iloc[i]:
            return "SELL"

    elif bot_name == "MACD_Momentum":
        macd, sig = calculate_macd(close, cfg.get("fast_period", 12), cfg.get("slow_period", 26), cfg.get("signal_period", 9))
        if macd.iloc[i - 1] <= sig.iloc[i - 1] and macd.iloc[i] > sig.iloc[i]:
            return "BUY"
        elif macd.iloc[i - 1] >= sig.iloc[i - 1] and macd.iloc[i] < sig.iloc[i]:
            return "SELL"

    elif bot_name == "EMA_Cross":
        fast = close.ewm(span=cfg.get("fast_ema", 9), adjust=False).mean()
        slow = close.ewm(span=cfg.get("slow_ema", 21), adjust=False).mean()
        trend = close.ewm(span=cfg.get("trend_ema", 50), adjust=False).mean()
        if fast.iloc[i - 1] <= slow.iloc[i - 1] and fast.iloc[i] > slow.iloc[i] and close.iloc[i] > trend.iloc[i]:
            return "BUY"
        elif fast.iloc[i - 1] >= slow.iloc[i - 1] and fast.iloc[i] < slow.iloc[i] and close.iloc[i] < trend.iloc[i]:
            return "SELL"

    elif bot_name == "Breakout_Volume":
        lookback = cfg.get("lookback_period", 20)
        recent_high = high.iloc[-lookback - 1:-1].max()
        recent_low = low.iloc[-lookback - 1:-1].min()
        avg_vol = volume.iloc[-lookback - 1:-1].mean()
        vol_mult = cfg.get("volume_multiplier", 1.8)

        if close.iloc[i] > recent_high and volume.iloc[i] > (avg_vol * vol_mult):
            return "BUY"
        elif close.iloc[i] < recent_low and volume.iloc[i] > (avg_vol * vol_mult):
            return "SELL"

    elif bot_name == "Mean_Reversion":
        period = cfg.get("z_score_period", 20)
        sma = close.rolling(period).mean()
        std = close.rolling(period).std()
        z_score = (close - sma) / (std + 1e-10)
        threshold = cfg.get("entry_threshold", 2.0)

        if z_score.iloc[i] < -threshold:
            return "BUY"
        elif z_score.iloc[i] > threshold:
            return "SELL"

    return None


def run():
    if not is_bot_active():
        print("[INFO] Bot is currently disabled via config/bot_enabled.txt. Exiting gracefully.")
        sys.exit(0)

    settings = load_json(CONFIG_PATH, {})
    open_signals = load_json(OPEN_SIGNALS_PATH, [])
    closed_signals = load_json(CLOSED_SIGNALS_PATH, [])
    stats = load_json(STATS_PATH, {
        "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
        "win_rate": 0.0, "total_pnl_usdt": 0.0, "last_run": None, "bots": {}
    })

    exchange = init_exchange(settings)
    symbols = settings.get("symbols", ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    timeframe = settings.get("timeframe", "15m")
    limit = settings.get("ohlcv_limit", 100)
    bots_cfg = settings.get("bots", {})
    trade_size_usdt = settings.get("trade_allocation_usdt", 50.0)

    now_iso = datetime.now(timezone.utc).isoformat()
    current_prices = {}
    market_data = {}

    # 1. Fetch OHLCV Market Data
    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            market_data[symbol] = df
            current_prices[symbol] = float(df['close'].iloc[-1])
        except Exception as e:
            print(f"[ERROR] Failed fetching {symbol}: {e}")

    # 2. Check and Manage Open Trades (SL/TP triggers)
    remaining_open = []
    for trade in open_signals:
        sym = trade["symbol"]
        if sym not in current_prices:
            remaining_open.append(trade)
            continue

        curr_price = current_prices[sym]
        side = trade["side"]
        entry = trade["entry_price"]
        tp = trade["take_profit"]
        sl = trade["stop_loss"]
        bot_name = trade["bot_name"]

        is_closed = False
        reason = ""

        if side == "BUY":
            if curr_price >= tp:
                is_closed = True
                reason = "Take Profit Hit"
            elif curr_price <= sl:
                is_closed = True
                reason = "Stop Loss Hit"
            pnl_pct = (curr_price - entry) / entry
        else:  # SELL
            if curr_price <= tp:
                is_closed = True
                reason = "Take Profit Hit"
            elif curr_price >= sl:
                is_closed = True
                reason = "Stop Loss Hit"
            pnl_pct = (entry - curr_price) / entry

        if is_closed:
            pnl_usdt = trade["position_size"] * pnl_pct
            trade["exit_price"] = curr_price
            trade["exit_time"] = now_iso
            trade["exit_reason"] = reason
            trade["pnl_pct"] = round(pnl_pct * 100, 2)
            trade["pnl_usdt"] = round(pnl_usdt, 4)
            trade["status"] = "CLOSED"
            closed_signals.append(trade)

            # Update stats
            stats["total_trades"] += 1
            if pnl_usdt > 0:
                stats["winning_trades"] += 1
            else:
                stats["losing_trades"] += 1
            stats["total_pnl_usdt"] = round(stats["total_pnl_usdt"] + pnl_usdt, 4)
            stats["win_rate"] = round((stats["winning_trades"] / stats["total_trades"]) * 100, 2)

            if bot_name not in stats["bots"]:
                stats["bots"][bot_name] = {"trades": 0, "wins": 0, "pnl": 0.0}
            stats["bots"][bot_name]["trades"] += 1
            if pnl_usdt > 0:
                stats["bots"][bot_name]["wins"] += 1
            stats["bots"][bot_name]["pnl"] = round(stats["bots"][bot_name]["pnl"] + pnl_usdt, 4)

            print(f"[CLOSED] {bot_name} | {sym} | Reason: {reason} | PnL: ${pnl_usdt:+.2f}")
        else:
            remaining_open.append(trade)

    open_signals = remaining_open

    # 3. Strategy Evaluation and Signal Generation
    max_open_per_bot = settings.get("max_open_trades_per_bot", 2)
    for bot_name, b_cfg in bots_cfg.items():
        if not b_cfg.get("enabled", True):
            continue

        active_bot_trades = len([t for t in open_signals if t["bot_name"] == bot_name])
        if active_bot_trades >= max_open_per_bot:
            continue

        for symbol in symbols:
            if symbol not in market_data:
                continue

            if any(t["bot_name"] == bot_name and t["symbol"] == symbol for t in open_signals):
                continue

            df = market_data[symbol]
            signal = evaluate_strategy(bot_name, df, b_cfg)

            if signal:
                curr_price = float(df['close'].iloc[-1])
                tp_mult = b_cfg.get("tp_percent", 2.0) / 100.0
                sl_mult = b_cfg.get("sl_percent", 1.5) / 100.0

                if signal == "BUY":
                    take_profit = round(curr_price * (1.0 + tp_mult), 4)
                    stop_loss = round(curr_price * (1.0 - sl_mult), 4)
                else:
                    take_profit = round(curr_price * (1.0 - tp_mult), 4)
                    stop_loss = round(curr_price * (1.0 + sl_mult), 4)

                qty = round(trade_size_usdt / curr_price, 6)

                new_trade = {
                    "id": str(uuid.uuid4())[:8],
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "side": signal,
                    "entry_price": curr_price,
                    "take_profit": take_profit,
                    "stop_loss": stop_loss,
                    "position_size": trade_size_usdt,
                    "quantity": qty,
                    "status": "OPEN",
                    "entry_time": now_iso
                }
                open_signals.append(new_trade)
                print(f"[NEW SIGNAL] {bot_name} -> {signal} {symbol} @ {curr_price} (TP: {take_profit}, SL: {stop_loss})")
                break

    # 4. Save Updated State
    stats["last_run"] = now_iso
    save_json(OPEN_SIGNALS_PATH, open_signals)
    save_json(CLOSED_SIGNALS_PATH, closed_signals)
    save_json(STATS_PATH, stats)
    print(f"[OK] Cycle complete at {now_iso}. Open: {len(open_signals)}, Closed: {len(closed_signals)}")


if __name__ == "__main__":
    run()