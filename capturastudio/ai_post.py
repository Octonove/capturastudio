"""Post-produccion con IA LOCAL (el foso frente a OBS):
  - Subtitulos automaticos con el filtro whisper de FFmpeg (-> SRT) y quemado.
  - Recorte automatico de silencios (auto-jumpcut) con silencedetect + trim/concat.
Todo offline y privado. Reutiliza el enfoque validado en TranscriptorIA.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from . import ffmpeg_utils as fu
from .config import work_dir

logger = logging.getLogger(__name__)


class AIError(Exception):
    pass


# get_duration unificado en octonove_core (via el shim fu): esa version anade el
# guard de fichero inexistente. Se conserva el nombre ai_post.get_duration porque
# chapters/content_factory/quality_check/autoframe lo llaman asi.
get_duration = fu.get_duration


# ---------------------------------------------------------------------------
# Subtitulos (Whisper)
# ---------------------------------------------------------------------------
def _safe_lang(language: str) -> str:
    """Lista blanca para 'language': codigo ISO (2-3 letras) o 'auto'. Cierra la
    inyeccion de opciones en el filtro whisper via ':'."""
    lang = (language or "auto").strip().lower()
    return lang if (lang == "auto" or re.match(r"^[a-z]{2,3}$", lang)) else "auto"


def transcribe_srt(ffmpeg_path: str, model_file: str, input_file: str,
                   language: str, out_srt: str, progress_cb=None, max_len: int = 0) -> str:
    """Genera un SRT con el filtro whisper. cwd=carpeta del modelo + nombres
    relativos (el parser de filtergraph no admite rutas absolutas de Windows).
    max_len: longitud maxima de segmento en caracteres (1 ~ una palabra/segmento,
    util para edicion a nivel de palabra estilo Descript)."""
    model_dir = str(Path(model_file).parent)
    model_rel = Path(model_file).name
    tmp_name = f".cs_tx_{os.getpid()}.srt"
    tmp_path = Path(model_dir) / tmp_name
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass

    total = get_duration(ffmpeg_path, input_file)
    lang = _safe_lang(language)
    ml = f":max_len={int(max_len)}" if max_len and max_len > 0 else ""
    filt = (f"aresample=16000,whisper=model={model_rel}:language={lang}"
            f":use_gpu=false:destination={tmp_name}:format=srt{ml}")
    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", "-i", input_file,
           "-af", filt, "-f", "null", "-"]
    try:
        proc = subprocess.Popen(cmd, cwd=model_dir, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, **fu.subprocess_kwargs())
    except OSError as exc:
        raise AIError(f"No se pudo iniciar FFmpeg: {exc}") from exc
    if proc.stdout is not None:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms=") and total > 0 and progress_cb:
                try:
                    sec = int(line.split("=", 1)[1]) / 1_000_000
                    progress_cb(min(0.99, sec / total))
                except ValueError:
                    pass
    proc.wait()
    if proc.returncode != 0 or not tmp_path.is_file():
        raise AIError("La transcripcion fallo.")
    Path(out_srt).parent.mkdir(parents=True, exist_ok=True)
    try:
        Path(out_srt).unlink(missing_ok=True)
    except OSError:
        pass
    os.replace(str(tmp_path), out_srt)
    if progress_cb:
        progress_cb(1.0)
    return Path(out_srt).read_text(encoding="utf-8", errors="replace")


def burn_subtitles(ffmpeg_path: str, video: str, srt: str, out_path: str,
                   encoder: str = "libx264", quality_key: str = "alta") -> None:
    """Quema el SRT en el video. cwd=carpeta del SRT + nombre relativo (mismo
    motivo que en whisper). Estilo legible (caja semitransparente)."""
    import shutil
    srt_dir = str(Path(srt).parent)
    # Copiamos el SRT a un nombre FIJO y seguro en el cwd: evita toda inyeccion
    # de opciones en el filtro 'subtitles' via ':' o '\\' en el nombre original.
    safe_name = f"_burn_{os.getpid()}.srt"
    safe_path = Path(srt_dir) / safe_name
    try:
        shutil.copyfile(srt, str(safe_path))
    except OSError as exc:
        raise AIError(f"No se pudo preparar el SRT: {exc}") from exc
    style = "FontName=Segoe UI,FontSize=22,PrimaryColour=&H00FFFFFF,BorderStyle=4,BackColour=&H803F3A1E,Outline=0,Shadow=0,MarginV=40"
    vf = f"subtitles={safe_name}:force_style='{style}'"
    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
           "-i", os.path.abspath(video), "-vf", vf,
           "-c:v", encoder] + fu.quality_args(encoder, quality_key) + [
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out_path]
    try:
        proc = subprocess.run(cmd, cwd=srt_dir, capture_output=True, timeout=1800,
                              **fu.subprocess_kwargs())
    finally:
        try:
            safe_path.unlink(missing_ok=True)
        except OSError:
            pass
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise AIError(fu._decode(proc.stderr)[-400:] or "No se pudieron quemar los subtitulos.")


# ---------------------------------------------------------------------------
# Recorte de silencios (auto-jumpcut)
# ---------------------------------------------------------------------------
def detect_speech_segments(ffmpeg_path: str, input_file: str, noise_db: int = -30,
                           min_silence: float = 0.6) -> list[tuple[float, float]]:
    """Devuelve los tramos CON voz (gaps entre silencios detectados)."""
    total = get_duration(ffmpeg_path, input_file)
    cmd = [ffmpeg_path, "-hide_banner", "-i", input_file,
           "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=600, **fu.subprocess_kwargs())
    text = fu._decode(proc.stderr)
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    # Construir intervalos de silencio y, de ahi, los de voz.
    silences: list[tuple[float, float]] = []
    for i, st in enumerate(starts):
        en = ends[i] if i < len(ends) else total
        silences.append((st, en))
    speech: list[tuple[float, float]] = []
    cur = 0.0
    for st, en in silences:
        if st - cur > 0.05:
            speech.append((cur, st))
        cur = en
    if total - cur > 0.05:
        speech.append((cur, total))
    return speech if speech else [(0.0, total)]


def cut_silences(ffmpeg_path: str, video: str, out_path: str, *, noise_db: int = -30,
                 min_silence: float = 0.6, padding: float = 0.10,
                 encoder: str = "libx264", quality_key: str = "alta") -> dict:
    """Elimina los silencios largos. Devuelve {orig, final, segmentos}."""
    total = get_duration(ffmpeg_path, video)
    speech = detect_speech_segments(ffmpeg_path, video, noise_db, min_silence)
    # padding + fusion de tramos casi contiguos
    segs: list[list[float]] = []
    for st, en in speech:
        st = max(0.0, st - padding)
        en = min(total, en + padding)
        if segs and st <= segs[-1][1] + 0.02:
            segs[-1][1] = max(segs[-1][1], en)
        else:
            segs.append([st, en])
    if not segs:
        raise AIError("No se detecto voz; no se recorta nada.")
    render_segments(ffmpeg_path, video, out_path, segs, encoder=encoder, quality_key=quality_key)
    return {"orig": total, "final": get_duration(ffmpeg_path, out_path),
            "segmentos": len(segs)}


def render_segments(ffmpeg_path: str, video: str, out_path: str, segs, *,
                    encoder: str = "libx264", quality_key: str = "alta") -> None:
    """Recorta el video a los intervalos `segs` ([ (start,end), ... ]) y los
    concatena en orden. Reusado por el recorte de silencios y el editor por texto."""
    segs = [(float(s), float(e)) for s, e in segs if e - s > 0.02]
    if not segs:
        raise AIError("No quedan tramos que conservar.")
    parts_v, parts_a, labels = [], [], ""
    for i, (st, en) in enumerate(segs):
        parts_v.append(f"[0:v]trim=start={st:.3f}:end={en:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts_a.append(f"[0:a]atrim=start={st:.3f}:end={en:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels += f"[v{i}][a{i}]"
    graph = ";".join(parts_v + parts_a) + f";{labels}concat=n={len(segs)}:v=1:a=1[v][a]"
    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
           "-i", video, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
           "-c:v", encoder] + fu.quality_args(encoder, quality_key) + [
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800, **fu.subprocess_kwargs())
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise AIError(fu._decode(proc.stderr)[-400:] or "No se pudo recortar.")
