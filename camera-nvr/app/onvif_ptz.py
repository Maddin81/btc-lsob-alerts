"""ONVIF-Anbindung: PTZ-Steuerung und Geraetesuche (WS-Discovery).

Kapselt die onvif-zeep-Bibliothek. Alle Aufrufe sind defensiv, weil billige
Kameras die ONVIF-Spec oft nur teilweise umsetzen.
"""
from __future__ import annotations

import logging

log = logging.getLogger("camera-nvr.onvif")


class PTZController:
    """Haelt eine ONVIF-Verbindung pro Kamera und steuert Schwenken/Neigen/Zoom."""

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._cam = None
        self._ptz = None
        self._token = None
        self._ok = False

    def _ensure(self) -> bool:
        if self._ok:
            return True
        try:
            from onvif import ONVIFCamera

            self._cam = ONVIFCamera(self.host, self.port, self.username, self.password)
            media = self._cam.create_media_service()
            self._ptz = self._cam.create_ptz_service()
            profiles = media.GetProfiles()
            if not profiles:
                raise RuntimeError("Kein Media-Profil vorhanden")
            self._token = profiles[0].token
            self._ok = True
            log.info("ONVIF verbunden: %s:%s", self.host, self.port)
        except Exception as exc:  # noqa: BLE001
            log.warning("ONVIF-Verbindung zu %s:%s fehlgeschlagen: %s", self.host, self.port, exc)
            self._ok = False
        return self._ok

    def move(self, pan: float, tilt: float, zoom: float = 0.0) -> bool:
        """Kontinuierliche Bewegung. Werte -1.0 .. 1.0."""
        if not self._ensure():
            return False
        try:
            req = self._ptz.create_type("ContinuousMove")
            req.ProfileToken = self._token
            req.Velocity = {
                "PanTilt": {"x": float(pan), "y": float(tilt)},
                "Zoom": {"x": float(zoom)},
            }
            self._ptz.ContinuousMove(req)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("PTZ move fehlgeschlagen (%s): %s", self.host, exc)
            self._ok = False
            return False

    def stop(self) -> bool:
        if not self._ensure():
            return False
        try:
            self._ptz.Stop({"ProfileToken": self._token, "PanTilt": True, "Zoom": True})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("PTZ stop fehlgeschlagen (%s): %s", self.host, exc)
            return False


def discover(timeout: int = 4) -> list[dict]:
    """WS-Discovery: findet ONVIF-Geraete im lokalen Netz.

    Gibt eine Liste von {address, xaddrs} zurueck.
    """
    try:
        from wsdiscovery.discovery import ThreadedWSDiscovery
        from wsdiscovery import QName
    except Exception as exc:  # noqa: BLE001
        log.warning("WS-Discovery nicht verfuegbar: %s", exc)
        return []

    found: list[dict] = []
    wsd = ThreadedWSDiscovery()
    try:
        wsd.start()
        # ONVIF-Geraete antworten auf diesen Typ.
        type_nd = QName("http://www.onvif.org/ver10/network/wsdl", "NetworkVideoTransmitter")
        services = wsd.searchServices(types=[type_nd], timeout=timeout)
        for svc in services:
            xaddrs = list(svc.getXAddrs())
            host = ""
            if xaddrs:
                # http://192.168.1.50/onvif/device_service -> 192.168.1.50
                try:
                    host = xaddrs[0].split("//", 1)[1].split("/", 1)[0].split(":")[0]
                except Exception:  # noqa: BLE001
                    host = ""
            found.append({"address": host, "xaddrs": xaddrs})
    except Exception as exc:  # noqa: BLE001
        log.warning("WS-Discovery fehlgeschlagen: %s", exc)
    finally:
        try:
            wsd.stop()
        except Exception:  # noqa: BLE001
            pass
    return found
