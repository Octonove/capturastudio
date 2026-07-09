"""Fabrica de contenido (V2): de UNA grabacion -> varios entregables.

- Master (el original) + vertical 9:16 auto-encuadrado (Reels/Shorts/TikTok)
- Audio-only MP3 (para podcast)
- GIF de un fragmento
- Subtitulos .srt (Whisper)
Todo local, en una cola tras pulsar 'generar'. El gran diferenciador frente a
OBS: una toma = paquete multiplataforma sin horas de edicion manual.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import ffmpeg_utils as fu
from . import ai_post

logger = logging.getLogger(__name__)


class FactoryError(Exception):
    pass


def _run(cmd: list[str], timeout: int = 1800) -> None:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **fu.subprocess_kwargs())
    if proc.returncode != 0:
        raise FactoryError(fu._decode(proc.stderr)[-400:] or "FFmpeg fallo.")


def to_vertical(ffmpeg: str, video: str, out_path: str, encoder: str = "libx264",
                quality_key: str = "alta", blurred_bg: bool = True) -> None:
    """Genera un vertical 9:16 (1080x1920). Con blurred_bg=True, el video va
    completo centrado sobre un fondo difuminado de si mismo (estilo Reels);
    si no, recorta el centro a 9:16."""
    if blurred_bg:
        vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
              "crop=1080:1920,boxblur=28:2[bg];"
              "[0:v]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
              "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
               "-filter_complex", vf, "-map", "[v]", "-map", "0:a?"]
    else:
        vf = ("crop='if(gt(iw/ih,9/16),ih*9/16,iw)':'if(gt(iw/ih,9/16),ih,iw*16/9)',"
              "scale=1080:1920,setsar=1")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
               "-vf", vf, "-map", "0:v", "-map", "0:a?"]
    cmd += ["-c:v", encoder] + fu.quality_args(encoder, quality_key)
    cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", out_path]
    _run(cmd)


def to_audio_mp3(ffmpeg: str, video: str, out_path: str) -> None:
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
          "-vn", "-c:a", "libmp3lame", "-q:a", "2", out_path])


def to_gif(ffmpeg: str, video: str, out_path: str, start: float = 0.0,
           dur: float = 4.0, width: int = 480, fps: int = 12) -> None:
    """GIF optimizado (2 pasadas con paleta) de un fragmento."""
    palette = str(Path(out_path).with_suffix(".pal.png"))
    vf = f"fps={fps},scale={width}:-1:flags=lanczos"
    try:
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start),
              "-t", str(dur), "-i", video, "-vf", f"{vf},palettegen=stats_mode=diff",
              palette], timeout=300)
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start),
              "-t", str(dur), "-i", video, "-i", palette,
              "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
              out_path], timeout=300)
    finally:
        try:
            Path(palette).unlink(missing_ok=True)
        except OSError:
            pass


def make_package(ffmpeg: str, video: str, out_dir: str, *, vertical: bool = True,
                 audio: bool = True, gif: bool = True, subtitles: bool = False,
                 model_file: str | None = None, encoder: str = "libx264",
                 quality_key: str = "alta", progress_cb=None) -> list[str]:
    """Genera el paquete de entregables y devuelve las rutas creadas."""
    src = Path(video)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    created: list[str] = []
    steps = sum([vertical, audio, gif, bool(subtitles and model_file)])
    done = 0

    def tick():
        nonlocal done
        done += 1
        if progress_cb and steps:
            progress_cb(done / steps)

    if audio:
        p = str(out / f"{stem}_audio.mp3")
        to_audio_mp3(ffmpeg, video, p)
        created.append(p)
        tick()
    if vertical:
        p = str(out / f"{stem}_vertical_9x16.mp4")
        to_vertical(ffmpeg, video, p, encoder, quality_key)
        created.append(p)
        tick()
    if gif:
        p = str(out / f"{stem}_clip.gif")
        total = ai_post.get_duration(ffmpeg, video)
        start = max(0.0, min(2.0, total / 4)) if total else 0.0
        to_gif(ffmpeg, video, p, start=start, dur=min(4.0, max(2.0, total)))
        created.append(p)
        tick()
    if subtitles and model_file:
        p = str(out / f"{stem}.srt")
        ai_post.transcribe_srt(ffmpeg, model_file, video, "es", p)
        created.append(p)
        tick()
    return created
