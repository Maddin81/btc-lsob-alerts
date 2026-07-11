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
import io
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests

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
    {"exchange": "binance", "symbol": "BTCUSDT", "label": "BTC", "tv": "BINANCE:BTCUSDT"},
    {"exchange": "binance", "symbol": "ETHUSDT", "label": "ETH", "tv": "BINANCE:ETHUSDT"},
    {"exchange": "binance", "symbol": "BNBUSDT", "label": "BNB", "tv": "BINANCE:BNBUSDT"},
    {"exchange": "binance", "symbol": "SOLUSDT", "label": "SOL", "tv": "BINANCE:SOLUSDT"},
    {"exchange": "binance", "symbol": "XRPUSDT", "label": "XRP", "tv": "BINANCE:XRPUSDT"},
    {"exchange": "binance", "symbol": "DOGEUSDT", "label": "DOGE", "tv": "BINANCE:DOGEUSDT"},
    {"exchange": "binance", "symbol": "ADAUSDT", "label": "ADA", "tv": "BINANCE:ADAUSDT"},
    {"exchange": "binance", "symbol": "TRXUSDT", "label": "TRX", "tv": "BINANCE:TRXUSDT"},
    {"exchange": "binance", "symbol": "LINKUSDT", "label": "LINK", "tv": "BINANCE:LINKUSDT"},
    {"exchange": "binance", "symbol": "AVAXUSDT", "label": "AVAX", "tv": "BINANCE:AVAXUSDT"},
    {"exchange": "binance", "symbol": "NEARUSDT", "label": "NEAR", "tv": "BINANCE:NEARUSDT"},
    {"exchange": "binance", "symbol": "SUIUSDT", "label": "SUI", "tv": "BINANCE:SUIUSDT"},
    {"exchange": "binance", "symbol": "XLMUSDT", "label": "XLM", "tv": "BINANCE:XLMUSDT"},
    {"exchange": "bitunix", "symbol": "XAUUSDT", "label": "Gold", "tv": "OANDA:XAUUSD"},
    {"exchange": "bitunix", "symbol": "XAGUSDT", "label": "Silber", "tv": "OANDA:XAGUSD"},
    {"exchange": "bitunix", "symbol": "AAPLUSDT", "label": "Apple", "tv": "NASDAQ:AAPL"},
    {"exchange": "bitunix", "symbol": "MSFTUSDT", "label": "Microsoft", "tv": "NASDAQ:MSFT"},
    {"exchange": "bitunix", "symbol": "NVDAUSDT", "label": "Nvidia", "tv": "NASDAQ:NVDA"},
    {"exchange": "bitunix", "symbol": "GOOGLUSDT", "label": "Google", "tv": "NASDAQ:GOOGL"},
    {"exchange": "bitunix", "symbol": "AMZNUSDT", "label": "Amazon", "tv": "NASDAQ:AMZN"},
    {"exchange": "bitunix", "symbol": "HYPEUSDT", "label": "HYPE", "tv": "BITUNIX:HYPEUSDT.P"},
    {"exchange": "bitunix", "symbol": "TTWOUSDT", "label": "Take-Two", "tv": "NASDAQ:TTWO"},
    {"exchange": "bitunix", "symbol": "SPCXUSDT", "label": "SpaceX", "tv": "NASDAQ:SPCX"},
    {"exchange": "bitunix", "symbol": "CLUSDT", "label": "Rohoel", "tv": "TVC:USOIL"},
    {"exchange": "mexc", "symbol": "XMRUSDT", "label": "Monero", "tv": "MEXC:XMRUSDT"},
]

TV_INTERVAL = {"1h": "60", "4h": "240", "8h": "480", "1d": "D", "1w": "W"}

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


MEXC_INTERVAL = {"1h": "60m", "4h": "4h", "8h": "8h", "1d": "1d", "1w": "1W"}


def fetch_klines_mexc(symbol, interval, limit=500):
    mexc_iv = MEXC_INTERVAL[interval]
    url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval={mexc_iv}&limit={limit}"
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


