"""Benachrichtigungen bei Bewegung (Telegram, Webhook)."""
from __future__ import annotations

import logging

import requests

from .config import NotifyConfig

log = logging.getLogger("camera-nvr.notify")


def send_alert(cfg: NotifyConfig, camera_name: str, message: str, snapshot_path: str | None = None) -> None:
    """Verschickt einen Alarm ueber alle aktivierten Kanaele. Fehler werden
    nur geloggt, damit ein ausgefallener Kanal die Erkennung nicht stoppt."""
    if cfg.telegram_enabled and cfg.telegram_bot_token and cfg.telegram_chat_id:
        _send_telegram(cfg, camera_name, message, snapshot_path)
    if cfg.webhook_enabled and cfg.webhook_url:
        _send_webhook(cfg, camera_name, message, snapshot_path)


def _send_telegram(cfg: NotifyConfig, camera_name: str, message: str, snapshot_path: str | None) -> None:
    text = f"\U0001F6A8 Bewegung: {camera_name}\n{message}"
    try:
        if snapshot_path:
            url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendPhoto"
            with open(snapshot_path, "rb") as fh:
                requests.post(
                    url,
                    data={"chat_id": cfg.telegram_chat_id, "caption": text},
                    files={"photo": fh},
                    timeout=15,
                )
        else:
            url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
            requests.post(url, data={"chat_id": cfg.telegram_chat_id, "text": text}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", exc)


def _send_webhook(cfg: NotifyConfig, camera_name: str, message: str, snapshot_path: str | None) -> None:
    payload = {"camera": camera_name, "message": message, "snapshot": snapshot_path}
    try:
        requests.post(cfg.webhook_url, json=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.warning("Webhook-Benachrichtigung fehlgeschlagen: %s", exc)
