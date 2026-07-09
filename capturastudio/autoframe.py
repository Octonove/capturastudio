"""Auto-encuadre: recorte que SIGUE al sujeto (estilo Center Stage) en post.

Diferencial frente a OBS sin depender de modelos pesados: la localizacion del
sujeto se hace por MOVIMIENTO/saliencia temporal con numpy (cero dependencias
nuevas; numpy y Pillow ya van empaquetados), asi que funciona out-of-the-box en
el .exe. Para una persona hablando a camara con fondo casi estatico, el centro
de movimiento es una buena aproximacion de "donde esta".

Pipeline: muestrea frames pequenos en gris -> centroide de movimiento por frame
-> trayectoria suavizada -> render en UNA pasada con crop guiado por sendcmd
(validado: 'sendcmd=f=cmds.txt,crop=...' con cwd + nombre relativo, porque un
path con ':' rompe el parser de filtros).
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np

_CMDS_CTR = itertools.count()

from . import ffmpeg_utils as fu
from .config import work_dir

logger = logging.getLogger(__name__)


class AutoframeError(Exception):
    pass


def available() -> bool:
    return True  # solo numpy + FFmpeg; siempre disponible


def video_dims(ffmpeg: str, path: str) -> tuple[int, int]:
    probe = fu.ffprobe_from(ffmpeg)
    if probe:
        try:
            out = fu._decode(subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0", path],
                capture_output=True, timeout=20, **fu.subprocess_kwargs()).stdout).strip()
            w, h = (int(x) for x in out.split(",")[:2])
            return w, h
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    # Fallback: parsear "1920x1080" del stderr de ffmpeg -i
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                           capture_output=True, timeout=30, **fu.subprocess_kwargs())
        m = re.search(r",\s*(\d{2,5})x(\d{2,5})", fu._decode(r.stderr))
        if m:
            return int(m.group(1)), int(m.group(2))
    except (OSError, subprocess.SubprocessError):
        pass
    raise AutoframeError("No se pudieron leer las dimensiones del video.")


def _sample_gray(ffmpeg: str, video: str, sw: int, sh: int, fps: float) -> np.ndarray:
    """Devuelve un stack (T, sh, sw) uint8 de frames en gris muestreados."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", video,
           "-vf", f"fps={fps},scale={sw}:{sh},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            **fu.subprocess_kwargs())
    fsize = sw * sh
    frames = []
    deadline = time.monotonic() + 600     # tope duro: no colgar el hilo de pulido
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                break
            buf = proc.stdout.read(fsize)
            if not buf or len(buf) < fsize:
                break
            frames.append(np.frombuffer(buf, np.uint8).reshape(sh, sw))
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not frames:
        raise AutoframeError("No se pudieron muestrear frames del video.")
    return np.stack(frames)


def _motion_centers(stack: np.ndarray) -> list[tuple[float, float]]:
    """Centro de movimiento normalizado [0,1] por frame; centro del cuadro si hay
    poco movimiento (escena estatica)."""
    sh, sw = stack.shape[1], stack.shape[2]
    arr = stack.astype(np.int16)
    diffs = np.abs(np.diff(arr, axis=0))           # (T-1, sh, sw)
    xs = np.arange(sw)[None, :]
    ys = np.arange(sh)[:, None]
    floor = 0.6 * sw * sh                           # umbral de "estatico"
    centers: list[tuple[float, float]] = []
    prev = (0.5, 0.5)
    for d in diffs:
        s = float(d.sum())
        if s < floor:
            cx, cy = prev
        else:
            cx = float((d * xs).sum()) / s / max(1, sw - 1)
            cy = float((d * ys).sum()) / s / max(1, sh - 1)
        prev = (cx, cy)
        centers.append((cx, cy))
    if not centers:
        centers = [(0.5, 0.5)]
    return [centers[0]] + centers                   # alinear longitud con los frames


def _smooth(vals: list[float], win: int) -> np.ndarray:
    """Media movil que respeta los bordes: en los extremos promedia SOLO las
    muestras disponibles (no asume ceros), evitando que el encuadre derive a la
    esquina al inicio/fin del clip."""
    win = max(1, int(win))
    a = np.asarray(vals, dtype=float)
    if win == 1 or len(a) < 3:
        return a
    k = np.ones(win)
    num = np.convolve(a, k, mode="same")
    den = np.convolve(np.ones_like(a), k, mode="same")
    return num / den


