#!/usr/bin/env python3
"""
TradingView-MCP-Server fuer die btc-lsob-alerts-Symbole.

Stellt Claude (Desktop oder Code) auf dem Mac Tools zur Verfuegung, um ueber
die Bibliothek `tradingview-ta` technische Analysen von TradingView abzurufen:
Buy/Sell-Rating (RECOMMENDATION), Oszillatoren, gleitende Durchschnitte sowie
Einzelindikatoren (RSI, MACD, EMA, ...).

Wichtig: TradingView bietet kein offizielles Live-API und keinen Zugriff auf
eigene Pine-Indikatoren (z.B. Custom_LSOB_Pro). `tradingview-ta` liest die
oeffentlich sichtbare Technical-Analysis-Zusammenfassung -- das ist ohne Login
und ohne Chart auf dem Mac moeglich, deckt aber nur die Standard-Indikatoren ab.

Die Symbolliste (Label -> "EXCHANGE:SYMBOL") ist aus lsob_check.py (ASSETS,
Feld "tv") abgeleitet. lsob_check.py bleibt die Quelle der Wahrheit; bei
Aenderungen dort bitte TRACKED unten angleichen.

Hinweis: Einige Symbole heissen im Technical-Analysis-Endpunkt anders als im
Chart-Link. Gold/Silber/Rohoel liegen deshalb hier als TVC:GOLD / TVC:SILVER /
NYMEX:CL1! (statt OANDA:XAUUSD / OANDA:XAGUSD / TVC:USOIL wie in den
Telegram-Chartlinks), weil nur diese von tradingview-ta aufgeloest werden.

Start als MCP-Server (stdio):   python server.py
Schnelltest ohne MCP:           python server.py --selftest BTC 1d
"""
import sys

try:
    from tradingview_ta import TA_Handler, Interval
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Fehlt: tradingview-ta. Installieren mit:\n"
        "  pip install -r mcp_tradingview/requirements.txt\n"
    )
    raise

# --- Symbolzuordnung (aus lsob_check.py ASSETS, Feld "tv") -------------------
TRACKED = {
    "BTC": "BINANCE:BTCUSDT",
    "ETH": "BINANCE:ETHUSDT",
    "BNB": "BINANCE:BNBUSDT",
    "SOL": "BINANCE:SOLUSDT",
    "XRP": "BINANCE:XRPUSDT",
    "DOGE": "BINANCE:DOGEUSDT",
    "ADA": "BINANCE:ADAUSDT",
    "TRX": "BINANCE:TRXUSDT",
    "LINK": "BINANCE:LINKUSDT",
    "AVAX": "BINANCE:AVAXUSDT",
    "NEAR": "BINANCE:NEARUSDT",
    "SUI": "BINANCE:SUIUSDT",
    "XLM": "BINANCE:XLMUSDT",
    "Gold": "TVC:GOLD",
    "Silber": "TVC:SILVER",
    "Apple": "NASDAQ:AAPL",
    "Microsoft": "NASDAQ:MSFT",
    "Nvidia": "NASDAQ:NVDA",
    "Google": "NASDAQ:GOOGL",
    "Amazon": "NASDAQ:AMZN",
    "HYPE": "BITUNIX:HYPEUSDT.P",
    "Take-Two": "NASDAQ:TTWO",
    "SpaceX": "NASDAQ:SPCX",
    "Rohoel": "NYMEX:CL1!",
    "Monero": "MEXC:XMRUSDT",
}
# case-insensitive Lookup fuer Labels
_LABEL_LC = {k.lower(): k for k in TRACKED}

# --- Interval-Mapping --------------------------------------------------------
# tradingview-ta kennt kein 8h; dafuer 4h als naechster verfuegbarer Wert.
INTERVALS = {
    "1m": Interval.INTERVAL_1_MINUTE,
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "2h": Interval.INTERVAL_2_HOURS,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
    "1w": Interval.INTERVAL_1_WEEK,
    "1M": Interval.INTERVAL_1_MONTH,
}
_INTERVAL_ALIAS = {
    "d": "1d", "1D": "1d", "day": "1d",
    "w": "1w", "1W": "1w", "week": "1w",
    "1mo": "1M", "month": "1M", "1month": "1M",
}

