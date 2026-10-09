# btc-lsob-alerts

LSOB-Alerts: reimplementiert die Logik aus `Custom_LSOB_Pro.pine` fuer mehrere
Assets und Zeitrahmen und meldet "LSOB Created" / "LSOB Entry" per Telegram.
Laeuft als GitHub-Actions-Cron (`.github/workflows/lsob-check.yml`, alle 5 min).

- `lsob_check.py` – Engine + Alerts (Details im Docstring)
- `backtest.py` – Backtest ueber die gleiche Engine
- `state.json` im Root ist nur ein einmaliger Seed; der Laufzeit-State
  (`state.json`, `signals.csv`) lebt auf dem Daten-Branch `lsob-state`.

## Ehemalige Unterprojekte

Camera-NVR, TradingView-MCP-Server und Crypto-Market-Monitor wurden aus diesem
Repo herausgeloest. Ihre Historie liegt auf den Branches `split/camera-nvr`,
`split/mcp_tradingview` und `split/crypto_market_monitor` und gehoert in eigene
Repos.