def _crop_size(W: int, H: int, aspect: str, zoom: float) -> tuple[int, int]:
    target_ar = (9 / 16) if aspect == "vertical" else (W / H)
    ch = int(H / max(1.05, zoom))
    cw = int(ch * target_ar)
    if cw > W:
        cw, ch = W, int(W / target_ar)
    if ch > H:
        ch, cw = H, int(H * target_ar)
    cw -= cw % 2
    ch -= ch % 2
    return max(2, cw), max(2, ch)


def autoframe(ffmpeg: str, video: str, out_path: str, *, aspect: str = "keep",
              zoom: float = 1.5, sample_fps: float = 4.0, encoder: str = "libx264",
              quality_key: str = "alta", trajectory=None) -> dict:
    """Genera una version recortada que SIGUE un objetivo. aspect: 'keep'|'vertical'.

    Si `trajectory` (lista de (t_seg, cx, cy) en [0,1]) se da, el recorte sigue esa
    ruta (p.ej. el cursor del raton); si no, la deduce por movimiento."""
    W, H = video_dims(ffmpeg, video)
    win = max(1, int(round(sample_fps)))            # suavizado ~1 s
    if trajectory:
        from . import ai_post
        total = ai_post.get_duration(ffmpeg, video) or (trajectory[-1][0] + 1)
        n = max(2, int(total * sample_fps))
        grid = np.arange(n) / sample_fps
        ts = np.asarray([p[0] for p in trajectory], dtype=float)
        cx = _smooth(list(np.interp(grid, ts, [p[1] for p in trajectory])), win)
        cy = _smooth(list(np.interp(grid, ts, [p[2] for p in trajectory])), win)
    else:
        sw = 128
        sh = max(2, int(round(sw * H / W)))
        sh -= sh % 2
        stack = _sample_gray(ffmpeg, video, sw, sh, sample_fps)
        centers = _motion_centers(stack)
        cx = _smooth([c[0] for c in centers], win)
        cy = _smooth([c[1] for c in centers], win)

    cw, ch = _crop_size(W, H, aspect, zoom)
    max_x, max_y = W - cw, H - ch

    def clamp_even(v, hi):
        v = int(round(v))
        v = max(0, min(hi, v))
        return v - (v % 2)

    xs_px = [clamp_even(cx[i] * W - cw / 2, max_x) for i in range(len(cx))]
    ys_px = [clamp_even(cy[i] * H - ch / 2, max_y) for i in range(len(cy))]

    wd = work_dir()
    # nombre unico por invocacion (pid + contador) para no colisionar si dos
    # auto-encuadres corren a la vez (Modo Docente + menu IA).
    cmds_name = f".cs_autoframe_{os.getpid()}_{next(_CMDS_CTR)}.cmds"
    cmds_path = wd / cmds_name
    lines = []
    for i in range(len(xs_px)):
        t = i / sample_fps
        lines.append(f"{t:.3f} crop x {xs_px[i]}, crop y {ys_px[i]};")
    cmds_path.write_text("\n".join(lines) + "\n", encoding="ascii")

    vf = f"sendcmd=f={cmds_name},crop=w={cw}:h={ch}:x={xs_px[0]}:y={ys_px[0]}"
    if aspect == "vertical":
        vf += ",scale=1080:1920,setsar=1"
    else:
        vf += f",scale={W}:{H},setsar=1"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(Path(video).resolve()),
           "-vf", vf, "-c:v", encoder] + fu.quality_args(encoder, quality_key) + [
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           str(Path(out_path).resolve())]
    try:
        proc = subprocess.run(cmd, cwd=str(wd), capture_output=True, timeout=1800,
                              **fu.subprocess_kwargs())
    finally:
        try:
            cmds_path.unlink(missing_ok=True)
        except OSError:
            pass
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise AutoframeError(fu._decode(proc.stderr)[-400:] or "No se pudo auto-encuadrar.")
    return {"crop": (cw, ch), "source": (W, H), "samples": len(xs_px), "aspect": aspect}
