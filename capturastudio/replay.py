"""Replay buffer / "Time Machine" (V2): mantiene en disco un buffer circular de
los ultimos N segundos compuestos (escena + audio) y permite GUARDAR ese tramo
en cualquier momento (Ctrl+Shift+M), aunque ya haya pasado.

Implementacion: FFmpeg escribe segmentos .ts rotatorios (segment muxer con
segment_wrap), con keyframes alineados al limite de segmento para cortes limpios.
Al 'guardar momento' se concatenan los segmentos recientes (sin recodificar).

Nota: el buffer es plano (escena ya compuesta), como el de OBS. La edicion
retroactiva multipista (capas separadas) queda como mejora futura.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import subprocess
from datetime import datetime
from pathlib import Path

from . import ffmpeg_utils as fu
from . import wincap
from .streaming import AudioPipe, stream_video_args
from .config import work_dir

logger = logging.getLogger(__name__)


class ReplayError(Exception):
    pass


class ReplayBuffer:
    def __init__(self, *, ffmpeg_path, scene, encoder, bitrate_k, audio_system,
                 audio_mic_device, out_dir, buffer_seconds=120, seg_seconds=4,
                 cursor=True, on_state=None, on_error=None):
        self.ffmpeg = ffmpeg_path
        self.scene = scene
        self.encoder = encoder
        self.bitrate_k = bitrate_k
        self.audio_system = audio_system
        self.audio_mic = audio_mic_device or None
        self.out_dir = Path(out_dir)
        self.seg = max(2, int(seg_seconds))
        self.buffer_seconds = max(self.seg * 3, int(buffer_seconds))
        self.cursor = cursor
        self.on_state = on_state or (lambda *a: None)
        self.on_error = on_error or (lambda *a: None)
        self.state = "idle"
        self._proc = None
        self._audio = None
        self._dir = work_dir() / "replay"
        self._wrap = math.ceil(self.buffer_seconds / self.seg) + 2
        # Captura WGC de ventanas (a prueba de oclusion), como en grabacion.
        self._pumpset = wincap.WindowPumpSet()

    def _build_cmd(self, has_audio: bool) -> list[str]:
        inputs, fc, vout = fu.build_scene(self.scene, self.scene.fps, self.cursor,
                                          work_dir(), self._pumpset.inputs)
        audio_idx = inputs.count("-i")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning"]
        cmd += inputs
        if has_audio:
            cmd += ["-use_wallclock_as_timestamps", "1", "-f", "s16le", "-ar", "48000",
                    "-ac", "2", "-thread_queue_size", "1024", "-i", "pipe:0"]
        cmd += ["-filter_complex", fc, "-map", vout]
        if has_audio:
            cmd += ["-map", f"{audio_idx}:a"]
        cmd += ["-c:v", self.encoder] + stream_video_args(self.encoder, self.bitrate_k, self.scene.fps)
        # keyframes alineados al limite de segmento -> cortes limpios al concatenar
        cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{self.seg})", "-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        pattern = str(self._dir / "seg_%04d.ts")
        cmd += ["-f", "segment", "-segment_time", str(self.seg), "-segment_wrap", str(self._wrap),
                "-segment_format", "mpegts", "-reset_timestamps", "1", pattern]
        return cmd

    def start(self) -> None:
        if self.state != "idle":
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        for f in self._dir.glob("seg_*.ts"):
            try:
                f.unlink()
            except OSError:
                pass
        has_audio = AudioPipe(self.audio_system, self.audio_mic).enabled
        self._pumpset.start(self.scene, self.scene.fps, self.cursor)
        try:
            self._proc = subprocess.Popen(
                self._build_cmd(has_audio), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **fu.subprocess_kwargs())
        except OSError as exc:
            self._pumpset.stop()
            self.on_error(f"No se pudo iniciar el buffer: {exc}")
            return
        if has_audio:
            self._audio = AudioPipe(self.audio_system, self.audio_mic)
            self._audio.start(self._proc.stdin)
        self.state = "buffering"
        threading.Thread(target=self._watch, daemon=True).start()
        self.on_state("buffering", None)

    def _watch(self) -> None:
        while self.state == "buffering":
            time.sleep(0.5)
            if self._proc and self._proc.poll() is not None:
                if self.state == "buffering":
                    self.state = "error"
                    self._pumpset.stop()
                    self.on_error("El buffer de replay se detuvo inesperadamente.")
                return

    def save_moment(self, seconds: int = 30) -> str:
        """Concatena los segmentos recientes que cubren los ultimos `seconds`."""
        if self.state != "buffering":
            raise ReplayError("El buffer no esta activo.")
        segs = sorted(self._dir.glob("seg_*.ts"), key=lambda p: p.stat().st_mtime)
        if len(segs) < 2:
            raise ReplayError("Aun no hay suficiente buffer; espera unos segundos.")
        # excluye el ultimo (en escritura) y toma los necesarios para cubrir 'seconds'
        usable = segs[:-1]
        n = min(len(usable), math.ceil(seconds / self.seg))
        chosen = usable[-n:]
        lst = self._dir / "_save.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in chosen), encoding="utf-8")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = self.out_dir / f"Replay_{ts}.mp4"
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
               "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)]
        proc = subprocess.run(cmd, capture_output=True, timeout=120, **fu.subprocess_kwargs())
        if proc.returncode != 0 or not out.is_file():
            raise ReplayError(fu._decode(proc.stderr)[-300:] or "No se pudo guardar el momento.")
        return str(out)

    def stop(self) -> None:
        self.state = "stopped"
        if self._audio:
            self._audio.stop()
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except OSError:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._pumpset.stop()
        for f in self._dir.glob("seg_*.ts"):
            try:
                f.unlink()
            except OSError:
                pass
        self.on_state("stopped", None)
