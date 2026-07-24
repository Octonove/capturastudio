"""Editor de video con linea de tiempo (post-produccion offline).

Filosofia: NO es un NLE generico; es el editor minimo que convierte una grabacion
en un video publicable sin salir de la app. Reutiliza los motores ya probados:
  - texto  -> ffmpeg_utils.render_text_png (mismo look que las fuentes de escena)
  - blur   -> el patron crop+boxblur+overlay de privacy_shield (con su clamp)
  - cortes -> trim+concat como ai_post.render_segments
Los overlays viven en la linea de tiempo ORIGINAL del video (enable='between(t,s,e)')
y los cortes se aplican DESPUES en el mismo filtergraph: lo que ves en el lienzo al
segundo t es lo que sale, y un tramo cortado se lleva consigo su parte de overlay.

Todo se exporta en UNA pasada de FFmpeg a un archivo NUEVO (el original no se toca).
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, colorchooser
import tkinter as tk

from PIL import Image, ImageFilter, ImageTk

from . import APP_NAME
from . import ffmpeg_utils as fu
from . import ai_post, autoframe, privacy_shield, theme
from .config import work_dir
from .quality_check import has_audio_stream

logger = logging.getLogger(__name__)

MIN_DUR = 0.2          # duracion minima de un overlay/corte (s)
EDGE_PX = 6            # zona de agarre de los bordes de una barra del timeline
HANDLE_PX = 10         # zona de agarre de la esquina de redimension en el lienzo


class EditorError(Exception):
    pass


# ------------------------------------------------------------------ modelo
@dataclass
class Overlay:
    kind: str                       # "text" | "image" | "blur"
    x: int
    y: int
    w: int
    h: int
    start: float
    end: float
    params: dict = field(default_factory=dict)

    def label(self) -> str:
        if self.kind == "text":
            t = (self.params.get("text", "") or "texto").splitlines()[0]
            return "T  " + t[:22]
        if self.kind == "image":
            return "🖼 " + Path(self.params.get("path", "")).name[:22]
        if self.kind == "box":
            return "▮  cuadro " + self.params.get("color", "")
        return "▒  difuminado"


def _even(v: int) -> int:
    v = int(v)
    return v if v % 2 == 0 else v - 1


def _chroma_preview(img: "Image.Image", color: str, thr: int = 100) -> "Image.Image":
    """Aproximacion del chromakey para el LIENZO: hace transparentes los pixeles
    cercanos al color clave. El export usa el filtro chromakey real de FFmpeg."""
    try:
        r0, g0, b0 = (int(color.lstrip("#")[j:j + 2], 16) for j in (0, 2, 4))
    except (ValueError, IndexError):
        return img
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for yy in range(h):
        for xx in range(w):
            r, g, b, a = px[xx, yy]
            if abs(r - r0) + abs(g - g0) + abs(b - b0) < thr:
                px[xx, yy] = (r, g, b, 0)
    return img


def merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Ordena y fusiona rangos solapados/contiguos."""
    rs = sorted((min(s, e), max(s, e)) for s, e in ranges)
    out: list[tuple[float, float]] = []
    for s, e in rs:
        if out and s <= out[-1][1] + 0.01:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def kept_segments(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Complemento de los cortes dentro de [0, duration]: lo que se CONSERVA."""
    kept: list[tuple[float, float]] = []
    cur = 0.0
    for s, e in merge_ranges(cuts):
        s = max(0.0, min(s, duration))
        e = max(0.0, min(e, duration))
        if s - cur > 0.05:
            kept.append((cur, s))
        cur = max(cur, e)
    if duration - cur > 0.05:
        kept.append((cur, duration))
    return kept


# ------------------------------------------------------------------ export
def build_export_cmd(ffmpeg: str, video: str, vw: int, vh: int, duration: float,
                     overlays: list[Overlay], cuts: list[tuple[float, float]],
                     out_path: str, *, encoder: str = "libx264",
                     quality_key: str = "alta", with_audio: bool = True,
                     crossfade: float = 0.0, fade_inout: float = 0.0) -> list[str]:
    """Comando FFmpeg de UNA pasada. Los overlays de texto/cuadro deben traer ya su
    PNG renderizado en params['png']; los de imagen usan params['path'].
    crossfade: fundido cruzado (s) al unir los tramos conservados tras los cortes.
    fade_inout: fundido desde/hacia negro (s) al principio y final del resultado."""
    inputs: list[str] = ["-i", video]
    idx = 1
    parts: list[str] = []
    cur = "[0:v]"
    for k, ov in enumerate(overlays):
        s = max(0.0, float(ov.start))
        e = min(float(duration), float(ov.end))
        if e - s < 0.01:
            continue
        # enable solo si el overlay no cubre el video entero
        full = s <= 0.01 and e >= duration - 0.01
        enable = "" if full else f":enable='between(t,{s:.3f},{e:.3f})'"
        if ov.kind == "blur":
            reg = privacy_shield.clamp_region(ov.x, ov.y, ov.w, ov.h, vw, vh)
            if reg is None:
                continue
            x, y, w, h = reg
            x, y, w, h = _even(x), _even(y), max(2, _even(w)), max(2, _even(h))
            strength = int(ov.params.get("strength", 24))
            r = privacy_shield.safe_blur_radius(w, h, strength)
            if r >= 2:
                eff = f"boxblur={r}:2"
            else:   # region pequena: pixelado (mismo criterio que privacy_shield)
                eff = (f"scale={max(2, w // 10)}:{max(2, h // 10)}:flags=area,"
                       f"scale={w}:{h}:flags=neighbor")
            parts.append(f"{cur}split=2[e{k}a][e{k}b]")
            parts.append(f"[e{k}b]crop={w}:{h}:{x}:{y},{eff}[e{k}f]")
            parts.append(f"[e{k}a][e{k}f]overlay={x}:{y}{enable}[o{k}]")
        else:
            src = (ov.params.get("path") if ov.kind == "image"
                   else ov.params.get("png"))
            if not src or not Path(src).is_file():
                continue
            w = max(2, _even(min(ov.w, vw)))
            h = max(2, _even(min(ov.h, vh)))
            inputs += ["-loop", "1", "-i", str(src)]
            # croma opcional (imagenes con fondo verde): mismo filtro que la escena
            ck = fu.safe_color(ov.params.get("chroma")) if ov.params.get("chroma") else None
            chroma = f",chromakey={ck}:0.30:0.08" if ck else ""
            parts.append(f"[{idx}:v]format=rgba{chroma},scale={w}:{h}[L{k}]")
            # shortest=1: la imagen entra con -loop 1 (infinita); sin esto, un export
            # SIN cortes no termina nunca (el grafo sigue vivo tras acabar el video).
            parts.append(f"{cur}[L{k}]overlay={int(ov.x)}:{int(ov.y)}:shortest=1"
                         f"{enable}[o{k}]")
            idx += 1
        cur = f"[o{k}]"

    kept = kept_segments(cuts, duration)
    cutting = cuts and kept and kept != [(0.0, duration)]
    out_dur = duration
    if cutting:
        n = len(kept)
        lens = [e - s for s, e in kept]
        # fundido cruzado: acotado para que quepa en el tramo mas corto
        xf = min(float(crossfade), min(lens) / 2.5) if crossfade > 0 and n >= 2 else 0.0
        if xf < 0.1:
            xf = 0.0
        parts.append(cur + f"split={n}" + "".join(f"[sv{j}]" for j in range(n)))
        if with_audio:
            parts.append(f"[0:a]asplit={n}" + "".join(f"[sa{j}]" for j in range(n)))
        for j, (s, e) in enumerate(kept):
            parts.append(f"[sv{j}]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{j}]")
            if with_audio:
                parts.append(f"[sa{j}]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{j}]")
        if xf > 0.0:
            # cadena de xfade/acrossfade: cada union resta xf al total
            cur_v, acc = "[v0]", lens[0]
            cur_a = "[a0]"
            for j in range(1, n):
                parts.append(f"{cur_v}[v{j}]xfade=transition=fade:duration={xf:.3f}"
                             f":offset={acc - xf:.3f}[xv{j}]")
                cur_v = f"[xv{j}]"
                if with_audio:
                    parts.append(f"{cur_a}[a{j}]acrossfade=d={xf:.3f}[xa{j}]")
                    cur_a = f"[xa{j}]"
                acc += lens[j] - xf
            out_dur = acc
            vout, aout = cur_v, (cur_a if with_audio else None)
        else:
            lab = "".join(f"[v{j}]" + (f"[a{j}]" if with_audio else "") for j in range(n))
            parts.append(f"{lab}concat=n={n}:v=1:a={1 if with_audio else 0}[vcat]"
                         + ("[acat]" if with_audio else ""))
            out_dur = sum(lens)
            vout, aout = "[vcat]", ("[acat]" if with_audio else None)
    else:
        vout, aout = cur, ("0:a" if with_audio else None)

    fi = min(float(fade_inout), max(0.0, out_dur / 3))
    if fi >= 0.1:
        st = max(0.0, out_dur - fi)
        parts.append(f"{vout if vout.startswith('[') else '[0:v]'}"
                     f"fade=t=in:st=0:d={fi:.3f},fade=t=out:st={st:.3f}:d={fi:.3f}[vfad]")
        vout = "[vfad]"
        if with_audio:
            src_a = aout if aout and aout.startswith("[") else "[0:a]"
            parts.append(f"{src_a}afade=t=in:st=0:d={fi:.3f},"
                         f"afade=t=out:st={st:.3f}:d={fi:.3f}[afad]")
            aout = "[afad]"

    if parts:
        maps = ["-map", vout if vout.startswith("[") else "0:v"]
        if with_audio:
            maps += ["-map", aout if aout else "0:a"]
    else:   # sin overlays, cortes ni fundidos: re-encode directo
        maps = ["-map", "0:v"] + (["-map", "0:a"] if with_audio else [])

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + inputs
    if parts:
        cmd += ["-filter_complex", ";".join(parts)]
    cmd += maps + ["-c:v", encoder] + fu.quality_args(encoder, quality_key) + \
        ["-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", out_path]
    return cmd


def run_export(cmd: list[str], out_path: str, timeout: int = 3600) -> None:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                          **fu.subprocess_kwargs())
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise EditorError(fu._decode(proc.stderr)[-400:] or "No se pudo exportar.")


# ------------------------------------------------------------------ frames
class FrameCache:
    """Extrae fotogramas del video con FFmpeg (a memoria, sin temporales) y los
    cachea por cubos de 0.25 s. La extraccion corre en un hilo unico."""

    def __init__(self, ffmpeg: str, video: str):
        self.ffmpeg = ffmpeg
        self.video = video
        self._cache: dict[int, Image.Image] = {}
        self._lock = threading.Lock()
        self._busy = False
        self._pending: tuple[float, object] | None = None

    @staticmethod
    def _key(t: float) -> int:
        return int(round(t * 4))

    def get(self, t: float) -> Image.Image | None:
        return self._cache.get(self._key(t))

    def request(self, t: float, cb) -> None:
        """Pide el fotograma en t; cb(img) se llama en el hilo extractor."""
        with self._lock:
            if self._busy:
                self._pending = (t, cb)
                return
            self._busy = True
        threading.Thread(target=self._work, args=(t, cb), daemon=True).start()

    def _work(self, t: float, cb) -> None:
        while True:
            img = self._extract(t)
            if img is not None:
                self._cache[self._key(t)] = img
                if len(self._cache) > 240:   # ~1 min de scrub fino: purga simple
                    self._cache.pop(next(iter(self._cache)))
            try:
                cb(img)
            except Exception:   # noqa: BLE001
                pass
            with self._lock:
                if self._pending is None:
                    self._busy = False
                    return
                t, cb = self._pending
                self._pending = None

    def _extract(self, t: float) -> Image.Image | None:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", f"{max(0.0, t):.3f}", "-i", self.video,
               "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30,
                                  **fu.subprocess_kwargs())
            if proc.returncode != 0 or not proc.stdout:
                return None
            return Image.open(BytesIO(proc.stdout)).convert("RGB")
        except (OSError, subprocess.SubprocessError, Exception):  # noqa: BLE001
            return None


# ------------------------------------------------------------------ ventana
class EditorWindow(tk.Toplevel):
    """Editor: lienzo (arrastrar/redimensionar overlays) + linea de tiempo
    (mover/estirar overlays y cortes en el tiempo) + export en una pasada."""

    def __init__(self, app, video: str):
        super().__init__(app)
        self.app = app
        self.video = video
        self.ffmpeg = app.ffmpeg
        try:
            self.vw, self.vh = autoframe.video_dims(self.ffmpeg, video)
            self.duration = ai_post.get_duration(self.ffmpeg, video)
        except Exception as exc:  # noqa: BLE001
            raise EditorError(f"No se pudo leer el video: {exc}") from exc
        if self.vw <= 0 or self.vh <= 0 or self.duration <= 0:
            raise EditorError("No se pudo leer el video (dimensiones o duracion).")
        self.with_audio = has_audio_stream(self.ffmpeg, video)

        self.overlays: list[Overlay] = []
        self.cuts: list[tuple[float, float]] = []
        self.sel: tuple[str, int] | None = None     # ("ov"|"cut", indice)
        self.t = 0.0
        self._drag = None
        self._tl_drag = None
        self._scrub_id = None
        self._photo = None
        self._ov_imgs: dict[int, Image.Image] = {}  # cache de PNG/imagen por overlay

        self.title(f"Editor — {Path(video).name}")
        self.configure(bg=theme.BG)
        self.geometry("1080x720")
        self.minsize(860, 560)
        self.frames = FrameCache(self.ffmpeg, video)
        self._build()
        self.bind("<Delete>", lambda _e: self._delete_sel())
        self._goto(0.0)

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="＋ Texto", command=self._add_text).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="＋ Imagen", command=self._add_image).pack(side="left", padx=6)
        ttk.Button(bar, text="＋ Cuadro", command=self._add_box).pack(side="left", padx=6)
        ttk.Button(bar, text="＋ Difuminado", command=self._add_blur).pack(side="left", padx=6)
        ttk.Button(bar, text="✂ Cortar tramo", command=self._add_cut).pack(side="left", padx=6)
        ttk.Button(bar, text="Croma verde", command=self._toggle_chroma).pack(side="left", padx=6)
        ttk.Button(bar, text="🗑 Eliminar", command=self._delete_sel).pack(side="left", padx=6)
        self.lbl_info = ttk.Label(bar, text="", style="Muted.TLabel")
        self.lbl_info.pack(side="left", padx=12)
        ttk.Button(bar, text="Exportar video…", style="Primary.TButton",
                   command=self._export).pack(side="right")

        self.cv = tk.Canvas(self, bg="#0B1118", highlightthickness=0)
        self.cv.pack(fill="both", expand=True, padx=10)
        self.cv.bind("<ButtonPress-1>", self._cv_press)
        self.cv.bind("<B1-Motion>", self._cv_drag)
        self.cv.bind("<ButtonRelease-1>", self._cv_release)
        self.cv.bind("<Configure>", lambda _e: self._refresh_canvas())

        srow = ttk.Frame(self, padding=(10, 4))
        srow.pack(fill="x")
        self.var_t = tk.DoubleVar(value=0.0)
        self.scale = ttk.Scale(srow, from_=0.0, to=self.duration, variable=self.var_t,
                               command=self._on_scrub)
        self.scale.pack(side="left", fill="x", expand=True)
        self.lbl_time = ttk.Label(srow, text="0:00.0", width=18)
        self.lbl_time.pack(side="left", padx=(8, 0))
        # transiciones: se aplican al EXPORTAR (el lienzo no simula fundidos)
        self.var_fade = tk.BooleanVar(value=False)
        ttk.Checkbutton(srow, text="Fundido inicio/fin",
                        variable=self.var_fade).pack(side="left", padx=(14, 4))
        self.var_xfade = tk.BooleanVar(value=False)
        ttk.Checkbutton(srow, text="Fundido en los cortes",
                        variable=self.var_xfade).pack(side="left", padx=4)

        self.tl = tk.Canvas(self, bg=theme.SURFACE, highlightthickness=0, height=120)
        self.tl.pack(fill="x", padx=10, pady=(0, 4))
        self.tl.bind("<ButtonPress-1>", self._tl_press)
        self.tl.bind("<B1-Motion>", self._tl_motion)
        self.tl.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_tl_drag", None))
        self.tl.bind("<Configure>", lambda _e: self._refresh_tl())

        self.status = ttk.Label(self, text=self._status_base(), style="Muted.TLabel",
                                padding=(12, 4))
        self.status.pack(fill="x")

    def _status_base(self) -> str:
        au = "con audio" if self.with_audio else "SIN audio"
        return (f"{self.vw}x{self.vh} · {self._fmt(self.duration)} · {au} — arrastra los "
                "elementos en el lienzo; en la linea de tiempo muevelos o estira sus bordes.")

    @staticmethod
    def _fmt(t: float) -> str:
        m, s = int(t) // 60, t % 60
        return f"{m}:{s:04.1f}"

    # ------------------------------------------------------------- scrub
    def _on_scrub(self, _v=None) -> None:
        if self._scrub_id is not None:
            try:
                self.after_cancel(self._scrub_id)
            except (tk.TclError, ValueError):
                pass
        self._scrub_id = self.after(90, lambda: self._goto(self.var_t.get()))

    def _goto(self, t: float) -> None:
        self.t = max(0.0, min(float(t), self.duration))
        self.lbl_time.config(text=f"{self._fmt(self.t)} / {self._fmt(self.duration)}")
        self._refresh_canvas()
        self._refresh_tl()
        if self.frames.get(self.t) is None:
            self.frames.request(self.t, lambda img: self.after(0, self._refresh_canvas))

    # ------------------------------------------------------- lienzo (video)
    def _pv_rect(self) -> tuple[float, float, float]:
        cw = max(2, self.cv.winfo_width())
        ch = max(2, self.cv.winfo_height())
        sc = min(cw / self.vw, ch / self.vh)
        return ((cw - self.vw * sc) / 2, (ch - self.vh * sc) / 2, sc)

    def _active(self, ov: Overlay) -> bool:
        return ov.start - 0.001 <= self.t <= ov.end + 0.001

    def _ov_image(self, i: int, ov: Overlay) -> Image.Image | None:
        """Imagen fuente (RGBA) del overlay i, cacheada."""
        if i in self._ov_imgs:
            return self._ov_imgs[i]
        img = None
        try:
            if ov.kind == "text":
                png = fu.render_text_png(
                    ov.params.get("text", ""), int(ov.params.get("size", 48)),
                    ov.params.get("color", "#FFFFFF"), ov.params.get("bg"),
                    work_dir(), bg_alpha=int(ov.params.get("bg_alpha", 86)))
                ov.params["png"] = png
                img = Image.open(png).convert("RGBA")
            elif ov.kind == "image":
                img = Image.open(ov.params["path"]).convert("RGBA")
                if ov.params.get("chroma"):
                    img = _chroma_preview(img, ov.params["chroma"])
            elif ov.kind == "box":
                # PNG solido cacheado por color+opacidad (el export lo escala)
                color = ov.params.get("color", "#1E3A5F")
                alpha = max(5, min(100, int(ov.params.get("alpha", 100))))
                r, g, b = (int(color.lstrip("#")[j:j + 2], 16) for j in (0, 2, 4))
                png = work_dir() / f"_box_{color.lstrip('#')}_{alpha}.png"
                if not png.is_file():
                    Image.new("RGBA", (64, 64),
                              (r, g, b, int(alpha * 255 / 100))).save(png)
                ov.params["png"] = str(png)
                img = Image.open(png).convert("RGBA")
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("No se pudo cargar el overlay: %s", exc)
        self._ov_imgs[i] = img
        return img

    def _refresh_canvas(self) -> None:
        ox, oy, sc = self._pv_rect()
        frame = self.frames.get(self.t)
        base = (frame.copy() if frame is not None
                else Image.new("RGB", (self.vw, self.vh), "#101820"))
        # compone a resolucion del video (misma geometria que el export)
        for i, ov in enumerate(self.overlays):
            if not self._active(ov):
                continue
            if ov.kind == "blur":
                reg = privacy_shield.clamp_region(ov.x, ov.y, ov.w, ov.h, self.vw, self.vh)
                if reg:
                    x, y, w, h = reg
                    crop = base.crop((x, y, x + w, y + h))
                    rad = max(2, privacy_shield.safe_blur_radius(
                        w, h, int(ov.params.get("strength", 24))))
                    base.paste(crop.filter(ImageFilter.BoxBlur(rad)), (x, y))
            else:
                src = self._ov_image(i, ov)
                if src is not None and ov.w > 1 and ov.h > 1:
                    layer = src.resize((max(1, ov.w), max(1, ov.h)), Image.LANCZOS)
                    base.paste(layer, (int(ov.x), int(ov.y)), layer)
        disp = base.resize((max(1, int(self.vw * sc)), max(1, int(self.vh * sc))),
                           Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(disp)
        self.cv.delete("all")
        self.cv.create_image(ox, oy, anchor="nw", image=self._photo)
        # marco de seleccion + asa (en coords canvas)
        if self.sel and self.sel[0] == "ov" and self.sel[1] < len(self.overlays):
            ov = self.overlays[self.sel[1]]
            x0, y0 = ox + ov.x * sc, oy + ov.y * sc
            x1, y1 = x0 + ov.w * sc, y0 + ov.h * sc
            dash = None if self._active(ov) else (4, 3)
            self.cv.create_rectangle(x0, y0, x1, y1, outline=theme.PRIMARY,
                                     width=2, dash=dash)
            self.cv.create_rectangle(x1 - HANDLE_PX, y1 - HANDLE_PX, x1, y1,
                                     fill=theme.PRIMARY, outline="")

    def _cv_press(self, ev) -> None:
        ox, oy, sc = self._pv_rect()
        vx, vy = (ev.x - ox) / sc, (ev.y - oy) / sc
        for i in range(len(self.overlays) - 1, -1, -1):
            ov = self.overlays[i]
            if not (self._active(ov) or (self.sel == ("ov", i))):
                continue
            if ov.x <= vx <= ov.x + ov.w and ov.y <= vy <= ov.y + ov.h:
                self.sel = ("ov", i)
                near = (abs(vx - (ov.x + ov.w)) < HANDLE_PX / sc
                        and abs(vy - (ov.y + ov.h)) < HANDLE_PX / sc)
                self._drag = ("resize" if near else "move", i, vx - ov.x, vy - ov.y)
                self._sel_changed()
                return
        self.sel = None
        self._drag = None
        self._sel_changed()

    def _cv_drag(self, ev) -> None:
        if not self._drag:
            return
        mode, i, dx, dy = self._drag
        ov = self.overlays[i]
        ox, oy, sc = self._pv_rect()
        vx, vy = (ev.x - ox) / sc, (ev.y - oy) / sc
        if mode == "move":
            ov.x = int(max(-ov.w + 8, min(vx - dx, self.vw - 8)))
            ov.y = int(max(-ov.h + 8, min(vy - dy, self.vh - 8)))
        else:
            neww = max(24, int(vx - ov.x))
            if ov.kind in ("blur", "box"):    # alto libre; texto/imagen con aspecto
                ov.w = neww
                ov.h = max(24, int(vy - ov.y))
            else:
                src = self._ov_image(i, ov)
                aspect = (src.height / src.width) if src is not None and src.width else \
                    (ov.h / ov.w if ov.w else 1.0)
                ov.w = neww
                ov.h = max(8, int(neww * aspect))
        self._refresh_canvas()

    def _cv_release(self, _ev) -> None:
        self._drag = None
        self._refresh_tl()

    # ----------------------------------------------------- linea de tiempo
    _TL_L, _TL_RULER, _TL_ROW, _TL_GAP = 8, 20, 18, 4

    def _tl_geom(self) -> tuple[float, float]:
        tw = max(60, self.tl.winfo_width() - 2 * self._TL_L)
        return self._TL_L, tw

    def _t2x(self, t: float) -> float:
        left, tw = self._tl_geom()
        return left + (t / self.duration) * tw

    def _x2t(self, x: float) -> float:
        left, tw = self._tl_geom()
        return max(0.0, min(self.duration, (x - left) / tw * self.duration))

    def _tl_rows(self) -> list[tuple[str, int]]:
        """Filas: ('cut', -1) fija + una por overlay."""
        return [("cut", -1)] + [("ov", i) for i in range(len(self.overlays))]

    def _row_y(self, r: int) -> int:
        return self._TL_RULER + 6 + r * (self._TL_ROW + self._TL_GAP)

    def _refresh_tl(self) -> None:
        rows = self._tl_rows()
        need = self._row_y(len(rows)) + 6
        if int(self.tl.cget("height")) != need:
            self.tl.config(height=need)
        self.tl.delete("all")
        left, tw = self._tl_geom()
        # regla
        step = max(1, int(self.duration // 10) or 1)
        t = 0
        while t <= self.duration:
            x = self._t2x(t)
            self.tl.create_line(x, self._TL_RULER - 6, x, self._TL_RULER, fill=theme.MUTED)
            self.tl.create_text(x + 2, 4, anchor="nw", text=self._fmt(float(t)),
                                fill=theme.MUTED, font=(theme.FONT, 7))
            t += step
        self.tl.create_line(left, self._TL_RULER, left + tw, self._TL_RULER,
                            fill=theme.BORDER)
        # fila de cortes
        y = self._row_y(0)
        self.tl.create_text(left, y + self._TL_ROW / 2, anchor="w", text="✂",
                            fill=theme.MUTED, font=(theme.FONT, 8))
        for j, (s, e) in enumerate(self.cuts):
            selected = self.sel == ("cut", j)
            self.tl.create_rectangle(
                self._t2x(s), y, self._t2x(e), y + self._TL_ROW,
                fill="#F3B0AA" if not selected else theme.PRIMARY,
                outline=theme.PRIMARY, width=2 if selected else 1)
        # filas de overlays
        colors = {"text": theme.NAVY, "image": theme.ACCENT, "blur": "#8A97A8",
                  "box": "#7A5EA8"}
        for r, (_kind, i) in enumerate(self._tl_rows()[1:], start=1):
            ov = self.overlays[i]
            y = self._row_y(r)
            selected = self.sel == ("ov", i)
            x0, x1 = self._t2x(ov.start), self._t2x(ov.end)
            self.tl.create_rectangle(x0, y, x1, y + self._TL_ROW,
                                     fill=colors.get(ov.kind, theme.NAVY),
                                     outline=theme.PRIMARY if selected else theme.BORDER,
                                     width=2 if selected else 1)
            self.tl.create_text(x0 + 5, y + self._TL_ROW / 2, anchor="w",
                                text=ov.label(), fill="#FFFFFF",
                                font=(theme.FONT, 8), width=max(20, x1 - x0 - 8))
        # playhead
        px = self._t2x(self.t)
        self.tl.create_line(px, 2, px, self._row_y(len(rows)), fill=theme.REC, width=2)

    def _tl_hit(self, ev):
        """(tipo, indice, zona) para el punto pulsado, o None. zona: l|m|r."""
        for r, (kind, i) in enumerate(self._tl_rows()):
            y = self._row_y(r)
            if not (y <= ev.y <= y + self._TL_ROW):
                continue
            items = ([(j, s, e) for j, (s, e) in enumerate(self.cuts)] if kind == "cut"
                     else [(i, self.overlays[i].start, self.overlays[i].end)])
            for j, s, e in items:
                x0, x1 = self._t2x(s), self._t2x(e)
                if x0 - EDGE_PX <= ev.x <= x1 + EDGE_PX:
                    zone = ("l" if abs(ev.x - x0) <= EDGE_PX
                            else "r" if abs(ev.x - x1) <= EDGE_PX else "m")
                    return (kind, j, zone)
        return None

    def _tl_press(self, ev) -> None:
        hit = self._tl_hit(ev)
        if hit:
            kind, j, zone = hit
            self.sel = (kind, j)
            s, e = ((self.cuts[j]) if kind == "cut"
                    else (self.overlays[j].start, self.overlays[j].end))
            self._tl_drag = (kind, j, zone, self._x2t(ev.x), s, e)
            self._sel_changed()
            self._refresh_tl()
            self._refresh_canvas()
            return
        # click en vacio: mover playhead
        self._tl_drag = ("scrub", -1, "m", 0.0, 0.0, 0.0)
        self.var_t.set(self._x2t(ev.x))
        self._on_scrub()

    def _tl_motion(self, ev) -> None:
        if not self._tl_drag:
            return
        kind, j, zone, t0, s0, e0 = self._tl_drag
        if kind == "scrub":
            self.var_t.set(self._x2t(ev.x))
            self._on_scrub()
            return
        dt = self._x2t(ev.x) - t0
        s, e = s0, e0
        if zone == "m":
            span = e0 - s0
            s = max(0.0, min(s0 + dt, self.duration - span))
            e = s + span
        elif zone == "l":
            s = max(0.0, min(s0 + dt, e0 - MIN_DUR))
        else:
            e = min(self.duration, max(e0 + dt, s0 + MIN_DUR))
        if kind == "cut":
            self.cuts[j] = (s, e)
        else:
            self.overlays[j].start, self.overlays[j].end = s, e
        self._refresh_tl()
        self._sel_changed()

    # ------------------------------------------------------------ acciones
    def _sel_changed(self) -> None:
        if self.sel and self.sel[0] == "ov" and self.sel[1] < len(self.overlays):
            ov = self.overlays[self.sel[1]]
            self.lbl_info.config(text=f"{ov.label()}   {self._fmt(ov.start)} → {self._fmt(ov.end)}")
        elif self.sel and self.sel[0] == "cut" and self.sel[1] < len(self.cuts):
            s, e = self.cuts[self.sel[1]]
            self.lbl_info.config(text=f"✂ corte   {self._fmt(s)} → {self._fmt(e)}")
        else:
            self.lbl_info.config(text="")

    def _default_span(self) -> tuple[float, float]:
        return self.t, min(self.duration, self.t + max(3.0, self.duration * 0.15))

    def _add_text(self) -> None:
        d = _TextDialog(self)
        if not d.result:
            return
        s, e = self._default_span()
        ov = Overlay("text", 0, 0, 10, 10, s, e, d.result)
        self.overlays.append(ov)
        i = len(self.overlays) - 1
        src = self._ov_image(i, ov)
        if src is None:
            self.overlays.pop()
            messagebox.showinfo(APP_NAME, "No se pudo renderizar el texto.", parent=self)
            return
        scale = min(1.0, (self.vw * 0.5) / src.width)
        ov.w, ov.h = max(24, int(src.width * scale)), max(12, int(src.height * scale))
        ov.x, ov.y = (self.vw - ov.w) // 2, int(self.vh * 0.78)
        self.sel = ("ov", i)
        self._sel_changed()
        self._refresh_canvas()
        self._refresh_tl()

    def _add_image(self) -> None:
        p = filedialog.askopenfilename(
            title="Elige la imagen", parent=self,
            filetypes=[("Imagen", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not p:
            return
        s, e = self._default_span()
        ov = Overlay("image", 0, 0, 10, 10, s, e, {"path": p})
        self.overlays.append(ov)
        i = len(self.overlays) - 1
        src = self._ov_image(i, ov)
        if src is None:
            self.overlays.pop()
            messagebox.showinfo(APP_NAME, "No se pudo abrir la imagen.", parent=self)
            return
        scale = min(1.0, (self.vw * 0.35) / src.width, (self.vh * 0.35) / src.height)
        ov.w, ov.h = max(24, int(src.width * scale)), max(24, int(src.height * scale))
        ov.x, ov.y = int(self.vw * 0.06), int(self.vh * 0.06)
        self.sel = ("ov", i)
        self._sel_changed()
        self._refresh_canvas()
        self._refresh_tl()

    def _add_box(self) -> None:
        d = _BoxDialog(self)
        if not d.result:
            return
        s, e = self._default_span()
        w, h = _even(max(48, self.vw // 4)), _even(max(48, self.vh // 6))
        ov = Overlay("box", (self.vw - w) // 2, int(self.vh * 0.66), w, h, s, e, d.result)
        self.overlays.append(ov)
        self.sel = ("ov", len(self.overlays) - 1)
        self._sel_changed()
        self._refresh_canvas()
        self._refresh_tl()

    def _toggle_chroma(self) -> None:
        """Quita (o restaura) el fondo verde de la IMAGEN seleccionada."""
        if not (self.sel and self.sel[0] == "ov" and self.sel[1] < len(self.overlays)):
            messagebox.showinfo(APP_NAME, "Selecciona una imagen para quitar su fondo "
                                "verde con croma.", parent=self)
            return
        i = self.sel[1]
        ov = self.overlays[i]
        if ov.kind != "image":
            messagebox.showinfo(APP_NAME, "El croma se aplica a las imagenes anadidas "
                                "(las que tengan fondo verde).", parent=self)
            return
        ov.params["chroma"] = None if ov.params.get("chroma") else "#00D000"
        self._ov_imgs.pop(i, None)
        self._refresh_canvas()

    def _add_blur(self) -> None:
        s, e = self._default_span()
        w, h = _even(max(48, self.vw // 4)), _even(max(48, self.vh // 5))
        ov = Overlay("blur", (self.vw - w) // 2, (self.vh - h) // 2, w, h, s, e,
                     {"strength": 24})
        self.overlays.append(ov)
        self.sel = ("ov", len(self.overlays) - 1)
        self._sel_changed()
        self._refresh_canvas()
        self._refresh_tl()

    def _add_cut(self) -> None:
        span = max(1.0, self.duration * 0.05)
        s = self.t
        e = min(self.duration, s + span)
        if e - s < MIN_DUR:
            s = max(0.0, e - MIN_DUR)
        self.cuts.append((s, e))
        self.sel = ("cut", len(self.cuts) - 1)
        self._sel_changed()
        self._refresh_tl()

    def _delete_sel(self) -> None:
        if not self.sel:
            return
        kind, j = self.sel
        if kind == "ov" and j < len(self.overlays):
            self.overlays.pop(j)
            self._ov_imgs = {}   # los indices cambian: invalida el cache
        elif kind == "cut" and j < len(self.cuts):
            self.cuts.pop(j)
        self.sel = None
        self._sel_changed()
        self._refresh_canvas()
        self._refresh_tl()

    # ------------------------------------------------------------- export
    def _export(self) -> None:
        if not self.overlays and not self.cuts:
            messagebox.showinfo(APP_NAME, "Anade un texto, imagen, difuminado o corte "
                                "antes de exportar.", parent=self)
            return
        out = filedialog.asksaveasfilename(
            title="Exportar video como…", defaultextension=".mp4", parent=self,
            initialfile=Path(self.video).stem + "_editado.mp4",
            initialdir=str(Path(self.video).parent),
            filetypes=[("Video MP4", "*.mp4")]) or None
        if not out:
            return
        # asegurar PNGs de texto/cuadro renderizados
        for i, ov in enumerate(self.overlays):
            if ov.kind in ("text", "box"):
                self._ov_image(i, ov)
        enc = fu.resolve_encoder(self.app.var_enc.get(), self.app.encoders, self.ffmpeg)
        cmd = build_export_cmd(
            self.ffmpeg, self.video, self.vw, self.vh, self.duration,
            list(self.overlays), list(self.cuts), out, encoder=enc,
            quality_key=self.app.var_quality.get(), with_audio=self.with_audio,
            crossfade=(0.5 if self.var_xfade.get() else 0.0),
            fade_inout=(0.5 if self.var_fade.get() else 0.0))

        def work():
            run_export(cmd, out)
            return out
        self.app._run_with_progress("Exportando video editado…", work,
                                    lambda r: f"Video exportado:\n{r}")


# --------------------------------------------------------------- dialogos
class _TextDialog:
    """Texto + tamano + color + fondo opcional. result = params del overlay."""

    def __init__(self, parent):
        self.result: dict | None = None
        win = tk.Toplevel(parent)
        theme.center_window(win)
        win.title("Anadir texto")
        win.transient(parent)
        win.resizable(False, False)
        win.grab_set()
        ttk.Label(win, text="Texto a mostrar", style="H.TLabel").pack(padx=16, pady=(14, 4))
        txt = tk.Text(win, width=42, height=3, font=(theme.FONT, 10))
        txt.pack(padx=16)
        row = ttk.Frame(win)
        row.pack(padx=16, pady=(10, 0), fill="x")
        ttk.Label(row, text="Tamano:").pack(side="left")
        var_size = tk.IntVar(value=48)
        ttk.Spinbox(row, from_=12, to=200, textvariable=var_size, width=5).pack(
            side="left", padx=(4, 14))
        color = {"fg": "#FFFFFF", "bg": "#000000"}
        btn_fg = tk.Button(row, text="Color", width=7, bg=color["fg"],
                           command=lambda: pick("fg", btn_fg))
        btn_fg.pack(side="left", padx=4)
        var_bg = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Fondo", variable=var_bg).pack(side="left", padx=(12, 2))
        btn_bg = tk.Button(row, text="Color fondo", width=10, bg=color["bg"], fg="#FFFFFF",
                           command=lambda: pick("bg", btn_bg))
        btn_bg.pack(side="left", padx=4)

        def pick(which, btn):
            c = colorchooser.askcolor(color[which], parent=win)
            if c and c[1]:
                color[which] = c[1]
                btn.config(bg=c[1])

        def ok():
            t = txt.get("1.0", "end").strip()
            if not t:
                win.destroy()
                return
            self.result = {"text": t, "size": max(4, min(600, var_size.get())),
                           "color": color["fg"],
                           "bg": color["bg"] if var_bg.get() else None,
                           "bg_alpha": 86}
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(pady=12)
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(bar, text="Anadir", style="Primary.TButton", command=ok).pack(
            side="left", padx=6)
        txt.focus_set()
        win.wait_window()


class _BoxDialog:
    """Cuadro de color solido: color + opacidad. result = params del overlay."""

    def __init__(self, parent):
        self.result: dict | None = None
        win = tk.Toplevel(parent)
        theme.center_window(win)
        win.title("Anadir cuadro de color")
        win.transient(parent)
        win.resizable(False, False)
        win.grab_set()
        ttk.Label(win, text="Cuadro de color (fondo para textos, franjas…)",
                  style="H.TLabel").pack(padx=16, pady=(14, 8))
        row = ttk.Frame(win)
        row.pack(padx=16, pady=(0, 4))
        color = {"c": "#1E3A5F"}
        btn = tk.Button(row, text="Color", width=9, bg=color["c"], fg="#FFFFFF",
                        command=lambda: pick())
        btn.pack(side="left", padx=(0, 14))

        def pick():
            c = colorchooser.askcolor(color["c"], parent=win)
            if c and c[1]:
                color["c"] = c[1]
                btn.config(bg=c[1])
        ttk.Label(row, text="Opacidad %:").pack(side="left")
        var_a = tk.IntVar(value=100)
        ttk.Spinbox(row, from_=5, to=100, textvariable=var_a, width=5).pack(
            side="left", padx=4)

        def ok():
            self.result = {"color": color["c"],
                           "alpha": max(5, min(100, var_a.get()))}
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(pady=12)
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(bar, text="Anadir", style="Primary.TButton", command=ok).pack(
            side="left", padx=6)
        win.wait_window()


def pick_blur_region(parent, ffmpeg: str, video: str) -> "privacy_shield.BlurRegion | None":
    """Selector VISUAL de zona a difuminar: fotograma del video + dibujar el
    rectangulo arrastrando (y ajustarlo), con tramo de tiempo opcional. Sustituye
    al dialogo de coordenadas cuando hay un video del que ensenar un fotograma."""
    try:
        vw, vh = autoframe.video_dims(ffmpeg, video)
        duration = ai_post.get_duration(ffmpeg, video)
    except Exception:  # noqa: BLE001
        return None
    if vw <= 0 or vh <= 0:
        return None
    frames = FrameCache(ffmpeg, video)

    win = tk.Toplevel(parent)
    theme.center_window(win)
    win.title("Censurar zona — dibuja el rectangulo sobre el video")
    win.transient(parent)
    win.grab_set()
    cw = min(880, max(480, vw // 2))
    ch = max(2, int(cw * vh / vw))
    cv = tk.Canvas(win, width=cw, height=ch, bg="#0B1118", highlightthickness=0)
    cv.pack(padx=12, pady=(12, 4))
    sc = cw / vw

    state = {"photo": None, "rect": None, "t": min(1.0, duration / 2), "drag": None}

    def show(img) -> None:
        if img is None:
            return
        disp = img.resize((cw, ch), Image.LANCZOS)
        state["photo"] = ImageTk.PhotoImage(disp)
        try:
            cv.delete("frame")
            cv.create_image(0, 0, anchor="nw", image=state["photo"], tags="frame")
            cv.tag_lower("frame")
        except tk.TclError:
            pass

    def request_frame() -> None:
        img = frames.get(state["t"])
        if img is not None:
            show(img)
        else:
            frames.request(state["t"], lambda im: win.after(0, lambda: show(im)))

    def redraw_rect() -> None:
        cv.delete("sel")
        r = state["rect"]
        if not r:
            return
        x0, y0, x1, y1 = r
        cv.create_rectangle(x0, y0, x1, y1, outline=theme.PRIMARY, width=2, tags="sel")
        cv.create_rectangle(x1 - 8, y1 - 8, x1, y1, fill=theme.PRIMARY, outline="",
                            tags="sel")

    def press(ev) -> None:
        r = state["rect"]
        if r and abs(ev.x - r[2]) < 10 and abs(ev.y - r[3]) < 10:
            state["drag"] = ("resize",)
        elif r and r[0] <= ev.x <= r[2] and r[1] <= ev.y <= r[3]:
            state["drag"] = ("move", ev.x - r[0], ev.y - r[1])
        else:
            state["rect"] = [ev.x, ev.y, ev.x + 2, ev.y + 2]
            state["drag"] = ("draw",)
        redraw_rect()

    def motion(ev) -> None:
        d = state["drag"]
        r = state["rect"]
        if not d or not r:
            return
        x = max(0, min(ev.x, cw))
        y = max(0, min(ev.y, ch))
        if d[0] in ("draw", "resize"):
            r[2], r[3] = x, y
        else:
            w, h = r[2] - r[0], r[3] - r[1]
            r[0] = max(0, min(x - d[1], cw - w))
            r[1] = max(0, min(y - d[2], ch - h))
            r[2], r[3] = r[0] + w, r[1] + h
        redraw_rect()

    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", motion)
    cv.bind("<ButtonRelease-1>", lambda _e: state.update(drag=None))

    srow = ttk.Frame(win)
    srow.pack(fill="x", padx=12)
    var_t = tk.DoubleVar(value=state["t"])

    def on_scrub(_v=None) -> None:
        state["t"] = var_t.get()
        request_frame()
    ttk.Label(srow, text="Momento:").pack(side="left")
    ttk.Scale(srow, from_=0.0, to=max(0.1, duration), variable=var_t,
              command=on_scrub).pack(side="left", fill="x", expand=True, padx=8)

    tr = ttk.Frame(win)
    tr.pack(padx=12, pady=(8, 0))
    ttk.Label(tr, text="Desde (s, vacio=todo):").pack(side="left")
    s_var = tk.StringVar()
    ttk.Entry(tr, textvariable=s_var, width=7).pack(side="left", padx=(4, 12))
    ttk.Label(tr, text="Hasta (s):").pack(side="left")
    e_var = tk.StringVar()
    ttk.Entry(tr, textvariable=e_var, width=7).pack(side="left", padx=4)

    res: dict = {}

    def to_f(s):
        try:
            return float(s.replace(",", ".")) if s.strip() else None
        except ValueError:
            return None

    def ok() -> None:
        r = state["rect"]
        if not r:
            messagebox.showinfo(APP_NAME, "Dibuja el rectangulo arrastrando sobre el video.",
                                parent=win)
            return
        x0, x1 = sorted((r[0], r[2]))
        y0, y1 = sorted((r[1], r[3]))
        res["r"] = privacy_shield.BlurRegion(
            int(x0 / sc), int(y0 / sc), max(8, int((x1 - x0) / sc)),
            max(8, int((y1 - y0) / sc)), to_f(s_var.get()), to_f(e_var.get()))
        win.destroy()

    bar = ttk.Frame(win)
    bar.pack(pady=12)
    ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="left", padx=6)
    ttk.Button(bar, text="Censurar", style="Primary.TButton", command=ok).pack(
        side="left", padx=6)
    request_frame()
    win.wait_window()
    return res.get("r")
