"""Orquestacion de la GRABACION compositada.

RecordEngine arranca un proceso FFmpeg que compone la escena (build_record_command)
hacia un segmento .mp4, mientras AudioCapture graba micro/sistema a WAV en paralelo.
Pausar cierra el segmento actual; reanudar abre otro. Al detener: concatena los
segmentos (sin recodificar) y mezcla el audio -> fichero final.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import ffmpeg_utils as fu
from .audio_capture import AudioCapture
from .config import work_dir

logger = logging.getLogger(__name__)


class EngineError(Exception):
    pass


class RecordEngine:
    """Estados: idle -> recording <-> paused -> (stop) -> saved/error."""

    def __init__(self, *, ffmpeg_path, scene, encoder, quality_key, container,
                 cursor, audio_system, audio_mic_device, denoise, out_dir,
                 on_state=None, on_error=None):
        self.ffmpeg = ffmpeg_path
        self.scene = scene
        self.encoder = encoder
        self.quality_key = quality_key
        self.container = container if container in ("mp4", "mkv") else "mp4"
        self.cursor = cursor
        self.denoise = denoise
        self.out_dir = Path(out_dir)
        self.on_state = on_state or (lambda *a: None)
        self.on_error = on_error or (lambda *a: None)

        self._work = work_dir()
        self._audio = AudioCapture(audio_system, audio_mic_device or None, str(self._work))
        self._segments: list[Path] = []
        self._proc: subprocess.Popen | None = None
        self._seg_log = self._work / ".cs_ffmpeg.log"
        self._lock = threading.Lock()
        self.state = "idle"
        self._watch: threading.Thread | None = None

    # -- ciclo de vida -----------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self.state != "idle":
                return
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._audio.start()
            try:
                self._start_segment()
                # Red de seguridad: si FFmpeg muere de inmediato con un encoder
                # hardware (NVENC/AMF/QSV que no abre), degradamos a libx264.
                if not self._segment_alive(0.9) and self.encoder != "libx264":
                    logger.warning("Encoder %s no arranco; reintento con libx264.", self.encoder)
                    self._proc = None
                    self._segments.clear()
                    self.encoder = "libx264"
                    self._start_segment()
            except EngineError:
                self._audio.stop()
                self.state = "idle"
                raise
            self.state = "recording"
        self._watch = threading.Thread(target=self._watchdog, daemon=True)
        self._watch.start()
        self.on_state("recording", None)

    def _segment_alive(self, wait: float) -> bool:
        proc = self._proc
        if proc is None:
            return False
        try:
            proc.wait(timeout=wait)
            return False   # termino dentro del tiempo -> no arranco bien
        except subprocess.TimeoutExpired:
            return True    # sigue vivo -> ok

    def pause(self) -> None:
        with self._lock:
            if self.state != "recording":
                return
            self._stop_segment()
            self._audio.pause()
            self.state = "paused"
        self.on_state("paused", None)

    def resume(self) -> None:
        with self._lock:
            if self.state != "paused":
                return
            self._audio.resume()
            self._start_segment()
            self.state = "recording"
        self.on_state("recording", None)

    def stop(self) -> None:
        with self._lock:
            if self.state not in ("recording", "paused"):
                return
            if self.state == "recording":
                self._stop_segment()
            audio_paths = self._audio.stop()
            self.state = "stopping"
        threading.Thread(target=self._finalize, args=(audio_paths,), daemon=True).start()

    # -- segmentos ---------------------------------------------------------
    def _start_segment(self) -> None:
        seg = self._work / f".cs_seg_{len(self._segments)}.mp4"
        try:
            seg.unlink(missing_ok=True)
        except OSError:
            pass
        cmd = fu.build_record_command(
            ffmpeg_path=self.ffmpeg, scene=self.scene, encoder=self.encoder,
            quality_key=self.quality_key, output_path=str(seg), cursor=self.cursor,
            tmp=self._work)
        logger.info("Segmento %d: %s", len(self._segments), " ".join(cmd))
        log = open(self._seg_log, "ab")
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=log, **fu.subprocess_kwargs())
        except OSError as exc:
            raise EngineError(f"No se pudo iniciar FFmpeg: {exc}") from exc
        finally:
            log.close()  # el hijo ya duplico el descriptor; cerramos el nuestro
        self._segments.append(seg)
        time.sleep(0.05)

    def _stop_segment(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(b"q")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass

    # -- watchdog: detecta muerte inesperada de FFmpeg ---------------------
    def _watchdog(self) -> None:
        while True:
            time.sleep(0.5)
            with self._lock:
                if self.state != "recording":
                    if self.state in ("stopping", "idle"):
                        return
                    continue
                proc = self._proc
            if proc is not None and proc.poll() is not None and proc.returncode not in (0, None):
                # FFmpeg murio mientras grababamos
                with self._lock:
                    if self.state != "recording":
                        continue
                    self.state = "error"
                self.on_error(self._tail_log() or "FFmpeg se detuvo inesperadamente.")
                return

    # -- finalizacion ------------------------------------------------------
    def _finalize(self, audio_paths: list[str]) -> None:
        try:
            valid = [s for s in self._segments if s.is_file() and s.stat().st_size > 4096]
            if not valid:
                raise EngineError(self._tail_log() or "No se grabo ningun video.")

            if len(valid) == 1:
                video = valid[0]
            else:
                list_file = self._work / ".cs_concat.txt"
                list_file.write_text(
                    "".join(f"file '{p.as_posix()}'\n" for p in valid), encoding="utf-8")
                video = self._work / ".cs_concat.mp4"
                self._run(fu.build_concat_command(self.ffmpeg, str(list_file), str(video)))

            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            ext = self.container
            final = self.out_dir / f"CapturaStudio_{ts}.{ext}"

            if audio_paths:
                self._run(fu.build_mux_command(self.ffmpeg, str(video), audio_paths,
                                               str(final), denoise=self.denoise))
            else:
                shutil.copyfile(str(video), str(final))

            if not final.is_file() or final.stat().st_size < 4096:
                raise EngineError("El archivo final no se genero correctamente.")
            self._cleanup(audio_paths)
            with self._lock:
                self.state = "saved"
            self.on_state("saved", str(final))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al finalizar la grabacion")
            with self._lock:
                self.state = "error"
            self.on_error(str(exc))

    def _run(self, cmd: list[str]) -> None:
        proc = subprocess.run(cmd, capture_output=True, timeout=600, **fu.subprocess_kwargs())
        if proc.returncode != 0:
            raise EngineError(fu._decode(proc.stderr)[-400:] or "FFmpeg fallo.")

    def _cleanup(self, audio_paths: list[str]) -> None:
        for p in self._segments + [self._work / ".cs_concat.mp4", self._work / ".cs_concat.txt"]:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        self._audio.cleanup()

    def _tail_log(self, n: int = 6) -> str:
        try:
            lines = self._seg_log.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join([ln for ln in lines if ln.strip()][-n:])
        except OSError:
            return ""
