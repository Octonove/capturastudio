"""Zoom que sigue al cursor: registra la posicion del raton DURANTE la grabacion
y, en post, convierte esa ruta en un recorte con zoom que sigue donde trabajas
(reutiliza el motor de autoframe via su parametro `trajectory`).

Solo aplica a grabaciones hechas en la app (necesita el registro del raton). El
registro es ligero (~20 Hz, ctypes GetCursorPos) y 100% local.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

from . import autoframe

logger = logging.getLogger(__name__)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MouseLogger:
    """Hilo ligero que apunta (t_rel, x, y) de pantalla a ~20 Hz."""

    def __init__(self, hz: float = 20.0):
        self._dt = 1.0 / max(5.0, hz)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[tuple[float, int, int]] = []
        self._t0: float | None = None

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            user32 = ctypes.windll.user32
        except (AttributeError, OSError):
            return
        p = _POINT()
        while not self._stop.is_set():
            try:
                if user32.GetCursorPos(ctypes.byref(p)):
                    self.samples.append((time.monotonic() - self._t0, int(p.x), int(p.y)))
            except OSError:
                pass
            self._stop.wait(self._dt)

    def stop(self) -> list[tuple[float, int, int]]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return self.samples


def log_to_trajectory(samples, region) -> list[tuple[float, float, float]]:
    """Pasa los puntos del raton (px de pantalla) a (t, cx, cy) normalizados dentro
    de la region grabada (left, top, w, h)."""
    left, top, w, h = region
    w = max(1, w)
    h = max(1, h)
    traj: list[tuple[float, float, float]] = []
    for t, x, y in samples:
        cx = min(1.0, max(0.0, (x - left) / w))
        cy = min(1.0, max(0.0, (y - top) / h))
        traj.append((float(t), cx, cy))
    return traj


def apply(ffmpeg: str, video: str, out_path: str, samples, region, *, zoom: float = 2.0,
          encoder: str = "libx264", quality_key: str = "alta") -> dict:
    traj = log_to_trajectory(samples, region)
    if len(traj) < 2:
        raise autoframe.AutoframeError("No hay suficiente registro del raton para el zoom.")
    return autoframe.autoframe(ffmpeg, video, out_path, aspect="keep", zoom=zoom,
                               trajectory=traj, encoder=encoder, quality_key=quality_key)
