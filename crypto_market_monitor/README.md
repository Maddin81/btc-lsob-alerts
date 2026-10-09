# Crypto-Market-Monitor

Lokaler Preisalarm-Bot: ueberwacht 10 Kryptowaehrungen (CoinPaprika + Fear & Greed)
und schickt bei Alarmschwellen (24h ±5 %, 1h ±2 %, Volumen/MarketCap > 0,15)
eine Telegram-Nachricht.

Laeuft **lokal** (z. B. per Cron), nicht in GitHub Actions. Keine externen
Abhaengigkeiten ausser der Python-Standardbibliothek.

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python crypto_market_monitor/crypto_market_monitor.py
```

Unabhaengig vom LSOB-Alert (`lsob_check.py` im Repo-Root).
