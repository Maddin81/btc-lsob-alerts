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
            host, port = "", 80
            if xaddrs:
                # http://192.168.1.50:8000/onvif/device_service -> 192.168.1.50 / 8000
                try:
                    hostport = xaddrs[0].split("//", 1)[1].split("/", 1)[0]
                    if ":" in hostport:
                        host, p = hostport.rsplit(":", 1)
                        port = int(p)
                    else:
                        host = hostport
                except Exception:  # noqa: BLE001
                    host = ""
            found.append({"address": host, "onvif_port": port, "xaddrs": xaddrs})
    except Exception as exc:  # noqa: BLE001
        log.warning("WS-Discovery fehlgeschlagen: %s", exc)
    finally:
        try:
            wsd.stop()
        except Exception:  # noqa: BLE001
            pass
    return found


# Haeufige ONVIF-Ports billiger Kameras (in Reihenfolge der Wahrscheinlichkeit).
COMMON_ONVIF_PORTS = [80, 8000, 8080, 2020, 8899, 8181, 85, 8999]


def _inject_credentials(url: str, username: str, password: str) -> str:
    """Fuegt user:pass in eine rtsp://host/... URL ein, falls noch nicht vorhanden.

    ONVIF GetStreamUri liefert die URL meist OHNE Zugangsdaten zurueck.
    """
    if not url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest.split("/", 1)[0]:
        return url  # bereits mit Zugangsdaten
    if username:
        cred = username + (":" + password if password else "")
        return f"{scheme}://{cred}@{rest}"
    return url


def probe_onvif(
    host: str,
    username: str,
    password: str,
    ports: list[int] | None = None,
    timeout: int = 5,
) -> dict | None:
    """Fragt eine Kamera per ONVIF nach ihren echten Stream-URLs, PTZ-Faehigkeit
    und Auflösungen. Probiert mehrere ONVIF-Ports durch.

    Rueckgabe (oder None, wenn keine Verbindung klappt):
        {
          host, onvif_port, manufacturer, model,
          ptz: bool,
          profiles: [{name, token, resolution, rtsp}],
          rtsp_main, rtsp_sub, snapshot,
        }
    """
    try:
        from onvif import ONVIFCamera
    except Exception as exc:  # noqa: BLE001
        log.warning("onvif-Bibliothek nicht verfuegbar: %s", exc)
        return None

    for port in (ports or COMMON_ONVIF_PORTS):
        try:
            cam = ONVIFCamera(host, port, username, password)
            dev = cam.create_devicemgmt_service()
            info = dev.GetDeviceInformation()
            media = cam.create_media_service()
            profiles = media.GetProfiles()
            if not profiles:
                continue

            has_ptz = False
            try:
                ptz_service = cam.create_ptz_service()
                cfgs = ptz_service.GetConfigurations()
                has_ptz = bool(cfgs)
            except Exception:  # noqa: BLE001
                has_ptz = False

            entries: list[dict] = []
            for p in profiles:
                token = p.token
                # Aufloesung (fuer Sortierung Haupt- vs Sub-Stream).
                pixels = 0
                res_str = ""
                try:
                    res = p.VideoEncoderConfiguration.Resolution
                    pixels = int(res.Width) * int(res.Height)
                    res_str = f"{res.Width}x{res.Height}"
                except Exception:  # noqa: BLE001
                    pass
                rtsp = ""
                try:
                    setup = {
                        "Stream": "RTP-Unicast",
                        "Transport": {"Protocol": "RTSP"},
                    }
                    uri = media.GetStreamUri({"StreamSetup": setup, "ProfileToken": token})
                    rtsp = _inject_credentials(uri.Uri, username, password)
                except Exception as exc:  # noqa: BLE001
                    log.debug("GetStreamUri fehlgeschlagen (%s/%s): %s", host, token, exc)
                # PTZ pro Profil?
                p_ptz = has_ptz and getattr(p, "PTZConfiguration", None) is not None
                entries.append(
                    {
                        "name": getattr(p, "Name", token),
                        "token": token,
                        "resolution": res_str,
                        "pixels": pixels,
                        "rtsp": rtsp,
                        "ptz": p_ptz,
                    }
                )

            snapshot = ""
            try:
                snap = media.GetSnapshotUri({"ProfileToken": profiles[0].token})
                snapshot = _inject_credentials(snap.Uri, username, password)
            except Exception:  # noqa: BLE001
                pass

            with_stream = [e for e in entries if e["rtsp"]]
            ordered = sorted(with_stream, key=lambda e: e["pixels"], reverse=True)
            rtsp_main = ordered[0]["rtsp"] if ordered else ""
            rtsp_sub = ordered[-1]["rtsp"] if len(ordered) > 1 else ""

            result = {
                "host": host,
                "onvif_port": port,
                "manufacturer": getattr(info, "Manufacturer", ""),
                "model": getattr(info, "Model", ""),
                "ptz": any(e["ptz"] for e in entries) or has_ptz,
                "profiles": entries,
                "rtsp_main": rtsp_main,
                "rtsp_sub": rtsp_sub,
                "snapshot": snapshot,
            }
            log.info(
                "ONVIF erkannt: %s:%s (%s %s), %d Profile, PTZ=%s",
                host, port, result["manufacturer"], result["model"],
                len(entries), result["ptz"],
            )
            return result
        except Exception as exc:  # noqa: BLE001
            log.debug("ONVIF-Probe %s:%s fehlgeschlagen: %s", host, port, exc)
            continue

    log.warning("Keine ONVIF-Antwort von %s auf Ports %s", host, ports or COMMON_ONVIF_PORTS)
    return None

