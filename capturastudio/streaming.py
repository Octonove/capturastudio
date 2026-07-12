"""Streaming en vivo a RTMP/RTMPS (Twitch/YouTube/Facebook/Kick/custom).

El compositing de la escena lo hace FFmpeg (igual que en grabacion), pero el
audio debe ir EN VIVO en el mismo proceso: AudioPipe mezcla micro+sistema
(soundcard/WASAPI) y lo canaliza por stdin como s16le. Salida flv al ingest,
con VOD opcional en .mkv (resistente a cortes) via el muxer 'tee'.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import subprocess
from collections import deque
from pathlib import Path

from . import ffmpeg_utils as fu
from .config import work_dir

logger = logging.getLogger(__name__)

# Redaccion de la stream_key (ultimo segmento de una URL rtmp/rtmps) tambien en
# las lineas de FFmpeg que guardamos en memoria: nunca debe quedar la clave a la vista.
_KEY_RX = re.compile(r'(rtmps?://[^\s"\'/]+/[^\s"\'/]+/)[^\s"\']+', re.IGNORECASE)

SR = 48000
BLOCK = 1024

# Servicios con URL de ingest fija (el usuario solo pega su clave).
SERVICES: dict[str, str | None] = {
    "Twitch": "rtmp://live.twitch.tv/app/",
    "YouTube": "rtmp://a.rtmp.youtube.com/live2/",
    "Facebook": "rtmps://live-api-s.facebook.com:443/rtmp/",
    "Kick / Otro (URL completa)": None,   # el usuario pega la URL de ingest completa
    "Personalizado (URL completa)": None,
}


def ingest_url(service: str, url_or_key: str) -> str:
    base = SERVICES.get(service)
    if base:
        return base + url_or_key.strip()
    return url_or_key.strip()


def build_test_command(ffmpeg_path: str, ingest: str, duration: int = 2) -> list[str]:
    """Prueba de conexion: publica unos segundos de negro al ingest para confirmar
    que la URL/clave funcionan (emite brevemente en tu canal)."""
    return [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=black:s=128x72:r=15", "-t", str(duration),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-f", "flv", ingest]


def stream_video_args(encoder: str, bitrate_k: int, fps: int) -> list[str]:
    b = f"{bitrate_k}k"
    buf = f"{bitrate_k * 2}k"
    g = str(fps * 2)
    if encoder == "h264_nvenc":
        return ["-rc", "cbr", "-b:v", b, "-maxrate", b, "-bufsize", buf,
                "-preset", "p4", "-tune", "ll", "-g", g]
    if encoder == "h264_amf":
        return ["-rc", "cbr", "-b:v", b, "-maxrate", b, "-bufsize", buf,
                "-quality", "speed", "-g", g]
    if encoder == "h264_qsv":
        return ["-b:v", b, "-maxrate", b, "-bufsize", buf, "-preset", "veryfast", "-g", g]
    return ["-b:v", b, "-maxrate", b, "-bufsize", buf, "-preset", "veryfast",
            "-x264-params", f"nal-hrd=cbr:keyint={g}:min-keyint={g}", "-g", g]


def build_stream_command(*, ffmpeg_path: str, scene, encoder: str, bitrate_k: int,
                         has_audio: bool, ingest: str, vod_path: str | None = None,
                         cursor: bool = True, tmp: Path | None = None,
                         duration: int | None = None,
                         output_override: str | None = None,
                         extra_ingests: list[str] | None = None) -> list[str]:
    inputs, fc, vout = fu.build_scene(scene, scene.fps, cursor, tmp)
    audio_idx = inputs.count("-i")  # el pipe de audio es la entrada siguiente
    cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-stats"]
    cmd += inputs
    if has_audio:
        # use_wallclock + aresample async: corrige la deriva de lip-sync en
        # directos largos al canalizar PCM en vivo desde Python.
        cmd += ["-use_wallclock_as_timestamps", "1", "-f", "s16le", "-ar", str(SR),
                "-ac", "2", "-thread_queue_size", "1024", "-i", "pipe:0"]
    cmd += ["-filter_complex", fc, "-map", vout]
    if has_audio:
        cmd += ["-map", f"{audio_idx}:a"]
    cmd += ["-c:v", encoder] + stream_video_args(encoder, bitrate_k, scene.fps)
    cmd += ["-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += ["-af", "aresample=async=1000:first_pts=0", "-c:a", "aac", "-b:a", "160k", "-ar", str(SR)]
    if duration:
        cmd += ["-t", str(duration)]
    if output_override:
        fmt = "matroska" if output_override.lower().endswith(".mkv") else "mp4"
        cmd += ["-f", fmt, output_override]
    else:
        targets = [f"[f=flv]{ingest}"]
        for ex in (extra_ingests or []):
            if ex.strip():
                targets.append(f"[f=flv]{ex.strip()}")
        if vod_path:
            targets.append(f"[f=matroska]{vod_path}")
        if len(targets) == 1:
            cmd += ["-f", "flv", ingest]   # destino unico: flv directo
        else:
            cmd += ["-f", "tee", "|".join(targets)]  # multistream / VOD via tee
    return cmd


class AudioPipe:
    """Mezcla micro+sistema y escribe s16le 48k estereo en el stdin de FFmpeg."""

    def __init__(self, system: bool, mic_name: str | None):
        self.system = system
        self.mic_name = mic_name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stdin = None

    @property
    def enabled(self) -> bool:
        # find_spec no ejecuta el modulo: importar soundcard aqui (hilo de la UI)
        # inicializaria COM/MTA y congelaria los dialogos nativos de Tk.
        import importlib.util
        if importlib.util.find_spec("soundcard") is None:
            return False
        return self.system or bool(self.mic_name)

    def start(self, stdin) -> None:
        self._stdin = stdin
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _open_recorders(self, sc):
        recs = []
        if self.system:
            try:
                spk = sc.default_speaker()
                lb = sc.get_microphone(id=str(spk.name), include_loopback=True)
                r = lb.recorder(samplerate=SR, channels=2, blocksize=BLOCK)
                r.__enter__()
                recs.append(r)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Loopback no disponible para streaming: %s", exc)
        if self.mic_name:
            try:
                from .audio_capture import find_microphone, open_recorder
                mic = find_microphone(sc, self.mic_name)
                # open robusto (los micros USB mono rechazan 48k/2ch exactos). El
                # samplerate se fija a SR porque el pipe hacia FFmpeg es s16le/SR:
                # solo se negocian los canales; el mono se duplica al mezclar.
                r, _, _ = open_recorder(mic, samplerates=(SR,), blocksize=BLOCK)
                recs.append(r)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Microfono no disponible para streaming: %s", exc)
        return recs

    def _run(self) -> None:
        # MediaFoundation/soundcard requiere COM inicializado en ESTE hilo. Sin
        # esto, una 2a sesion de audio en el proceso falla con 0x800401f0.
        import ctypes
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # MULTITHREADED
        except (AttributeError, OSError):
            pass
        try:
            import numpy as np
            import soundcard as sc
        except Exception as exc:  # noqa: BLE001
            logger.warning("AudioPipe sin soundcard/numpy: %s", exc)
            return
        recs = self._open_recorders(sc)
        if not recs:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except (AttributeError, OSError):
                pass
            return
        def stereo(b):
            # El pipe declara a FFmpeg s16le ESTEREO: un buffer mono (micro USB
            # abierto con 1 canal) debe duplicarse o el audio saldria acelerado.
            if b.ndim == 1:
                b = b[:, None]
            if b.shape[1] == 1:
                b = np.repeat(b, 2, axis=1)
            return b[:, :2]

        try:
            while not self._stop.is_set():
                bufs = [stereo(r.record(numframes=BLOCK)) for r in recs]
                mix = bufs[0] if len(bufs) == 1 else np.clip(sum(bufs), -1.0, 1.0)
                pcm = (np.clip(mix, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                try:
                    self._stdin.write(pcm)
                except (BrokenPipeError, ValueError, OSError):
                    break  # FFmpeg cerro
        finally:
            for r in recs:
                try:
                    r.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            try:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()
            except (AttributeError, OSError):
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


class StreamEngine:
    """Estados: idle -> streaming -> stopped/error. Con reconexion basica."""

    MAX_RECONNECT = 3

    def __init__(self, *, ffmpeg_path, scene, encoder, bitrate_k, audio_system,
                 audio_mic_device, ingest, vod_path=None, cursor=True,
                 extra_ingests=None, on_state=None, on_error=None):
        self.ffmpeg = ffmpeg_path
        self.scene = scene
        self.encoder = encoder
        self.bitrate_k = bitrate_k
        self.audio_system = audio_system
        self.audio_mic = audio_mic_device or None
        self._has_audio = AudioPipe(self.audio_system, self.audio_mic).enabled
        self.ingest = ingest
        self.vod_path = vod_path
        self.extra_ingests = extra_ingests or []
        self.cursor = cursor
        self.on_state = on_state or (lambda *a: None)
        self.on_error = on_error or (lambda *a: None)
        self.state = "idle"
        self.dropped = 0
        self._proc: subprocess.Popen | None = None
        self._audio: AudioPipe | None = None
        self._stop = threading.Event()
        self._reconnects = 0
        # Ultimas lineas de FFmpeg (ya redactadas) para poder mostrar el MOTIVO real
        # de un fallo (clave invalida, conexion rechazada…) en vez de un mensaje generico.
        self._errbuf: deque[str] = deque(maxlen=40)

    def start(self) -> None:
        if self.state != "idle":
            return
        self.state = "streaming"
        threading.Thread(target=self._supervise, daemon=True).start()
        self.on_state("streaming", None)

    def _spawn(self) -> bool:
        has_audio = self._has_audio
        cmd = build_stream_command(
            ffmpeg_path=self.ffmpeg, scene=self.scene, encoder=self.encoder,
            bitrate_k=self.bitrate_k, has_audio=has_audio, ingest=self.ingest,
            vod_path=self.vod_path, cursor=self.cursor, tmp=work_dir(),
            extra_ingests=self.extra_ingests)
        logger.debug("Stream: %s", " ".join(cmd))  # nivel debug; el filtro de logging redacta la clave
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, **fu.subprocess_kwargs())
        except OSError as exc:
            self.on_error(f"No se pudo iniciar FFmpeg: {exc}")
            return False
        if has_audio:
            self._audio = AudioPipe(self.audio_system, self.audio_mic)
            self._audio.start(self._proc.stdin)
        threading.Thread(target=self._read_stats, args=(self._proc,), daemon=True).start()
        return True

    def _supervise(self) -> None:
        while not self._stop.is_set():
            if not self._spawn():
                self.state = "error"
                return
            self._proc.wait()
            if self._stop.is_set():
                return
            # muerte inesperada -> reconectar
            self._reconnects += 1
            if self._reconnects > self.MAX_RECONNECT:
                self.state = "error"
                self.on_error(self._tail() or "Se perdio la conexion con el servidor.")
                return
            self.on_state("reconnecting", self._reconnects)
            if self._audio:
                self._audio.stop()
            time.sleep(2)

    def _read_stats(self, proc) -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            line = fu._decode(raw).rstrip()
            if "drop=" in line:
                try:
                    self.dropped = int(line.split("drop=")[1].split()[0])
                except (ValueError, IndexError):
                    pass
            # Guardamos las lineas que NO son de progreso (frame=…): son las que
            # llevan el motivo real de un fallo. Redactadas por si aparece la URL.
            elif line.strip() and not line.startswith("frame="):
                self._errbuf.append(_KEY_RX.sub(r"\1***", line))

    def stop(self) -> None:
        self._stop.set()
        if self._audio:
            self._audio.stop()
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.state = "stopped"
        self.on_state("stopped", self.vod_path)

    def _tail(self, n: int = 6) -> str:
        """Ultimas lineas relevantes de FFmpeg (ya redactadas), para explicar el fallo."""
        return "\n".join(list(self._errbuf)[-n:])
