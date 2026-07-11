#!/usr/bin/env python3
"""
Reimplementiert die Kernlogik aus Custom_LSOB_Pro.pine (f_sweepEngine, f_findOB,
Box-Lifecycle) fuer BTCUSDT auf 1h/4h, laeuft als GitHub Actions Cron (unabhaengig
von jedem lokalen Geraet) und schickt bei "LSOB Created" / "LSOB Entry" (Retest)
eine Telegram-Nachricht. State (letzte benachrichtigte Kerze je Timeframe) wird in
state.json im Repo persistiert und vom Workflow zurueckcommittet.
"""
import json
import os
import time
import urllib.parse
import urllib.request

SYMBOL = "BTCUSDT"
TIMEFRAMES = ["1h", "4h"]
KLINES_LIMIT = 500

PIVOT_LEN = 5
INVAL_TOL_PCT = 20
RETEST_TOL_PCT = 10
STRICT_WICK = False
MAX_WICK_PEN_PCT = 50
MAX_HISTORY_BARS = 2000
EXPIRY_BARS = 150
PLACEMENT_DELAY = 0
OB_LOOKBACK = 20

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def fetch_klines(symbol, interval, limit=500):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    now_ms = time.time() * 1000
    candles = []
    for row in data:
        close_time = row[6]
        if close_time > now_ms:
            continue
        candles.append({
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "close_time": close_time,
        })
    return candles


def run_engine(candles):
    last_piv_high = None
    last_piv_high_bar = None
    last_piv_low = None
    last_piv_low_bar = None
    ph_swept = True
    pl_swept = True
    short_boxes = []
    long_boxes = []

    n = len(candles)
    last_idx = n - 1
    events = {"short_created": False, "long_created": False, "short_entry": False, "long_entry": False}

    for idx in range(n):
        c = candles[idx]

        if idx >= 2 * PIVOT_LEN:
            p = idx - PIVOT_LEN
            window = candles[p - PIVOT_LEN: p + PIVOT_LEN + 1]
            p_high = candles[p]["high"]
            p_low = candles[p]["low"]
            highs = [w["high"] for w in window]
            lows = [w["low"] for w in window]
            if p_high == max(highs) and highs.count(p_high) == 1:
                last_piv_high = p_high
                last_piv_high_bar = p
                ph_swept = False
            if p_low == min(lows) and lows.count(p_low) == 1:
                last_piv_low = p_low
                last_piv_low_bar = p
                pl_swept = False

        if MAX_HISTORY_BARS > 0:
            if last_piv_high_bar is not None and (idx - last_piv_high_bar) > MAX_HISTORY_BARS:
                ph_swept = True
            if last_piv_low_bar is not None and (idx - last_piv_low_bar) > MAX_HISTORY_BARS:
                pl_swept = True

        bear_sweep = (not ph_swept) and last_piv_high is not None and c["high"] > last_piv_high and c["close"] < last_piv_high
        bull_sweep = (not pl_swept) and last_piv_low is not None and c["low"] < last_piv_low and c["close"] > last_piv_low

        if bear_sweep:
            ph_swept = True
        if bull_sweep:
            pl_swept = True

        if bear_sweep:
            ob = None
            for j in range(idx - 1, max(idx - OB_LOOKBACK, 0) - 1, -1):
                if candles[j]["close"] < candles[j]["open"]:
                    ob = j
                    break
            if ob is not None:
                short_boxes.append({"top": candles[ob]["high"], "bottom": candles[ob]["low"], "created_bar": ob + PLACEMENT_DELAY})
                if idx == last_idx:
                    events["short_created"] = True

        if bull_sweep:
            ob = None
            for j in range(idx - 1, max(idx - OB_LOOKBACK, 0) - 1, -1):
                if candles[j]["close"] > candles[j]["open"]:
                    ob = j
                    break
            if ob is not None:
                long_boxes.append({"top": candles[ob]["high"], "bottom": candles[ob]["low"], "created_bar": ob + PLACEMENT_DELAY})
                if idx == last_idx:
                    events["long_created"] = True

        kept = []
        for b in short_boxes:
            top, bottom = b["top"], b["bottom"]
            h = top - bottom
            tol_price = h * INVAL_TOL_PCT / 100
            pen_price = h * MAX_WICK_PEN_PCT / 100
            retest_px = h * RETEST_TOL_PCT / 100
            invalidated = (c["high"] > top + tol_price) if STRICT_WICK else (c["high"] > bottom + pen_price)
            expired = EXPIRY_BARS > 0 and (idx - b["created_bar"]) > EXPIRY_BARS
            if invalidated or expired:
                continue
            if bottom - retest_px <= c["high"] <= top:
                if idx == last_idx:
                    events["short_entry"] = True
            kept.append(b)
        short_boxes = kept

        kept = []
        for b in long_boxes:
            top, bottom = b["top"], b["bottom"]
            h = top - bottom
            tol_price = h * INVAL_TOL_PCT / 100
            pen_price = h * MAX_WICK_PEN_PCT / 100
            retest_px = h * RETEST_TOL_PCT / 100
            invalidated = (c["low"] < bottom - tol_price) if STRICT_WICK else (c["low"] < top - pen_price)
            expired = EXPIRY_BARS > 0 and (idx - b["created_bar"]) > EXPIRY_BARS
            if invalidated or expired:
                continue
            if bottom <= c["low"] <= top + retest_px:
                if idx == last_idx:
                    events["long_entry"] = True
            kept.append(b)
        long_boxes = kept

    return events


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    state = load_state()
    min_len = 2 * PIVOT_LEN + OB_LOOKBACK + 5

    for tf in TIMEFRAMES:
        try:
            candles = fetch_klines(SYMBOL, tf, KLINES_LIMIT)
            if len(candles) < min_len:
                continue
            last_close_time = candles[-1]["close_time"]
            tf_state = state.get(tf, {})
            if tf_state.get("last_notified_close_time") == last_close_time:
                continue
            events = run_engine(candles)
            price = candles[-1]["close"]
            if events["short_created"]:
                send_telegram(f"LSOB Short erstellt ({tf})\nBTC/USDT ${price:,.0f} - neue Short-Zone")
            if events["long_created"]:
                send_telegram(f"LSOB Long erstellt ({tf})\nBTC/USDT ${price:,.0f} - neue Long-Zone")
            if events["short_entry"]:
                send_telegram(f"LSOB Short Entry ({tf})\nBTC/USDT ${price:,.0f} - Retest Short-Zone")
            if events["long_entry"]:
                send_telegram(f"LSOB Long Entry ({tf})\nBTC/USDT ${price:,.0f} - Retest Long-Zone")
            tf_state["last_notified_close_time"] = last_close_time
            state[tf] = tf_state
        except Exception as e:
            print(f"Error processing {tf}: {e}")

    save_state(state)


if __name__ == "__main__":
    main()
