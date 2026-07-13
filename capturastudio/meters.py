"""Medidores VU en tiempo real (independientes de FFmpeg).

Un hilo ligero abre el loopback del sistema y/o el microfono con `soundcard` y
publica el nivel de pico (0..1 lineal) del ultimo bloque. La UI lo lee sin lock
(asignar/leer un float es atomico bajo el GIL) y aplica su propio suavizado.

Es un MONITOR opcional: abrir el microfono enciende su indicador en algunos
equipos, asi que se activa/desactiva a peticion del usuario, nunca solo.

soundcard se carga perezosamente via audio_capture._load() y SOLO en el hilo
del medidor (importarlo en el hilo de la UI inicializa COM/MTA y congela los
dialogos nativos de Tk). db_to_unit usa math, no numpy, por la misma razon:
lo llama el hilo de la UI en cada tick.
"""

from __future__ import annotations

import logging
import math
import threading

from .audio_capture import AVAILABLE, BLOCK as _CAP_BLOCK  # noqa: F401 (AVAILABLE re-export)
from . import audio_capture as _cap

logger = logging.getLogger(__name__)

SR = 48000
BLOCK = 1024  # ~21 ms: refresco fluido sin saturar la CPU


def db_to_unit(peak: float, floor_db: float = -60.0) -> float:
    """Mapea un pico lineal 0..1 a 0..1 perceptual usando dBFS (floor_db..0)."""
    if peak <= 1e-6:
        return 0.0
    db = 20.0 * math.log10(min(peak, 1.0))
    if db <= floor_db:
        return 0.0
    return float((db - floor_db) / (0.0 - floor_db))


class AudioMeter:
    """Publica `sys` y `mic` (pico 0..1 del ultimo bloque). best-effort."""

    def __init__(self, system: bool, mic_name: str | None, ffmpeg: str = ""):
        self.system = bool(system) and AVAILABLE
        # el micro tiene respaldo DirectShow: no exige soundcard
        self.mic_name = mic_name
        self.ffmpeg = ffmpeg
        self.sys = 0.0
        self.mic = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.system or bool(self.mic_name and (AVAILABLE or self.ffmpeg))

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self.sys = self.mic = 0.0

    def _open(self, sc):
        """Devuelve [(recorder_ctx, atributo)] abiertos. Cada uno es best-effort."""
        opened = []
        if self.system:
            try:
                spk = sc.default_speaker()
                lb = sc.get_microphone(id=str(spk.name), include_loopback=True)
                rec = lb.recorder(samplerate=SR, channels=2, blocksize=BLOCK)
                rec.__enter__()
                opened.append((rec, "sys"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("VU: loopback no disponible: %s", exc)
        if self.mic_name:
            try:
                mic = _cap.find_microphone(sc, self.mic_name)
                # open robusto: los micros USB mono rechazan configs exactas y sin
                # fallback el medidor se quedaba a cero (parecia que no captaba).
                rec, _, _ = _cap.open_recorder(mic, samplerates=(SR, 44100),
                                               blocksize=BLOCK)
                opened.append((rec, "mic"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("VU: microfono no disponible: %s", exc)
        return opened

    def _run(self) -> None:
        # COM por-hilo: soundcard/MediaFoundation lo requiere en este hilo.
        import ctypes
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x0)
        except (AttributeError, OSError):
            pass
        libs = _cap._load()
        if libs is None:
            return
        np, sc = libs
        opened = self._open(sc)
        # Si el micro se pidio pero soundcard no pudo abrirlo (tipico en micros
        # USB PnP), medir su nivel por FFmpeg/DirectShow en un hilo aparte, para
        # que el VU no se quede plano aunque el video SI se grabe por esa via.
        mic_por_sc = any(a == "mic" for _, a in opened)
        dshow_thread = None
        if self.mic_name and not mic_por_sc and self.ffmpeg:
            dshow_thread = threading.Thread(target=self._dshow_mic_vu, daemon=True)
            dshow_thread.start()
        if not opened and dshow_thread is None:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except (AttributeError, OSError):
                pass
            return
        try:
            while not self._stop.is_set():
                if not opened:
                    self._stop.wait(0.05)   # solo micro por dshow: no bloquear
                    continue
                for rec, attr in opened:
                    try:
                        data = rec.record(numframes=BLOCK)
                        peak = float(np.abs(data).max()) if data.size else 0.0
                    except Exception:  # noqa: BLE001
                        peak = 0.0
                    setattr(self, attr, peak)
        finally:
            for rec, _ in opened:
                try:
                    rec.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            if dshow_thread is not None:
                dshow_thread.join(timeout=3)
            try:
                ctypes.windll.ole32.CoUninitialize()
            except (AttributeError, OSError):
                pass

    def _dshow_mic_vu(self) -> None:
        """Publica self.mic leyendo PCM del micro por FFmpeg/DirectShow."""
        import numpy as np
        from octonove_core import dshow
        dev = dshow.match_device(self.mic_name or "", dshow.list_audio_devices(self.ffmpeg))
        if not dev:
            return
        ch = 2
        nbytes = BLOCK * ch * 2
        proc = dshow.open_pcm(self.ffmpeg, dev, SR, ch)
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read(nbytes)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                n = len(chunk) - (len(chunk) % 2)
                if n:
                    self.mic = float(np.abs(np.frombuffer(chunk[:n], dtype="<i2")).max()) / 32768.0
        except Exception as exc:  # noqa: BLE001
            logger.debug("VU dshow fallo: %s", exc)
        finally:
            self.mic = 0.0
            dshow.stop_pcm(proc)
