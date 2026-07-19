# TradingView-MCP-Server (für den Mac)

Ein kleiner [MCP](https://modelcontextprotocol.io)-Server, mit dem **Claude
Desktop** oder **Claude Code** auf deinem MacBook TradingView-Daten für die
`btc-lsob-alerts`-Symbole abfragen kann: Buy/Sell-Rating (`RECOMMENDATION`),
Oszillatoren, gleitende Durchschnitte und Einzelindikatoren (RSI, MACD, EMA …).

## Was das kann – und was nicht

Der Server nutzt die Bibliothek [`tradingview-ta`](https://pypi.org/project/tradingview-ta/),
die die **öffentlich sichtbare Technical-Analysis-Zusammenfassung** von
TradingView liest.

- ✅ Ohne Login, ohne geöffneten Chart, ohne laufendes TradingView auf dem Mac.
- ✅ Standard-Indikatoren über alle Zeitrahmen (1m … 1M).
- ❌ **Kein Zugriff auf deinen eigenen Pine-Indikator** (`Custom_LSOB_Pro`).
  TradingView bietet dafür kein API. Deine LSOB-Signale kommen weiterhin aus
  `lsob_check.py` (GitHub-Actions-Cron → Telegram).
- ❌ Kein 8h-Zeitrahmen (bietet TradingViews TA nicht an – nimm 4h).

## Tools

| Tool | Zweck |
|------|-------|
| `list_tracked_symbols` | Alle getrackten Symbole (Label → TradingView-Symbol). |
| `get_analysis(symbol, interval, screener?, exchange?)` | Rating + Preis, RSI, MACD, EMA20/50/200. |
| `get_indicators(symbol, interval, …)` | Kompletter roher Indikator-Dict. |
| `get_multi_timeframe(symbol, intervals?)` | Ein Symbol über mehrere Zeitrahmen. |
| `scan_tracked(interval, only)` | Alle Symbole scannen; `only` = `all`/`buy`/`sell`. |

`symbol` akzeptiert ein Projekt-Label (`BTC`, `Gold`, `Apple`), ein
`EXCHANGE:SYMBOL` (`BINANCE:BTCUSDT`) oder ein blankes Symbol plus `exchange`.

## Installation auf dem Mac

```bash
# 1. Repo holen (falls noch nicht vorhanden)
git clone https://github.com/maddin81/btc-lsob-alerts.git
cd btc-lsob-alerts

# 2. Virtuelle Umgebung + Abhängigkeiten
python3 -m venv mcp_tradingview/.venv
mcp_tradingview/.venv/bin/pip install -r mcp_tradingview/requirements.txt

# 3. Schnelltest (ohne Claude) – sollte JSON mit BTC-Daten ausgeben
mcp_tradingview/.venv/bin/python mcp_tradingview/server.py --selftest BTC 1d
```

Merke dir die **absoluten Pfade** (in den Beispielen unten `<REPO>` = z.B.
`/Users/DEINNAME/btc-lsob-alerts`).

### Variante A: Claude Desktop

Datei bearbeiten (anlegen, falls nicht vorhanden):

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

```json
{
  "mcpServers": {
    "tradingview": {
      "command": "<REPO>/mcp_tradingview/.venv/bin/python",
      "args": ["<REPO>/mcp_tradingview/server.py"]
    }
  }
}
```

Claude Desktop danach **komplett beenden und neu starten**. Unten links am
Eingabefeld erscheint das MCP-/Werkzeug-Symbol; dort sollten die fünf Tools
auftauchen.

### Variante B: Claude Code (CLI)

```bash
claude mcp add tradingview \
  <REPO>/mcp_tradingview/.venv/bin/python \
  <REPO>/mcp_tradingview/server.py
```

Prüfen mit `claude mcp list`. (Der Server läuft über stdio – kein Port, keine
laufende Hintergrund-App nötig; Claude startet ihn bei Bedarf.)

## Ausprobieren

Frag Claude auf dem Mac z.B.:

- „Welche TradingView-Symbole trackt das Projekt?"
- „Gib mir die TradingView-Analyse für BTC auf 4h."
- „Scanne alle getrackten Symbole auf dem Tageschart und zeig mir nur die mit Kaufsignal."
- „Vergleiche das Rating von ETH über 1h, 4h, 1d und 1w."

## Fehlersuche

- **`command not found` / Server startet nicht:** Prüfe, dass die Pfade zu
  `python` und `server.py` absolut und korrekt sind.
- **`Exchange or symbol not found`:** Das Symbol ist im TA-Endpunkt anders
  benannt/gelistet. Gib `exchange` und `screener`
  (`crypto`/`america`/`forex`/`cfd`/`futures`) explizit an.
- **Rating wirkt „verzögert":** Tradingview-ta liefert die zuletzt
  abgeschlossene Kerze des Zeitrahmens – bei großen Zeitrahmen also nicht
  tick-aktuell.

> Hinweis: Inoffizielle Nutzung öffentlicher TradingView-Daten. Bitte die
> TradingView-Nutzungsbedingungen beachten und den Server nur für den eigenen
> Bedarf einsetzen.
