"""Anbindung an die Synology Surveillance Station - nur fuer PTZ-Positionen.

WARUM: Die gespeicherten Positionen mit den vom Nutzer vergebenen Namen
(„Schaukel", „Terasse", „Grill") liegen NICHT in der Kamera, sondern in der
Surveillance Station. Ueber ONVIF liefert die Kamera lediglich ihre
Werksfunktionen („Auto-flip", „Call patrol 1", „Remote reboot") und
durchnummerierte Leerplaetze. Gemessen, nicht vermutet.
"""
from __future__ import annotations

import logging
import re
import threading

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("camera-nvr.surveillance")
TIMEOUT = (5, 30)

# Leerplaetze der Kamera: "Preset 12", "12", "P3", "Position 4".
_AUTO_NAME = re.compile(r"^(preset|position|punkt|p)?[\s_-]*\d+$", re.IGNORECASE)


def ist_eigener_name(name: str) -> bool:
    name = (name or "").strip()
    return bool(name) and not _AUTO_NAME.match(name)


class SurveillanceStation:
    """Meldet sich bei Bedarf an und erneuert die Sitzung, wenn sie ablaeuft."""

    def __init__(self, host: str, user: str, password: str,
                 device_id: str = "", verify_tls: bool = False):
        self.basis = host.rstrip("/") if host.startswith("http") else f"https://{host}:5001"
        self.user, self.password, self.device_id = user, password, device_id
        self._s = requests.Session()
        self._s.verify = verify_tls
        self._sid: str | None = None
        self._lock = threading.Lock()

    def _login(self) -> None:
        daten = {"api": "SYNO.API.Auth", "method": "Login", "version": 6,
                 "account": self.user, "passwd": self.password,
                 "session": "SurveillanceStation", "format": "sid"}
        if self.device_id:
            daten["device_id"] = self.device_id
        j = self._s.post(f"{self.basis}/webapi/entry.cgi", data=daten, timeout=TIMEOUT).json()
        if not j.get("success"):
            raise RuntimeError(f"Anmeldung fehlgeschlagen: {j.get('error')}")
        self._sid = j["data"]["sid"]

    def call(self, method: str, **params):
        with self._lock:
            if not self._sid:
                self._login()
            daten = {"api": "SYNO.SurveillanceStation.PTZ", "method": method,
                     "version": 1, "_sid": self._sid}
            daten.update(params)
            j = self._s.post(f"{self.basis}/webapi/entry.cgi", data=daten, timeout=TIMEOUT).json()
            # 105/106/107/119 = Sitzung abgelaufen -> einmal neu anmelden.
            if not j.get("success") and (j.get("error") or {}).get("code") in (105, 106, 107, 119):
                self._login()
                daten["_sid"] = self._sid
                j = self._s.post(f"{self.basis}/webapi/entry.cgi", data=daten, timeout=TIMEOUT).json()
            if not j.get("success"):
                raise RuntimeError(f"{method} fehlgeschlagen: {j.get('error')}")
            return j.get("data") or {}

    def presets(self, camera_id: int) -> list[dict]:
        d = self.call("ListPreset", cameraId=camera_id)
        roh = d if isinstance(d, list) else d.get("presets", [])
        return [{"token": str(p.get("id")), "name": str(p.get("name", "")).strip(),
                 "eigen": ist_eigener_name(p.get("name", ""))} for p in roh if p.get("id") is not None]

    def goto(self, camera_id: int, preset_id: str) -> None:
        self.call("GoPreset", cameraId=camera_id, presetId=int(preset_id))