def fetch_klines(asset, interval):
    if asset["exchange"] == "binance":
        return fetch_klines_binance(asset["symbol"], interval, limit=500)
    if asset["exchange"] == "mexc":
        return fetch_klines_mexc(asset["symbol"], interval, limit=500)
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
    zones = {"short_created": None, "long_created": None, "short_entry": None, "long_entry": None}

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
                box = {"top": candles[ob]["high"], "bottom": candles[ob]["low"], "created_bar": ob + PLACEMENT_DELAY}
                short_boxes.append(box)
                if idx == last_idx:
                    events["short_created"] = True
                    zones["short_created"] = box

        if bull_sweep:
            ob = None
            for j in range(idx - 1, max(idx - OB_LOOKBACK, 0) - 1, -1):
                if candles[j]["close"] > candles[j]["open"]:
                    ob = j
                    break
            if ob is not None:
                box = {"top": candles[ob]["high"], "bottom": candles[ob]["low"], "created_bar": ob + PLACEMENT_DELAY}
                long_boxes.append(box)
                if idx == last_idx:
                    events["long_created"] = True
                    zones["long_created"] = box

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
                    zones["short_entry"] = b
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
                    zones["long_entry"] = b
            kept.append(b)
        long_boxes = kept

    return events, zones


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def send_telegram_photo(image_bytes, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", image_bytes, "image/png")}
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
    resp = requests.post(url, data=data, files=files, timeout=20)
    resp.raise_for_status()


def render_chart(tail, tail_offset, box, direction, label, tf):
    offset = tail_offset
    box_color = "#39FF14" if direction == "long" else "#FF6EC7"

    fig, ax = plt.subplots(figsize=(9, 5), dpi=120)
    for i, c in enumerate(tail):
        color = "#39FF14" if c["close"] >= c["open"] else "#FF6EC7"
        ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1)
        body_bottom = min(c["open"], c["close"])
        body_height = max(abs(c["close"] - c["open"]), (c["high"] - c["low"]) * 0.01)
        ax.add_patch(plt.Rectangle((i - 0.3, body_bottom), 0.6, body_height, color=color))

    if box is not None:
        left = box["created_bar"] - offset
        left = max(left, 0)
        right = len(tail) - 1
        height = box["top"] - box["bottom"]
        ax.add_patch(plt.Rectangle((left, box["bottom"]), right - left + 1, height,
                                    facecolor=box_color, alpha=0.2, edgecolor=box_color, linewidth=1.5))

    ax.set_title(f"{label} - {tf} LSOB {direction.upper()}", color="white")
    ax.set_facecolor("#0d1117")
    fig.patch.set_facecolor("#0d1117")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#444444")
    ax.set_xlim(-1, len(tail))

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


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
    events, zones = run_engine(candles)
    price = candles[-1]["close"]
    tail = candles[-80:]
    tail_offset = len(candles) - len(tail)
    return {
        "label": asset["label"],
        "tv": asset["tv"],
        "tf": tf,
        "last_close_time": last_close_time,
        "events": events,
        "zones": zones,
        "price": price,
        "candles": tail,
        "tail_offset": tail_offset,
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
        label, tf, price, events, zones = res["label"], res["tf"], res["price"], res["events"], res["zones"]
        tv_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(res['tv'])}&interval={TV_INTERVAL[tf]}"

        notifications = [
            ("short_created", "short", "LSOB Short erstellt", "neue Short-Zone"),
            ("long_created", "long", "LSOB Long erstellt", "neue Long-Zone"),
            ("short_entry", "short", "LSOB Short Entry", "Retest Short-Zone"),
            ("long_entry", "long", "LSOB Long Entry", "Retest Long-Zone"),
        ]
        for event_key, direction, title, desc in notifications:
            if not events[event_key]:
                continue
            caption = f"{title} ({label}, {tf})\n${price:,.4g} - {desc}\n{tv_url}"
            try:
                img = render_chart(res["candles"], res["tail_offset"], zones[event_key], direction, label, tf)
                send_telegram_photo(img, caption)
            except Exception as e:
                print(f"Chart render/send failed for {key}: {e}")
                send_telegram(caption)

        state[key] = {"last_notified_close_time": res["last_close_time"]}

    save_state(state)


if __name__ == "__main__":
    main()
