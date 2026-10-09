#!/usr/bin/env python3
"""
Markt-Radar: stuendliche Ueberwachung einer Krypto-Watchlist auf grosse
Bewegungen und Sentiment-Wechsel. Ergaenzt den LSOB-Signal-Bot (lsob_check.py)
um Makro-Kontext und schreibt den Fear-&-Greed-Index in den geteilten State,
damit die LSOB-Alerts ihn als Konfluenz nutzen koennen.

Laeuft im selben GitHub-Actions-Workflow wie lsob_check.py, drosselt sich aber
selbst auf ~stuendlich (der Workflow tickt alle 5 Minuten). Alarmiert bewusst
nur bei ZUSTANDSWECHSELN (ein Coin ueberschreitet neu eine Schwelle, das F&G-
Regime kippt) statt jede Stunde denselben Stand zu wiederholen.

Datenquellen ohne API-Key (beide getestet):
  - CoinPaprika:  https://api.coinpaprika.com/v1/tickers/{id}  +  /v1/global
  - alternative.me Fear & Greed:  https://api.alternative.me/fng/
CoinGecko ist per robots.txt blockiert -- bewusst NICHT verwendet.
"""
import json
import os
import time

from lsob_check import (MARKET_STATE_FILE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                        http_get_json, send_telegram)

# Nur laufen, wenn seit dem letzten erfolgreichen Lauf so viel Zeit vergangen
# ist (der Workflow selbst tickt haeufiger). Ueber env fuer Tests uebersteuerbar.
MIN_INTERVAL_S = int(os.environ.get("MARKET_MIN_INTERVAL_S", 55 * 60))

# Watchlist: (CoinPaprika-ID, Label)
WATCHLIST = [
    ("btc-bitcoin", "BTC"),
    ("eth-ethereum", "ETH"),
    ("sol-solana", "SOL"),
    ("xmr-monero", "XMR"),
    ("bnb-binance-coin", "BNB"),
    ("xrp-xrp", "XRP"),
    ("ada-cardano", "ADA"),
    ("doge-dogecoin", "DOGE"),
    ("avax-avalanche", "AVAX"),
    ("shib-shiba-inu", "SHIB"),
]

# Schwellen mit Hysterese: Alarm ab TRIGGER, Entwarnung/Re-Arm erst unter CLEAR,
# damit ein Coin, der um die Schwelle pendelt, nicht dauernd erneut alarmiert.
MOVE_24H_TRIGGER = 5.0
MOVE_24H_CLEAR = 4.0
FAST_1H_TRIGGER = 2.0
FAST_1H_CLEAR = 1.5


def fmt_price(p):
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.6g}"


def fmt_pct(x):
    return f"{x:+.1f}%"


