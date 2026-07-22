"""Panel de PULIDO (post-produccion) que se muestra en la fila inferior cuando el
modo es Docente o Curso. La captura (escena, fuentes, camara arrastrable, preview,
grabar) la hace el ESTUDIO compartido: este panel solo encadena la IA local sobre
la grabacion resultante (o un video elegido).

Orden del pipeline (importa): privacidad -> zoom al cursor -> silencios ->
auto-encuadre -> subtitulos -> material de estudio -> fabrica.
"""

from __future__ import annotations

import logging
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
import tkinter as tk

from . import APP_NAME
from . import ai_post, models, content_factory, privacy_shield, autoframe, chapters
from . import study, notes, cursorzoom  # noqa: F401  (cursorzoom usado en _polish)

logger = logging.getLogger(__name__)


class PolishPanel(ttk.LabelFrame):
    """Opciones de 'Pulir leccion' (post-produccion) sobre la ultima grabacion."""

    def __init__(self, parent, app, profile: str = "docente"):
        super().__init__(parent, text="✨ Pulir leccion (post-produccion con IA local)", padding=6)
        self.app = app
        self.profile = profile if profile in ("docente", "curso") else "docente"
        self.source_video: str | None = None
        self.regions: list = []
        self._polishing = False
        # el estudio rellena estos datos si se grabo con "zoom al cursor" activo:
        self._cursor_samples = None
        self._cursor_region = None
        self._cursor_for: str | None = None
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        top = ttk.Frame(self); top.pack(fill="x")
        self.lbl_src = ttk.Label(top, text="Graba con el boton de arriba o elige un video →",
                                 style="Muted.TLabel")
        self.lbl_src.pack(side="left")
        ttk.Button(top, text="Elegir un video…", command=self._choose_existing).pack(side="left", padx=8)
        self.btn_polish = ttk.Button(top, text="✨  Pulir leccion", style="Primary.TButton",
                                     command=self._polish, state="disabled")
        self.btn_polish.pack(side="right")

        opts = ttk.Frame(self); opts.pack(fill="x", pady=(6, 0))
        # 3 columnas de opciones (max 3 filas) + material del curso a la derecha.
        # Antes eran columnas mas altas y el panel se cortaba por abajo en
        # Docente/Curso; asi es mas bajo y a lo ancho cabe en la ventana.
        ca = ttk.Frame(opts); ca.grid(row=0, column=0, sticky="nw", padx=(0, 22))
        self.var_silence = tk.BooleanVar(value=True)
        ttk.Checkbutton(ca, text="Quitar pausas y muletillas", variable=self.var_silence).pack(anchor="w")
        self.var_subs = tk.BooleanVar(value=True)
        ttk.Checkbutton(ca, text="Subtitulos accesibles incrustados", variable=self.var_subs).pack(anchor="w")
        self.var_subs_en = tk.BooleanVar(value=False)
        ttk.Checkbutton(ca, text="     + .srt traducido al ingles", variable=self.var_subs_en).pack(anchor="w")

        cb = ttk.Frame(opts); cb.grid(row=0, column=1, sticky="nw", padx=(0, 22))
        self.var_autoframe = tk.BooleanVar(value=False)
        ttk.Checkbutton(cb, text="Auto-encuadre: seguirme", variable=self.var_autoframe).pack(anchor="w")
        # zoom al cursor: opcion de captura que el estudio aplica al grabar
        self.var_cursorzoom = tk.BooleanVar(value=False)
        ttk.Checkbutton(cb, text="🔍 Zoom que sigue mi cursor",
                        variable=self.var_cursorzoom).pack(anchor="w")

        cc = ttk.Frame(opts); cc.grid(row=0, column=2, sticky="nw", padx=(0, 22))
        pr = ttk.Frame(cc); pr.pack(anchor="w")
        self.var_priv = tk.BooleanVar(value=False)
        ttk.Checkbutton(pr, text="Difuminar datos de alumnos", variable=self.var_priv).pack(side="left")
        ttk.Button(pr, text="Zonas…", command=self._add_region, width=7).pack(side="left", padx=4)
        self.lbl_regions = ttk.Label(pr, text="", style="Muted.TLabel"); self.lbl_regions.pack(side="left")
        self.var_factory = tk.BooleanVar(value=False)
        ttk.Checkbutton(cc, text="Material multiplataforma (9:16 + MP3 + SRT)",
                        variable=self.var_factory).pack(anchor="w")

        # columna material del curso (exclusivo Curso): a la derecha. SIN LabelFrame:
        # su titulo anadia una 4a fila y hacia el panel mas alto que las demas
        # columnas -> se cortaba por abajo en pantallas bajas. El 📚 en la 1a casilla
        # basta para agrupar visualmente y deja la columna en 3 filas como el resto.
        self.var_chapters = tk.BooleanVar(value=(self.profile == "curso"))
        self.var_notes = tk.BooleanVar(value=False)
        self.var_quiz = tk.BooleanVar(value=False)
        if self.profile == "curso":
            cd = ttk.Frame(opts)
            cd.grid(row=0, column=3, sticky="nw")
            ttk.Checkbutton(cd, text="📚 Capitulos + indice (YouTube)", variable=self.var_chapters).pack(anchor="w")
            ttk.Checkbutton(cd, text="📚 Auto-apuntes (PDF)", variable=self.var_notes).pack(anchor="w")
            ttk.Checkbutton(cd, text="📚 Resumen + autoexamen", variable=self.var_quiz).pack(anchor="w")

    def _set_step2_enabled(self, on: bool) -> None:
        self.btn_polish.config(state=("normal" if on and self.source_video else "disabled"))

    def _choose_existing(self) -> None:
        p = filedialog.askopenfilename(
            title="Elige un video para pulir", initialdir=self.app.cfg.videos_dir,
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi")], parent=self.app)
        if p:
            self.set_source(p)

    def set_source(self, path: str | None, *, cursor_samples=None, cursor_region=None) -> None:
        """Fija el video a pulir. El estudio llama a esto al terminar una grabacion;
        si se grabo con zoom-al-cursor, pasa las muestras del raton."""
        if not path:
            return
        self.source_video = path
        try:
            self.lbl_src.config(text=f"Video: {Path(path).name}")
        except tk.TclError:
            return
        self.regions = []
        self._cursor_samples = cursor_samples
        self._cursor_region = cursor_region
        self._cursor_for = path if cursor_samples else None
        self._update_regions_label()
        self._set_step2_enabled(True)

    def _add_region(self) -> None:
        r = self.app._ask_region()
        if r:
            self.regions.append(r)
            self.var_priv.set(True)
            self._update_regions_label()

    def _update_regions_label(self) -> None:
        n = len(self.regions)
        self.lbl_regions.config(text=(f"{n} zona(s)" if n else ""))

    def _polish(self) -> None:
        if self._polishing:
            return
        video = self.source_video
        if not video or not Path(video).is_file():
            messagebox.showinfo(APP_NAME, "Primero graba (boton de arriba) o elige un video.",
                                parent=self.app)
            return
        do_priv = self.var_priv.get() and self.regions
        do_cursor = bool(self._cursor_samples and self._cursor_for == video)
        # El zoom al cursor necesita la posicion del raton, que solo se capta al GRABAR
        # con la app. En un video importado no hay esos datos: avisamos en vez de
        # omitirlo en silencio (el resto de mejoras si se aplican).
        if self.var_cursorzoom.get() and not do_cursor:
            messagebox.showinfo(
                APP_NAME, "El «zoom que sigue mi cursor» solo funciona en videos GRABADOS "
                "con la app (capta la posicion del raton mientras grabas). Este video es "
                "importado, asi que esa opcion se omitira; el resto de mejoras si se aplican.",
                parent=self.app)
        do_sil = self.var_silence.get()
        do_frame = self.var_autoframe.get() and not do_cursor
        do_subs = self.var_subs.get()
        do_subs_en = self.var_subs_en.get()
        do_chap = self.var_chapters.get()
        do_notes = self.var_notes.get()
        do_quiz = self.var_quiz.get()
        do_fac = self.var_factory.get()
        if not any((do_priv, do_cursor, do_sil, do_frame, do_subs, do_chap, do_notes,
                    do_quiz, do_fac)):
            messagebox.showinfo(APP_NAME, "Elige al menos una mejora.", parent=self.app)
            return

        needs_whisper = do_subs or do_subs_en or do_chap or do_notes or do_quiz
        model_key = models.first_available() if needs_whisper else None
        if needs_whisper and not model_key:
            key = self.app.cfg.whisper_model
            if not messagebox.askyesno(APP_NAME, f"Para subtitulos/capitulos hay que descargar el "
                                       f"modelo Whisper '{key}' una sola vez.\n\nDescargar ahora?",
                                       parent=self.app):
                return

        enc = "libx264"   # post-produccion offline: universal y robusto
        qk = self.app.cfg.video_quality
        ff = self.app.ffmpeg
        stem = Path(video).stem
        outdir = Path(video).parent
        regions = list(self.regions)
        cur_samples = self._cursor_samples
        cur_region = self._cursor_region

        def work():
            steps = []
            cur = video
            if do_priv:
                out = str(outdir / f"{stem}_privado.mp4")
                privacy_shield.blur_regions(ff, cur, out, regions, encoder=enc, quality_key=qk)
                cur = out; steps.append("privacidad")
            if do_cursor:
                out = str(outdir / f"{stem}_zoomcursor.mp4")
                cursorzoom.apply(ff, cur, out, cur_samples, cur_region, encoder=enc, quality_key=qk)
                cur = out; steps.append("zoom al cursor")
            if do_sil:
                out = str(outdir / f"{stem}_fluido.mp4")
                info = ai_post.cut_silences(ff, cur, out, encoder=enc, quality_key=qk)
                cur = out; steps.append(f"silencios ({info['orig']:.0f}s→{info['final']:.0f}s)")
            if do_frame:
                out = str(outdir / f"{stem}_encuadrado.mp4")
                autoframe.autoframe(ff, cur, out, aspect="keep", encoder=enc, quality_key=qk)
                cur = out; steps.append("auto-encuadre")
            final = cur
            srt_text = ""
            mp = None
            if needs_whisper:
                mp = (str(models.model_path(model_key)) if model_key and models.is_downloaded(model_key)
                      else models.download(self.app.cfg.whisper_model))
            if do_subs:
                srt = str(outdir / f"{stem}.srt")
                srt_text = ai_post.transcribe_srt(ff, mp, cur, "es", srt)
                burned = str(outdir / f"{stem}_leccion.mp4")
                ai_post.burn_subtitles(ff, cur, srt, burned, encoder=enc, quality_key=qk)
                final = burned; steps.append("subtitulos")
            if do_subs_en:
                ai_post.transcribe_srt(ff, mp, cur, "en", str(outdir / f"{stem}_en.srt"))
                steps.append("subtitulos EN")
            if final == cur and cur != video:
                lesson = str(outdir / f"{stem}_leccion.mp4")
                try:
                    import os
                    os.replace(cur, lesson); final = lesson
                except OSError:
                    final = cur
            if do_chap or do_notes or do_quiz:
                if not srt_text:
                    srt_text = ai_post.transcribe_srt(ff, mp, final, "es", str(outdir / f"{stem}.srt"))
                segs = chapters.parse_srt(srt_text)
                if do_chap:
                    chapters.make_chapters(ff, final, srt_text, str(outdir / f"{stem}_capitulos"), embed=True)
                    steps.append("capitulos")
                if do_notes:
                    notes.make_notes_pdf(ff, final, srt_text, str(outdir / f"{stem}_apuntes.pdf"), title=stem)
                    steps.append("apuntes PDF")
                if do_quiz:
                    html = study.material_html(study.summarize(segs), study.quiz(segs), stem)
                    (outdir / f"{stem}_estudio.html").write_text(html, encoding="utf-8")
                    steps.append("resumen+quiz")
            extras = []
            if do_fac:
                extras = content_factory.make_package(
                    ff, final, str(outdir / f"{stem}_material"), vertical=True, audio=True,
                    gif=False, subtitles=False, encoder=enc, quality_key=qk)
                steps.append(f"material x{len(extras)}")
            return final, steps, extras

        self._polishing = True
        try:
            self.btn_polish.config(state="disabled")
        except tk.TclError:
            pass

        def reset():
            self._polishing = False
            try:
                self.btn_polish.config(state="normal")
            except tk.TclError:
                pass

        self.app._run_with_progress(
            "Puliendo tu leccion (todo en tu PC)…", work,
            lambda r: f"¡Leccion lista!\n{r[0]}\n\nHecho: {', '.join(r[1])}"
            + (f"\nMaterial extra: {len(r[2])} archivos" if r[2] else ""),
            always=reset)
