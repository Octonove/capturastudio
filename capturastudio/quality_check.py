"""Control de calidad: la app se audita sola tras grabar (algo que OBS no hace).

Pasa una bateria de probes de FFmpeg sobre la grabacion y avisa de problemas
tipicos del docente: no se te oye, micro saturado (clipping), casi sin voz, o
tramos de pantalla en negro. Ofrece un arreglo de un clic (normalizar el audio).
Todo local. Los parsers son puros y testeables.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg_utils as fu

logger = logging.getLogger(__name__)


class QualityError(Exception):
    pass


@dataclass
class Issue:
    level: str       # "alerta" | "aviso" | "ok"
    message: str
    fix: str | None = None   # "normalizar" | None


def parse_volumedetect(stderr: str) -> dict:
    """Extrae mean/max volume (dB) de la salida de volumedetect."""
    out = {}
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    if m:
        out["mean"] = float(m.group(1))
    m = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    if m:
        out["max"] = float(m.group(1))
    return out


def parse_blackdetect(stderr: str) -> list[tuple[float, float]]:
    spans = []
    for m in re.finditer(r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)", stderr):
        spans.append((float(m.group(1)), float(m.group(2))))
    return spans


def has_audio_stream(ffmpeg: str, video: str) -> bool:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", video],
                           capture_output=True, timeout=30, **fu.subprocess_kwargs())
        return "Audio:" in fu._decode(r.stderr)
    except (OSError, subprocess.SubprocessError):
        return False


def evaluate(vol: dict, blacks: list[tuple[float, float]], duration: float,
             has_audio: bool) -> list[Issue]:
    """Logica pura: de los probes a una lista de avisos accionables."""
    issues: list[Issue] = []
    if not has_audio:
        issues.append(Issue("alerta", "La grabacion no tiene pista de audio."))
    else:
        mx = vol.get("max")
        mn = vol.get("mean")
        if mx is None and mn is None:
            # hay audio pero no se pudo medir: NO declarar 'todo bien'
            issues.append(Issue("aviso", "No se pudo medir el volumen del audio; revisalo a mano."))
        else:
            if mx is not None and mx < -45:
                issues.append(Issue("alerta", "Apenas se oye nada (¿micro apagado o en silencio?)."))
            elif mn is not None and mn < -35:
                issues.append(Issue("aviso", "El audio suena muy bajo.", fix="normalizar"))
            if mx is not None and mx > -0.3:
                issues.append(Issue("aviso", "El audio satura (clipping); puede sonar distorsionado.",
                                    fix="normalizar"))
    black_total = sum(e - s for s, e in blacks)
    # umbral absoluto independiente del proporcional: si la duracion no se pudo
    # leer (0), seguimos detectando tramos largos en negro.
    if black_total > 2.0 or (duration > 0 and black_total > 0.15 * duration):
        issues.append(Issue("aviso", f"Hay ~{black_total:.0f}s de pantalla en negro."))
    if not issues:
        issues.append(Issue("ok", "Todo correcto: audio y vIdeo se ven bien."))
    return issues


def analyze(ffmpeg: str, video: str) -> list[Issue]:
    from . import ai_post
    duration = ai_post.get_duration(ffmpeg, video)
    has_audio = has_audio_stream(ffmpeg, video)
    vol = {}
    if has_audio:
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-i", video, "-af", "volumedetect",
                                "-f", "null", "-"], capture_output=True, timeout=600,
                               **fu.subprocess_kwargs())
            vol = parse_volumedetect(fu._decode(r.stderr))
        except (OSError, subprocess.SubprocessError):
            vol = {}
    blacks = []
    try:
        rb = subprocess.run([ffmpeg, "-hide_banner", "-i", video, "-vf",
                             "blackdetect=d=1:pic_th=0.98", "-an", "-f", "null", "-"],
                            capture_output=True, timeout=600, **fu.subprocess_kwargs())
        blacks = parse_blackdetect(fu._decode(rb.stderr))
    except (OSError, subprocess.SubprocessError):
        blacks = []
    return evaluate(vol, blacks, duration, has_audio)


def normalize_audio(ffmpeg: str, video: str, out_path: str, *, encoder: str = "libx264",
                    quality_key: str = "alta") -> None:
    """Normaliza el volumen (EBU R128 loudnorm). Intenta copiar el video; si su
    codec no es compatible con el contenedor .mp4, lo recodifica."""
    af = "loudnorm=I=-16:TP=-1.5:LRA=11"
    base = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video, "-af", af]
    tail = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    copy_cmd = base + ["-c:v", "copy"] + tail
    proc = subprocess.run(copy_cmd, capture_output=True, timeout=1800, **fu.subprocess_kwargs())
    if proc.returncode == 0 and Path(out_path).is_file():
        return
    # reintento recodificando el video (cubre fuentes mkv/webm con codec no-MP4)
    enc_cmd = base + ["-c:v", encoder] + fu.quality_args(encoder, quality_key) + \
        ["-pix_fmt", "yuv420p"] + tail
    proc = subprocess.run(enc_cmd, capture_output=True, timeout=1800, **fu.subprocess_kwargs())
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise QualityError(fu._decode(proc.stderr)[-300:] or "No se pudo normalizar el audio.")
