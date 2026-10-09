# btc-lsob-alerts

LSOB-Alerts: reimplementiert die Logik aus `Custom_LSOB_Pro.pine` fuer mehrere
Assets und Zeitrahmen und meldet "LSOB Created" / "LSOB Entry" per Telegram.
Laeuft als GitHub-Actions-Cron (`.github/workflows/lsob-check.yml`, alle 5 min).

- `lsob_check.py` – Engine + Alerts (Details im Docstring)
- `backtest.py` – Backtest ueber die gleiche Engine
- `market_check.py` – stuendlicher Marktradar; pflegt den gemeinsamen
  Markt-State und ergaenzt LSOB-Benachrichtigungen um Sentiment.
- `state.json` im Root ist nur ein einmaliger Seed; der Laufzeit-State
  (`state.json`, `signals.csv`) lebt auf dem Daten-Branch `lsob-state`.

## Ehemalige Unterprojekte

Die unabhaengigen Projekte werden mit ihrer Git-Historie in eigenen privaten
Repositories weitergefuehrt:

- [Camera-NVR](https://github.com/Maddin81/camera-nvr): ONVIF-/RTSP-Kameras
  verwalten, Livebilder anzeigen und Bewegung erkennen; Docker/Synology.
- [TradingView-MCP-Server](https://github.com/Maddin81/mcp-tradingview):
  technische Marktanalysen per MCP abfragen; lokal, insbesondere auf dem Mac.
- [Crypto-Market-Monitor](https://github.com/Maddin81/crypto-market-monitor):
  eigenstaendiger Preisalarm-Bot; lokal oder per Cron.

Diese Anwendungen sind keine Abhaengigkeiten des LSOB-Workflows.