# Crypto-Boersen, die tradingview-ta ueber den "crypto"-Screener aufloest.
_CRYPTO_EXCHANGES = {
    "BINANCE", "MEXC", "BITUNIX", "COINBASE", "BYBIT", "OKX",
    "KUCOIN", "BITSTAMP", "KRAKEN", "BITFINEX", "GATEIO", "HUOBI",
}
_US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}
_FOREX_EXCHANGES = {"OANDA", "FX_IDC", "FOREXCOM", "FXCM", "FX"}
_CFD_EXCHANGES = {"TVC"}
_FUTURES_EXCHANGES = {"NYMEX", "COMEX", "CME", "CBOT", "NYBOT", "ICEUS", "ICEEUR"}


def to_interval(tf):
    key = (tf or "").strip()
    key = _INTERVAL_ALIAS.get(key, key)
    if key not in INTERVALS:
        raise ValueError(
            f"Unbekanntes Interval '{tf}'. Erlaubt: {', '.join(INTERVALS)} "
            "(8h wird von TradingViews TA nicht angeboten -> nutze 4h)."
        )
    return INTERVALS[key]


def guess_screener(exchange):
    ex = exchange.upper()
    if ex in _CRYPTO_EXCHANGES:
        return "crypto"
    if ex in _US_EXCHANGES:
        return "america"
    if ex in _FOREX_EXCHANGES:
        return "forex"
    if ex in _CFD_EXCHANGES:
        return "cfd"
    if ex in _FUTURES_EXCHANGES:
        return "futures"
    return "crypto"


def resolve(symbol, exchange=None, screener=None):
    """Nimmt ein Projekt-Label ('BTC'), 'EXCHANGE:SYMBOL' oder ein blankes
    Symbol (mit optionalem exchange) und liefert (exchange, symbol, screener)."""
    raw = (symbol or "").strip()
    if not raw:
        raise ValueError("Kein Symbol angegeben.")

    # Projekt-Label?
    if raw.lower() in _LABEL_LC:
        raw = TRACKED[_LABEL_LC[raw.lower()]]

    if ":" in raw:
        ex, sym = raw.split(":", 1)
        ex = ex.strip().upper()
        sym = sym.strip().upper()
    else:
        sym = raw.upper()
        ex = (exchange or "").strip().upper()
        if not ex:
            raise ValueError(
                f"Symbol '{raw}' ohne Boerse. Gib 'EXCHANGE:SYMBOL' an "
                "(z.B. BINANCE:BTCUSDT) oder setze das Feld exchange."
            )

    scr = (screener or "").strip().lower() or guess_screener(ex)
    return ex, sym, scr


def _round(v, ndigits=6):
    return round(v, ndigits) if isinstance(v, (int, float)) else v


def _analyze(symbol, interval, screener=None, exchange=None):
    ex, sym, scr = resolve(symbol, exchange, screener)
    handler = TA_Handler(
        symbol=sym, screener=scr, exchange=ex, interval=to_interval(interval)
    )
    a = handler.get_analysis()
    ind = a.indicators or {}
    return {
        "symbol": f"{ex}:{sym}",
        "screener": scr,
        "interval": interval,
        "recommendation": a.summary.get("RECOMMENDATION"),
        "summary": a.summary,
        "oscillators": a.oscillators.get("RECOMMENDATION"),
        "moving_averages": a.moving_averages.get("RECOMMENDATION"),
        "price": _round(ind.get("close")),
        "change_pct": _round(ind.get("change"), 3),
        "rsi": _round(ind.get("RSI"), 2),
        "macd": {
            "macd": _round(ind.get("MACD.macd"), 4),
            "signal": _round(ind.get("MACD.signal"), 4),
        },
        "ema": {
            "ema20": _round(ind.get("EMA20")),
            "ema50": _round(ind.get("EMA50")),
            "ema200": _round(ind.get("EMA200")),
        },
    }


