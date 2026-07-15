"""Localizacion de FFmpeg, deteccion de dispositivos/encoders y construccion del
filter_complex que compone una Escena (modelo declarativo) en el render.

El nucleo es build_scene(): recorre las fuentes por z-order y genera la lista de
entradas (-i ...) y el grafo de filtros (scale/crop/mascara/opacidad/overlay)
que FFmpeg materializa al grabar o emitir. Validado por el prototipo.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import VIDEO_QUALITY, work_dir
from . import scene as scn

logger = logging.getLogger(__name__)

# Bloque comun (subprocess sin consola + localizacion de FFmpeg + probes)
# unificado en octonove_core; aqui quedan solo las capacidades/escena propias.
from octonove_core.ffmpeg import (  # noqa: F401
    ffprobe_from,
    get_duration,
    has_whisper,
)
from octonove_core.ffmpeg import find_ffmpeg as _core_find_ffmpeg
from octonove_core.procutil import (  # noqa: F401
    CREATE_NO_WINDOW,
    _decode,
    subprocess_kwargs,
)

_subprocess_kwargs = subprocess_kwargs   # alias historico usado por este modulo


def find_ffmpeg(override: str = "") -> str | None:
    # package_file=__file__: en desarrollo busca ffmpeg.exe junto a ESTA app.
    return _core_find_ffmpeg(override, package_file=__file__)


# ---------------------------------------------------------------------------
# Capacidades / dispositivos
# ---------------------------------------------------------------------------
def list_encoders(ffmpeg_path: str) -> set[str]:
    wanted = {"libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_amf", "h264_qsv"}
    try:
        out = _decode(subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, timeout=20, **_subprocess_kwargs()).stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("No se pudieron listar encoders: %s", exc)
        return {"libx264"}
    return {e for e in wanted if re.search(rf"\b{e}\b", out)} | {"libx264"}


def _list_dshow(ffmpeg_path: str, kind: str) -> list[str]:
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=20, **_subprocess_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("No se pudieron listar dispositivos dshow: %s", exc)
        return []
    text = _decode(proc.stderr) + _decode(proc.stdout)
    out: list[str] = []
    for m in re.finditer(rf'"([^"]+)"\s*\(({kind})\)', text):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def list_video_devices(ffmpeg_path: str) -> list[str]:
    return _list_dshow(ffmpeg_path, "video")


def list_audio_devices(ffmpeg_path: str) -> list[str]:
    return _list_dshow(ffmpeg_path, "audio")


def best_default_encoder(available: set[str]) -> str:
    for enc in ("h264_nvenc", "h264_amf"):
        if enc in available:
            return enc
    return "libx264"


_encoder_open_cache: dict[str, bool] = {}


def encoder_opens(ffmpeg_path: str, encoder: str, timeout: int = 12) -> bool:
    """Comprueba que el encoder ABRE realmente (no basta con que aparezca en
    -encoders): codifica unos frames de prueba. Cachea el resultado por sesion.
    Evita perder la toma cuando NVENC/AMF/QSV estan listados pero fallan al abrir."""
    if encoder == "libx264":
        return True
    if encoder in _encoder_open_cache:
        return _encoder_open_cache[encoder]
    try:
        cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=black:s=256x144:r=10", "-frames:v", "6", "-c:v", encoder]
        cmd += quality_args(encoder, "media") + ["-pix_fmt", "yuv420p", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **_subprocess_kwargs())
        ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Probe de encoder %s fallo: %s", encoder, exc)
        ok = False
    _encoder_open_cache[encoder] = ok
    if not ok:
        logger.info("El encoder %s esta listado pero NO abre; se usara libx264.", encoder)
    return ok


def resolve_encoder(name: str, available: set[str], ffmpeg_path: str | None = None) -> str:
    """Resuelve el encoder a usar. Si se pasa ffmpeg_path, VERIFICA que abre y cae
    a libx264 si no (probe real). Sin ffmpeg_path mantiene el comportamiento previo."""
    if name == "auto" or name not in available:
        candidates = [e for e in ("h264_nvenc", "h264_amf", "h264_qsv") if e in available]
        candidates.append("libx264")
    else:
        candidates = [name, "libx264"]
    if ffmpeg_path:
        for enc in candidates:
            if encoder_opens(ffmpeg_path, enc):
                return enc
        return "libx264"
    return candidates[0]


def _even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def quality_args(encoder: str, quality_key: str) -> list[str]:
    q = VIDEO_QUALITY.get(quality_key, VIDEO_QUALITY["alta"])
    if encoder in ("libx264", "libx265"):
        return ["-preset", q["x264_preset"], "-crf", str(q["x264_crf"])]
    if encoder in ("h264_nvenc", "hevc_nvenc"):
        return ["-preset", q["nvenc_preset"], "-rc", "vbr", "-cq", str(q["nvenc_cq"]), "-b:v", "0"]
    if encoder == "h264_amf":
        cq = q["nvenc_cq"]
        return ["-quality", "quality", "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq)]
    if encoder == "h264_qsv":
        return ["-global_quality", str(q["nvenc_cq"]), "-preset", "veryfast"]
    return ["-crf", str(q["x264_crf"])]


# ---------------------------------------------------------------------------
# Assets generados (mascaras circulares, texto) con Pillow
# ---------------------------------------------------------------------------
def _hex_to_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = (color or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        r, g, b = 30, 58, 95
    return (r, g, b, alpha)


_HEX6 = re.compile(r"^#?[0-9A-Fa-f]{6}$")
_SAFE_COLOR_NAMES = {"black", "white", "red", "green", "blue", "navy", "gray",
                     "grey", "yellow", "orange", "cyan", "magenta", "transparent"}


def safe_color(color: str, default: str = "0x101418") -> str:
    """Color seguro para el filtergraph de FFmpeg. Acepta hex (#RRGGBB / 0xRRGGBB)
    o un nombre de la lista blanca; cualquier otra cosa (posible inyeccion desde
    una escena .json no confiable) cae al valor por defecto. Cierra el vector de
    inyeccion via 'color=c=...' / 'pad=...:color=...'."""
    c = (color or "").strip()
    if _HEX6.match(c):
        return "0x" + c.lstrip("#")
    if re.match(r"^0x[0-9A-Fa-f]{6}$", c):
        return c
    if c.lower() in _SAFE_COLOR_NAMES:
        return c.lower()
    return default


def circle_mask(w: int, h: int, tmp: Path) -> str:
    from PIL import Image, ImageDraw
    path = tmp / f"_mask_{w}x{h}.png"
    if not path.is_file():
        img = Image.new("L", (w, h), 0)
        ImageDraw.Draw(img).ellipse([2, 2, w - 2, h - 2], fill=255)
        img.save(path)
    return str(path)


def render_text_png(text: str, size: int, color: str, bg: str | None, tmp: Path,
                    name_hint: str = "") -> str:
    import hashlib
    from PIL import Image, ImageDraw, ImageFont
    # Cache por CONTENIDO: si no cambian texto/tamano/color/fondo, no re-renderiza
    # (antes el preview reescribia el PNG a disco ~6 veces/segundo).
    key = hashlib.md5(f"{text}|{size}|{color}|{bg}".encode("utf-8")).hexdigest()[:12]
    path = tmp / f"_text_{key}.png"
    if path.is_file():
        return str(path)
    font = None
    for fp in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
               "C:/Windows/Fonts/calibri.ttf"):
        try:
            font = ImageFont.truetype(fp, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = max(10, size // 3)
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if bg:
        d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=pad,
                            fill=_hex_to_rgba(bg, 220))
    d.text((pad - bbox[0], pad - bbox[1]), text, fill=_hex_to_rgba(color), font=font)
    img.save(path)
    return str(path)


# ---------------------------------------------------------------------------
# Construccion de la escena -> entradas + filter_complex
# ---------------------------------------------------------------------------
class _InputBag:
    def __init__(self) -> None:
        self.args: list[str] = []
        self.count = 0

    def add(self, group: list[str]) -> int:
        idx = self.count
        self.args.extend(group)
        self.count += 1
        return idx


def _source_input(src: scn.Source, fps: int, cursor: bool, tmp: Path) -> list[str]:
    t = src.transform
    if src.kind == scn.KIND_SCREEN:
        p = src.params
        return ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1" if cursor else "0",
                "-thread_queue_size", "1024", "-offset_x", str(p.get("left", 0)),
                "-offset_y", str(p.get("top", 0)),
                "-video_size", f"{_even(int(p.get('width', 1920)))}x{_even(int(p.get('height', 1080)))}",
                "-i", "desktop"]
    if src.kind == scn.KIND_WINDOW:
        # gdigrab 'title=' hace BitBlt del DC de la ventana: sale NEGRO con apps
        # aceleradas por GPU (Chrome, Edge, Electron...) y ademas con tamano DPI
        # erroneo. Se graba la REGION de pantalla del area cliente (framebuffer
        # de DWM): funciona con cualquier app. La region se resuelve al empezar.
        from . import winlist
        rect = winlist.window_rect(src.params.get("title", "")) or (0, 0, 1280, 720)
        x, y, w, h = rect
        return ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1" if cursor else "0",
                "-thread_queue_size", "1024", "-offset_x", str(x), "-offset_y", str(y),
                "-video_size", f"{_even(w)}x{_even(h)}", "-i", "desktop"]
    if src.kind == scn.KIND_WEBCAM:
        return ["-f", "dshow", "-rtbufsize", "256M", "-thread_queue_size", "1024",
                "-i", f"video={src.params.get('device', '')}"]
    if src.kind == scn.KIND_IMAGE:
        return ["-loop", "1", "-i", src.params.get("path", "")]
    if src.kind == scn.KIND_TEXT:
        png = render_text_png(src.params.get("text", ""), int(src.params.get("size", 48)),
                              src.params.get("color", "#FFFFFF"), src.params.get("bg"),
                              tmp, name_hint=str(src.id))
        return ["-loop", "1", "-i", png]
    if src.kind == scn.KIND_COLOR:
        w = t.w or 1920
        h = t.h or 1080
        col = safe_color(src.params.get("color", "#1E3A5F"), "0x1E3A5F")
        return ["-f", "lavfi", "-i", f"color=c={col}:s={_even(w)}x{_even(h)}:r={fps}"]
    if src.kind == scn.KIND_MEDIA:
        return ["-stream_loop", "-1", "-i", src.params.get("path", "")]
    return ["-f", "lavfi", "-i", f"color=c=black:s=320x180:r={fps}"]


def _crop_expr(crop: tuple[int, int, int, int] | None) -> str:
    """Filtro crop ACOTADO al tamano real de la fuente (con expresiones min()):
    un recorte mayor que la entrada (p. ej. la ventana se encogio, o desajuste
    mss/gdigrab) reventaba ffmpeg ('Invalid too big') y se perdia TODA la
    grabacion. Ahora se acota y nunca aborta. Devuelve 'crop=...,' o ''."""
    if not crop:
        return ""
    cx, cy, cwd, chd = (int(v) for v in crop)
    if cwd < 2 or chd < 2:
        return ""
    return (f"crop=w='min(iw,{cwd})':h='min(ih,{chd})':"
            f"x='min({max(0, cx)},iw-ow)':y='min({max(0, cy)},ih-oh)',")


def _base_chain(in_label: str, cw: int, ch: int, bg: str, out: str,
                crop: tuple[int, int, int, int] | None = None) -> str:
    # el recorte de la fuente tambien debe aplicarse a la capa base (una ventana
    # recortada como unica fuente): antes solo lo hacian las capas superiores.
    return (f"[{in_label}]{_crop_expr(crop)}scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1[{out}]")


def _layer_chain(src: scn.Source, in_label: str, idx: int, bag: _InputBag,
                 tmp: Path) -> tuple[str, str]:
    """Devuelve (cadena de filtros, etiqueta de salida) para una capa."""
    t = src.transform
    tw, th = int(t.w or 0), int(t.h or 0)
    pre = f"[{in_label}]"
    chain = pre
    # recorte de la fuente (acotado a la entrada real: no debe reventar ffmpeg)
    chain += _crop_expr(t.crop)
    # escalado al tamano de la capa
    if tw > 0 and th > 0:
        chain += f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}"
    elif tw > 0:
        chain += f"scale={tw}:-2"
    else:
        chain += "null"
    out = f"L{idx}"
    chroma = safe_color(t.chroma) if t.chroma else None
    if chroma:
        # Chroma key (quitar fondo de color, p.ej. pantalla verde) por fuente.
        chain += f",format=rgba,chromakey={chroma}:0.30:0.08"
    if t.shape == "circle" and tw > 0 and th > 0 and not chroma:
        mask = bag.add(["-loop", "1", "-i", circle_mask(tw, th, tmp)])
        chain += f",format=rgba[pre{idx}];[{mask}:v]format=gray,scale={tw}:{th}[mk{idx}];[pre{idx}][mk{idx}]alphamerge[{out}]"
    elif t.opacity < 1.0:
        chain += f",format=rgba,colorchannelmixer=aa={max(0.0, min(1.0, t.opacity)):.3f}[{out}]"
    else:
        chain += f"[{out}]"
    return chain, out


def build_scene(scene: scn.Scene, fps: int | None = None, cursor: bool = True,
                tmp: Path | None = None) -> tuple[list[str], str, str]:
    """Construye (input_args, filter_complex, '[vout]') para la escena."""
    fps = fps or scene.fps
    tmp = tmp or work_dir()
    cw, ch = _even(scene.canvas_w), _even(scene.canvas_h)
    bg = safe_color(scene.bg_color)
    ordered = scene.visible_sorted()
    bag = _InputBag()

    if not ordered:
        # lienzo vacio (color) para no fallar
        bag.add(["-f", "lavfi", "-i", f"color=c={bg}:s={cw}x{ch}:r={fps}"])
        return bag.args, f"[0:v]null[vout]", "[vout]"

    filters: list[str] = []
    # capa base = fuente mas baja, escalada+padded al lienzo
    base = ordered[0]
    bi = bag.add(_source_input(base, fps, cursor, tmp))
    filters.append(_base_chain(f"{bi}:v", cw, ch, bg, "base", base.transform.crop))
    cur = "base"

    for src in ordered[1:]:
        si = bag.add(_source_input(src, fps, cursor, tmp))
        chain, lbl = _layer_chain(src, f"{si}:v", si, bag, tmp)
        filters.append(chain)
        nxt = f"o{si}"
        filters.append(f"[{cur}][{lbl}]overlay={src.transform.x}:{src.transform.y}:format=auto[{nxt}]")
        cur = nxt

    filters.append(f"[{cur}]null[vout]")
    return bag.args, ";".join(filters), "[vout]"


# ---------------------------------------------------------------------------
# Comandos de grabacion / mux / concat
# ---------------------------------------------------------------------------
def build_record_command(*, ffmpeg_path: str, scene: scn.Scene, encoder: str,
                         quality_key: str, output_path: str, cursor: bool = True,
                         duration: int | None = None,
                         tmp: Path | None = None) -> list[str]:
    inputs, fc, vout = build_scene(scene, scene.fps, cursor, tmp)
    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning"]
    cmd += inputs
    cmd += ["-filter_complex", fc, "-map", vout]
    if duration:
        cmd += ["-t", str(duration)]
    is_hevc = encoder in ("libx265", "hevc_nvenc")
    cmd += ["-c:v", encoder] + quality_args(encoder, quality_key)
    # -g (keyframe cada 2 s): mejora seek/edicion y alinea con streaming/replay.
    cmd += ["-pix_fmt", "yuv420p", "-r", str(scene.fps), "-g", str(max(1, scene.fps * 2))]
    if is_hevc and output_path.lower().endswith(".mp4"):
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-movflags", "+faststart", output_path]
    return cmd


def build_mux_command(ffmpeg_path: str, video_path: str, audio_paths: list[str],
                      output_path: str, denoise: bool = False) -> list[str]:
    """Combina video (sin audio) + pistas WAV -> mp4 (video copiado, audio AAC)."""
    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-i", video_path]
    for w in audio_paths:
        cmd += ["-i", w]
    af = "highpass=f=80,afftdn=nr=12" if denoise else None
    if len(audio_paths) == 1:
        if af:
            cmd += ["-filter_complex", f"[1:a]{af}[a]", "-map", "0:v:0", "-map", "[a]"]
        else:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        chains = []
        labels = ""
        for i in range(len(audio_paths)):
            lab = f"a{i}"
            chains.append(f"[{i + 1}:a]{af}[{lab}]" if af else f"[{i + 1}:a]anull[{lab}]")
            labels += f"[{lab}]"
        chains.append(f"{labels}amix=inputs={len(audio_paths)}:duration=longest:normalize=0[a]")
        cmd += ["-filter_complex", ";".join(chains), "-map", "0:v:0", "-map", "[a]"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", output_path]
    return cmd


def build_concat_command(ffmpeg_path: str, list_file: str, output_path: str) -> list[str]:
    return [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c", "copy", "-movflags", "+faststart", output_path]
