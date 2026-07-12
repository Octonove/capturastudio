"""Captura de audio del sistema (loopback WASAPI) y/o microfono a WAV.

Usa `soundcard` (loopback WASAPI nativo de Windows): funciona en cualquier
Windows 10/11 sin "Stereo Mix". Es BEST-EFFORT: cualquier fallo se registra y
NO interrumpe la grabacion de video.

REGLA COM (importante): al importarse, soundcard inicializa COM en modo MTA en
el hilo llamante. Si eso ocurre en el hilo de la UI, los dialogos nativos de Tk
(elegir carpeta/archivo, que necesitan STA) se CONGELAN. Por eso soundcard se
carga perezosamente con _load() y SOLO desde hilos de trabajo; el hilo de la UI
no debe llamar a list_microphones() ni a nada que toque soundcard.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLERATE = 48000
BLOCK = 2048

# Disponible = las dependencias existen (sin importarlas: find_spec no ejecuta
# el modulo y por tanto no inicializa COM). El import real ocurre en _load().
AVAILABLE = (importlib.util.find_spec("soundcard") is not None
             and importlib.util.find_spec("numpy") is not None)

_libs: tuple | None = None      # (numpy, soundcard) una vez cargados
_libs_failed = False
_libs_lock = threading.Lock()


def _load():
    """Importa numpy+soundcard (una vez). SOLO llamar desde hilos de trabajo:
    el primer import inicializa COM/MTA en el hilo llamante (ver cabecera)."""
    global _libs, _libs_failed
    if _libs is not None:
        return _libs
    if _libs_failed:
        return None
    with _libs_lock:
        if _libs is None and not _libs_failed:
            try:
                import numpy as np
                import soundcard as sc
                _libs = (np, sc)
            except Exception as exc:  # noqa: BLE001
                _libs_failed = True
                logger.warning("Captura de audio no disponible (soundcard/numpy): %s", exc)
    return _libs


def list_microphones() -> list[str]:
    """Nombres de microfonos. SOLO desde hilos de trabajo (carga soundcard)."""
    libs = _load()
    if libs is None:
        return []
    _, sc = libs
    try:
        return [m.name for m in sc.all_microphones(include_loopback=False)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron listar microfonos: %s", exc)
        return []


def find_microphone(sc, name: str):
    """Microfono por nombre exacto; si no, por subcadena (WASAPI a veces varia
    el sufijo del nombre); en ultimo recurso el predeterminado, con un warning
    claro (antes se caia al predeterminado EN SILENCIO y parecia que el micro
    elegido 'no captaba nada')."""
    mics = sc.all_microphones(include_loopback=False)
    m = next((x for x in mics if x.name == name), None)
    if m is None and name:
        m = next((x for x in mics if name in x.name or x.name in name), None)
    if m is None:
        logger.warning("Microfono '%s' no encontrado; se usa el predeterminado.", name)
        m = sc.default_microphone()
    return m


def open_recorder(device, prefer_channels: int | None = None,
                  samplerates: tuple = (SAMPLERATE, 44100), blocksize: int = BLOCK):
    """Abre device.recorder() probando combinaciones de samplerate/canales y
    devuelve (recorder_ya_abierto, samplerate, canales).

    Algunos microfonos (tipicamente USB mono) rechazan la configuracion exacta
    pedida (48 kHz estereo); sin este fallback la pista fallaba y se perdia en
    silencio: el usuario veia su micro en la lista pero no grababa nada."""
    prefer = int(prefer_channels or getattr(device, "channels", 2) or 2)
    prefer = max(1, min(2, prefer))
    last_exc: Exception | None = None
    for sr in samplerates:
        for ch in dict.fromkeys((prefer, 1, 2)):
            try:
                rec = device.recorder(samplerate=sr, channels=ch, blocksize=blocksize)
                rec.__enter__()
                if (sr, ch) != (samplerates[0], prefer):
                    logger.info("Recorder '%s' abierto con fallback %d Hz / %d canal(es).",
                                getattr(device, "name", "?"), sr, ch)
                return rec, sr, ch
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
    raise last_exc if last_exc else RuntimeError("No se pudo abrir el dispositivo.")


class _Track:
    def __init__(self, kind: str, wav_path: str):
        self.kind = kind                    # "system" | "mic"
        self.wav_path = wav_path
        self.thread: threading.Thread | None = None
        self.ok = False


class AudioCapture:
    """Captura system/mic a WAV(s) en hilos; soporta pausa/reanudar.

    Toda interaccion con soundcard (enumerar, abrir, grabar) ocurre DENTRO del
    hilo de cada pista, nunca en el hilo que construye/arranca esta clase."""

    def __init__(self, system: bool, mic_name: str | None, work_dir: str):
        self.system = bool(system) and AVAILABLE
        self.mic_name = mic_name if AVAILABLE else None
        self.work_dir = work_dir
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._tracks: list[_Track] = []
        self.problems: list[str] = []       # avisos legibles para la UI (p.ej. mic fallo)

    @property
    def enabled(self) -> bool:
        return AVAILABLE and (self.system or bool(self.mic_name))

    def start(self) -> None:
        if not self.enabled:
            return
        if self.system:
            self._tracks.append(_Track("system", str(Path(self.work_dir) / ".cs_sys.wav")))
        if self.mic_name:
            self._tracks.append(_Track("mic", str(Path(self.work_dir) / ".cs_mic.wav")))
        for t in self._tracks:
            t.thread = threading.Thread(target=self._run, args=(t,), daemon=True)
            t.thread.start()

    def _open_device(self, sc, kind: str):
        if kind == "system":
            spk = sc.default_speaker()
            return sc.get_microphone(id=str(spk.name), include_loopback=True)
        return find_microphone(sc, self.mic_name or "")

    def _run(self, track: _Track) -> None:
        libs = _load()
        if libs is None:
            return
        np, sc = libs
        rec = None
        try:
            device = self._open_device(sc, track.kind)
            rec, sr, ch = open_recorder(device)
            wf = wave.open(track.wav_path, "wb")
            wf.setnchannels(ch)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            try:
                while not self._stop.is_set():
                    data = rec.record(numframes=BLOCK)
                    if self._paused.is_set():
                        continue
                    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    wf.writeframes(pcm)
            finally:
                wf.close()
            track.ok = Path(track.wav_path).is_file() and Path(track.wav_path).stat().st_size > 1024
        except Exception as exc:  # noqa: BLE001
            logger.warning("Captura de audio (%s) fallo: %s", track.kind, exc)
            track.ok = False
            nombre = "el audio del sistema" if track.kind == "system" else \
                f"el microfono «{self.mic_name}»"
            self.problems.append(f"No se pudo grabar {nombre}: {exc}")
        finally:
            if rec is not None:
                try:
                    rec.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> list[str]:
        self._stop.set()
        for t in self._tracks:
            if t.thread:
                t.thread.join(timeout=5)
        return [t.wav_path for t in self._tracks if t.ok]

    def cleanup(self) -> None:
        for t in self._tracks:
            try:
                Path(t.wav_path).unlink(missing_ok=True)
            except OSError:
                pass