# --- MCP-Server --------------------------------------------------------------
def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("tradingview")

    @mcp.tool()
    def list_tracked_symbols() -> dict:
        """Alle vom btc-lsob-alerts-Projekt getrackten Symbole (Label -> TradingView-Symbol)."""
        return {"count": len(TRACKED), "symbols": TRACKED}

    @mcp.tool()
    def get_analysis(
        symbol: str, interval: str = "1d", screener: str = "", exchange: str = ""
    ) -> dict:
        """TradingView-Analyse fuer ein Symbol.

        symbol:   Projekt-Label ('BTC', 'Gold', 'Apple'), 'EXCHANGE:SYMBOL'
                  (z.B. 'BINANCE:BTCUSDT') oder ein blankes Symbol + exchange.
        interval: 1m,5m,15m,30m,1h,2h,4h,1d,1w,1M (kein 8h -> 4h nehmen).
        screener: optional 'crypto'|'america'|'forex'|'cfd' (sonst automatisch).
        exchange: optional, falls symbol keine Boerse enthaelt.

        Liefert RECOMMENDATION, Summary-Zaehlung, Oszillatoren/MAs sowie
        Preis, RSI, MACD und EMA20/50/200.
        """
        try:
            return _analyze(symbol, interval, screener or None, exchange or None)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "symbol": symbol, "interval": interval}

    @mcp.tool()
    def get_indicators(
        symbol: str, interval: str = "1d", screener: str = "", exchange: str = ""
    ) -> dict:
        """Wie get_analysis, aber der komplette rohe Indikator-Dict von TradingView
        (alle verfuegbaren Werte, z.B. Stoch, ADX, BB, Pivots ...)."""
        try:
            ex, sym, scr = resolve(symbol, exchange or None, screener or None)
            handler = TA_Handler(
                symbol=sym, screener=scr, exchange=ex, interval=to_interval(interval)
            )
            a = handler.get_analysis()
            return {
                "symbol": f"{ex}:{sym}",
                "interval": interval,
                "recommendation": a.summary.get("RECOMMENDATION"),
                "indicators": a.indicators,
            }
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "symbol": symbol, "interval": interval}

    @mcp.tool()
    def get_multi_timeframe(symbol: str, intervals: list[str] | None = None) -> dict:
        """Analyse eines Symbols ueber mehrere Zeitrahmen (Default: 1h,4h,1d,1w)."""
        tfs = intervals or ["1h", "4h", "1d", "1w"]
        out = {}
        for tf in tfs:
            try:
                out[tf] = _analyze(symbol, tf)
            except Exception as e:  # noqa: BLE001
                out[tf] = {"error": f"{type(e).__name__}: {e}"}
        return {"symbol": symbol, "timeframes": out}

    @mcp.tool()
    def scan_tracked(interval: str = "1d", only: str = "all") -> dict:
        """Alle getrackten Symbole scannen und ihre RECOMMENDATION zurueckgeben.

        interval: Zeitrahmen (Default 1d).
        only:     'all' | 'buy' | 'sell' -- filtert auf STRONG_BUY/BUY bzw.
                  STRONG_SELL/SELL.
        """
        only = (only or "all").lower()
        rows = []
        for label in TRACKED:
            try:
                r = _analyze(label, interval)
                rec = r.get("recommendation") or ""
                if only == "buy" and "BUY" not in rec:
                    continue
                if only == "sell" and "SELL" not in rec:
                    continue
                rows.append({
                    "label": label,
                    "symbol": r["symbol"],
                    "recommendation": rec,
                    "price": r["price"],
                    "rsi": r["rsi"],
                })
            except Exception as e:  # noqa: BLE001
                rows.append({"label": label, "error": f"{type(e).__name__}: {e}"})
        return {"interval": interval, "filter": only, "count": len(rows), "results": rows}

    return mcp


def _selftest(argv):
    symbol = argv[0] if argv else "BTC"
    interval = argv[1] if len(argv) > 1 else "1d"
    import json
    print(json.dumps(_analyze(symbol, interval), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest(sys.argv[2:])
    else:
        build_server().run()
