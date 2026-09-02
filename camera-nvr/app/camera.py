"""Kamera-Worker: liest den RTSP-Stream in einem Hintergrund-Thread,
haelt das aktuelle Bild bereit, fuehrt Bewegungserkennung durch und loest
bei Bedarf Alarme aus.

Ein Worker pro Kamera. Beliebig viele Browser koennen sich denselben Stream
teilen (MJPEG), ohne die Kamera mehrfach zu belasten.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

import cv2
import numpy as np

from .config import AppConfig, CameraConfig, aspect_to_ratio
from .motion import MotionDetector
from .notify import send_alert
from .onvif_ptz import PTZController

log = logging.getLogger("camera-nvr.camera")

# RTSP ueber TCP erzwingen (stabiler bei billigen Kameras / WLAN).
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

# Der Browser bekommt ohnehin nur ~10 Bilder/s, die Bewegungserkennung braucht
# noch weniger. Alles darueber ist verschenkte Rechenzeit - und Dekodieren ist
# der teuerste Schritt ueberhaupt.
MAX_ANZEIGE_FPS = 10.0
MAX_BEWEGUNG_FPS = 5.0


def _placeholder_jpeg(text: str) -> bytes:
    """Graues Bild mit Text, wenn (noch) kein Stream da ist."""
    img = np.full((360, 640, 3), 40, dtype=np.uint8)
    cv2.putText(img, text, (30, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes() if ok else b""


class CameraWorker:
    def __init__(self, cam: CameraConfig, app_cfg: AppConfig):
        self.cam = cam
        self.app_cfg = app_cfg
        self._frame: bytes | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_motion_ts = 0.0
        self._last_alert_ts = 0.0
        # Rohbild + Zaehler. Das JPEG entsteht erst, wenn es jemand ABHOLT -
        # vorher wurde jeder einzelne Frame kodiert, auch ohne Zuschauer.
        self._raw: np.ndarray | None = None
        self._raw_id = 0
        self._jpeg: bytes | None = None
        self._jpeg_id = -1
        self._note = ""
        # Zuschauerzaehlung fuer den Bereitschaftsbetrieb.
        self.viewers = 0
        self._last_viewer_ts = time.time()
        self.standby = False
        # Tatsaechliche Bildgroesse des Streams. Wird beim ersten Frame gesetzt
        # und ans Dashboard gemeldet, damit die Kachel im richtigen Seiten-
        # verhaeltnis dargestellt wird (nicht jede Kamera liefert 16:9).
        self.frame_width = 0
        self.frame_height = 0

        self.detector = MotionDetector(
            sensitivity_percent=cam.motion.sensitivity_percent,
            region=cam.motion.region,
        )
        # ONVIF braucht ggf. EIGENE Zugangsdaten (siehe CameraConfig.onvif_login):
        # bei der TandemVu kennt der ONVIF-Namensraum "admin" nicht.
        onvif_user, onvif_pw = cam.onvif_login
        self.ptz = (
            PTZController(cam.host, cam.onvif_port, onvif_user, onvif_pw)
            if cam.ptz
            else None
        )

    # -- Lebenszyklus ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.cam.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- Bereitschaftsbetrieb -------------------------------------------------
    def _standby_faellig(self) -> bool:
        """Wahr, wenn niemand zuschaut, keine Bewegungserkennung laeuft und die
        Wartezeit abgelaufen ist. Dann wird der RTSP-Stream freigegeben."""
        if self.cam.standby_seconds <= 0 or self.cam.motion.enabled:
            return False
        if self.viewers > 0:
            return False
        return (time.time() - self._last_viewer_ts) >= self.cam.standby_seconds

    def _warte_auf_bedarf(self) -> bool:
        """Haelt den Worker im Leerlauf an, bis wieder jemand zuschaut.
        Gibt True zurueck, wenn gewartet wurde (Schleife neu beginnen)."""
        if not self._standby_faellig():
            if self.standby:
                self.standby = False
            return False
        if not self.standby:
            self.standby = True
            self.connected = False
            self._set_placeholder("Bereitschaft")
            log.info("Kamera %s: Bereitschaft - kein Zuschauer.", self.cam.id)
        self._stop.wait(1.0)
        return True

    # -- Interner Lauf --------------------------------------------------------
    def _run(self) -> None:
        url = self.cam.live_url
        if not url:
            log.error("Kamera %s hat keine RTSP-URL.", self.cam.id)
            return

        while not self._stop.is_set():
            if self._warte_auf_bedarf():
                continue

            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            # Puffer klein halten -> geringe Latenz.
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:  # noqa: BLE001
                pass

            if not cap.isOpened():
                self.connected = False
                self._set_placeholder("Verbinde...")
                log.warning("Kamera %s: Stream nicht erreichbar, neuer Versuch in 5s.", self.cam.id)
                time.sleep(5)
                continue

            self.connected = True
            log.info("Kamera %s: Stream verbunden.", self.cam.id)
            fail = 0
            letzte_anzeige = 0.0
            letzte_bewegung = 0.0

            while not self._stop.is_set():
                # grab() holt den Frame, dekodiert ihn aber NICHT. Dekodiert wird
                # nur, wenn das Bild auch gebraucht wird - das ist der teure Teil.
                if not cap.grab():
                    fail += 1
                    if fail > 30:
                        log.warning("Kamera %s: zu viele Lesefehler, reconnecte.", self.cam.id)
                        break
                    time.sleep(0.1)
                    continue
                fail = 0

                jetzt = time.time()
                fuer_anzeige = self.viewers > 0 and (jetzt - letzte_anzeige) >= 1.0 / MAX_ANZEIGE_FPS
                fuer_bewegung = (self.cam.motion.enabled
                                 and (jetzt - letzte_bewegung) >= 1.0 / MAX_BEWEGUNG_FPS)
                # Muss VOR dem naechsten continue stehen: sonst wird die
                # Bereitschaft genau bei den Kameras nie geprueft, die sie
                # brauchen (keine Zuschauer, keine Bewegungserkennung).
                if self._standby_faellig():
                    break  # Verbindung freigeben, niemand schaut zu

                if not (fuer_anzeige or fuer_bewegung):
                    continue  # Frame verwerfen, ohne ihn zu dekodieren

                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    continue

                h, w = frame.shape[:2]
                if (w, h) != (self.frame_width, self.frame_height):
                    self.frame_width, self.frame_height = w, h
                    log.info("Kamera %s: Bildgroesse %dx%d.", self.cam.id, w, h)

                if fuer_bewegung:
                    letzte_bewegung = jetzt
                    try:
                        moved, ratio = self.detector.update(frame)
                        if moved:
                            self._handle_motion(frame, ratio)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Bewegungserkennung Fehler (%s): %s", self.cam.id, exc)

                if fuer_anzeige:
                    letzte_anzeige = jetzt
                # Rohbild ablegen; das JPEG entsteht erst beim Abholen.
                with self._lock:
                    self._raw = frame
                    self._raw_id += 1
                    self._note = ""

            cap.release()
            self.connected = False
            if not self._stop.is_set():
                time.sleep(2)

    def _handle_motion(self, frame: np.ndarray, ratio: float) -> None:
        now = time.time()
        self.last_motion_ts = now
        if now - self._last_alert_ts < self.app_cfg.notify.cooldown_seconds:
            return
        self._last_alert_ts = now

        snapshot_path = self._save_snapshot(frame)
        msg = f"{ratio * 100:.1f}% Bildaenderung um {datetime.now():%H:%M:%S}"
        log.info("ALARM %s: %s", self.cam.name, msg)
        try:
            send_alert(self.app_cfg.notify, self.cam.name, msg, snapshot_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Alarmversand fehlgeschlagen: %s", exc)

    def _save_snapshot(self, frame: np.ndarray) -> str | None:
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            folder = os.path.join(self.app_cfg.events_dir, self.cam.id, day)
            os.makedirs(folder, exist_ok=True)
            fname = datetime.now().strftime("%H-%M-%S_%f") + ".jpg"
            path = os.path.join(folder, fname)
            cv2.imwrite(path, frame)
            return path
        except Exception as exc:  # noqa: BLE001
            log.warning("Snapshot speichern fehlgeschlagen (%s): %s", self.cam.id, exc)
            return None

    def _set_placeholder(self, text: str) -> None:
        with self._lock:
            self._raw = None
            self._jpeg = None
            self._jpeg_id = -1
            self._note = text

    # -- Oeffentliche Helfer --------------------------------------------------
    def viewer_an(self) -> None:
        with self._lock:
            self.viewers += 1
            self._last_viewer_ts = time.time()

    def viewer_ab(self) -> None:
        with self._lock:
            self.viewers = max(0, self.viewers - 1)
            self._last_viewer_ts = time.time()

    def wecken(self, timeout: float = 6.0) -> None:
        """Holt eine Kamera aus der Bereitschaft und wartet kurz auf ein Bild.
        Fuer Einzelabrufe (Schnappschuss), die keinen Dauerstream aufmachen."""
        self.viewer_an()
        try:
            ende = time.time() + timeout
            while time.time() < ende:
                with self._lock:
                    if self._raw is not None:
                        return
                time.sleep(0.2)
        finally:
            self.viewer_ab()

    def get_jpeg(self) -> bytes:
        """Kodiert das aktuelle Rohbild - aber nur einmal je Frame, egal wie
        viele Betrachter es abholen."""
        with self._lock:
            if self._raw is None:
                return _placeholder_jpeg(f"{self.cam.name}: {self._note or 'kein Bild'}")
            if self._jpeg is not None and self._jpeg_id == self._raw_id:
                return self._jpeg
            ok, buf = cv2.imencode(".jpg", self._raw, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                return _placeholder_jpeg(f"{self.cam.name}: Bildfehler")
            self._jpeg = buf.tobytes()
            self._jpeg_id = self._raw_id
            return self._jpeg

    @property
    def aspect_ratio(self) -> float:
        """Breite/Hoehe fuer die Kachel. Eingestelltes Format schlaegt die
        Messung; ohne beides der uebliche 16:9-Rueckfall."""
        fixed = aspect_to_ratio(self.cam.aspect)
        if fixed:
            return fixed
        if self.frame_width and self.frame_height:
            return self.frame_width / self.frame_height
        return 16 / 9

    def status(self) -> dict:
        return {
            "id": self.cam.id,
            "name": self.cam.name,
            "host": self.cam.host,
            "connected": self.connected,
            "ptz": bool(self.cam.ptz),
            "motion_enabled": self.cam.motion.enabled,
            "last_motion": self.last_motion_ts,
            "width": self.frame_width,
            "height": self.frame_height,
            "aspect": self.cam.aspect,
            "aspect_ratio": round(self.aspect_ratio, 4),
            "columns": self.cam.columns,
            "fill": self.cam.fill,
            "viewers": self.viewers,
            "standby": self.standby,
        }
