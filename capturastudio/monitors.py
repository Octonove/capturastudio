"""Deteccion de monitores y DPI awareness (pixeles fisicos)."""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass

import mss

logger = logging.getLogger(__name__)


def set_dpi_awareness() -> None:
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


@dataclass
class Monitor:
    index: int
    left: int
    top: int
    width: int
    height: int
    primary: bool

    @property
    def label(self) -> str:
        if self.index == 0:
            return f"Todo el escritorio ({self.width}x{self.height})"
        tag = " (principal)" if self.primary else ""
        return f"Pantalla {self.index}: {self.width}x{self.height}{tag}"

    @property
    def region(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


def list_monitors() -> list[Monitor]:
    out: list[Monitor] = []
    try:
        with mss.mss() as sct:
            for i, mon in enumerate(sct.monitors[1:], start=1):
                primary = mon["left"] == 0 and mon["top"] == 0
                out.append(Monitor(i, mon["left"], mon["top"], mon["width"],
                                   mon["height"], primary))
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_monitors fallo: %s", exc)
    if not out:
        try:
            u = ctypes.windll.user32
            out.append(Monitor(1, 0, 0, int(u.GetSystemMetrics(0)),
                              int(u.GetSystemMetrics(1)), True))
        except (AttributeError, OSError):
            out.append(Monitor(1, 0, 0, 1920, 1080, True))
    return out


def primary_monitor() -> Monitor:
    mons = list_monitors()
    for m in mons:
        if m.primary:
            return m
    return mons[0]
