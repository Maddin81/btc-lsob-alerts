#!/usr/bin/env python3
"""
Krypto-Marktüberwachungs-Bot
=============================
Überwacht 10 Kryptowährungen auf Preisbewegungen und sendet
bei Alarmschwellen eine Telegram-Benachrichtigung.

Läuft lokal (z.B. per Cron). Die Telegram-Zugangsdaten kommen aus
Umgebungsvariablen und landen NICHT im Repo.

Datenquellen (kein API-Key nötig):
  - CoinPaprika (api.coinpaprika.com): Coin-Daten + globale Marktdaten
  - alternative.me (api.alternative.me): Fear & Greed Index

Alarmschwellen:
  🚨 ALARM: 24h-Veränderung ≥ ±5%
  ⚡ SCHNELLE BEWEGUNG: 1h-Veränderung ≥ ±2%
  📊 VOLUMEN: volume_24h / market_cap > 0,15
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ─── Konfiguration ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

ALARM_THRESHOLD_24H  = 5.0   # %
QUICK_THRESHOLD_1H   = 2.0   # %
VOLUME_THRESHOLD     = 0.15  # vol/mcap ratio

COINS = [
    {"id": "btc-bitcoin",      "symbol": "BTC",  "name": "Bitcoin"},
    {"id": "eth-ethereum",     "symbol": "ETH",  "name": "Ethereum"},
    {"id": "sol-solana",       "symbol": "SOL",  "name": "Solana"},
    {"id": "xmr-monero",       "symbol": "XMR",  "name": "Monero"},
    {"id": "bnb-binance-coin", "symbol": "BNB",  "name": "BNB"},
    {"id": "xrp-xrp",          "symbol": "XRP",  "name": "XRP"},
    {"id": "ada-cardano",      "symbol": "ADA",  "name": "Cardano"},
    {"id": "doge-dogecoin",    "symbol": "DOGE", "name": "Dogecoin"},
    {"id": "avax-avalanche",   "symbol": "AVAX", "name": "Avalanche"},
    {"id": "shib-shiba-inu",   "symbol": "SHIB", "name": "Shiba Inu"},
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CryptoMonitor/1.0)"}

# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def format_price(price):
    if price >= 1000:
        return f"${price:,.0f}"
    if price >= 1:
        return f"${price:,.2f}"
    if price >= 0.01:
        return f"${price:.4f}"
    if price >= 0.000001:
        return f"${price:.8f}"
    return f"${price:.10f}"


def format_pct(v):
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def pct_arrow(today, yesterday):
    delta = today - yesterday
    if delta > 1:
        return "↑"
    if delta < -1:
        return "↓"
    return "→"


def translate_fng(classification):
    mapping = {
        "Extreme Fear":  "Extreme Angst",
        "Fear":          "Angst",
        "Neutral":       "Neutral",
        "Greed":         "Gier",
        "Extreme Greed": "Extreme Gier",
    }
    return mapping.get(classification, classification)


def format_mcap(value):
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.2f}M"
    return f"${value:,.0f}"


def berlin_time():
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    if 3 < month < 10:
        tz_offset = timedelta(hours=2)
        tz_name = "MESZ"
    else:
        tz_offset = timedelta(hours=1)
        tz_name = "MEZ"
    local_time = utc_now + tz_offset
    return local_time.strftime("%d.%m.%Y %H:%M") + f" {tz_name}"


# ─── Datenabruf ───────────────────────────────────────────────────────────────

def fetch_coin(coin):
    url = f"https://api.coinpaprika.com/v1/tickers/{coin['id']}"
    try:
        data = http_get(url)
        q = data["quotes"]["USD"]
        return {
            "id":      coin["id"],
            "symbol":  coin["symbol"],
            "name":    coin["name"],
            "price":   q["price"],
            "1h":      q["percent_change_1h"],
            "24h":     q["percent_change_24h"],
            "7d":      q["percent_change_7d"],
            "vol24h":  q["volume_24h"],
            "mcap":    q["market_cap"],
            "ath_pct": q["percent_from_price_ath"],
        }
    except Exception as e:
        print(f"[WARN] Coin {coin['symbol']} nicht abrufbar: {e}")
        return None


def fetch_global():
    try:
        data = http_get("https://api.coinpaprika.com/v1/global")
        return {
            "mcap":     data["market_cap_usd"],
            "mcap_24h": data["market_cap_change_24h"],
            "btc_dom":  data["bitcoin_dominance_percentage"],
        }
    except Exception as e:
        print(f"[WARN] Globale Marktdaten nicht abrufbar: {e}")
        return None


def fetch_fng():
    try:
        data = http_get("https://api.alternative.me/fng/?limit=2")
        entries = data["data"]
        return {
            "today_value":     int(entries[0]["value"]),
            "today_class":     entries[0]["value_classification"],
            "yesterday_value": int(entries[1]["value"]),
            "yesterday_class": entries[1]["value_classification"],
        }
    except Exception as e:
        print(f"[WARN] Fear & Greed Index nicht abrufbar: {e}")
        return None


def search_news(query):
    try:
        url = "https://api.duckduckgo.com/?q=" + urllib.parse.quote(query) + "&format=json&no_html=1&skip_disambig=1"
        data = http_get(url, timeout=10)
        if data.get("AbstractText"):
            return data["AbstractText"][:300]
        for t in data.get("RelatedTopics", []):
            if isinstance(t, dict) and t.get("Text"):
                return t["Text"][:300]
        return ""
    except Exception:
        return ""


# ─── Analyse ──────────────────────────────────────────────────────────────────

def analyze(coins):
    alarms, quick, high_vol = [], [], []
    for c in coins:
        if abs(c["24h"]) >= ALARM_THRESHOLD_24H:
            alarms.append(c)
        if abs(c["1h"]) >= QUICK_THRESHOLD_1H:
            quick.append(c)
        if c["mcap"] and c["mcap"] > 0:
            ratio = c["vol24h"] / c["mcap"]
            if ratio > VOLUME_THRESHOLD:
                high_vol.append((c, ratio))

    sorted_24h = sorted(coins, key=lambda x: x["24h"], reverse=True)
    return {
        "alarms":     alarms,
        "quick":      quick,
        "high_vol":   high_vol,
        "top_gainer": sorted_24h[0] if sorted_24h else None,
        "top_loser":  sorted_24h[-1] if sorted_24h else None,
    }


def research_alarms(alarm_coins):
    results = {}
    for i, c in enumerate(alarm_coins[:2]):
        snippet = search_news(f"{c['name']} crypto price news today")
        if snippet:
            results[c["symbol"]] = snippet
        if i == 0 and len(alarm_coins) > 1:
            time.sleep(1)
    return results


# ─── Nachrichtenformatierung ──────────────────────────────────────────────────

def build_message(coins, analysis, fng, glob, news, timestamp):
    alarms   = analysis["alarms"]
    quick    = analysis["quick"]
    high_vol = analysis["high_vol"]
    top_g    = analysis["top_gainer"]
    top_l    = analysis["top_loser"]

    lines = []

    fng_str = ""
    if fng:
        fng_str = f" | F&G: {fng['today_value']} ({translate_fng(fng['today_class'])})"

    if alarms:
        parts = [f"{c['symbol']} {format_pct(c['24h'])}" for c in alarms[:3]]
        headline = "🚨 " + " · ".join(parts) + fng_str
    else:
        btc = next((c for c in coins if c["symbol"] == "BTC"), None)
        btc_str = f"BTC {format_price(btc['price'])} ({format_pct(btc['24h'])})" if btc else ""
        headline = f"✅ Ruhiger Markt | {btc_str}{fng_str}"

    lines.append(headline)
    lines.append("")

    if alarms:
        lines.append("━━━ 🚨 ALARM (24h ≥ ±5%) ━━━")
        for c in alarms:
            line = (f"  {c['symbol']}: {format_price(c['price'])}  "
                    f"1h {format_pct(c['1h'])}  24h {format_pct(c['24h'])}  7d {format_pct(c['7d'])}")
            if c["ath_pct"] is not None:
                line += f"  ATH-Abstand: {c['ath_pct']:+.1f}%"
            lines.append(line)
            if news.get(c["symbol"]):
                snippet = news[c["symbol"]]
                if len(snippet) > 200:
                    snippet = snippet[:197] + "…"
                lines.append(f"  📰 {snippet}")
        lines.append("")

    quick_only = [c for c in quick if c not in alarms]
    if quick_only:
        lines.append("━━━ ⚡ SCHNELLE BEWEGUNG (1h ≥ ±2%) ━━━")
        for c in quick_only:
            lines.append(f"  {c['symbol']}: {format_price(c['price'])}  "
                         f"1h {format_pct(c['1h'])}  24h {format_pct(c['24h'])}")
        lines.append("")

    lines.append("━━━ 📊 MARKT-ÜBERBLICK ━━━")
    if glob:
        lines.append(f"  Marktkapitalisierung: {format_mcap(glob['mcap'])} "
                     f"({format_pct(glob['mcap_24h'])} 24h)")
        lines.append(f"  BTC-Dominanz: {glob['btc_dom']:.1f}%")
    if fng:
        trend = pct_arrow(fng["today_value"], fng["yesterday_value"])
        delta = fng["today_value"] - fng["yesterday_value"]
        delta_str = f"+{delta}" if delta >= 0 else str(delta)
        lines.append(f"  Fear & Greed: {fng['today_value']} "
                     f"({translate_fng(fng['today_class'])}) {trend} ({delta_str} vs. gestern)")
    if top_g and top_l:
        lines.append(f"  📈 Bester: {top_g['symbol']} {format_pct(top_g['24h'])}  "
                     f"📉 Schlechtester: {top_l['symbol']} {format_pct(top_l['24h'])}")
    if high_vol:
        hv_str = ", ".join(f"{c['symbol']} ({ratio:.2f}×)" for c, ratio in high_vol)
        lines.append(f"  📈 Hohes Volumen: {hv_str}")
    lines.append("")

    lines.append("━━━ WATCHLIST ━━━")
    header = f"{'Coin':<6} {'Preis':>12} {'1h':>8} {'24h':>8} {'7d':>8}"
    lines.append(header)
    lines.append("─" * len(header))

    alarm_symbols = {c["symbol"] for c in alarms}
    quick_symbols = {c["symbol"] for c in quick}

    for c in coins:
        flag = ""
        if c["symbol"] in alarm_symbols:
            flag = " 🚨"
        elif c["symbol"] in quick_symbols:
            flag = " ⚡"
        lines.append(f"{c['symbol']:<6} {format_price(c['price']):>12} "
                     f"{format_pct(c['1h']):>8} {format_pct(c['24h']):>8} "
                     f"{format_pct(c['7d']):>8}{flag}")

    lines.append("")
    lines.append(f"🕐 Stand: {timestamp}")
    return "\n".join(lines)


# ─── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[INFO] Telegram nicht konfiguriert – Ausgabe nur in Konsole.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk_start in range(0, len(text), 4000):
        chunk = text[chunk_start:chunk_start + 4000]
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text":    chunk,
        }).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        if chunk_start + 4000 < len(text):
            time.sleep(0.5)


# ─── Hauptprogramm ────────────────────────────────────────────────────────────

def main():
    timestamp = berlin_time()
    print(f"[INFO] Krypto-Marktmonitor gestartet – {timestamp}")

    coins = []
    for coin in COINS:
        result = fetch_coin(coin)
        if result:
            coins.append(result)
        time.sleep(0.1)

    if not coins:
        print("[ERROR] Keine Coin-Daten abrufbar. Abbruch.")
        return

    glob = fetch_global()
    fng  = fetch_fng()

    analysis = analyze(coins)

    news = {}
    if analysis["alarms"]:
        print(f"[INFO] {len(analysis['alarms'])} Alarm(e) – starte Nachrichten-Recherche …")
        news = research_alarms(analysis["alarms"])

    message = build_message(coins, analysis, fng, glob, news, timestamp)
    print(message)
    send_telegram(message)
    print("[INFO] Fertig.")


if __name__ == "__main__":
    main()
