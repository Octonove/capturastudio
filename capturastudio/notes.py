"""Auto-apuntes: convierte una grabacion en un PDF de apuntes — un fotograma por
tema (capitulo) + la transcripcion de ese tramo. Genera el PDF con Pillow (ya
empaquetado), sin dependencias nuevas.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ffmpeg_utils as fu
from . import chapters as ch

logger = logging.getLogger(__name__)

PAGE_W, PAGE_H = 1240, 1754      # A4 a ~150 dpi
MARGIN = 80
NAVY = (30, 58, 95)
INK = (20, 20, 20)


class NotesError(Exception):
    pass


def _font(size: int):
    for name in ("segoeui.ttf", "arial.ttf", "calibri.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in text.split():
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _grab_frame(ffmpeg: str, video: str, t: float, out_png: str) -> bool:
    try:
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", str(max(0.0, t)), "-i", video, "-frames:v", "1",
                        "-q:v", "3", out_png], capture_output=True, timeout=60,
                       **fu.subprocess_kwargs())
    except (OSError, subprocess.SubprocessError):
        return False
    return Path(out_png).is_file()


def make_notes_pdf(ffmpeg: str, video: str, srt_text: str, out_pdf: str,
                   title: str = "") -> str:
    segs = ch.parse_srt(srt_text)
    if not segs:
        raise NotesError("La transcripcion esta vacia; no hay apuntes que generar.")
    chapters = ch.group_chapters(segs)
    tmp_dir = Path(out_pdf).parent
    pages: list[Image.Image] = []
    f_title, f_body = _font(40), _font(26)
    tmp_frames: list[str] = []
    try:
        for i, (t, ctitle) in enumerate(chapters):
            end = chapters[i + 1][0] if i + 1 < len(chapters) else 1e9
            body = " ".join(tx for (s, _e, tx) in segs if t <= s < end).strip()
            page = Image.new("RGB", (PAGE_W, PAGE_H), "white")
            d = ImageDraw.Draw(page)
            y = MARGIN
            if i == 0 and title:
                d.text((MARGIN, y), title, fill=NAVY, font=_font(48))
                y += 70
            d.text((MARGIN, y), f"{ch._fmt_hms(t)}  ·  {ctitle}", fill=NAVY, font=f_title)
            y += 64
            fp = str(tmp_dir / f".cs_note_{i}.jpg")
            tmp_frames.append(fp)   # registrar ANTES: el finally lo borra exista o no
            if _grab_frame(ffmpeg, video, t + 1.0, fp):
                try:
                    im = Image.open(fp).convert("RGB")
                    im.thumbnail((PAGE_W - 2 * MARGIN, 520))
                    page.paste(im, (MARGIN, y))
                    y += im.height + 30
                except OSError:
                    pass
            for ln in _wrap(d, body, f_body, PAGE_W - 2 * MARGIN):
                if y > PAGE_H - MARGIN:
                    break
                d.text((MARGIN, y), ln, fill=INK, font=f_body)
                y += 38
            pages.append(page)
        if not pages:
            raise NotesError("No se pudieron componer los apuntes.")
        pages[0].save(out_pdf, save_all=True, append_images=pages[1:], format="PDF")
    finally:
        for fp in tmp_frames:
            try:
                Path(fp).unlink(missing_ok=True)
            except OSError:
                pass
    if not Path(out_pdf).is_file():
        raise NotesError("No se pudo guardar el PDF de apuntes.")
    return out_pdf
