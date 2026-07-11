#!/usr/bin/env python3
"""
Reimplementiert die Kernlogik aus Custom_LSOB_Pro.pine (f_sweepEngine, f_findOB,
Box-Lifecycle) fuer mehrere Assets x mehrere Zeitrahmen, laeuft als GitHub Actions
Cron (unabhaengig von jedem lokalen Geraet) und schickt bei "LSOB Created" /
"LSOB Entry" (Retest) eine Telegram-Nachricht.

Datenquellen (beide 24/7 handelbar, damit auch Zeitrahmen wie 8h ueberall sauber
funktionieren -- keine Handelspausen wie bei klassischen Boersen/Forex):
  - Binance (data-api.binance.vision): Top-10-Kryptos
  - Bitunix Futures (fapi.bitunix.com): Gold/Silber-Token + tokenisierte Aktien-
    Perpetuals (24/7 handelbar, im Gegensatz zu den echten Boersen-Handelszeiten)

State (letzte benachrichtigte Kerze je Asset+Timeframe) wird in state.json im
Repo persistiert und vom Workflow zurueckcommittet.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BITUNIX_SEMAPHORE = threading.Semaphore(4)

PIVOT_LEN = 5
INVAL_TOL_PCT = 20
RETEST_TOL_PCT = 10
STRICT_WICK = False
MAX_WICK_PEN_PCT = 50
MAX_HISTORY_BARS = 2000
EXPIRY_BARS = 150
PLACEMENT_DELAY = 0
OB_LOOKBACK = 20

TIMEFRAMES = ["1h", "4h", "8h", "1d", "1w"]
INTERVAL_MS = {
    "1h": 3_600_000,
    "4h": 4 * 3_600_000,
    "8h": 8 * 3_600_000,
    "1d": 24 * 3_600_000,
    "1w": 7 * 24 * 3_600_000,
}

ASSETS = [
    {"exchange": "binance", "symbol": "BTCUSDT", "label": "BTC"},
    {"exchange": "binance", "symbol": "ETHUSDT", "label": "ETH"},
    {"exchange": "binance", "symbol": "BNBUSDT", "label": "BNB"},
    {"exchange": "binance", "symbol": "SOLUSDT", "label": "SOL"},
    {"exchange": "binance", "symbol": "XRPUSDT", "label": "XRP"},
    {"exchange": "binance", "symbol": "DOGEUSDT", "label": "DOGE"},
    {"exchange": "binance", "symbol": "ADAUSDT", "label": "ADA"},
    {"exchange": "binance", "symbol": "TRXUSDT", "label": "TRX"},
    {"exchange": "binance", "symbol": "LINKUSDT", "label": "LINK"},
    {"exchange": "binance", "symbol": "AVAXUSDT", "label": "AVAX"},
    {"exchange": "bitunix", "symbol": "XAUUSDT", "label": "Gold"},
    {"exchange": "bitunix", "symbol": "XAGUSDT", "label": "Silber"},
    {"exchange": "bitunix", "symbol": "AAPLUSDT", "label": "Apple"},
    {"exchange": "bitunix", "symbol": "MSFTUSDT", "label": "Microsoft"},
    {"exchange": "bitunix", "symbol": "NVDAUSDT", "label": "Nvidia"},
    {"exchange": "bitunix", "symbol": "GOOGLUSDT", "label": "Google"},
    {"exchange": "bitunix", "symbol": "AMZNUSDT", "label": "Amazon"},
]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_klines_binance(symbol, interval, limit=500):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
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


def fetch_klines_bitunix(symbol, interval, limit=200):
    url = f"https://fapi.bitunix.com/api/v1/futures/market/kline?symbol={symbol}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers=HEADERS)

    payload = None
    last_err = None
    for attempt in range(4):
        with BITUNIX_SEMAPHORE:
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    payload = json.loads(r.read())
            finally:
                time.sleep(0.15)
        if payload is not None and payload.get("code") == 0 and payload.get("data") is not None:
            break
        last_err = payload.get("msg") if payload else "no response"
        payload = None
        time.sleep(0.5 * (attempt + 1))
    if payload is None:
        raise RuntimeError(f"bitunix kline failed for {symbol} {interval}: {last_err}")

    interval_ms = INTERVAL_MS[interval]
    now_ms = time.time() * 1000
    candles = []
    for row in payload.get("data", []):
        open_time = int(row["time"])
        close_time = open_time + interval_ms
        if close_time > now_ms:
            continue
        candles.append({
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "close_time": close_time,
        })
    candles.sort(key=lambda c: c["close_time"])
    return candles


def fetch_klines(asset, interval):
    if asset["exchange"] == "binance":
        return fetch_klines_binance(asset["symbol"], interval, limit=500)
    return fetch_klines_bitunix(asset["symbol"], interval, limit=200)


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
        json.dump(state, f, indent=2, sort_keys=True)


def process(asset, tf):
    min_len = 2 * PIVOT_LEN + OB_LOOKBACK + 5
    candles = fetch_klines(asset, tf)
    if len(candles) < min_len:
        return None
    last_close_time = candles[-1]["close_time"]
    events = run_engine(candles)
    price = candles[-1]["close"]
    return {
        "label": asset["label"],
        "tf": tf,
        "last_close_time": last_close_time,
        "events": events,
        "price": price,
    }


def main():
    state = load_state()
    jobs = [(a, tf) for a in ASSETS for tf in TIMEFRAMES]

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}
        for asset, tf in jobs:
            key = f"{asset['label']}|{tf}"
            already = state.get(key, {}).get("last_notified_close_time")
            futures[pool.submit(process, asset, tf)] = (key, asset, tf, already)

        for fut in as_completed(futures):
            key, asset, tf, already = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"Error processing {key}: {e}")
                continue
            if res is None:
                continue
            if already == res["last_close_time"]:
                continue
            results.append((key, res))

    for key, res in results:
        label, tf, price, events = res["label"], res["tf"], res["price"], res["events"]
        if events["short_created"]:
            send_telegram(f"LSOB Short erstellt ({label}, {tf})\n${price:,.4g} - neue Short-Zone")
        if events["long_created"]:
            send_telegram(f"LSOB Long erstellt ({label}, {tf})\n${price:,.4g} - neue Long-Zone")
        if events["short_entry"]:
            send_telegram(f"LSOB Short Entry ({label}, {tf})\n${price:,.4g} - Retest Short-Zone")
        if events["long_entry"]:
            send_telegram(f"LSOB Long Entry ({label}, {tf})\n${price:,.4g} - Retest Long-Zone")
        state[key] = {"last_notified_close_time": res["last_close_time"]}

    save_state(state)


if __name__ == "__main__":
    main()
