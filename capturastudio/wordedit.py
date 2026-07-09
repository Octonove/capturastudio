"""Edicion por transcripcion (estilo Descript), 100% local: transcribe palabra a
palabra (Whisper con max_len=1), el usuario tacha palabras y el video se recorta
quitando esos tramos. Usa los tiempos por palabra del SRT y el recorte por
intervalos de ai_post (trim/concat).
"""

from __future__ import annotations

from . import ai_post, chapters


def words_from_srt(srt_text: str) -> list[tuple[float, float, str]]:
    """Lista de (inicio, fin, palabra) a partir del SRT a nivel de palabra."""
    return chapters.parse_srt(srt_text)


def kept_intervals(words, deleted, pad: float = 0.04) -> list[tuple[float, float]]:
    """Intervalos a CONSERVAR (fusiona palabras contiguas no borradas)."""
    deleted = set(deleted)
    segs: list[list[float]] = []
    for i, (s, e, _t) in enumerate(words):
        if i in deleted:
            continue
        s2 = max(0.0, float(s) - pad)
        e2 = float(e) + pad
        if segs and s2 <= segs[-1][1] + 0.06:
            segs[-1][1] = max(segs[-1][1], e2)
        else:
            segs.append([s2, e2])
    return [(s, e) for s, e in segs]


def apply_cut(ffmpeg: str, video: str, out_path: str, words, deleted, *,
              encoder: str = "libx264", quality_key: str = "alta") -> list:
    segs = kept_intervals(words, deleted)
    if not segs:
        raise ai_post.AIError("No queda nada que conservar.")
    ai_post.render_segments(ffmpeg, video, out_path, segs, encoder=encoder, quality_key=quality_key)
    return segs
