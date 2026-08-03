"""Laden, Validieren und Schreiben der YAML-Konfiguration.

Unterstuetzt ${VAR}-Platzhalter, die aus Umgebungsvariablen ersetzt werden,
damit Passwoerter nicht im Klartext in der config.yaml stehen muessen.

WICHTIG: Zum Bearbeiten (Kamera hinzufuegen/aendern/loeschen) wird immer der
ROHE YAML-Baum veraendert und zurueckgeschrieben - nie die geladene AppConfig.
Sonst wuerden die ${VAR}-Platzhalter beim Speichern durch die aufgeloesten
Klartext-Passwoerter ersetzt.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Erlaubte Werte fuer das Bildformat einer Kamera.
# "auto" = wird aus dem tatsaechlichen Stream ermittelt.
_ASPECT_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)\s*$")


def aspect_to_ratio(value: str) -> float | None:
    """'16:9' -> 1.777..., 'auto'/leer/ungueltig -> None."""
    if not value or value.strip().lower() == "auto":
        return None
    m = _ASPECT_PATTERN.match(value)
    if not m:
        return None
    w, h = float(m.group(1)), float(m.group(2))
    return w / h if h > 0 else None


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
    enabled: bool = True
    # Bildformat der Kachel: "auto" (aus dem Stream ermittelt) oder z.B. "16:9",
    # "4:3", "10:3". Noetig, weil nicht jede Kamera 16:9 liefert - die Reolink
    # am Carport z.B. 1920x576 (sehr breit).
    aspect: str = "auto"
    # Breite der Kachel im Gitter: 0 = automatisch (breite Bilder ab 2.2:1
    # bekommen zwei Spalten), 1-3 = feste Spaltenzahl, -1 = volle Zeilenbreite.
    # Volle Breite ist der zuverlaessige Weg, wenn zwei Kacheln GARANTIERT
    # untereinander stehen sollen - bei fester Spaltenzahl haengt das sonst
    # von der Fensterbreite ab.
    columns: int = 0
    # "contain" = ganzes Bild, ggf. mit Balken. "cover" = formatfuellend
    # zuschneiden. Nuetzlich, wenn eine Kachel bewusst breiter gezogen wird.
    fill: str = "contain"
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


# -- Rohe YAML-Ebene (fuer das Bearbeiten) ------------------------------------

def default_raw() -> dict:
    """Leeres, gueltiges Config-Geruest ohne Kameras."""
    return {
        "server": {"host": "0.0.0.0", "port": 8080, "auth_user": "", "auth_pass": ""},
        "storage": {"events_dir": "/data/events", "retention_days": 14},
        "notify": {
            "telegram": {"enabled": False, "bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "${TELEGRAM_CHAT_ID}"},
            "webhook": {"enabled": False, "url": ""},
            "cooldown_seconds": 60,
        },
        "cameras": [],
    }


def load_raw(path: str) -> dict:
    """Liest die config.yaml unveraendert (mit ${VAR}-Platzhaltern).
    Fehlt die Datei, kommt das leere Geruest zurueck."""
    if not os.path.isfile(path):
        return default_raw()
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml enthaelt kein Objekt auf oberster Ebene.")
    raw.setdefault("cameras", [])
    return raw


def dump_raw(raw: dict) -> str:
    return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False)


def save_raw(path: str, raw: dict) -> None:
    """Schreibt atomar und nur, wenn das Ergebnis auch ladbar ist."""
    parse_config(raw)  # wirft ValueError bei Unsinn
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dump_raw(raw))
    os.replace(tmp, path)


_CAMERA_FIELDS = (
    "id", "name", "host", "username", "password", "rtsp_main", "rtsp_sub",
    "onvif_port", "ptz", "enabled", "aspect", "columns", "fill", "motion",
)


def normalize_camera(data: dict, existing_ids: set[str] | None = None) -> dict:
    """Prueft eine einzelne Kamera aus dem Formular und bringt sie in die
    Form, wie sie in der YAML steht. Wirft ValueError mit Klartext-Grund."""
    existing_ids = existing_ids or set()
    cam = {k: v for k, v in (data or {}).items() if k in _CAMERA_FIELDS}

    name = str(cam.get("name", "")).strip()
    if not name:
        raise ValueError("Name fehlt.")
    cid = str(cam.get("id", "")).strip() or slugify(name)
    if not cid:
        raise ValueError("Kennung (id) fehlt und laesst sich nicht aus dem Namen ableiten.")
    if cid in existing_ids:
        raise ValueError(f"Kennung '{cid}' ist bereits vergeben.")

    rtsp_main = str(cam.get("rtsp_main", "")).strip()
    rtsp_sub = str(cam.get("rtsp_sub", "")).strip()
    if not (rtsp_main or rtsp_sub):
        raise ValueError("Mindestens eine RTSP-Adresse (Haupt- oder Sub-Stream) wird gebraucht.")

    aspect = str(cam.get("aspect", "auto")).strip() or "auto"
    if aspect.lower() != "auto" and aspect_to_ratio(aspect) is None:
        raise ValueError(f"Bildformat '{aspect}' ist ungueltig - erwartet z.B. 16:9 oder auto.")

    try:
        port = int(cam.get("onvif_port", 80) or 80)
    except (TypeError, ValueError):
        raise ValueError("ONVIF-Port muss eine Zahl sein.")

    try:
        spalten = int(cam.get("columns", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("Kachelbreite muss eine Zahl sein.")
    if spalten not in (-1, 0, 1, 2, 3):
        raise ValueError("Kachelbreite: -1 (volle Breite), 0 (automatisch) oder 1-3 Spalten.")

    fuellung = str(cam.get("fill", "contain") or "contain").strip().lower()
    if fuellung not in ("contain", "cover"):
        raise ValueError("Bildanpassung muss 'contain' oder 'cover' sein.")

    m = cam.get("motion") or {}
    out = {
        "id": cid,
        "name": name,
        "host": str(cam.get("host", "")).strip(),
        "username": str(cam.get("username", "admin") or "admin"),
        "password": str(cam.get("password", "") or ""),
        "rtsp_main": rtsp_main,
        "rtsp_sub": rtsp_sub,
        "onvif_port": port,
        "ptz": bool(cam.get("ptz", False)),
        "enabled": bool(cam.get("enabled", True)),
        "aspect": aspect,
        "columns": spalten,
        "fill": fuellung,
        "motion": {
            "enabled": bool(m.get("enabled", True)),
            "sensitivity_percent": float(m.get("sensitivity_percent", 1.5) or 1.5),
            "region": list(m.get("region", []) or []),
        },
    }
    return out


def slugify(text: str, fallback: str = "") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower().replace("ä", "ae")
               .replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")).strip("-")
    return s or fallback


def unique_id(base: str, taken: set[str]) -> str:
    cid, n = base or "cam", 2
    while cid in taken:
        cid = f"{base}-{n}"
        n += 1
    return cid


# -- Geparste Ebene (fuer die Laufzeit) ---------------------------------------

def parse_config(raw: dict) -> AppConfig:
    """Loest ${VAR} auf und baut die typisierte Konfiguration.
    Eine leere Kameraliste ist erlaubt - die App geht dann in den
    Einrichtungsmodus."""
    raw = _expand(raw or {})

    server = raw.get("server", {}) or {}
    storage = raw.get("storage", {}) or {}
    notify_raw = raw.get("notify", {}) or {}
    tg = notify_raw.get("telegram", {}) or {}
    wh = notify_raw.get("webhook", {}) or {}

    notify = NotifyConfig(
        telegram_enabled=bool(tg.get("enabled", False)),
        telegram_bot_token=str(tg.get("bot_token", "")),
        telegram_chat_id=str(tg.get("chat_id", "")),
        webhook_enabled=bool(wh.get("enabled", False)),
        webhook_url=str(wh.get("url", "")),
        cooldown_seconds=int(notify_raw.get("cooldown_seconds", 60)),
    )

    cameras: list[CameraConfig] = []
    seen: set[str] = set()
    for c in raw.get("cameras", []) or []:
        m = c.get("motion", {}) or {}
        cid = str(c["id"])
        if cid in seen:
            raise ValueError(f"Doppelte Kamera-Kennung '{cid}'.")
        seen.add(cid)
        cameras.append(
            CameraConfig(
                id=cid,
                name=str(c.get("name", cid)),
                host=str(c.get("host", "")),
                username=str(c.get("username", "admin")),
                password=str(c.get("password", "")),
                rtsp_main=str(c.get("rtsp_main", "")),
                rtsp_sub=str(c.get("rtsp_sub", "")),
                onvif_port=int(c.get("onvif_port", 80)),
                ptz=bool(c.get("ptz", False)),
                enabled=bool(c.get("enabled", True)),
                aspect=str(c.get("aspect", "auto") or "auto"),
                columns=int(c.get("columns", 0) or 0),
                fill=str(c.get("fill", "contain") or "contain"),
                motion=MotionConfig(
                    enabled=bool(m.get("enabled", True)),
                    sensitivity_percent=float(m.get("sensitivity_percent", 1.5)),
                    region=list(m.get("region", []) or []),
                ),
            )
        )

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


def load_config(path: str) -> AppConfig:
    return parse_config(load_raw(path))
