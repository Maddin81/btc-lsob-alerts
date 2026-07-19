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

State (letzte gesehene Kerze, aktive Zonen, bereits gemeldete Entries je
Asset+Timeframe) wird in state.json persistiert; der Workflow committet
state.json + signals.csv auf den Daten-Branch "lsob-state".
"""
import csv
import io
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

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

# Rauschfilter: Boxen, deren Hoehe kleiner als dieser Anteil der ATR(14) ist,
# werden gar nicht erst angelegt (betrifft Alerts UND Backtest gleichermassen).
MIN_BOX_ATR_MULT = 0.25
# Optionaler Trendfilter: Long-Zonen nur ueber, Short-Zonen nur unter EMA(200)
# des jeweiligen Timeframes. Bewusst per Default aus.
TREND_FILTER = False
EMA_LEN = 200

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

ASSET_BY_LABEL = {a["label"]: a for a in ASSETS}

TV_INTERVAL = {"1h": "60", "4h": "240", "8h": "480", "1d": "D", "1w": "W"}

STATE_DIR = os.environ.get("STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SIGNALS_FILE = os.path.join(STATE_DIR, "signals.csv")
# Vom Markt-Radar (market_check.py) gepflegt; hier nur gelesen, um LSOB-Alerts
# mit dem Sentiment (Fear & Greed) anzureichern.
MARKET_STATE_FILE = os.path.join(STATE_DIR, "market_state.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {"User-Agent": "Mozilla/5.0"}

MIN_CANDLES = 2 * PIVOT_LEN + OB_LOOKBACK + 5

SIGNAL_FIELDS = ["ts_utc", "close_time", "label", "tf", "event", "direction",
                 "price", "box_top", "box_bottom", "box_id", "confluence"]


def http_get_json(url, attempts=3, timeout=15):
    last_err = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last_err}")


def fetch_klines_binance(symbol, interval, limit=500):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = http_get_json(url)
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

    payload = None
    last_err = None
    for attempt in range(4):
        with BITUNIX_SEMAPHORE:
            try:
                payload = http_get_json(url, attempts=1)
            except Exception as e:
                payload = None
                last_err = str(e)
            finally:
                time.sleep(0.15)
        if payload is not None and payload.get("code") == 0 and payload.get("data") is not None:
            break
        if payload is not None:
            last_err = payload.get("msg")
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
    data = http_get_json(url)
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


def compute_atr(candles, period=14):
    atrs = []
    trs = []
    prev_close = None
    for c in candles:
        if prev_close is None:
            tr = c["high"] - c["low"]
        else:
            tr = max(c["high"] - c["low"], abs(c["high"] - prev_close), abs(c["low"] - prev_close))
        trs.append(tr)
        if len(trs) > period:
            trs.pop(0)
        atrs.append(sum(trs) / len(trs))
        prev_close = c["close"]
    return atrs


def compute_ema(candles, period=EMA_LEN):
    emas = []
    ema = None
    k = 2 / (period + 1)
    for c in candles:
        ema = c["close"] if ema is None else c["close"] * k + ema * (1 - k)
        emas.append(ema)
    return emas


def run_engine(candles):
    """Replayed die komplette History und liefert:
    - events: chronologische Liste aller Created/Entry-Ereignisse inkl. Box
    - active: am Ende noch gueltige Short-/Long-Boxen (fuer Konfluenz-Cache)
    """
    last_piv_high = None
    last_piv_high_bar = None
    last_piv_low = None
    last_piv_low_bar = None
    ph_swept = True
    pl_swept = True
    short_boxes = []
    long_boxes = []

    atr = compute_atr(candles)
    ema = compute_ema(candles) if TREND_FILTER else None

    events = []

    for idx, c in enumerate(candles):
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
                top, bottom = candles[ob]["high"], candles[ob]["low"]
                ok = (top - bottom) >= MIN_BOX_ATR_MULT * atr[idx]
                if TREND_FILTER and c["close"] >= ema[idx]:
                    ok = False
                if ok:
                    box = {"top": top, "bottom": bottom, "created_bar": ob + PLACEMENT_DELAY,
                           "id": f"S{int(candles[ob]['close_time'])}",
                           "pivot": last_piv_high, "pivot_bar": last_piv_high_bar}
                    short_boxes.append(box)
                    events.append({"type": "short_created", "direction": "short", "bar": idx,
                                   "close_time": c["close_time"], "box": box})

        if bull_sweep:
            ob = None
            for j in range(idx - 1, max(idx - OB_LOOKBACK, 0) - 1, -1):
                if candles[j]["close"] > candles[j]["open"]:
                    ob = j
                    break
            if ob is not None:
                top, bottom = candles[ob]["high"], candles[ob]["low"]
                ok = (top - bottom) >= MIN_BOX_ATR_MULT * atr[idx]
                if TREND_FILTER and c["close"] <= ema[idx]:
                    ok = False
                if ok:
                    box = {"top": top, "bottom": bottom, "created_bar": ob + PLACEMENT_DELAY,
                           "id": f"L{int(candles[ob]['close_time'])}",
                           "pivot": last_piv_low, "pivot_bar": last_piv_low_bar}
                    long_boxes.append(box)
                    events.append({"type": "long_created", "direction": "long", "bar": idx,
                                   "close_time": c["close_time"], "box": box})

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
                events.append({"type": "short_entry", "direction": "short", "bar": idx,
                               "close_time": c["close_time"], "box": b})
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
                events.append({"type": "long_entry", "direction": "long", "bar": idx,
                               "close_time": c["close_time"], "box": b})
            kept.append(b)
        long_boxes = kept

    return events, {"short": short_boxes, "long": long_boxes}


def evaluate_trade(candles, direction, entry, sl):
    """Bewertet ein Entry-Signal gegen die Folgekerzen.
    SL auf der abgewandten Boxseite; Ziele bei 1R und 2R. Beruehrt eine Kerze
    SL und Ziel gleichzeitig, zaehlt konservativ der SL.
    Rueckgabe: "tp2", "tp1", "sl", "open" oder "invalid".
    """
    risk = (entry - sl) if direction == "long" else (sl - entry)
    if risk <= 0:
        return "invalid"
    if direction == "long":
        tp1, tp2 = entry + risk, entry + 2 * risk
    else:
        tp1, tp2 = entry - risk, entry - 2 * risk
    reached_1r = False
    for c in candles:
        if direction == "long":
            hit_sl = c["low"] <= sl
            hit_tp1 = c["high"] >= tp1
            hit_tp2 = c["high"] >= tp2
        else:
            hit_sl = c["high"] >= sl
            hit_tp1 = c["low"] <= tp1
            hit_tp2 = c["low"] <= tp2
        if hit_sl:
            return "tp1" if reached_1r else "sl"
        if hit_tp2:
            return "tp2"
        if hit_tp1:
            reached_1r = True
    return "tp1" if reached_1r else "open"


def fmt_price(p):
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.6g}"


def sentiment_note(direction):
    """Kontext-Hinweis aus dem Fear-&-Greed-Index (vom Markt-Radar gecacht).
    Angst = Rueckenwind fuer Longs (Contrarian), Gier = Rueckenwind fuer Shorts;
    ein Trade GEGEN ein extremes Sentiment bekommt eine Vorsicht-Markierung."""
    try:
        with open(MARKET_STATE_FILE) as f:
            fng = json.load(f).get("fear_greed")
        value = int(fng["value"])
        klass = fng["classification"]
    except Exception:
        return None
    extreme_fear, fear = value <= 24, value <= 44
    greed, extreme_greed = value >= 56, value >= 76
    if direction == "long":
        if extreme_fear:
            return f"🧠 Contrarian-Konfluenz: Markt in extremer Angst (F&G {value})"
        if fear:
            return f"🧠 Sentiment-Rueckenwind: Markt-Angst (F&G {value})"
        if extreme_greed:
            return f"⚠️ Vorsicht: Long bei extremer Gier (F&G {value})"
    else:
        if extreme_greed:
            return f"🧠 Contrarian-Konfluenz: Markt in extremer Gier (F&G {value})"
        if greed:
            return f"🧠 Sentiment-Rueckenwind: Markt-Gier (F&G {value})"
        if extreme_fear:
            return f"⚠️ Vorsicht: Short bei extremer Angst (F&G {value})"
    return None


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


def render_chart(tail, tail_offset, event, label, tf):
    offset = tail_offset
    box = event["box"]
    direction = event["direction"]
    box_color = "#39FF14" if direction == "long" else "#FF6EC7"

    fig, ax = plt.subplots(figsize=(9, 5), dpi=120)
    for i, c in enumerate(tail):
        color = "#39FF14" if c["close"] >= c["open"] else "#FF6EC7"
        ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1)
        body_bottom = min(c["open"], c["close"])
        body_height = max(abs(c["close"] - c["open"]), (c["high"] - c["low"]) * 0.01)
        ax.add_patch(plt.Rectangle((i - 0.3, body_bottom), 0.6, body_height, color=color))

    y_min = min(c["low"] for c in tail)
    y_max = max(c["high"] for c in tail)
    y_pad = (y_max - y_min) * 0.03 or y_max * 0.001

    if box is not None:
        left = max(box["created_bar"] - offset, 0)
        right = len(tail) - 1
        height = box["top"] - box["bottom"]
        ax.add_patch(plt.Rectangle((left, box["bottom"]), right - left + 1, height,
                                    facecolor=box_color, alpha=0.2, edgecolor=box_color, linewidth=1.5))

    # Gesweeptes Pivot-Level als gestrichelte Linie bis zur Sweep-Kerze
    event_x = event["bar"] - offset
    pivot = box.get("pivot") if box else None
    if pivot is not None and y_min - y_pad <= pivot <= y_max + y_pad:
        piv_left = max((box.get("pivot_bar") or 0) - offset, 0)
        piv_right = min(max(event_x, piv_left + 1), len(tail) - 1)
        ax.plot([piv_left, piv_right], [pivot, pivot], color="#FFD700",
                linewidth=1.2, linestyle="--", alpha=0.9)

    # Signal-Kerze markieren
    if 0 <= event_x < len(tail):
        c = tail[event_x]
        if direction == "long":
            ax.scatter([event_x], [c["low"] - y_pad], marker="^", color="#39FF14", s=60, zorder=5)
        else:
            ax.scatter([event_x], [c["high"] + y_pad], marker="v", color="#FF6EC7", s=60, zorder=5)

    ax.set_title(f"{label} - {tf} LSOB {direction.upper()}", color="white")
    ax.set_facecolor("#0d1117")
    fig.patch.set_facecolor("#0d1117")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#444444")
    ax.set_xlim(-1, len(tail))

    # Zeitachse beschriften
    step = max(1, len(tail) // 6)
    ticks = list(range(0, len(tail), step))
    fmt = "%d.%m %H:%M" if tf in ("1h", "4h", "8h") else "%d.%m.%y"
    labels = [datetime.fromtimestamp(tail[i]["close_time"] / 1000, timezone.utc).strftime(fmt)
              for i in ticks]
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=7)

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


def should_fetch(state, key, tf, now_ms):
    """Nur fetchen, wenn seit der letzten gesehenen Kerze ueberhaupt eine neue
    Kerze geschlossen haben kann -- spart den Grossteil der API-Calls."""
    last = state.get(key, {}).get("last_notified_close_time")
    if not last:
        return True
    return now_ms > last + INTERVAL_MS[tf]


def process(asset, tf):
    candles = fetch_klines(asset, tf)
    if not candles:
        return None
    if len(candles) < MIN_CANDLES:
        # Zu wenig History (z.B. frisch gelistete Bitunix-Perpetuals auf 1d/1w):
        # kein Fehler -- State trotzdem pflegen, damit nicht jeder Run erneut fetcht.
        return {"label": asset["label"], "tf": tf, "insufficient": True,
                "last_close_time": candles[-1]["close_time"]}
    events, active = run_engine(candles)
    tail = candles[-80:]
    return {
        "label": asset["label"],
        "tv": asset["tv"],
        "tf": tf,
        "last_close_time": candles[-1]["close_time"],
        "events": events,
        "active": active,
        "price": candles[-1]["close"],
        "candles": tail,
        "tail_offset": len(candles) - len(tail),
    }


def track_error(state, key, msg, now_ms):
    errs = state.setdefault("_errors", {})
    e = errs.setdefault(key, {"count": 0, "last_alert": 0})
    e["count"] += 1
    if e["count"] >= 3 and now_ms - e["last_alert"] > 24 * 3_600_000:
        try:
            send_telegram(f"⚠️ Datenfehler {key}: {msg} ({e['count']} Fehlversuche in Folge)")
            e["last_alert"] = now_ms
        except Exception as ex:
            print(f"Error alert failed for {key}: {ex}")


def find_confluence(state, label, tf, direction, box):
    """Prueft, ob auf hoeheren Timeframes eine aktive Zone gleicher Richtung
    mit der Event-Box ueberlappt (Zonen-Cache aus dem State)."""
    hits = []
    for htf in TIMEFRAMES[TIMEFRAMES.index(tf) + 1:]:
        zones = state.get(f"{label}|{htf}", {}).get(f"active_{direction}", [])
        for top, bottom in zones:
            if top >= box["bottom"] and bottom <= box["top"]:
                hits.append(htf)
                break
    return hits


def log_signal(res, event, confluence):
    exists = os.path.exists(SIGNALS_FILE) and os.path.getsize(SIGNALS_FILE) > 0
    with open(SIGNALS_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(SIGNAL_FIELDS)
        box = event["box"]
        w.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            int(event["close_time"]),
            res["label"], res["tf"], event["type"], event["direction"],
            res["price"], box["top"], box["bottom"], box["id"],
            "/".join(confluence),
        ])


EVENT_META = {
    "short_created": ("🔴 LSOB Short erstellt", "neue Short-Zone"),
    "long_created": ("🟢 LSOB Long erstellt", "neue Long-Zone"),
    "short_entry": ("🔴 LSOB Short Entry", "Retest Short-Zone"),
    "long_entry": ("🟢 LSOB Long Entry", "Retest Long-Zone"),
}


def notify_events(state, key, res):
    entry_state = state[key]
    label, tf = res["label"], res["tf"]
    already = res["already"]
    # Erstlauf eines Assets: nur Ereignisse der letzten Kerze melden. Sonst
    # verpasste Kerzen nachholen, aber maximal 3 -- nach laengerer Downtime
    # sind aeltere Signale ohnehin nicht mehr handelbar (keine Alert-Flut).
    if already:
        threshold = max(already, res["last_close_time"] - 3 * INTERVAL_MS[tf])
    else:
        threshold = res["last_close_time"] - 1
    notified = list(entry_state.get("notified_entries", []))
    tv_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(res['tv'])}&interval={TV_INTERVAL[tf]}"

    for event in res["events"]:
        if event["close_time"] <= threshold:
            continue
        box = event["box"]
        is_entry = event["type"].endswith("_entry")
        if is_entry:
            # Pro Box nur ein Entry-Alert, egal wie lange der Kurs in der Zone bleibt
            if box["id"] in notified:
                continue
            notified.append(box["id"])

        title, desc = EVENT_META[event["type"]]
        direction = event["direction"]
        confluence = find_confluence(state, label, tf, direction, box)

        lines = [
            f"{title} ({label}, {tf})",
            f"Preis: {fmt_price(res['price'])} - {desc}",
            f"Zone: {fmt_price(box['bottom'])} - {fmt_price(box['top'])}",
        ]
        if is_entry:
            if direction == "long":
                lines.append(f"SL-Idee: unter {fmt_price(box['bottom'])}")
            else:
                lines.append(f"SL-Idee: ueber {fmt_price(box['top'])}")
        if confluence:
            lines.append(f"⭐ Konfluenz: aktive {'/'.join(confluence)} {direction.capitalize()}-Zone")
        note = sentiment_note(direction)
        if note:
            lines.append(note)
        lines.append(tv_url)
        caption = "\n".join(lines)

        try:
            img = render_chart(res["candles"], res["tail_offset"], event, label, tf)
            send_telegram_photo(img, caption)
        except Exception as e:
            print(f"Chart render/send failed for {key}: {e}")
            try:
                send_telegram(caption)
            except Exception as e2:
                print(f"Telegram send failed for {key}: {e2}")
        try:
            log_signal(res, event, confluence)
        except Exception as e:
            print(f"Signal log failed for {key}: {e}")

    entry_state["notified_entries"] = notified[-50:]


def read_recent_signals(days=7):
    if not os.path.exists(SIGNALS_FILE):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    with open(SIGNALS_FILE, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["ts_utc"])
            except (ValueError, KeyError):
                continue
            if ts >= cutoff:
                rows.append(row)
    return rows


def maybe_weekly_report(state):
    """Sonntags ab 18:00 UTC einmalig: Wochenstatistik + Auswertung der
    Entry-Signale (1R/2R/SL) per Telegram."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 6 or now.hour < 18:
        return
    meta = state.setdefault("_meta", {})
    today = now.strftime("%Y-%m-%d")
    if meta.get("last_weekly_report") == today:
        return
    meta["last_weekly_report"] = today

    rows = read_recent_signals(days=7)
    created = [r for r in rows if r["event"].endswith("_created")]
    entries = [r for r in rows if r["event"].endswith("_entry")]

    outcomes = {"tp2": 0, "tp1": 0, "sl": 0, "open": 0}
    kline_cache = {}
    for r in entries:
        asset = ASSET_BY_LABEL.get(r["label"])
        if asset is None:
            continue
        cache_key = (r["label"], r["tf"])
        if cache_key not in kline_cache:
            try:
                kline_cache[cache_key] = fetch_klines(asset, r["tf"])
            except Exception as e:
                print(f"Weekly report fetch failed for {cache_key}: {e}")
                kline_cache[cache_key] = []
        candles = kline_cache[cache_key]
        try:
            signal_ct = int(r["close_time"])
            entry_price = float(r["price"])
            sl = float(r["box_bottom"]) if r["direction"] == "long" else float(r["box_top"])
        except (ValueError, KeyError):
            continue
        after = [c for c in candles if c["close_time"] > signal_ct]
        outcome = evaluate_trade(after, r["direction"], entry_price, sl)
        if outcome in outcomes:
            outcomes[outcome] += 1

    counts = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    top_str = ", ".join(f"{lbl} ({n})" for lbl, n in top) if top else "-"

    week_start = (now - timedelta(days=7)).strftime("%d.%m.")
    lines = [
        f"📊 LSOB Wochenreport ({week_start} - {now.strftime('%d.%m.')})",
        f"Signale: {len(rows)} gesamt - {len(created)} Zonen erstellt, {len(entries)} Entries",
    ]
    closed = outcomes["tp2"] + outcomes["tp1"] + outcomes["sl"]
    if entries:
        lines.append(f"Entry-Bilanz: 2R: {outcomes['tp2']} | 1R: {outcomes['tp1']} | "
                     f"SL: {outcomes['sl']} | offen: {outcomes['open']}")
        if closed:
            winrate = 100 * (outcomes["tp2"] + outcomes["tp1"]) / closed
            lines.append(f"Trefferquote (>=1R): {winrate:.0f}%")
    lines.append(f"Aktivste Assets: {top_str}")
    send_telegram("\n".join(lines))


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen")

    state = load_state()
    now_ms = time.time() * 1000
    errors = state.setdefault("_errors", {})

    jobs = []
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            key = f"{asset['label']}|{tf}"
            if should_fetch(state, key, tf, now_ms):
                jobs.append((key, asset, tf))

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(process, asset, tf): key for key, asset, tf in jobs}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"Error processing {key}: {e}")
                track_error(state, key, str(e), now_ms)
                continue
            errors.pop(key, None)
            if res is None:
                continue
            results.append((key, res))

    # Erst den kompletten Zonen-Cache aktualisieren, damit die Konfluenz-Pruefung
    # beim Benachrichtigen schon die frischesten Zonen aller Timeframes sieht.
    for key, res in results:
        entry_state = state.setdefault(key, {})
        res["already"] = entry_state.get("last_notified_close_time")
        if res["already"] == res["last_close_time"]:
            res["skip"] = True
            continue
        entry_state["last_notified_close_time"] = res["last_close_time"]
        if not res.get("insufficient"):
            entry_state["active_short"] = [[b["top"], b["bottom"]] for b in res["active"]["short"]][-10:]
            entry_state["active_long"] = [[b["top"], b["bottom"]] for b in res["active"]["long"]][-10:]

    for key, res in results:
        if res.get("insufficient") or res.get("skip"):
            continue
        notify_events(state, key, res)

    try:
        maybe_weekly_report(state)
    except Exception as e:
        print(f"Weekly report failed: {e}")

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Kompletter Crash: einmal (mit 6h-Drossel) nach Telegram melden,
        # dann den Workflow trotzdem fehlschlagen lassen.
        try:
            state = load_state()
            meta = state.setdefault("_meta", {})
            now_ms = time.time() * 1000
            if now_ms - meta.get("last_crash_alert", 0) > 6 * 3_600_000:
                send_telegram(f"🚨 LSOB-Check abgestuerzt: {exc}")
                meta["last_crash_alert"] = now_ms
                save_state(state)
        except Exception:
            pass
        raise
