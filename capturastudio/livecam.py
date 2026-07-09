"""Center Stage EN VIVO: la webcam pasa por Python durante la grabacion, se
detecta al sujeto por movimiento (numpy, sin dependencias) y se recorta con zoom
para mantenerlo encuadrado, en tiempo real. Aislado del motor de grabacion
principal: webcam -> FFmpeg(dshow) -> pipe -> Python(recorta) -> FFmpeg(encode).

NOTA: la captura real de webcam requiere una camara fisica; la logica de recorte
(process_frame) es pura y testeable con frames sinteticos.
"""

from __future__ import annotations

import logging
import subprocess
import threading

import numpy as np

from . import ffmpeg_utils as fu

logger = logging.getLogger(__name__)


class LiveCamError(Exception):
    pass


def crop_box(W: int, H: int, cx: float, cy: float, zoom: float) -> tuple[int, int, int, int]:
    """Caja de recorte (x, y, w, h) centrada en (cx,cy) normalizado, con `zoom`."""
    cw = max(2, int(W / max(1.05, zoom)))
    ch = max(2, int(H / max(1.05, zoom)))
    cw -= cw % 2
    ch -= ch % 2
    x = int(round(cx * W - cw / 2))
    y = int(round(cy * H - ch / 2))
    x = max(0, min(x, W - cw))
    y = max(0, min(y, H - ch))
    return x, y, cw, ch


def _motion_center(gray: np.ndarray, prev_gray, prev_center, floor_ratio: float = 0.6):
    """Centroide de movimiento (frame vs anterior); mantiene el anterior si hay
    poco movimiento. Devuelve (cx, cy) normalizado [0,1]."""
    if prev_gray is None or prev_gray.shape != gray.shape:
        return prev_center
    d = np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16))
    s = float(d.sum())
    sh, sw = gray.shape
    if s < floor_ratio * sw * sh:
        return prev_center
    xs = np.arange(sw)[None, :]
    ys = np.arange(sh)[:, None]
    cx = float((d * xs).sum()) / s / max(1, sw - 1)
    cy = float((d * ys).sum()) / s / max(1, sh - 1)
    return cx, cy


def process_frame(frame_bgr: np.ndarray, state: dict, *, zoom: float = 1.6,
                  alpha: float = 0.18) -> np.ndarray:
    """Recorta el frame siguiendo al sujeto. `state` mantiene el centro suavizado
    y el gris anterior entre llamadas. Pura (testeable con arrays sinteticos)."""
    H, W = frame_bgr.shape[0], frame_bgr.shape[1]
    # gris reducido para deteccion barata
    small = frame_bgr[::8, ::8].mean(axis=2).astype(np.uint8)
    prev_center = state.get("center", (0.5, 0.5))
    target = _motion_center(small, state.get("prev"), prev_center)
    # suavizado exponencial: estable y con latencia baja, apto para vivo
    cx = prev_center[0] * (1 - alpha) + target[0] * alpha
    cy = prev_center[1] * (1 - alpha) + target[1] * alpha
    state["center"] = (cx, cy)
    state["prev"] = small
    x, y, cw, ch = crop_box(W, H, cx, cy, zoom)
    crop = frame_bgr[y:y + ch, x:x + cw]
    return crop


class LiveCenterStage:
    """Graba la webcam recortada siguiendo al sujeto. Pipeline aislado."""

    def __init__(self, ffmpeg: str, device: str, out_path: str, *, width: int = 1280,
                 height: int = 720, fps: int = 30, zoom: float = 1.6,
                 encoder: str = "libx264", quality_key: str = "alta", on_error=None):
        self.ffmpeg = ffmpeg
        self.device = device
        self.out_path = out_path
        self.W, self.H, self.fps, self.zoom = width, height, fps, zoom
        self.encoder, self.quality_key = encoder, quality_key
        self.on_error = on_error or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap = None
        self._enc = None
        self.error: str | None = None
        self._failed = False

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _fail(self, msg: str) -> None:
        self.error = msg
        if not self._failed:
            self._failed = True
            try:
                self.on_error(msg)
            except Exception:  # noqa: BLE001
                pass

    def _cap_cmd(self) -> list[str]:
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "dshow",
                "-video_size", f"{self.W}x{self.H}", "-framerate", str(self.fps),
                "-i", f"video={self.device}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]

    def _enc_cmd(self, cw: int, ch: int) -> list[str]:
        return [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                "-pix_fmt", "bgr24", "-s", f"{cw}x{ch}", "-r", str(self.fps), "-i", "pipe:0",
                "-vf", f"scale={self.W}:{self.H},setsar=1", "-c:v", self.encoder]\
            + fu.quality_args(self.encoder, self.quality_key)\
            + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", self.out_path]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        fsize = self.W * self.H * 3
        try:
            self._cap = subprocess.Popen(self._cap_cmd(), stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, **fu.subprocess_kwargs())
        except OSError as exc:
            self._fail(f"No se pudo abrir la webcam: {exc}")
            return
        state: dict = {}
        cw = ch = None
        got = False
        try:
            while not self._stop.is_set():
                buf = self._cap.stdout.read(fsize)
                if not buf or len(buf) < fsize:
                    break
                got = True
                frame = np.frombuffer(buf, np.uint8).reshape(self.H, self.W, 3)
                crop = process_frame(frame, state, zoom=self.zoom)
                if self._enc is None:
                    ch, cw = crop.shape[0], crop.shape[1]
                    self._enc = subprocess.Popen(self._enc_cmd(cw, ch), stdin=subprocess.PIPE,
                                                 stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL, **fu.subprocess_kwargs())
                if crop.shape[1] != cw or crop.shape[0] != ch:  # tamano constante
                    crop = crop[:ch, :cw]
                try:
                    self._enc.stdin.write(crop.tobytes())
                except (BrokenPipeError, OSError, ValueError):
                    break
        finally:
            if not got and not self._stop.is_set():
                err = ""
                try:
                    if self._cap and self._cap.stderr:
                        err = fu._decode(self._cap.stderr.read())[-300:].strip()
                except OSError:
                    pass
                self._fail(err or "No se recibio video de la camara (¿esta en uso?).")
            self._cleanup()

    def _cleanup(self) -> None:
        # 1) Captura: no finaliza nada -> matar directo (desbloquea el read()).
        if self._cap is not None:
            try:
                self._cap.terminate()
                self._cap.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._cap.kill()
                except OSError:
                    pass
        # 2) Encoder: EOF (close stdin) y ESPERAR a que escriba el moov/+faststart
        # ANTES de cualquier terminate, o el MP4 queda truncado/corrupto.
        enc = self._enc
        if enc is not None:
            try:
                if enc.stdin:
                    enc.stdin.close()
            except OSError:
                pass
            try:
                enc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    enc.terminate()
                    enc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        enc.kill()
                    except OSError:
                        pass

    def stop(self) -> str | None:
        """Parada limpia: deja que el encoder finalice el MP4 (puede tardar)."""
        self._stop.set()
        if self._cap is not None:        # desbloquea el read() del hilo
            try:
                self._cap.terminate()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=35)   # > espera del encoder en _cleanup
        return self.error

    def abort(self) -> None:
        """Parada rapida (al cerrar): mata ambos procesos sin esperar al moov."""
        self._stop.set()
        for proc in (self._cap, self._enc):
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
