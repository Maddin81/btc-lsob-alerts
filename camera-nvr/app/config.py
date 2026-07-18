"""Laden und Validieren der YAML-Konfiguration.

Unterstuetzt ${VAR}-Platzhalter, die aus Umgebungsvariablen ersetzt werden,
damit Passwoerter nicht im Klartext in der config.yaml stehen muessen.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand(value: Any) -> Any:
    """Ersetzt ${VAR} rekursiv durch Umgebungsvariablen."""
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            return os.environ.get(match.group(1), "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class MotionConfig:
    enabled: bool = True
    sensitivity_percent: float = 1.5
    region: list[float] = field(default_factory=list)


@dataclass
class CameraConfig:
    id: str
    name: str
    host: str
    username: str = "admin"
    password: str = ""
    rtsp_main: str = ""
    rtsp_sub: str = ""
    onvif_port: int = 80
    ptz: bool = False
    motion: MotionConfig = field(default_factory=MotionConfig)

    @property
    def live_url(self) -> str:
        """Bevorzugt den Sub-Stream fuer Live-Grid + Bewegungserkennung."""
        return self.rtsp_sub or self.rtsp_main


@dataclass
class NotifyConfig:
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_enabled: bool = False
    webhook_url: str = ""
    cooldown_seconds: int = 60


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    auth_user: str = ""
    auth_pass: str = ""
    events_dir: str = "/data/events"
    retention_days: int = 14
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    cameras: list[CameraConfig] = field(default_factory=list)


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw = _expand(raw)

    server = raw.get("server", {})
    storage = raw.get("storage", {})
    notify_raw = raw.get("notify", {})
    tg = notify_raw.get("telegram", {})
    wh = notify_raw.get("webhook", {})

    notify = NotifyConfig(
        telegram_enabled=bool(tg.get("enabled", False)),
        telegram_bot_token=str(tg.get("bot_token", "")),
        telegram_chat_id=str(tg.get("chat_id", "")),
        webhook_enabled=bool(wh.get("enabled", False)),
        webhook_url=str(wh.get("url", "")),
        cooldown_seconds=int(notify_raw.get("cooldown_seconds", 60)),
    )

    cameras: list[CameraConfig] = []
    for c in raw.get("cameras", []):
        m = c.get("motion", {}) or {}
        cameras.append(
            CameraConfig(
                id=str(c["id"]),
                name=str(c.get("name", c["id"])),
                host=str(c.get("host", "")),
                username=str(c.get("username", "admin")),
                password=str(c.get("password", "")),
                rtsp_main=str(c.get("rtsp_main", "")),
                rtsp_sub=str(c.get("rtsp_sub", "")),
                onvif_port=int(c.get("onvif_port", 80)),
                ptz=bool(c.get("ptz", False)),
                motion=MotionConfig(
                    enabled=bool(m.get("enabled", True)),
                    sensitivity_percent=float(m.get("sensitivity_percent", 1.5)),
                    region=list(m.get("region", []) or []),
                ),
            )
        )

    if not cameras:
        raise ValueError("Keine Kameras in der Konfiguration gefunden.")

    return AppConfig(
        host=str(server.get("host", "0.0.0.0")),
        port=int(server.get("port", 8080)),
        auth_user=str(server.get("auth_user", "")),
        auth_pass=str(server.get("auth_pass", "")),
        events_dir=str(storage.get("events_dir", "/data/events")),
        retention_days=int(storage.get("retention_days", 14)),
        notify=notify,
        cameras=cameras,
    )