def load_market_state():
    if os.path.exists(MARKET_STATE_FILE):
        try:
            with open(MARKET_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_market_state(state):
    with open(MARKET_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fetch_coin(coin_id):
    d = http_get_json(f"https://api.coinpaprika.com/v1/tickers/{coin_id}")
    q = d["quotes"]["USD"]
    return {
        "price": q["price"],
        "ch1h": q.get("percent_change_1h") or 0.0,
        "ch24h": q.get("percent_change_24h") or 0.0,
        "ch7d": q.get("percent_change_7d") or 0.0,
        "ath_pct": q.get("percent_from_price_ath"),
    }


def signed_bucket(value, trigger, clear, prev):
    """Zustandsmaschine mit Hysterese. prev in {-1,0,1}. Liefert (neuer_zustand,
    alarm?). Alarm nur beim NEU-Eintritt in eine Zone oder beim Vorzeichenwechsel."""
    if value >= trigger:
        new = 1
    elif value <= -trigger:
        new = -1
    elif abs(value) < clear:
        new = 0
    else:
        new = prev  # in der Hysterese-Zone: Zustand halten
    fired = new != 0 and new != prev
    return new, fired


def fng_classification(value):
    if value <= 24:
        return "Extreme Angst"
    if value <= 44:
        return "Angst"
    if value <= 55:
        return "Neutral"
    if value <= 75:
        return "Gier"
    return "Extreme Gier"


def run():
    state = load_market_state()
    now = time.time()
    last_run = state.get("last_run", 0)
    if now - last_run < MIN_INTERVAL_S:
        return  # noch nicht faellig -- still beenden

    coins = {}
    for coin_id, label in WATCHLIST:
        try:
            coins[label] = fetch_coin(coin_id)
        except Exception as e:
            print(f"market: fetch {label} fehlgeschlagen: {e}")

    try:
        gl = http_get_json("https://api.coinpaprika.com/v1/global")
        global_mcap_ch = gl.get("market_cap_change_24h")
        btc_dom = gl.get("bitcoin_dominance_percentage")
    except Exception as e:
        print(f"market: global fehlgeschlagen: {e}")
        global_mcap_ch = btc_dom = None

    fng = None
    try:
        f = http_get_json("https://api.alternative.me/fng/?limit=1")
        val = int(f["data"][0]["value"])
        fng = {"value": val, "classification": fng_classification(val)}
    except Exception as e:
        print(f"market: F&G fehlgeschlagen: {e}")

    move_state = state.get("move_24h", {})
    fast_state = state.get("fast_1h", {})
    alarms, fasts = [], []

    for label, c in coins.items():
        ns, fired = signed_bucket(c["ch24h"], MOVE_24H_TRIGGER, MOVE_24H_CLEAR,
                                  move_state.get(label, 0))
        move_state[label] = ns
        if fired:
            alarms.append((label, c))
        nf, fired_f = signed_bucket(c["ch1h"], FAST_1H_TRIGGER, FAST_1H_CLEAR,
                                    fast_state.get(label, 0))
        fast_state[label] = nf
        if fired_f:
            fasts.append((label, c))

    # F&G-Regimewechsel gegen den zuletzt GEMELDETEN Stand pruefen
    fng_line = None
    prev_fng = state.get("fear_greed") or {}
    if fng and fng["classification"] != prev_fng.get("classification"):
        old = prev_fng.get("classification")
        change = f" (war {old} →)" if old else ""
        fng_line = f"😶‍🌫️ Fear & Greed: {fng['value']} {fng['classification']}{change}"

    # Nachricht nur bauen, wenn es tatsaechlich etwas Neues gibt
    if alarms or fasts or fng_line:
        head_bits = []
        for label, c in alarms:
            head_bits.append(f"{label} {fmt_pct(c['ch24h'])}")
        if fng and fng_line:
            head_bits.append(f"F&G {fng['value']}")
        headline = "🚨 Markt-Radar: " + " · ".join(head_bits) if head_bits else "⚡ Markt-Radar"

        lines = [headline, ""]
        for label, c in alarms:
            emoji = "🟢" if c["ch24h"] > 0 else "🔴"
            ath = f" · ATH {c['ath_pct']:.0f}%" if c.get("ath_pct") is not None else ""
            lines.append(f"{emoji} {label} {fmt_price(c['price'])}  {fmt_pct(c['ch24h'])} 24h  ({fmt_pct(c['ch7d'])} 7d){ath}")
        for label, c in fasts:
            if any(label == a[0] for a in alarms):
                continue  # nicht doppelt listen
            lines.append(f"⚡ {label} {fmt_price(c['price'])}  {fmt_pct(c['ch1h'])} 1h")
        if fng_line:
            lines.append(fng_line)
        ctx = []
        if global_mcap_ch is not None:
            ctx.append(f"Gesamtmarkt {fmt_pct(global_mcap_ch)} 24h")
        if btc_dom is not None:
            ctx.append(f"BTC-Dominanz {btc_dom:.1f}%")
        if ctx:
            lines.append("· " + " · ".join(ctx))

        try:
            send_telegram("\n".join(lines))
        except Exception as e:
            print(f"market: Telegram-Versand fehlgeschlagen: {e}")

    # State immer aktualisieren (auch ohne Alarm), damit F&G fuer die LSOB-
    # Anreicherung frisch bleibt und die Hysterese-Zustaende erhalten werden.
    state["last_run"] = now
    state["move_24h"] = move_state
    state["fast_1h"] = fast_state
    if fng:
        state["fear_greed"] = fng
    if global_mcap_ch is not None:
        state["global_mcap_change_24h"] = global_mcap_ch
    if btc_dom is not None:
        state["btc_dominance"] = btc_dom
    save_market_state(state)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen")
    run()
