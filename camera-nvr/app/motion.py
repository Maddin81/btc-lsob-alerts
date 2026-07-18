"""Einfache, robuste Bewegungserkennung per Frame-Differenz.

Bewusst leichtgewichtig gehalten, damit auch mehrere Kameras gleichzeitig
auf einer Synology laufen. Arbeitet auf verkleinerten Graustufenbildern.
"""
from __future__ import annotations

import cv2
import numpy as np


class MotionDetector:
    def __init__(self, sensitivity_percent: float = 1.5, region: list[float] | None = None):
        # Anteil veraenderter Pixel (0-100), ab dem Bewegung gemeldet wird.
        self.threshold_ratio = max(0.05, sensitivity_percent) / 100.0
        self.region = region or []  # [x, y, w, h] in Prozent
        self._prev: np.ndarray | None = None

    def _crop(self, gray: np.ndarray) -> np.ndarray:
        if not self.region or len(self.region) != 4:
            return gray
        h, w = gray.shape[:2]
        x = int(self.region[0] / 100.0 * w)
        y = int(self.region[1] / 100.0 * h)
        rw = int(self.region[2] / 100.0 * w)
        rh = int(self.region[3] / 100.0 * h)
        x, y = max(0, x), max(0, y)
        return gray[y:y + rh, x:x + rw]

    def update(self, frame_bgr: np.ndarray) -> tuple[bool, float]:
        """Gibt (bewegung_erkannt, anteil_veraenderter_pixel) zurueck."""
        # Verkleinern -> weniger Rauschen, deutlich schneller.
        small = cv2.resize(frame_bgr, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        gray = self._crop(gray)

        if self._prev is None or self._prev.shape != gray.shape:
            self._prev = gray
            return False, 0.0

        delta = cv2.absdiff(self._prev, gray)
        # Gleitender Hintergrund: langsam nachfuehren, damit Lichtaenderungen
        # nicht dauerhaft als Bewegung zaehlen.
        self._prev = cv2.addWeighted(self._prev, 0.9, gray, 0.1, 0).astype(np.uint8)

        _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)

        changed = float(np.count_nonzero(thresh))
        total = float(thresh.size)
        ratio = changed / total if total else 0.0
        return ratio >= self.threshold_ratio, ratio
