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

from .config import AppConfig, CameraConfig
from .motion import MotionDetector
from .notify import send_alert
from .onvif_ptz import PTZController

log = logging.getLogger("camera-nvr.camera")

# RTSP ueber TCP erzwingen (stabiler bei billigen Kameras / WLAN).
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

_PLACEHOLDER = None


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

        self.detector = MotionDetector(
            sensitivity_percent=cam.motion.sensitivity_percent,
            region=cam.motion.region,
        )
        self.ptz = (
            PTZController(cam.host, cam.onvif_port, cam.username, cam.password)
            if cam.ptz
            else None
        )

    # -- Lebenszyklus ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.cam.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- Interner Lauf --------------------------------------------------------
    def _run(self) -> None:
        url = self.cam.live_url
        if not url:
            log.error("Kamera %s hat keine RTSP-URL.", self.cam.id)
            return

        while not self._stop.is_set():
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
            frame_idx = 0

            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fail += 1
                    if fail > 30:
                        log.warning("Kamera %s: zu viele Lesefehler, reconnecte.", self.cam.id)
                        break
                    time.sleep(0.1)
                    continue
                fail = 0
                frame_idx += 1

                # Bewegungserkennung nur auf jedem 2. Frame -> spart CPU.
                if self.cam.motion.enabled and frame_idx % 2 == 0:
                    try:
                        moved, ratio = self.detector.update(frame)
                        if moved:
                            self._handle_motion(frame, ratio)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Bewegungserkennung Fehler (%s): %s", self.cam.id, exc)

                # Aktuelles Bild als JPEG puffern.
                ok_enc, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok_enc:
                    with self._lock:
                        self._frame = buf.tobytes()

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
        global _PLACEHOLDER
        with self._lock:
            self._frame = _placeholder_jpeg(f"{self.cam.name}: {text}")

    # -- Oeffentliche Helfer --------------------------------------------------
    def get_jpeg(self) -> bytes:
        with self._lock:
            if self._frame is not None:
                return self._frame
        return _placeholder_jpeg(f"{self.cam.name}: kein Bild")

    def status(self) -> dict:
        return {
            "id": self.cam.id,
            "name": self.cam.name,
            "connected": self.connected,
            "ptz": bool(self.cam.ptz),
            "motion_enabled": self.cam.motion.enabled,
            "last_motion": self.last_motion_ts,
        }
