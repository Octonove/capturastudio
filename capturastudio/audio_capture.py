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


def _com_init() -> bool:
    """Inicializa COM (MTA) en el HILO ACTUAL. soundcard/WASAPI exigen COM
    inicializado en el hilo que enumera o graba; sin ello las llamadas fallan con
    0x800401F0 (CO_E_NOTINITIALIZED). No basta con el init que hace soundcard al
    importarse: solo ocurre en el primer hilo que lo importa (p. ej. el efimero
    que puebla la lista de micros), que ya murio cuando se graba. Por eso cada
    hilo de trabajo debe inicializar COM por su cuenta (igual que meters.py y
    streaming.py). MTA (0x0) coincide con el modo de soundcard. Devuelve True si
    hay que llamar a _com_uninit() al terminar."""
    try:
        import ctypes
        RPC_E_CHANGED_MODE = 0x80010106  # el hilo ya estaba en otro apartamento
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0) & 0xFFFFFFFF
        return hr != RPC_E_CHANGED_MODE
    except (AttributeError, OSError):
        return False


def _com_uninit() -> None:
    try:
        import ctypes
        ctypes.windll.ole32.CoUninitialize()
    except (AttributeError, OSError):
        pass


def list_microphones() -> list[str]:
    """Nombres de microfonos. SOLO desde hilos de trabajo (carga soundcard)."""
    libs = _load()
    if libs is None:
        return []
    _, sc = libs
    co = _com_init()   # COM por-hilo: enumerar dispositivos WASAPI tambien lo exige
    try:
        return [m.name for m in sc.all_microphones(include_loopback=False)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron listar microfonos: %s", exc)
        return []
    finally:
        if co:
            _com_uninit()


def find_microphone(sc, name: str, on_fallback=None):
    """Microfono por nombre exacto; si no, por subcadena (WASAPI a veces varia
    el sufijo del nombre); en ultimo recurso el predeterminado. Si se cae al
    predeterminado se avisa via `on_fallback(mic)` ademas del warning: grabar
    con OTRO micro sin decirlo hace creer que el elegido 'no captaba nada'."""
    mics = sc.all_microphones(include_loopback=False)
    m = next((x for x in mics if x.name == name), None)
    if m is None and name:
        m = next((x for x in mics if name in x.name or x.name in name), None)
    if m is None:
        logger.warning("Microfono '%s' no encontrado; se usa el predeterminado.", name)
        m = sc.default_microphone()
        if on_fallback:
            on_fallback(m)
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
    hilo de cada pista, nunca en el hilo que construye/arranca esta clase.

    Si soundcard/WASAPI no puede abrir el microfono (algunos micros USB PnP
    reportan un formato de mezcla no EXTENSIBLE y soundcard aborta con un
    AssertionError vacio), se cae a grabar ese micro con FFmpeg/DirectShow."""

    def __init__(self, system: bool, mic_name: str | None, work_dir: str,
                 ffmpeg: str = ""):
        self.system = bool(system) and AVAILABLE
        # el micro tiene via de respaldo por FFmpeg: no exige soundcard
        self.mic_name = mic_name
        self.work_dir = work_dir
        self.ffmpeg = ffmpeg
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._tracks: list[_Track] = []
        self.problems: list[str] = []       # avisos legibles para la UI (p.ej. mic fallo)

    @property
    def enabled(self) -> bool:
        # el sistema exige soundcard; el micro funciona con soundcard O ffmpeg
        return self.system or bool(self.mic_name and (AVAILABLE or self.ffmpeg))

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
        return find_microphone(
            sc, self.mic_name or "",
            on_fallback=lambda m: self.problems.append(
                f"El microfono «{self.mic_name}» no aparecio; se grabo con "
                f"«{m.name}» (el predeterminado)."))

    def _run(self, track: _Track) -> None:
        libs = _load()
        if libs is None:
            # sin soundcard/numpy: el micro aun puede grabarse via FFmpeg
            if track.kind == "mic" and self.ffmpeg:
                self._intentar_dshow(track, None)
            return
        np, sc = libs
        co = _com_init()   # COM por-hilo: WASAPI lo EXIGE en el hilo que graba
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
            if track.kind == "mic" and self.ffmpeg and not self._stop.is_set():
                # FALLBACK: DirectShow via FFmpeg. Algunos micros USB (p.ej.
                # 'USB PnP Audio Device') no se pueden abrir con soundcard.
                self._intentar_dshow(track, exc)
            else:
                self._reportar_fallo(track, exc)
        finally:
            if rec is not None:
                try:
                    rec.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            if co:
                _com_uninit()   # tras liberar el recorder (mantiene objetos COM)

    def _reportar_fallo(self, track: _Track, exc: Exception | None) -> None:
        nombre = "el audio del sistema" if track.kind == "system" else \
            f"el microfono «{self.mic_name}»"
        # los AssertionError de soundcard llegan con mensaje VACIO: dar el tipo
        detalle = (str(exc).strip() or type(exc).__name__) if exc else "sin detalle"
        self.problems.append(f"No se pudo grabar {nombre}: {detalle}")

    def _intentar_dshow(self, track: _Track, exc_original: Exception | None) -> None:
        try:
            if self._grabar_dshow(track):
                track.ok = True
                logger.info("Micro «%s» grabado via FFmpeg/DirectShow (fallback).",
                            self.mic_name)
                return
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Fallback dshow fallo: %s", exc2)
        self._reportar_fallo(track, exc_original)

    # ------------------------------------------------ fallback FFmpeg/dshow
    def _listar_dshow_audio(self) -> list[str]:
        """Nombres de dispositivos de audio segun DirectShow (via FFmpeg)."""
        import re
        import subprocess
        from octonove_core.procutil import subprocess_kwargs
        try:
            p = subprocess.run([self.ffmpeg, "-hide_banner", "-list_devices", "true",
                                "-f", "dshow", "-i", "dummy"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10, **subprocess_kwargs())
        except (OSError, subprocess.SubprocessError):
            return []
        devs = []
        for ln in (p.stderr or "").splitlines():
            m = re.search(r'"([^"]+)"\s*\(audio', ln)
            if m:
                devs.append(m.group(1))
        return devs

    @staticmethod
    def _dshow_match(wanted: str, devs: list[str]) -> str | None:
        """El nombre dshow puede diferir un poco del WASAPI: exacto, subcadena
        o el mas parecido."""
        if not devs:
            return None
        if wanted in devs:
            return wanted
        for d in devs:
            if wanted and (wanted in d or d in wanted):
                return d
        import difflib
        close = difflib.get_close_matches(wanted or "", devs, n=1, cutoff=0.5)
        return close[0] if close else None

    def _grabar_dshow(self, track: _Track) -> bool:
        """Graba el micro con FFmpeg -f dshow hasta que paren la captura.
        La pausa se implementa por segmentos (parar/relanzar FFmpeg), igual que
        hace el video, y al final se unen los WAV."""
        import subprocess
        from octonove_core.procutil import subprocess_kwargs
        dev = self._dshow_match(self.mic_name or "", self._listar_dshow_audio())
        if not dev:
            logger.warning("dshow: no se encontro dispositivo para «%s»", self.mic_name)
            return False
        logger.info("dshow: usando dispositivo «%s»", dev)
        segs: list[str] = []
        proc = None

        def _arrancar():
            s = f"{track.wav_path}.seg{len(segs)}.wav"
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "dshow", "-i", f"audio={dev}",
                   "-ar", str(SAMPLERATE), "-ac", "2", "-c:a", "pcm_s16le", s]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **subprocess_kwargs())
            segs.append(s)
            return p

        def _parar(p) -> None:
            if p is None or p.poll() is not None:
                return
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()

        try:
            proc = _arrancar()
            # si FFmpeg muere nada mas arrancar, el dispositivo no vale
            if self._stop.wait(1.0):
                pass
            if proc.poll() is not None and not self._stop.is_set():
                return False
            while not self._stop.is_set():
                if self._paused.is_set():
                    if proc is not None:
                        _parar(proc)
                        proc = None
                else:
                    if proc is None:
                        proc = _arrancar()
                    elif proc.poll() is not None:
                        # el micro se cayo a mitad: conservar lo grabado
                        logger.warning("dshow: FFmpeg termino inesperadamente.")
                        break
                self._stop.wait(0.15)
            _parar(proc)
            proc = None
            return self._unir_wavs(segs, track.wav_path)
        finally:
            if proc is not None:
                _parar(proc)
            for s in segs:
                try:
                    Path(s).unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _unir_wavs(segs: list[str], out: str) -> bool:
        """Une segmentos WAV homogeneos en uno, por bloques (sin cargar en RAM)."""
        params = None
        with_data = []
        for s in segs:
            try:
                r = wave.open(s, "rb")
            except (OSError, wave.Error, EOFError):
                continue
            if r.getnframes() > 0:
                with_data.append((s, r.getparams()))
            r.close()
        if not with_data:
            return False
        params = with_data[0][1]
        try:
            w = wave.open(out, "wb")
        except (OSError, wave.Error):
            return False
        try:
            w.setparams(params)
            for s, _ in with_data:
                with wave.open(s, "rb") as r:
                    while True:
                        chunk = r.readframes(SAMPLERATE)  # ~1 s por bloque
                        if not chunk:
                            break
                        w.writeframes(chunk)
        finally:
            w.close()
        return Path(out).is_file() and Path(out).stat().st_size > 1024

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
