"""Enumeracion y captura de ventanas para 'Captura de ventana' (gdigrab title=).

CLAVE: el preview y el dialogo de recorte deben ver LO MISMO que grabara ffmpeg.
- ffmpeg usa `gdigrab -i title=X`, que hace FindWindow(NULL, title) y captura el
  AREA CLIENTE de esa ventana (sin barra de titulo ni bordes), a prueba de que
  este tapada por otras.
- Por eso aqui se identifica la ventana con FindWindow (la MISMA que gdigrab) y
  se captura su area cliente con PrintWindow (tambien a prueba de oclusion). Asi
  el recorte que el usuario marca coincide, al pixel, con lo grabado.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

_SHELL = {"Program Manager", "Windows Input Experience", "NVIDIA GeForce Overlay"}

try:
    _u = ctypes.windll.user32
    _gdi = ctypes.windll.gdi32
    _dwm = ctypes.windll.dwmapi
    _u.IsWindowVisible.argtypes = [wintypes.HWND]
    _u.IsIconic.argtypes = [wintypes.HWND]
    _u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _u.FindWindowW.restype = wintypes.HWND
    _u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _u.GetDC.restype = wintypes.HDC
    _u.GetDC.argtypes = [wintypes.HWND]
    _u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _gdi.CreateCompatibleDC.restype = wintypes.HDC
    _gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi.SelectObject.restype = wintypes.HGDIOBJ
    _gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi.DeleteDC.argtypes = [wintypes.HDC]
    _gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                               wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                               wintypes.UINT]
    _WIN = True
except Exception as exc:  # noqa: BLE001
    _WIN = False
    logger.warning("winlist no disponible: %s", exc)

_DWMWA_CLOAKED = 14
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080


def _is_cloaked(hwnd) -> bool:
    """Ventanas UWP fantasma (p. ej. 'Experiencia de entrada de Windows') estan
    'cloaked' por DWM aunque IsWindowVisible sea True. Filtro por idioma-neutro."""
    try:
        val = ctypes.c_int(0)
        _dwm.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(val),
                                   ctypes.sizeof(val))
        return val.value != 0
    except Exception:  # noqa: BLE001
        return False


def _hwnd_for(title: str):
    """El MISMO hwnd que usara gdigrab: FindWindow(NULL, title)."""
    if not _WIN or not title:
        return None
    h = _u.FindWindowW(None, title)
    return h or None


def _client_screen_rect(hwnd) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) del AREA CLIENTE en coordenadas de pantalla (lo que captura
    gdigrab). None si esta minimizada o sin tamano."""
    if not hwnd or _u.IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    _u.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    if w <= 0 or h <= 0:
        return None
    p = wintypes.POINT(0, 0)
    _u.ClientToScreen(hwnd, ctypes.byref(p))
    return (p.x, p.y, w, h)


def list_windows(exclude_titles: tuple[str, ...] = ()) -> list[tuple[str, tuple[int, int, int, int]]]:
    """[(titulo, (x, y, w, h) del area cliente)] de ventanas de aplicacion
    visibles, de mayor a menor area."""
    if not _WIN:
        return []
    out: list[tuple[str, tuple[int, int, int, int]]] = []
    excl = set(exclude_titles) | _SHELL

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _l):
        try:
            if not _u.IsWindowVisible(hwnd) or _u.IsIconic(hwnd) or _is_cloaked(hwnd):
                return True
            if _u.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
                return True          # tool windows (paletas, no apps)
            n = _u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            _u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            if not title or title in excl:
                return True
            rect = _client_screen_rect(hwnd)
            if not rect or rect[2] < 160 or rect[3] < 100:
                return True
            out.append((title, rect))
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        _u.EnumWindows(EnumProc(_cb), 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EnumWindows fallo: %s", exc)
    mejor: dict[str, tuple[int, int, int, int]] = {}
    for t, rect in out:
        if t not in mejor or rect[2] * rect[3] > mejor[t][2] * mejor[t][3]:
            mejor[t] = rect
    return sorted(mejor.items(), key=lambda it: -(it[1][2] * it[1][3]))


def window_rect(title: str) -> tuple[int, int, int, int] | None:
    """Area cliente en pantalla de LA MISMA ventana que grabara gdigrab
    (FindWindow por titulo exacto). None si no existe / esta minimizada."""
    return _client_screen_rect(_hwnd_for(title))


def count_title(title: str) -> int:
    """Cuantas ventanas visibles comparten ESE titulo exacto (para avisar de que
    gdigrab no puede desambiguar y grabara la que este mas al frente)."""
    if not _WIN:
        return 0
    n = [0]
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _l):
        try:
            if _u.IsWindowVisible(hwnd) and not _u.IsIconic(hwnd) and not _is_cloaked(hwnd):
                ln = _u.GetWindowTextLengthW(hwnd)
                if ln > 0:
                    b = ctypes.create_unicode_buffer(ln + 1)
                    _u.GetWindowTextW(hwnd, b, ln + 1)
                    if b.value == title:
                        n[0] += 1
        except Exception:  # noqa: BLE001
            pass
        return True
    try:
        _u.EnumWindows(EnumProc(_cb), 0)
    except Exception:  # noqa: BLE001
        pass
    return n[0]


def capture_window(title: str):
    """Imagen PIL RGB del CONTENIDO (area cliente) de la ventana via PrintWindow
    -a prueba de oclusion, igual que gdigrab-. None si no se puede (usar mss como
    respaldo). Asi el preview/recorte ve exactamente lo que se grabara."""
    if not _WIN:
        return None
    from PIL import Image
    hwnd = _hwnd_for(title)
    if not hwnd or _u.IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    _u.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    if w <= 0 or h <= 0:
        return None
    hdc = _u.GetDC(hwnd)
    if not hdc:
        return None
    memdc = _gdi.CreateCompatibleDC(hdc)
    bmp = _gdi.CreateCompatibleBitmap(hdc, w, h)
    old = _gdi.SelectObject(memdc, bmp)
    try:
        PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
        ok = _u.PrintWindow(hwnd, memdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not ok:
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h            # top-down
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0        # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = _gdi.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if not got:
            return None
        return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("PrintWindow fallo: %s", exc)
        return None
    finally:
        _gdi.SelectObject(memdc, old)
        _gdi.DeleteObject(bmp)
        _gdi.DeleteDC(memdc)
        _u.ReleaseDC(hwnd, hdc)
