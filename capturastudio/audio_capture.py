"""Captura de audio del sistema (loopback WASAPI) y/o microfono a WAV.

Usa `soundcard` (loopback WASAPI nativo de Windows): funciona en cualquier
Windows 10/11 sin "Stereo Mix". Es BEST-EFFORT: cualquier fallo se registra y
NO interrumpe la grabacion de video.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLERATE = 48000
BLOCK = 2048

try:
    import numpy as np
    import soundcard as sc
    AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    AVAILABLE = False
    logger.warning("Captura de audio no disponible (soundcard/numpy): %s", exc)


def list_microphones() -> list[str]:
    if not AVAILABLE:
        return []
    try:
        return [m.name for m in sc.all_microphones(include_loopback=False)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron listar microfonos: %s", exc)
        return []


class _Track:
    def __init__(self, device, wav_path: str, kind: str):
        self.device = device
        self.wav_path = wav_path
        self.kind = kind
        self.channels = max(1, min(2, int(getattr(device, "channels", 2) or 2)))
        self.thread: threading.Thread | None = None
        self.ok = False


class AudioCapture:
    """Captura system/mic a WAV(s) en hilos; soporta pausa/reanudar."""

    def __init__(self, system: bool, mic_name: str | None, work_dir: str):
        self.system = bool(system) and AVAILABLE
        self.mic_name = mic_name if AVAILABLE else None
        self.work_dir = work_dir
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._tracks: list[_Track] = []

    @property
    def enabled(self) -> bool:
        return AVAILABLE and (self.system or bool(self.mic_name))

    def _resolve_tracks(self) -> list[_Track]:
        tracks: list[_Track] = []
        if self.system:
            try:
                spk = sc.default_speaker()
                lb = sc.get_microphone(id=str(spk.name), include_loopback=True)
                tracks.append(_Track(lb, str(Path(self.work_dir) / ".cs_sys.wav"), "system"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Loopback del sistema no disponible: %s", exc)
        if self.mic_name:
            try:
                mic = None
                for m in sc.all_microphones(include_loopback=False):
                    if m.name == self.mic_name:
                        mic = m
                        break
                if mic is None:
                    mic = sc.default_microphone()
                tracks.append(_Track(mic, str(Path(self.work_dir) / ".cs_mic.wav"), "mic"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Microfono no disponible: %s", exc)
        return tracks

    def start(self) -> None:
        if not self.enabled:
            return
        self._tracks = self._resolve_tracks()
        for t in self._tracks:
            t.thread = threading.Thread(target=self._run, args=(t,), daemon=True)
            t.thread.start()

    def _run(self, track: _Track) -> None:
        try:
            wf = wave.open(track.wav_path, "wb")
            wf.setnchannels(track.channels)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLERATE)
            try:
                with track.device.recorder(samplerate=SAMPLERATE,
                                           channels=track.channels, blocksize=BLOCK) as rec:
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
