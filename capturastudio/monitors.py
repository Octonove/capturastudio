"""Deteccion de monitores y DPI awareness (pixeles fisicos)."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

import mss

logger = logging.getLogger(__name__)


def work_area_v(hwnd) -> tuple[int, int]:
    """(top, bottom) del AREA DE TRABAJO (pantalla menos la barra de tareas) del
    monitor que contiene `hwnd`, en pixeles FISICOS. El proceso es DPI-aware, asi
    que coinciden con las coordenadas de Tk (winfo_x/y). Sirve para dimensionar y
    colocar la ventana sin que su borde inferior quede debajo de la barra de tareas.
    Si algo falla, cae al alto de pantalla menos una barra de tareas estimada."""
    try:
        u = ctypes.windll.user32
        u.MonitorFromWindow.restype = ctypes.c_void_p            # HMONITOR (64-bit)
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                        ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]

        MONITOR_DEFAULTTONEAREST = 2
        hmon = u.MonitorFromWindow(ctypes.c_void_p(int(hwnd)), MONITOR_DEFAULTTONEAREST)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if hmon and u.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            return int(mi.rcWork.top), int(mi.rcWork.bottom)
    except (AttributeError, OSError, ValueError) as exc:  # noqa: BLE001
        logger.debug("work_area_v fallo (%s); uso fallback", exc)
    try:
        h = int(ctypes.windll.user32.GetSystemMetrics(1))    # SM_CYSCREEN (primario)
    except (AttributeError, OSError):
        h = 1080
    return 0, max(240, h - 72)


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
