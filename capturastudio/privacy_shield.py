"""Escudo de privacidad (V2): difumina zonas sensibles de un video, incluso de
forma RETROACTIVA (sobre una grabacion ya hecha) y con marca de tiempo.

El gran diferenciador de marca: ninguna herramienta cloud hara esto (conflicto
de interes); solo una app 100% local puede prometer 'te protejo de ti mismo'.
Ej.: 'mostre mi email sin querer en el minuto 4' -> se difumina ese rectangulo
solo en ese tramo.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg_utils as fu


class ShieldError(Exception):
    pass


@dataclass
class BlurRegion:
    x: int
    y: int
    w: int
    h: int
    start: float | None = None   # segundos; None = todo el video
    end: float | None = None
    strength: int = 24           # intensidad del difuminado


def safe_blur_radius(w: int, h: int, strength: int) -> int:
    """Radio de boxblur valido para una region w x h en yuv420.

    boxblur exige radio <= min(plano)/2; el plano de croma esta submuestreado x2,
    asi que el limite efectivo es min(w,h)//4. Devolver < 2 indica 'usa pixelado'."""
    return min(int(strength), max(0, min(int(w), int(h)) // 4))


def _video_dims(ffmpeg: str, video: str) -> tuple[int, int] | None:
    try:
        from . import autoframe
        return autoframe.video_dims(ffmpeg, video)
    except Exception:  # noqa: BLE001
        return None


def clamp_region(x: int, y: int, w: int, h: int, W: int, H: int):
    """Recorta (x,y,w,h) dentro de WxH. Devuelve None si la region esta
    completamente fuera del cuadro o no queda area util."""
    x, y, w, h = int(x), int(y), int(w), int(h)
    if x >= W or y >= H or x + w <= 0 or y + h <= 0:
        return None                       # completamente fuera del video
    x = max(0, min(x, W - 2))
    y = max(0, min(y, H - 2))
    w = min(w, W - x)
    h = min(h, H - y)
    if w < 2 or h < 2:
        return None
    return x, y, w - (w % 2), h - (h % 2)


def blur_regions(ffmpeg: str, video: str, out_path: str, regions: list[BlurRegion],
                 *, encoder: str = "libx264", quality_key: str = "alta",
                 pixelate: bool = False) -> None:
    """Difumina (o pixela) cada region; si tiene start/end, solo en ese tramo."""
    if not regions:
        raise ShieldError("No hay regiones que censurar.")
    dims = _video_dims(ffmpeg, video)
    filters: list[str] = []
    cur = "0:v"
    idx = 0
    for r in regions:
        x, y, w, h = int(r.x), int(r.y), int(r.w), int(r.h)
        if dims:                                    # clamp a las dimensiones reales
            cl = clamp_region(x, y, w, h, dims[0], dims[1])
            if not cl:
                continue                            # region fuera de cuadro: omitir
            x, y, w, h = cl
        i = idx
        idx += 1
        # clamp del radio (sin el, una zona pequena rompe el filtro de croma)
        safe_r = safe_blur_radius(w, h, r.strength)
        if pixelate or safe_r < 2:
            # pixelado: robusto a cualquier tamano (y mas privado en zonas chicas)
            patch = (f"[0:v]crop={w}:{h}:{x}:{y},scale={max(8, w // 16)}:{max(8, h // 16)}:flags=neighbor,"
                     f"scale={w}:{h}:flags=neighbor[b{i}]")
        else:
            patch = f"[0:v]crop={w}:{h}:{x}:{y},boxblur={safe_r}:2[b{i}]"
        filters.append(patch)
        en = ""
        if r.start is not None or r.end is not None:
            s = r.start if r.start is not None else 0
            e = r.end if r.end is not None else 1e9
            en = f":enable='between(t,{s},{e})'"
        nxt = f"v{i}"
        filters.append(f"[{cur}][b{i}]overlay={x}:{y}{en}[{nxt}]")
        cur = nxt
    if not filters:
        raise ShieldError("Las regiones quedan fuera del area del video.")
    graph = ";".join(filters)
    # -c:a aac (no copy): la salida es .mp4 y la fuente puede traer audio no-MP4.
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
           "-filter_complex", graph, "-map", f"[{cur}]", "-map", "0:a?",
           "-c:v", encoder] + fu.quality_args(encoder, quality_key) + [
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800, **fu.subprocess_kwargs())
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise ShieldError(fu._decode(proc.stderr)[-400:] or "No se pudo aplicar el difuminado.")


def focus_region(ffmpeg: str, video: str, out_path: str, rect: tuple[int, int, int, int],
                 *, start: float | None = None, end: float | None = None,
                 darkness: float = 0.5, encoder: str = "libx264",
                 quality_key: str = "alta") -> None:
    """Oscurece TODO menos `rect` (x,y,w,h): foco/spotlight sobre lo importante.

    Inverso del difuminado. Si se da start/end, el efecto solo actua en ese tramo
    (fuera, el video se ve normal). Util para 'mira aqui' en un tutorial."""
    x, y, w, h = (int(v) for v in rect)
    dims = _video_dims(ffmpeg, video)
    if dims:
        cl = clamp_region(x, y, w, h, dims[0], dims[1])
        if not cl:
            raise ShieldError("La ventana a enfocar queda fuera del area del video.")
        x, y, w, h = cl
    b = -abs(float(darkness))
    en = ""
    if start is not None or end is not None:
        s = start if start is not None else 0
        e = end if end is not None else 1e9
        en = f":enable='between(t,{s},{e})'"
    graph = (f"[0:v]eq=brightness={b:.3f}:saturation=0.4{en}[base];"
             f"[0:v]crop={w}:{h}:{x}:{y}[bright];"
             f"[base][bright]overlay={x}:{y}{en}[out]")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
           "-filter_complex", graph, "-map", "[out]", "-map", "0:a?",
           "-c:v", encoder] + fu.quality_args(encoder, quality_key) + [
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800, **fu.subprocess_kwargs())
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise ShieldError(fu._decode(proc.stderr)[-400:] or "No se pudo aplicar el foco.")


def window_rect(title_substr: str) -> tuple[int, int, int, int] | None:
    """Rectangulo (x,y,w,h) en pixeles fisicos de una ventana por subcadena de
    titulo (Win32). Util para censurar 'siempre esta app' durante la grabacion."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list[int] = []

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_substr.lower() in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(EnumProc(_cb), 0)
    if not found:
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(found[0], ctypes.byref(rect))
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
