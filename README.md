# btc-lsob-alerts

Dieses Repo enthaelt vier voneinander unabhaengige Projekte. Jedes liegt in
seinem eigenen Ordner mit eigener README und eigenen Abhaengigkeiten.

| Ordner | Projekt | Laeuft wo |
|--------|---------|-----------|
| `/` (Root) | **LSOB-Alerts** – `lsob_check.py`, `backtest.py`, `requirements.txt`, Workflow `.github/workflows/lsob-check.yml` | GitHub Actions (Cron alle 5 min) → Telegram |
| `mcp_tradingview/` | **TradingView-MCP-Server** fuer Claude Desktop / Claude Code auf dem Mac | lokal (MacBook) |
| `camera-nvr/` | **Camera-NVR** – eigene Software fuer ONVIF-Kameras | Docker (z. B. Synology) |
| `crypto_market_monitor/` | **Crypto-Market-Monitor** – einfacher Preisalarm-Bot | lokal (Cron) |

## LSOB-Alerts (Hauptprojekt)

Reimplementiert die Logik aus `Custom_LSOB_Pro.pine` fuer mehrere Assets und
Zeitrahmen und meldet "LSOB Created" / "LSOB Entry" per Telegram. Details im
Docstring von `lsob_check.py`. Der Laufzeit-State (`state.json`, `signals.csv`)
lebt auf dem Daten-Branch `lsob-state`; die `state.json` im Root ist nur ein
einmaliger Seed.

Der Workflow fuehrt ausschliesslich `lsob_check.py` aus. Die anderen Ordner
werden von GitHub Actions nicht angefasst.
