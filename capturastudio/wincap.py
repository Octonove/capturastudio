"""Captura de ventana con Windows Graphics Capture (WGC), la API moderna que usa
OBS. A prueba de OCLUSION (aunque otra ventana la tape) y SIGUE a la ventana
aunque se mueva, tambien con apps aceleradas por GPU (Chrome, Edge, Electron...).

Reemplaza a gdigrab de region (que copia el framebuffer de pantalla y por tanto
graba lo que este ENCIMA de la ventana). Si WGC no esta disponible en el equipo,
el motor cae automaticamente a gdigrab de region (comportamiento anterior).

Dos usos, misma superficie de ventana (mismo espacio de pixeles -> el recorte
marcado en el preview coincide al pixel con lo grabado):
- WindowGrabber: sesion persistente que expone el ULTIMO fotograma (para el
  preview y el dialogo de recorte).
- WindowPump: sesion + named pipe de Windows que alimenta a FFmpeg con rawvideo
  (para la grabacion). El pipe RECONECTA en cada segmento (el motor relanza
  FFmpeg al pausar/reanudar).

GOTCHA: windows_capture/__init__.py hace `import cv2` duro, pero cv2 solo lo usa
save_as_image (que no usamos). Se inyecta un stub de cv2 antes de importar para
no depender de opencv (ahorra ~44 MB en el .exe).
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import os
import sys
import threading
import types
from ctypes import wintypes

logger = logging.getLogger(__name__)

# contador monotonico: el nombre del pipe lleva PID + secuencia, asi dos apps o
# dos grabaciones sucesivas NUNCA colisionan (una instancia colgada daria
# ERROR_PIPE_BUSY al recrear el mismo nombre).
_pipe_seq = itertools.count(1)

# --- carga perezosa de windows_capture (con stub de cv2) --------------------
_WC = None  # None=sin probar, False=no disponible, modulo=disponible


def _load_wc():
    global _WC
    if _WC is not None:
        return _WC or None
    try:
        sys.modules.setdefault("cv2", types.ModuleType("cv2"))
        import windows_capture  # noqa: PLC0415
        _WC = windows_capture
    except Exception as exc:  # noqa: BLE001
        _WC = False
        logger.info("WGC no disponible (%s); se usara gdigrab de region.", exc)
    return _WC or None


def available() -> bool:
    """True si se puede capturar con WGC en este equipo."""
    return _load_wc() is not None


# --- helpers de named pipe (ctypes) -----------------------------------------
_INVALID = ctypes.c_void_p(-1).value
_PIPE_ACCESS_OUTBOUND = 0x00000002
_PIPE_TYPE_BYTE = 0x0
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3

try:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateNamedPipeW.restype = ctypes.c_void_p
    _k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, ctypes.c_void_p]
    _k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
    _k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    _k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p]
    _PIPES_OK = True
except Exception as exc:  # noqa: BLE001
    _PIPES_OK = False
    logger.warning("named pipes no disponibles: %s", exc)


# ---------------------------------------------------------------------------
# Sesion WGC: mantiene el ULTIMO fotograma a tamano fijo
# ---------------------------------------------------------------------------
class _WgcSession:
    """Arranca WGC sobre un hwnd y guarda el ultimo fotograma (BGRA) a tamano
    fijado en el primer frame. Con pad/crop si la ventana cambia de tamano, para
    que el flujo de bytes hacia FFmpeg no varie de dimensiones."""

    def __init__(self, hwnd: int, cursor: bool = True):
        self.hwnd = int(hwnd)
        self.cursor = cursor
        self.w = 0
        self.h = 0
        self._frame: bytes | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._ctrl = None
        self._cap = None

    def start(self, timeout: float = 4.0, wait: bool = True) -> bool:
        """Arranca la sesion WGC. Con wait=True bloquea hasta el primer frame
        (para conocer el tamano); con wait=False vuelve enseguida (el preview no
        debe congelar la UI) y el tamano/frame llegan de forma asincrona."""
        wc = _load_wc()
        if wc is None or not self.hwnd:
            return False
        try:
            import numpy as np  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.warning("WGC necesita numpy: %s", exc)
            return False

        try:
            cap = wc.WindowsCapture(window_hwnd=self.hwnd, cursor_capture=bool(self.cursor),
                                    draw_border=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WGC no pudo abrir la ventana %s: %s", self.hwnd, exc)
            return False

        @cap.event
        def on_frame_arrived(frame, ctrl):  # noqa: ANN001
            try:
                buf = frame.frame_buffer          # (H, W, 4) BGRA
                with self._lock:
                    if self.w == 0:
                        self.h, self.w = int(buf.shape[0]), int(buf.shape[1])
                    h, w = self.h, self.w
                    b = buf
                    if b.shape[0] != h or b.shape[1] != w:
                        fixed = np.zeros((h, w, 4), dtype=np.uint8)
                        hh, ww = min(h, b.shape[0]), min(w, b.shape[1])
                        fixed[:hh, :ww] = b[:hh, :ww]
                        b = fixed
                    self._frame = np.ascontiguousarray(b).tobytes()
                self._ready.set()
            except Exception as exc:  # noqa: BLE001
                logger.debug("WGC on_frame: %s", exc)

        @cap.event
        def on_closed():  # la ventana se cerro
            self._closed.set()

        try:
            self._cap = cap
            self._ctrl = cap.start_free_threaded()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WGC start fallo: %s", exc)
            return False

        if not wait:
            return True   # el frame/tamano llegaran de forma asincrona
        if not self._ready.wait(timeout=timeout) or self.w == 0:
            logger.warning("WGC no entrego fotograma en %.1fs", timeout)
            self.stop()
            return False
        return True

    @property
    def alive(self) -> bool:
        return not self._closed.is_set()

    def latest_bytes(self) -> bytes | None:
        with self._lock:
            return self._frame

    def latest_pil(self):
        """Ultimo fotograma como PIL RGB (o None)."""
        with self._lock:
            data, w, h = self._frame, self.w, self.h
        if not data or w == 0:
            return None
        from PIL import Image  # noqa: PLC0415
        return Image.frombuffer("RGB", (w, h), data, "raw", "BGRX", 0, 1)

    def stop(self) -> None:
        self._closed.set()
        ctrl = self._ctrl
        self._ctrl = None
        if ctrl is not None:
            try:
                ctrl.stop()
            except Exception:  # noqa: BLE001
                pass
        self._cap = None   # soltar el objeto WindowsCapture (recursos GPU) al GC


# ---------------------------------------------------------------------------
# Preview: sesion persistente con el ultimo fotograma
# ---------------------------------------------------------------------------
class WindowGrabber:
    """Sesion WGC persistente para el preview/recorte. .frame() -> PIL RGB."""

    def __init__(self, hwnd: int, cursor: bool = True):
        self._sess = _WgcSession(hwnd, cursor)
        self._started = False

    def start(self) -> bool:
        # no bloqueante: la UI del preview no debe congelarse esperando el 1er
        # frame; frame() devuelve None hasta que llegue.
        self._started = self._sess.start(wait=False)
        return self._started

    @property
    def alive(self) -> bool:
        return self._started and self._sess.alive

    @property
    def hwnd(self) -> int:
        return self._sess.hwnd

    @property
    def size(self) -> tuple[int, int]:
        return (self._sess.w, self._sess.h)

    def frame(self):
        return self._sess.latest_pil() if self._started else None

    def stop(self) -> None:
        self._sess.stop()


# ---------------------------------------------------------------------------
# Grabacion: sesion + named pipe hacia FFmpeg (rawvideo bgra)
# ---------------------------------------------------------------------------
class WindowPump:
    """Sirve los fotogramas WGC de una ventana por un named pipe para que FFmpeg
    los componga como una fuente mas. El escritor RECONECTA el pipe en cada
    segmento (el motor relanza FFmpeg al pausar/reanudar)."""

    def __init__(self, hwnd: int, fps: int, name: str, cursor: bool = True):
        self._sess = _WgcSession(hwnd, cursor)
        self.fps = max(1, int(fps))
        self.name = name
        self.pipe_path = rf"\\.\pipe\cs_wgc_{os.getpid()}_{next(_pipe_seq)}_{name}"
        self._pipe = None
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        # stop() puede llegar a la vez desde dos hilos (p.ej. en streaming: el
        # boton Parar y la ruta de error del supervisor). Sin este cerrojo, ambos
        # leerian el MISMO handle y harian CloseHandle dos veces: Windows podria
        # haber reasignado ese valor a otro pipe -> cerrariamos el ajeno.
        self._stop_lock = threading.Lock()
        self._stopped = False

    @property
    def size(self) -> tuple[int, int]:
        return (self._sess.w, self._sess.h)

    def start(self) -> bool:
        """Arranca WGC y el pipe. False si algo falla (el motor caera a gdigrab).
        Espera el 1er frame acotado a 2 s: la grabacion necesita el tamano, pero
        el path suele estar caliente por el preview; si excede, cae a gdigrab."""
        if not _PIPES_OK:
            return False
        if not self._sess.start(timeout=2.0, wait=True):
            return False
        w, h = self._sess.w, self._sess.h
        try:
            self._pipe = _k32.CreateNamedPipeW(
                self.pipe_path, _PIPE_ACCESS_OUTBOUND, _PIPE_TYPE_BYTE,
                1, w * h * 4, w * h * 4, 0, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CreateNamedPipe fallo: %s", exc)
            self._sess.stop()
            return False
        if self._pipe in (0, None, _INVALID):
            logger.warning("CreateNamedPipe devolvio handle invalido (err=%s)",
                           ctypes.get_last_error())
            self._pipe = None
            self._sess.stop()
            return False
        self._writer = threading.Thread(target=self._pump, name=f"wgc-{self.name}",
                                        daemon=True)
        self._writer.start()
        return True

    def ffmpeg_input(self) -> list[str]:
        w, h = self._sess.w, self._sess.h
        return ["-f", "rawvideo", "-pixel_format", "bgra", "-video_size", f"{w}x{h}",
                "-framerate", str(self.fps), "-thread_queue_size", "64", "-i", self.pipe_path]

    def _pump(self) -> None:
        interval = 1.0 / self.fps
        n = wintypes.DWORD(0)
        while not self._stop.is_set():
            # esperar a que un segmento de FFmpeg conecte como cliente
            try:
                ok_conn = _k32.ConnectNamedPipe(self._pipe, None)
            except Exception:  # noqa: BLE001
                break
            # ERROR_PIPE_CONNECTED (535) = un cliente ya estaba conectado: seguir.
            # Otro error real -> esperar antes de reintentar (no saturar un nucleo).
            if not ok_conn and ctypes.get_last_error() not in (0, 535):
                if self._stop.wait(0.1):
                    break
                continue
            if self._stop.is_set():
                break
            # escribir el ultimo fotograma a ritmo fijo hasta que el cliente cierre
            while not self._stop.is_set():
                data = self._sess.latest_bytes()
                if data:
                    try:
                        ok = _k32.WriteFile(self._pipe, data, len(data),
                                            ctypes.byref(n), None)
                    except Exception:  # noqa: BLE001
                        ok = 0
                    if not ok:
                        break                       # el segmento se desconecto
                self._stop.wait(interval)
            try:
                _k32.DisconnectNamedPipe(self._pipe)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return          # idempotente: dos hilos no deben cerrar el mismo handle
            self._stopped = True
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._stop.set()
        self._sess.stop()
        # Despertar el escritor si esta bloqueado en ConnectNamedPipe: cerrar el
        # handle NO cancela una operacion SINCRONA de pipe, asi que abrimos el
        # pipe como cliente un instante -> ConnectNamedPipe retorna, el escritor
        # ve _stop y sale. (Sin esto, el hilo quedaria colgado.)
        pipe = self._pipe
        if pipe not in (None, 0, _INVALID):
            try:
                hc = _k32.CreateFileW(self.pipe_path, _GENERIC_READ, 0, None,
                                      _OPEN_EXISTING, 0, None)
                if hc not in (None, 0, _INVALID):
                    _k32.CloseHandle(ctypes.c_void_p(hc))
            except Exception:  # noqa: BLE001
                pass
        w = self._writer
        alive_after = False
        if w is not None:
            w.join(timeout=3.0)
            alive_after = w.is_alive()
        self._pipe = None
        # Si el escritor NO termino (p.ej. FFmpeg dejo de drenar y WriteFile sigue
        # bloqueado: la autoconexion no lo despierta porque el pipe esta ocupado),
        # NO cerramos el handle: Windows podria reasignar ese valor a otro pipe y
        # el escritor zombie escribiria frames en un destino ajeno. Es un daemon:
        # morira al salir el proceso. Preferimos filtrar el handle a corromper.
        if pipe not in (None, 0, _INVALID) and not alive_after:
            try:
                _k32.CloseHandle(ctypes.c_void_p(pipe))
            except Exception:  # noqa: BLE001
                pass
        elif alive_after:
            logger.warning("wgc-%s: el escritor no termino en 3s; se filtra el "
                           "handle del pipe para no cerrarlo en uso.", self.name)


# ---------------------------------------------------------------------------
# Conjunto de pumps de una escena (compartido por grabacion/streaming/replay)
# ---------------------------------------------------------------------------
class WindowPumpSet:
    """Un WindowPump (WGC) por cada fuente de ventana visible de una escena.

    Ciclo de vida comun a los tres motores (RecordEngine, StreamEngine,
    ReplayBuffer): start() ANTES de lanzar FFmpeg, pasar `.inputs` como
    `window_pipes` a build_scene/build_record_command/build_stream_command, y
    stop() al terminar. Las ventanas cuyo pump falle -o si WGC no esta
    disponible- no entran en `.inputs` y caen a gdigrab de region en build_scene.
    Los pumps sobreviven a los relanzamientos de FFmpeg (segmentos de pausa,
    reconexiones de streaming): el escritor reconecta el named pipe."""

    def __init__(self) -> None:
        self._pumps: dict = {}
        self.inputs: dict = {}   # {src.id: [args de entrada FFmpeg]}

    @property
    def count(self) -> int:
        return len(self._pumps)

    def start(self, scene, fps: int, cursor: bool = True) -> None:
        self.stop()
        if not available():
            return
        from . import winlist                    # perezoso: sin acoplar imports
        from . import scene as scn
        for src in scene.visible_sorted():
            if src.kind != scn.KIND_WINDOW:
                continue
            hwnd = winlist.hwnd_for(src.params.get("title", ""))
            if not hwnd:
                continue
            pump = WindowPump(hwnd, fps, name=str(src.id), cursor=cursor)
            try:
                ok = pump.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WindowPump WGC fallo para «%s»: %s",
                               src.params.get("title", ""), exc)
                ok = False
            if ok:
                self._pumps[src.id] = pump
                self.inputs[src.id] = pump.ffmpeg_input()
            else:
                pump.stop()
        if self._pumps:
            logger.info("WGC activo para %d ventana(s).", len(self._pumps))

    def stop(self) -> None:
        pumps = list(self._pumps.values())
        self._pumps = {}
        self.inputs = {}
        for p in pumps:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass
