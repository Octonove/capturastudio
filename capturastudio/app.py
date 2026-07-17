"""Ventana principal de CapturaStudio: editor de escena (fuentes + inspector),
preview de encuadre a baja tasa y controles de grabacion."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox, colorchooser
from pathlib import Path

from . import APP_NAME, APP_VERSION, theme
from . import scene as scn
from . import ffmpeg_utils as fu
from . import streaming as stream
from . import (ai_post, models, content_factory, privacy_shield, bg_removal, meters,
               autoframe, chapters, quality_check, study, notes, llm, wordedit, cursorzoom,
               winlist, wincap)
from .teacher_mode import PolishPanel
from .config import (AppConfig, load_config, save_config, CANVAS_PRESETS,
                     VIDEO_QUALITY, QUALITY_ORDER, work_dir, get_data_dir, DEFAULT_HOTKEYS)
from .monitors import list_monitors, primary_monitor
from .audio_capture import list_microphones, AVAILABLE as AUDIO_OK
from .engine import RecordEngine
from .hotkeys import (GlobalHotkeys, parse_hotkey, format_hotkey, keysym_to_vk,
                      validate_hotkey_map, MOD_CONTROL, MOD_SHIFT, MOD_ALT)
from .replay import ReplayBuffer

logger = logging.getLogger(__name__)

PREVIEW_W, PREVIEW_H = 640, 360

# Descripcion de la app para el asistente (system prompt de Ollama y guia estatica).
APP_GUIDE = (
    "CapturaStudio es una app de escritorio Windows, 100% local y gratis, para grabar "
    "pantalla/camara y streaming, con post-produccion por IA local. Tiene 3 MODOS (menu "
    "'🚀 Modos'): DOCENTE (graba una clase y al parar la pule: quita pausas, subtitulos "
    "accesibles, difumina datos de alumnos, auto-encuadre, material multiplataforma); "
    "CURSO PARA YOUTUBE (lo de Docente + capitulos por tema, auto-apuntes en PDF y "
    "resumen+autoexamen); STREAMER/ESTUDIO (escenas por capas, chroma, directo a Twitch/"
    "YouTube multidestino, buffer de repeticion). En el menu 'Post-produccion IA' hay: "
    "subtitulos Whisper, quitar silencios, auto-encuadre, capitulos, buscador dentro del "
    "video, auto-apuntes PDF, resumen+autoexamen, control de calidad, paquete de contenido, "
    "escudo de privacidad, foco de ventana y quitar fondo. Todo se procesa en el PC del "
    "usuario sin enviar nada a internet.")

# Acciones con atajo global remapeable: (clave, etiqueta, nombre del metodo).
HOTKEY_ACTIONS = [
    ("record", "Grabar / Detener", "_toggle_record"),
    ("pause", "Pausar / Reanudar", "_toggle_pause"),
    ("stream", "Directo on / off", "_toggle_stream"),
    ("moment", "Guardar momento (replay)", "_save_replay_moment"),
]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.minsize(1140, 820)
        theme.apply(self)

        self.cfg: AppConfig = load_config()
        if getattr(self.cfg, "ollama_model", ""):
            llm.set_model(self.cfg.ollama_model)   # respeta el modelo IA preferido
        self.ffmpeg = fu.find_ffmpeg(self.cfg.ffmpeg_path) or ""
        # Sondear encoders y camaras lanza subprocesos de FFmpeg (~1-2 s) que
        # retrasaban la aparicion de la ventana: se hace en un hilo al final del
        # __init__ (_probe_ffmpeg_caps); mientras, estos valores seguros.
        self.encoders = {"libx264"}
        self.video_devices = []
        # Los microfonos se listan en un hilo (_probe_ffmpeg_caps): enumerar con
        # soundcard aqui importaria soundcard en el hilo de la UI, y su init de
        # COM (MTA) congelaba los dialogos nativos ("Carpeta de salida…").
        self.mics: list[str] = []

        cw, ch = CANVAS_PRESETS.get(self.cfg.canvas, (1920, 1080))
        self.scene = scn.Scene(canvas_w=cw, canvas_h=ch, fps=self.cfg.fps)
        self._seed_scene()
        self.scenes: list[scn.Scene] = [self.scene]
        self._scene_i = 0
        self._load_last_scene()   # puebla self.scenes desde la coleccion si existe

        self.engine: RecordEngine | None = None
        self.stream_engine: stream.StreamEngine | None = None
        self.replay: ReplayBuffer | None = None
        self.meter: meters.AudioMeter | None = None
        self._vu_sys = 0.0
        self._vu_mic = 0.0
        self._meter_gen = 0
        self._hotkeys_win = None
        self._capturing_btn = None
        self._sched_start_id: str | None = None
        self._sched_stop_id: str | None = None
        self._sched_desc: str = ""
        self._polish_panel = None     # panel de pulido (Docente/Curso); None en Streamer
        self._cursor_logger = None    # MouseLogger activo durante la grabacion (si zoom-cursor)
        self._cursor_region = None    # region de pantalla asociada al registro del cursor
        self.extra_dests: list[str] = []
        self.last_recording: str | None = None
        self._rec_t0: float | None = None
        self._stream_t0: float | None = None
        self._preview_imgtk = None
        self._preview_dirty = True
        self._mss = None
        self._sel_id: int | None = None
        self._boxes: dict[int, tuple[float, float, float, float]] = {}
        self._drag = None
        # Sesiones WGC persistentes para el preview/recorte de fuentes de ventana
        # (a prueba de oclusion, MISMO espacio que la grabacion -> el recorte
        # coincide al pixel). Clave: titulo de la ventana.
        self._win_grabbers: dict = {}

        self._build_ui()
        self._sync_canvas_from_scene()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.ffmpeg:
            messagebox.showerror(APP_NAME, "No se encontro FFmpeg. Instalalo o configura su ruta.")
        self.hotkeys: GlobalHotkeys | None = None
        if self.cfg.hotkeys_enabled:
            self._setup_hotkeys()
        self.after(200, self._preview_loop)
        threading.Thread(target=self._probe_ffmpeg_caps, daemon=True).start()
        if not self.cfg.seen_welcome:
            self.after(400, self._show_welcome)

    def _probe_ffmpeg_caps(self) -> None:
        """Sondea encoders, camaras dshow y microfonos en segundo plano. Los
        microfonos DEBEN enumerarse aqui (hilo de trabajo): la primera vez carga
        soundcard, que inicializa COM en el hilo llamante. Al terminar refresca
        los combobox en el hilo de UI."""
        mics = list_microphones()

        def apply_mics():
            self.mics = mics
            try:
                self.cmb_mic.config(values=mics)
                if mics and self.var_micdev.get() not in mics:
                    self.var_micdev.set(mics[0])
            except (tk.TclError, AttributeError):
                pass
        try:
            self.after(0, apply_mics)
        except (RuntimeError, tk.TclError):
            return

        if not self.ffmpeg:
            return
        enc = fu.list_encoders(self.ffmpeg)
        devs = fu.list_video_devices(self.ffmpeg)

        def apply_caps():
            self.encoders = enc
            self.video_devices = devs
            try:
                vals = ["auto"] + sorted(e for e in enc if e.startswith("h264") or e == "libx264")
                self.cmb_enc.config(values=vals)
            except (tk.TclError, AttributeError):
                pass
        try:
            self.after(0, apply_caps)
        except (RuntimeError, tk.TclError):
            pass

    def _show_welcome(self) -> None:
        self.cfg.seen_welcome = True
        save_config(self.cfg)
        self._show_mode_chooser()

    def _show_mode_chooser(self) -> None:
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title(f"{APP_NAME} — ¿que quieres hacer?")
        win.configure(bg=theme.BG)
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=22)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Elige tu modo", style="H.TLabel").pack(anchor="w", pady=(0, 2))
        ttk.Label(frm, text="Puedes cambiar cuando quieras desde el menu '🚀 Modos'.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 14))

        def card(emoji, title, desc, button, command):
            c = ttk.LabelFrame(frm, padding=12)
            c.pack(fill="x", pady=5)
            ttk.Label(c, text=f"{emoji}  {title}", style="H.TLabel").pack(anchor="w")
            ttk.Label(c, text=desc, style="Muted.TLabel", justify="left").pack(
                anchor="w", pady=(2, 8))
            ttk.Button(c, text=button, style="Primary.TButton",
                       command=lambda: (win.destroy(), command())).pack(anchor="e")

        card("🎓", "Docente", "Graba tu clase y, al parar, la IA local la pule: quita pausas,\n"
             "subtitulos accesibles, difumina datos de alumnos y crea material.\n"
             "Sin editar y sin que nada salga de tu PC.",
             "Empezar clase", lambda: self._open_teacher_mode("docente"))
        card("📚", "Curso para YouTube", "Todo lo de Docente y, ademas, material de estudio:\n"
             "capitulos por tema + indice clicable, auto-apuntes en PDF y\n"
             "resumen + autoexamen. Listo para publicar un curso.",
             "Grabar curso", lambda: self._open_teacher_mode("curso"))
        card("🎬", "Streamer / Estudio", "El estudio completo: escenas por capas, camara, chroma,\n"
             "directo a Twitch/YouTube, multidestino y buffer de repeticion.",
             "Abrir estudio", self._focus_studio)
        win.grab_set()

    def _focus_studio(self) -> None:
        self._set_mode("studio")

    def _setup_hotkeys(self) -> None:
        try:
            hk = GlobalHotkeys(self)
            for action, label, methodname in HOTKEY_ACTIONS:
                combo = (self.cfg.hotkeys or {}).get(action) or DEFAULT_HOTKEYS[action]
                parsed = parse_hotkey(combo)
                if not parsed:
                    logger.warning("Atajo invalido para %s: %r (se omite)", action, combo)
                    continue
                mods, vk = parsed
                hk.add(mods, vk, getattr(self, methodname), label)
            hk.start()
            self.hotkeys = hk
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudieron registrar atajos globales: %s", exc)

    def _apply_hotkeys(self) -> None:
        """Reinicia el registro de atajos tras un cambio de configuracion."""
        if self.hotkeys:
            try:
                self.hotkeys.stop()
            except Exception:  # noqa: BLE001
                pass
            self.hotkeys = None
        if self.cfg.hotkeys_enabled:
            self._setup_hotkeys()

    # -- escena inicial ----------------------------------------------------
    def _seed_scene(self) -> None:
        mon = primary_monitor()
        self.scene.sources.clear()
        self.scene.add(scn.screen_source(mon.region, name=f"Pantalla {mon.index}"))
        if self.video_devices:
            cam = next((c for c in self.video_devices if "broadcast" not in c.lower()),
                       self.video_devices[0])
            x = self.scene.canvas_w - 400
            y = self.scene.canvas_h - 400
            self.scene.add(scn.webcam_source(cam, x=x, y=y, size=340, circle=True))

    # -- construccion de la interfaz --------------------------------------
    def _build_ui(self) -> None:
        self._build_menubar()
        theme.header(self, APP_NAME, "Estudio de grabacion y streaming local · 100% en tu PC")
        self._build_mode_bar()
        self.status = theme.status_bar(self, "Listo.")   # barra inferior compartida

        # Contenedor del ESTUDIO. El mismo estudio sirve a los 3 modos; el modo solo
        # cambia la fila inferior (Directo vs Pulir leccion), no la vista completa.
        self._content = ttk.Frame(self)
        self._content.pack(fill="both", expand=True)
        # El estudio (escena + preview + grabar) esta SIEMPRE visible; el modo solo
        # cambia la fila inferior (Directo vs Pulir leccion).
        self._studio_view = ttk.Frame(self._content)
        self._studio_view.pack(fill="both", expand=True)

        body = ttk.Frame(self._studio_view, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # --- Columna izquierda: fuentes + inspector ---
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))

        scbar = ttk.Frame(left)
        scbar.pack(fill="x", pady=(0, 8))
        ttk.Label(scbar, text="Escena", style="H.TLabel").pack(side="left")
        self.scene_combo = ttk.Combobox(scbar, width=15, state="readonly")
        self.scene_combo.pack(side="left", padx=(8, 4))
        self.scene_combo.bind("<<ComboboxSelected>>", self._on_scene_combo)
        ttk.Button(scbar, text="+", width=2, command=self._new_scene).pack(side="left")
        ttk.Button(scbar, text="⧉", width=2, command=self._dup_scene).pack(side="left", padx=2)
        ttk.Button(scbar, text="✎", width=2, command=self._rename_scene).pack(side="left")
        ttk.Button(scbar, text="✕", width=2, command=self._del_scene).pack(side="left", padx=(2, 0))

        src_box = ttk.LabelFrame(left, text="Fuentes de la escena", padding=10)
        src_box.pack(fill="x")
        self.src_list = tk.Listbox(src_box, height=8, activestyle="none",
                                   font=(theme.FONT, 10), bg=theme.WHITE, fg=theme.TEXT,
                                   selectbackground=theme.PRIMARY, selectforeground=theme.WHITE,
                                   highlightthickness=1, highlightbackground=theme.BORDER,
                                   relief="flat", exportselection=False, width=34)
        self.src_list.pack(fill="x")
        self.src_list.bind("<<ListboxSelect>>", self._on_select_source)

        btns = ttk.Frame(src_box)
        btns.pack(fill="x", pady=(8, 0))
        add_btn = ttk.Menubutton(btns, text="+ Anadir", style="Primary.TButton")
        add_btn.pack(side="left")
        self._build_add_menu(add_btn)
        ttk.Button(btns, text="Quitar", command=self._remove_source).pack(side="left", padx=4)
        ttk.Button(btns, text="↑", width=3, command=lambda: self._reorder(+1)).pack(side="left")
        ttk.Button(btns, text="↓", width=3, command=lambda: self._reorder(-1)).pack(side="left", padx=(2, 0))
        self.vis_btn = ttk.Button(btns, text="Ocultar", command=self._toggle_visible)
        self.vis_btn.pack(side="right")

        # Inspector
        insp = ttk.LabelFrame(left, text="Propiedades", padding=10)
        insp.pack(fill="x", pady=(12, 0))
        self.insp = insp
        self._build_inspector(insp)

        # --- Columna derecha: preview ---
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        ttk.Label(right, text="Vista previa de encuadre (baja tasa · el render final va a calidad completa)",
                  style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.canvas = tk.Canvas(right, width=PREVIEW_W, height=PREVIEW_H, bg="#0B1118",
                                highlightthickness=1, highlightbackground=theme.BORDER,
                                cursor="fleur")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        # --- Barra inferior del estudio: ajustes + controles ---
        self._build_bottom()
        self._refresh_source_list()
        self._refresh_scene_combo()
        self._set_mode("studio")          # arranca mostrando el estudio

    # -- barra de modos (cambia toda la ventana) --------------------------
    def _build_mode_bar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Modo:", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self._mode_btns = {}
        for key, label in (("docente", "🎓 Docente"), ("curso", "📚 Curso para YouTube"),
                           ("studio", "🎬 Streamer / Estudio")):
            b = ttk.Button(bar, text=label, command=lambda k=key: self._set_mode(k))
            b.pack(side="left", padx=(0, 6))
            self._mode_btns[key] = b

    def _set_mode(self, mode: str) -> None:
        """Cambia el MODO sin tocar el estudio (escena+preview+grabar siguen igual):
        solo cambia la fila inferior: 'Directo' (Streamer) o 'Pulir leccion' (Docente/
        Curso)."""
        if mode not in ("docente", "curso", "studio"):
            mode = "studio"
        if mode == getattr(self, "_mode", None):
            return
        # soltar el panel de pulido anterior (si lo habia)
        if self._polish_panel is not None:
            self._polish_panel.destroy()
            self._polish_panel = None
        if mode == "studio":
            self._directo_row.pack(fill="x", pady=(8, 0))   # mostrar fila Directo
            self._set_status("Modo Streamer: monta tu escena y emite desde la fila 'Directo'.")
        else:
            self._directo_row.pack_forget()                 # ocultar Directo
            self._polish_panel = PolishPanel(self._extras_holder, self, profile=mode)
            self._polish_panel.pack(fill="x", pady=(8, 0))
            if self.last_recording and Path(self.last_recording).is_file():
                self._polish_panel.set_source(self.last_recording)
            self._set_status("Modo " + ("Curso para YouTube: graba y conviertelo en curso."
                                        if mode == "curso" else
                                        "Docente: graba tu clase y pulsa 'Pulir leccion'."))
        self._mode = mode
        for key, btn in getattr(self, "_mode_btns", {}).items():
            btn.configure(style="Primary.TButton" if key == mode else "TButton")

    def _build_add_menu(self, btn: ttk.Menubutton) -> None:
        m = tk.Menu(btn, tearoff=0)
        mons = tk.Menu(m, tearoff=0)
        for mon in list_monitors():
            mons.add_command(label=mon.label, command=lambda r=mon: self._add_screen(r))
        m.add_cascade(label="Pantalla", menu=mons)
        m.add_command(label="Ventana de una aplicacion…", command=self._add_window_dialog)
        if self.video_devices:
            cams = tk.Menu(m, tearoff=0)
            for dev in self.video_devices:
                cams.add_command(label=dev, command=lambda d=dev: self._add_webcam(d))
            m.add_cascade(label="Webcam / Captura", menu=cams)
        m.add_command(label="Imagen (PNG/JPG)…", command=self._add_image)
        m.add_command(label="Texto…", command=self._add_text)
        m.add_command(label="Color / Fondo…", command=self._add_color)
        m.add_command(label="Video / Media…", command=self._add_media)
        btn["menu"] = m

    def _build_inspector(self, parent: ttk.Frame) -> None:
        self.var_x = tk.IntVar(value=0)
        self.var_y = tk.IntVar(value=0)
        self.var_w = tk.IntVar(value=0)
        self.var_h = tk.IntVar(value=0)
        self.var_op = tk.IntVar(value=100)
        self.var_shape = tk.StringVar(value="rect")
        # X/Y admiten negativos (sacar la fuente del lienzo), pero ancho y alto
        # NO: un tamano negativo llega a FFmpeg como «s=-100x200» y aborta la
        # grabacion entera (se perderia la toma).
        rows = [("X", self.var_x, -8000), ("Y", self.var_y, -8000),
                ("Ancho (0=auto)", self.var_w, 0), ("Alto (0=auto)", self.var_h, 0)]
        for i, (lab, var, minimo) in enumerate(rows):
            ttk.Label(parent, text=lab, style="CardMuted.TLabel").grid(row=i, column=0, sticky="w", pady=3)
            sp = ttk.Spinbox(parent, from_=minimo, to=8000, increment=10, textvariable=var,
                             width=10, command=self._apply_inspector)
            sp.grid(row=i, column=1, sticky="e", pady=3)
            sp.bind("<Return>", lambda e: self._apply_inspector())
            sp.bind("<FocusOut>", lambda e: self._apply_inspector())
        ttk.Label(parent, text="Forma", style="CardMuted.TLabel").grid(row=4, column=0, sticky="w", pady=3)
        shp = ttk.Frame(parent)
        shp.grid(row=4, column=1, sticky="e")
        ttk.Radiobutton(shp, text="Rect", value="rect", variable=self.var_shape,
                        command=self._apply_inspector).pack(side="left")
        ttk.Radiobutton(shp, text="Circulo", value="circle", variable=self.var_shape,
                        command=self._apply_inspector).pack(side="left")
        ttk.Label(parent, text="Opacidad %", style="CardMuted.TLabel").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Scale(parent, from_=10, to=100, variable=self.var_op, orient="horizontal",
                  command=lambda e: self._apply_inspector()).grid(row=5, column=1, sticky="ew", pady=3)
        self.var_chroma = tk.BooleanVar(value=False)
        ttk.Checkbutton(parent, text="Croma (quitar fondo verde)", variable=self.var_chroma,
                        command=self._apply_inspector).grid(row=6, column=0, columnspan=2,
                                                            sticky="w", pady=(6, 2))
        crop_row = ttk.Frame(parent)
        crop_row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.btn_crop = ttk.Button(crop_row, text="✂ Recortar…", command=self._crop_dialog)
        self.btn_crop.pack(side="left")
        self.lbl_crop = ttk.Label(crop_row, text="", style="CardMuted.TLabel")
        self.lbl_crop.pack(side="left", padx=(8, 0))
        # solo visible para fuentes de texto (se muestra/oculta en _load_inspector)
        self.btn_text = ttk.Button(parent, text="✎ Editar texto…", command=self._edit_text)
        self.btn_text.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.btn_text.grid_remove()
        parent.columnconfigure(1, weight=1)
        self._set_inspector_enabled(False)

    def _build_bottom(self) -> None:
        wrap = ttk.Frame(self._studio_view, padding=(12, 8))
        wrap.pack(fill="x", side="bottom")
        bar = ttk.Frame(wrap)
        bar.pack(fill="x")

        # Ajustes
        s = ttk.Frame(bar)
        s.pack(side="left")
        self.var_canvas = tk.StringVar(value=self.cfg.canvas)
        self.var_fps = tk.IntVar(value=self.cfg.fps)
        self.var_quality = tk.StringVar(value=self.cfg.video_quality)
        self.var_enc = tk.StringVar(value=self.cfg.encoder)
        self.var_sys = tk.BooleanVar(value=self.cfg.audio_system and AUDIO_OK)
        self.var_mic = tk.BooleanVar(value=self.cfg.audio_mic and AUDIO_OK)
        self.var_micdev = tk.StringVar(value=self.cfg.audio_mic_device)
        self.var_denoise = tk.BooleanVar(value=self.cfg.audio_denoise)

        def combo(label, var, values, width=18, cb=None):
            ttk.Label(s, text=label, style="Muted.TLabel").pack(side="left", padx=(0, 4))
            c = ttk.Combobox(s, textvariable=var, values=values, width=width, state="readonly")
            c.pack(side="left", padx=(0, 12))
            if cb:
                c.bind("<<ComboboxSelected>>", cb)
            return c

        combo("Lienzo", self.var_canvas, list(CANVAS_PRESETS.keys()), 20, self._on_canvas)
        combo("FPS", self.var_fps, [15, 24, 30, 60], 5, self._on_fps)
        combo("Calidad", self.var_quality, QUALITY_ORDER, 8, self._on_setting)
        enc_vals = ["auto"] + sorted(e for e in self.encoders if e.startswith("h264") or e == "libx264")
        self.cmb_enc = combo("Encoder", self.var_enc, enc_vals, 12, self._on_setting)

        au = ttk.Frame(bar)
        au.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(au, text="Audio sistema", variable=self.var_sys,
                        command=self._on_setting).pack(side="left")
        ttk.Checkbutton(au, text="Micro", variable=self.var_mic,
                        command=self._on_setting).pack(side="left", padx=(8, 4))
        # Siempre presente: la lista de micros llega en segundo plano
        # (_probe_ffmpeg_caps) y se rellena entonces via cmb_mic.config(values=…).
        self.cmb_mic = ttk.Combobox(au, textvariable=self.var_micdev, values=self.mics,
                                    width=16, state="readonly")
        self.cmb_mic.pack(side="left")
        self.cmb_mic.bind("<<ComboboxSelected>>", self._on_setting)
        ttk.Checkbutton(au, text="Reducir ruido", variable=self.var_denoise,
                        command=self._on_setting).pack(side="left", padx=(8, 0))

        # Medidores VU (monitor opcional, independiente de la grabacion)
        if meters.AVAILABLE:
            self.var_monitor = tk.BooleanVar(value=False)
            ttk.Checkbutton(au, text="🎚 Monitor", variable=self.var_monitor,
                            command=self._toggle_monitor).pack(side="left", padx=(12, 4))
            vu = ttk.Frame(au)
            vu.pack(side="left")
            self.vu_sys = self._make_vu(vu, "Sis")
            self.vu_mic = self._make_vu(vu, "Mic")

        # Controles de grabacion
        ctrl = ttk.Frame(bar)
        ctrl.pack(side="right")
        self.btn_rec = ttk.Button(ctrl, text="●  Grabar", style="Rec.TButton", command=self._toggle_record)
        self.btn_rec.pack(side="left")
        self.btn_pause = ttk.Button(ctrl, text="⏸ Pausa", command=self._toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=6)
        self.btn_sched = ttk.Button(ctrl, text="⏰ Programar", command=self._schedule_dialog)
        self.btn_sched.pack(side="left")

        # --- Fila inferior segun el MODO: 'Directo' (Streamer) o el panel de
        # 'Pulir leccion' (Docente/Curso). _set_mode decide cual mostrar aqui.
        self._extras_holder = ttk.Frame(wrap)
        self._extras_holder.pack(fill="x")
        row2 = self._directo_row = ttk.Frame(self._extras_holder)
        ttk.Label(row2, text="Directo:", style="H.TLabel").pack(side="left", padx=(0, 8))
        self.var_service = tk.StringVar(value=self.cfg.stream_service if self.cfg.stream_service in stream.SERVICES else "Twitch")
        ttk.Combobox(row2, textvariable=self.var_service, values=list(stream.SERVICES.keys()),
                     width=24, state="readonly").pack(side="left")
        ttk.Label(row2, text="Clave / URL:", style="Muted.TLabel").pack(side="left", padx=(10, 4))
        self.var_streamkey = tk.StringVar(value=self.cfg.stream_key)
        ttk.Entry(row2, textvariable=self.var_streamkey, width=32, show="•").pack(side="left")
        self.var_vod = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="Grabar VOD", variable=self.var_vod).pack(side="left", padx=(10, 0))
        ttk.Button(row2, text="Destinos…", command=self._ask_extra_destinations).pack(side="left", padx=(10, 0))
        ttk.Button(row2, text="Probar", command=self._test_rtmp).pack(side="left", padx=(6, 0))
        self.btn_replay = ttk.Button(row2, text="⏺ Buffer replay", command=self._toggle_replay)
        self.btn_replay.pack(side="left", padx=(10, 0))
        self.btn_stream = ttk.Button(row2, text="▶ Emitir en directo", style="Primary.TButton",
                                     command=self._toggle_stream)
        self.btn_stream.pack(side="right")
        self.lbl_stream = ttk.Label(row2, text="", style="Muted.TLabel")
        self.lbl_stream.pack(side="right", padx=(0, 12))

    # -- añadir / quitar fuentes ------------------------------------------
    def _add_screen(self, mon) -> None:
        self.scene.add(scn.screen_source(mon.region, name=mon.label.split(":")[0]))
        self._refresh_source_list(select_last=True)

    def _add_webcam(self, dev: str) -> None:
        x = self.scene.canvas_w - 400
        y = self.scene.canvas_h - 400
        self.scene.add(scn.webcam_source(dev, x=x, y=y, size=340, circle=True))
        self._refresh_source_list(select_last=True)

    def _add_window_dialog(self) -> None:
        """Elige una de las ventanas abiertas para grabar solo esa aplicacion."""
        wins = winlist.list_windows(exclude_titles=(self.title(),))
        if not wins:
            messagebox.showinfo(APP_NAME, "No se encontraron ventanas de aplicaciones "
                                          "abiertas. Abre la app que quieras grabar.")
            return
        win = tk.Toplevel(self)
        win.title("Elegir ventana a grabar")
        win.transient(self)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Se grabara solo esa ventana (aunque quede detras de otras). "
                            "Luego puedes recortarla para quitar barras o pestanas.",
                  style="Muted.TLabel", wraplength=460, justify="left").pack(anchor="w", pady=(0, 8))
        lb = tk.Listbox(frm, height=min(14, len(wins)), width=64, activestyle="none",
                        font=(theme.FONT, 10), exportselection=False)
        for t, r in wins:
            lb.insert(tk.END, f"{t}   ·   {r[2]}x{r[3]}")
        lb.selection_set(0)
        lb.pack(fill="both", expand=True)
        lb.bind("<Double-Button-1>", lambda e: _ok())

        def _ok():
            sel = lb.curselection()
            if not sel:
                return
            title = wins[sel[0]][0]
            if winlist.count_title(title) > 1 and not messagebox.askyesno(
                    APP_NAME, f"Hay varias ventanas llamadas «{title}». Se grabara "
                              "la que este mas al frente (Windows no permite elegir "
                              "otra por el titulo). ¿Continuar?"):
                return
            # se guarda tambien el HWND (+PID) para SEGUIR la ventana aunque
            # cambie de titulo (navegadores, etc.).
            hwnd = winlist.hwnd_for(title)
            self.scene.add(scn.window_source(title, hwnd=hwnd,
                                             pid=winlist.pid_of(hwnd) if hwnd else None))
            self._refresh_source_list(select_last=True)
            win.destroy()

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="Anadir", style="Primary.TButton", command=_ok).pack(side="right")
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="right", padx=(0, 6))
        theme.center_window(win)
        win.grab_set()

    # -- captura WGC de ventanas (preview / recorte) ----------------------
    def _wgc_frame(self, s):
        """Ultimo fotograma WGC (PIL RGB) de la ventana de la fuente s, o None si
        WGC no esta disponible o falla. SIGUE la ventana por HWND (aunque cambie
        de titulo) y mantiene una sesion persistente por fuente."""
        if not wincap.available():
            return None
        hwnd = winlist.resolve_window(s.params)
        if not hwnd:
            # la ventana no esta (minimizada/cerrada): suelta el grabber viejo
            gr = self._win_grabbers.pop(s.id, None)
            if gr is not None:
                try:
                    gr.stop()
                except Exception:  # noqa: BLE001
                    pass
            return None
        # mantener fresco en la sesion (sobrevive cambios de titulo)
        s.params["hwnd"] = hwnd
        s.params["pid"] = winlist.pid_of(hwnd)
        gr = self._win_grabbers.get(s.id)
        if gr is None or not gr.alive or gr.hwnd != hwnd:
            if gr is not None:
                try:
                    gr.stop()
                except Exception:  # noqa: BLE001
                    pass
            gr = wincap.WindowGrabber(hwnd)
            try:
                ok = gr.start()
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                self._win_grabbers.pop(s.id, None)
                return None
            self._win_grabbers[s.id] = gr
        return gr.frame()

    def _prune_grabbers(self) -> None:
        """Cierra sesiones WGC de fuentes de ventana que ya no estan visibles."""
        if not self._win_grabbers:
            return
        active = {s.id for s in self.scene.visible_sorted() if s.kind == scn.KIND_WINDOW}
        for sid in [k for k in self._win_grabbers if k not in active]:
            try:
                self._win_grabbers.pop(sid).stop()
            except Exception:  # noqa: BLE001
                pass

    # -- recorte de una fuente --------------------------------------------
    def _grab_source_frame(self, s):
        """Un fotograma (PIL RGB, sin recortar) de la fuente, para el dialogo de
        recorte. None si no se puede capturar."""
        from PIL import Image
        try:
            if s.kind == scn.KIND_WINDOW:
                # 1a opcion: WGC (a prueba de oclusion), la MISMA superficie de
                # ventana que graba el motor -> el recorte coincide al pixel.
                img = self._wgc_frame(s)
                if img is not None:
                    return img
                # Respaldo (WGC no disponible): mss del area cliente de la MISMA
                # ventana (por HWND, aunque haya cambiado de titulo).
                hwnd = winlist.resolve_window(s.params)
                rect = winlist.client_rect(hwnd) if hwnd else None
                if not rect:
                    return None
                region = {"left": rect[0], "top": rect[1], "width": rect[2], "height": rect[3]}
            elif s.kind == scn.KIND_SCREEN:
                p = s.params
                region = {"left": p["left"], "top": p["top"],
                          "width": p["width"], "height": p["height"]}
            elif s.kind == scn.KIND_IMAGE:
                return Image.open(s.params["path"]).convert("RGB")
            elif s.kind == scn.KIND_MEDIA:
                return self._first_media_frame(s.params.get("path", ""))
            else:
                return None
            if self._mss is None:
                import mss
                self._mss = mss.mss()
            g = self._mss.grab(region)
            return Image.frombytes("RGB", g.size, g.bgra, "raw", "BGRX")
        except Exception as exc:  # noqa: BLE001
            logger.warning("no se pudo capturar fotograma de %s: %s", s.kind, exc)
            return None

    def _first_media_frame(self, path: str):
        from PIL import Image
        if not path or not self.ffmpeg:
            return None
        out = str(work_dir() / ".cs_cropframe.png")
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", path,
               "-frames:v", "1", out]
        import subprocess
        from octonove_core.procutil import subprocess_kwargs
        try:
            subprocess.run(cmd, timeout=20, **subprocess_kwargs())
            return Image.open(out).convert("RGB")
        except Exception:  # noqa: BLE001
            return None

    def _crop_dialog(self) -> None:
        s = self._selected()
        if not s:
            return
        frame = self._grab_source_frame(s)
        if frame is None:
            messagebox.showinfo(APP_NAME, "No se pudo capturar la fuente para recortar "
                                          "(¿la ventana esta minimizada o cerrada?).")
            return
        fw, fh = frame.size
        MAXW, MAXH = 820, 480
        sc = min(MAXW / fw, MAXH / fh, 1.0)
        dw, dh = int(fw * sc), int(fh * sc)
        from PIL import ImageTk
        disp = frame.resize((dw, dh))

        win = tk.Toplevel(self)
        win.title(f"Recortar: {s.label()}")
        win.transient(self)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Arrastra para marcar la zona que quieres GRABAR "
                            "(p. ej. solo el contenido, sin barras ni pestanas).",
                  style="Muted.TLabel", wraplength=dw, justify="left").pack(anchor="w", pady=(0, 6))
        cv = tk.Canvas(frm, width=dw, height=dh, highlightthickness=1,
                       highlightbackground=theme.BORDER, cursor="crosshair")
        cv.pack()
        imgtk = ImageTk.PhotoImage(disp)
        cv.create_image(0, 0, anchor="nw", image=imgtk)
        cv._imgtk = imgtk       # evitar recoleccion

        # rectangulo inicial = recorte actual (en coords de pantalla escaladas) o todo
        if s.transform.crop:
            cx, cy, cw2, ch2 = s.transform.crop
            r0 = (cx * sc, cy * sc, (cx + cw2) * sc, (cy + ch2) * sc)
        else:
            r0 = (0, 0, dw, dh)
        st = {"rect": list(r0), "drawing": False, "id": None, "sh": []}

        def redraw():
            for h in st["sh"]:
                cv.delete(h)
            st["sh"] = []
            x0, y0, x1, y1 = st["rect"]
            x0, x1 = sorted((x0, x1)); y0, y1 = sorted((y0, y1))
            # oscurecer fuera del recorte
            for a, b, c2, d in ((0, 0, dw, y0), (0, y1, dw, dh), (0, y0, x0, y1), (x1, y0, dw, y1)):
                if c2 > a and d > b:
                    st["sh"].append(cv.create_rectangle(a, b, c2, d, fill="#000000",
                                                        stipple="gray50", outline=""))
            st["sh"].append(cv.create_rectangle(x0, y0, x1, y1, outline="#38BDF8", width=2))

        def press(e):
            st["rect"] = [e.x, e.y, e.x, e.y]; st["drawing"] = True; redraw()

        def drag(e):
            if st["drawing"]:
                st["rect"][2] = max(0, min(dw, e.x)); st["rect"][3] = max(0, min(dh, e.y)); redraw()

        def release(_e):
            st["drawing"] = False
        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        redraw()

        def aplicar():
            x0, y0, x1, y1 = st["rect"]
            x0, x1 = sorted((x0, x1)); y0, y1 = sorted((y0, y1))
            if x1 - x0 < 8 or y1 - y0 < 8:
                messagebox.showinfo(APP_NAME, "Marca una zona mas grande para recortar.")
                return
            # de coords mostradas a pixeles de la fuente
            cropx, cropy = int(x0 / sc), int(y0 / sc)
            cropw, croph = int((x1 - x0) / sc), int((y1 - y0) / sc)
            # si abarca casi todo el fotograma, es 'sin recorte' (no un crop=todo,
            # que ademas seria un no-op y arriesgaria desbordes)
            if cropx <= 2 and cropy <= 2 and cropw >= fw - 4 and croph >= fh - 4:
                s.transform.crop = None
            else:
                s.transform.crop = (cropx, cropy, cropw, croph)
            self._preview_dirty = True
            self._load_inspector(s)
            win.destroy()

        def quitar():
            s.transform.crop = None
            self._preview_dirty = True
            self._load_inspector(s)
            win.destroy()

        bar = ttk.Frame(frm); bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="Aplicar recorte", style="Primary.TButton", command=aplicar).pack(side="right")
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Quitar recorte", command=quitar).pack(side="left")
        theme.center_window(win)
        win.grab_set()

    def _add_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Elegir imagen", filetypes=[("Imagenes", "*.png *.jpg *.jpeg *.bmp *.webp")])
        if path:
            self.scene.add(scn.image_source(path, x=40, y=40, w=320))
            self._refresh_source_list(select_last=True)

    def _text_dialog(self, existing=None):
        """Crear/editar una fuente de texto: contenido, tamano, color del texto,
        y fondo (con su propio color y una opacidad INDEPENDIENTE de la de la
        capa, para poder tener texto opaco sobre un fondo tenue)."""
        p = existing.params if existing else {}
        cur = {"color": p.get("color", "#FFFFFF"), "bg": p.get("bg") or "#1E3A5F"}
        win = tk.Toplevel(self)
        win.title("Editar texto" if existing else "Nuevo texto")
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Texto:").grid(row=0, column=0, sticky="w", pady=4)
        var_text = tk.StringVar(value=p.get("text", "Texto de prueba"))
        ent = ttk.Entry(frm, textvariable=var_text, width=34)
        ent.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(frm, text="Tamaño:").grid(row=1, column=0, sticky="w", pady=4)
        var_size = tk.IntVar(value=int(p.get("size", 48)))
        ttk.Spinbox(frm, from_=8, to=400, increment=2, textvariable=var_size,
                    width=8).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="Color del texto:").grid(row=2, column=0, sticky="w", pady=4)
        sw_text = tk.Label(frm, width=4, bg=cur["color"], relief="solid", bd=1)
        sw_text.grid(row=2, column=1, sticky="w", padx=6)

        def pick_text():
            _, hx = colorchooser.askcolor(color=cur["color"], parent=win, title="Color del texto")
            if hx:
                cur["color"] = hx
                sw_text.config(bg=hx)
        ttk.Button(frm, text="Elegir…", command=pick_text).grid(row=2, column=2, sticky="w")

        var_bg = tk.BooleanVar(value=(p.get("bg") is not None) if existing else True)
        ttk.Checkbutton(frm, text="Fondo detrás del texto", variable=var_bg,
                        command=lambda: _sync()).grid(row=3, column=0, columnspan=3,
                                                      sticky="w", pady=(10, 2))

        ttk.Label(frm, text="Color del fondo:").grid(row=4, column=0, sticky="w", pady=4)
        sw_bg = tk.Label(frm, width=4, bg=cur["bg"], relief="solid", bd=1)
        sw_bg.grid(row=4, column=1, sticky="w", padx=6)

        def pick_bg():
            _, hx = colorchooser.askcolor(color=cur["bg"], parent=win, title="Color del fondo")
            if hx:
                cur["bg"] = hx
                sw_bg.config(bg=hx)
        btn_bg = ttk.Button(frm, text="Elegir…", command=pick_bg)
        btn_bg.grid(row=4, column=2, sticky="w")

        ttk.Label(frm, text="Opacidad del fondo %:").grid(row=5, column=0, sticky="w", pady=4)
        var_alpha = tk.DoubleVar(value=float(p.get("bg_alpha", 86)))
        sc_alpha = ttk.Scale(frm, from_=0, to=100, variable=var_alpha, orient="horizontal")
        sc_alpha.grid(row=5, column=1, columnspan=2, sticky="ew")

        def _sync():
            st = "normal" if var_bg.get() else "disabled"
            btn_bg.config(state=st)
            sc_alpha.config(state=st)
        _sync()

        out = {}

        def ok():
            t = var_text.get().strip()
            if not t:
                messagebox.showinfo(APP_NAME, "Escribe algún texto.", parent=win)
                return
            # _ival tolera el campo vacio/no numerico (un Spinbox lo permite) sin
            # reventar int() al Aceptar; se acota a un rango razonable.
            out.update({"text": t, "size": self._ival(var_size, 8, 400, 48),
                        "color": cur["color"],
                        "bg": cur["bg"] if var_bg.get() else None,
                        "bg_alpha": max(0, min(100, int(round(var_alpha.get()))))})
            win.destroy()
        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancelar", command=win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Aceptar", command=ok).pack(side="right")
        frm.columnconfigure(1, weight=1)
        ent.focus_set()
        ent.selection_range(0, "end")
        win.grab_set()
        self.wait_window(win)
        return out or None

    def _add_text(self) -> None:
        params = self._text_dialog()
        if params:
            self.scene.add(scn.text_source(
                params["text"], x=60, y=self.scene.canvas_h - 140,
                size=params["size"], color=params["color"],
                bg=params["bg"], bg_alpha=params["bg_alpha"]))
            self._refresh_source_list(select_last=True)

    def _edit_text(self) -> None:
        s = self._selected()
        if not s or s.kind != scn.KIND_TEXT:
            return
        params = self._text_dialog(s)
        if params:
            s.params.update(params)
            self._preview_dirty = True
            self._load_inspector(s)

    def _add_color(self) -> None:
        rgb, hx = colorchooser.askcolor(color="#1E3A5F", parent=self)
        if hx:
            self.scene.add(scn.color_source(hx, self.scene.canvas_w, self.scene.canvas_h))
            self._refresh_source_list(select_last=True)

    def _add_media(self) -> None:
        path = filedialog.askopenfilename(
            title="Elegir video", filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi")])
        if path:
            src = scn.Source(kind=scn.KIND_MEDIA, name="Video", params={"path": path},
                             transform=scn.Transform(x=0, y=0, w=640))
            self.scene.add(src)
            self._refresh_source_list(select_last=True)

    def _remove_source(self) -> None:
        if self._sel_id is not None:
            self.scene.remove(self._sel_id)
            self._sel_id = None
            self._refresh_source_list()
            self._set_inspector_enabled(False)
            self.btn_text.grid_remove()

    def _reorder(self, direction: int) -> None:
        if self._sel_id is None:
            return
        (self.scene.raise_ if direction > 0 else self.scene.lower)(self._sel_id)
        self._refresh_source_list(keep_sel=True)

    def _toggle_visible(self) -> None:
        s = self._selected()
        if s:
            s.visible = not s.visible
            self._refresh_source_list(keep_sel=True)

    # -- lista / seleccion -------------------------------------------------
    def _ordered(self) -> list[scn.Source]:
        # mostrar de arriba (z alto) a abajo
        return sorted(self.scene.sources, key=lambda s: -s.z)

    def _refresh_source_list(self, select_last=False, keep_sel=False) -> None:
        prev = self._sel_id
        self.src_list.delete(0, tk.END)
        self._row_ids: list[int] = []
        for s in self._ordered():
            mark = "  " if s.visible else "✕ "
            self.src_list.insert(tk.END, f"{mark}{s.label()}  ·  {scn.KIND_LABELS.get(s.kind, s.kind)}")
            self._row_ids.append(s.id)
        if select_last and self._row_ids:
            self._select_id(self._ordered()[0].id)
        elif keep_sel and prev in self._row_ids:
            self._select_id(prev)
        self._preview_dirty = True

    def _select_id(self, sid: int) -> None:
        if sid in self._row_ids:
            i = self._row_ids.index(sid)
            self.src_list.selection_clear(0, tk.END)
            self.src_list.selection_set(i)
            self._sel_id = sid
            self._load_inspector(self._selected())

    def _on_select_source(self, _evt=None) -> None:
        sel = self.src_list.curselection()
        if not sel:
            return
        self._sel_id = self._row_ids[sel[0]]
        self._load_inspector(self._selected())

    def _selected(self) -> scn.Source | None:
        return next((s for s in self.scene.sources if s.id == self._sel_id), None)

    def _load_inspector(self, s: scn.Source | None) -> None:
        if not s:
            self._set_inspector_enabled(False)
            self.btn_text.grid_remove()   # no dejar el boton de texto en gris sin seleccion
            return
        self._loading = True
        try:
            self.var_x.set(s.transform.x)
            self.var_y.set(s.transform.y)
            self.var_w.set(s.transform.w)
            self.var_h.set(s.transform.h)
            self.var_op.set(int(s.transform.opacity * 100))
            self.var_shape.set(s.transform.shape)
            self.var_chroma.set(bool(s.transform.chroma))
            self.vis_btn.config(text="Mostrar" if not s.visible else "Ocultar")
            self._set_inspector_enabled(True)
            # el recorte solo aplica a fuentes de imagen real (pantalla/ventana/foto/video)
            cropable = s.kind in (scn.KIND_SCREEN, scn.KIND_WINDOW, scn.KIND_IMAGE, scn.KIND_MEDIA)
            self.btn_crop.config(state="normal" if cropable else "disabled")
            c = s.transform.crop
            self.lbl_crop.config(text=(f"recorte {c[2]}×{c[3]}" if c else ("" if cropable else "no aplica")))
            if s.kind == scn.KIND_TEXT:
                self.btn_text.grid()
                self.btn_text.config(state="normal")
            else:
                self.btn_text.grid_remove()
        finally:
            # sin el finally, un fallo aqui dejaba _loading en True para siempre
            # y el inspector no volvia a aplicar nada (en silencio).
            self._loading = False

    def _set_inspector_enabled(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        for child in self.insp.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                for sub in child.winfo_children():
                    try:
                        sub.configure(state=state)
                    except tk.TclError:
                        pass

    @staticmethod
    def _ival(var, minimo: int = -8000, maximo: int = 8000, default: int = 0) -> int:
        """Lee un IntVar tolerando que el campo este vacio o con texto: un
        Spinbox con -textvariable escribe la variable en CADA tecla, y un get()
        con «» lanzaba TclError que abortaba _apply_inspector a medias y dejaba
        TODO el inspector inerte hasta reiniciar."""
        try:
            v = int(var.get())
        except (tk.TclError, ValueError, TypeError):
            return default
        return max(minimo, min(maximo, v))

    def _apply_inspector(self) -> None:
        if getattr(self, "_loading", False):
            return
        s = self._selected()
        if not s:
            return
        t = s.transform
        t.x, t.y = self._ival(self.var_x), self._ival(self.var_y)
        t.w, t.h = self._ival(self.var_w, 0), self._ival(self.var_h, 0)
        # la forma se asigna ANTES de cuadrar la caja: si se leia la forma vieja,
        # al marcar «Circulo» hacia falta un segundo cambio para que el circulo
        # se cuadrase (y parecia que la forma no hacia nada).
        t.shape = self.var_shape.get()
        if t.shape == "circle":
            # el circulo necesita caja cuadrada Y con tamano: si falta un lado se
            # deriva del otro, y si no hay ninguno se da uno por defecto (antes
            # marcar «Circulo» con tamano automatico no hacia nada visible).
            lado = t.w or t.h or max(80, min(self.scene.canvas_w, self.scene.canvas_h) // 3)
            t.w = t.h = int(lado)
            self._sync_size_vars(t)
        t.opacity = max(0.1, min(1.0, self._ival(self.var_op, 10, 100, 100) / 100.0))
        t.chroma = "#00D000" if self.var_chroma.get() else None
        self._preview_dirty = True

    def _sync_size_vars(self, t) -> None:
        """Refleja en el inspector un tamano ajustado por la app (sin re-aplicar)."""
        if self._ival(self.var_w, 0) == t.w and self._ival(self.var_h, 0) == t.h:
            return
        self._loading = True
        try:
            self.var_w.set(t.w)
            self.var_h.set(t.h)
        finally:
            self._loading = False

    # -- ajustes -----------------------------------------------------------
    def _on_canvas(self, _evt=None) -> None:
        cw, ch = CANVAS_PRESETS.get(self.var_canvas.get(), (1920, 1080))
        self.scene.canvas_w, self.scene.canvas_h = cw, ch
        self._preview_dirty = True
        self._on_setting()

    def _on_fps(self, _evt=None) -> None:
        self.scene.fps = int(self.var_fps.get())
        self._on_setting()

    def _on_setting(self, _evt=None) -> None:
        self.cfg.canvas = self.var_canvas.get()
        self.cfg.fps = int(self.var_fps.get())
        self.cfg.video_quality = self.var_quality.get()
        self.cfg.encoder = self.var_enc.get()
        self.cfg.audio_system = bool(self.var_sys.get())
        self.cfg.audio_mic = bool(self.var_mic.get())
        self.cfg.audio_mic_device = self.var_micdev.get()
        self.cfg.audio_denoise = bool(self.var_denoise.get())
        save_config(self.cfg)

    # -- preview -----------------------------------------------------------
    def _preview_loop(self) -> None:
        # El estudio (y su preview) esta visible en los 3 modos: renderizamos siempre.
        # Al grabar bajamos la cadencia para no competir por CPU con la captura.
        try:
            self._render_preview()
            self._prune_grabbers()
        except Exception as exc:  # noqa: BLE001
            logger.debug("preview: %s", exc)
        delay = 450 if (self.engine and self.engine.state == "recording") else 160
        self.after(delay, self._preview_loop)

    def _render_preview(self) -> None:
        from PIL import Image, ImageDraw, ImageTk
        import mss

        cw, ch = self.scene.canvas_w, self.scene.canvas_h
        canvas = Image.new("RGBA", (cw, ch), fu._hex_to_rgba(self.scene.bg_color.replace("0x", "#")))

        if self._mss is None:
            self._mss = mss.mss()
        boxes: dict[int, tuple[float, float, float, float]] = {}
        for s in self.scene.visible_sorted():
            t = s.transform
            try:
                # 1) contenido real de la fuente (None -> se dibuja un marcador)
                img = None
                if s.kind == scn.KIND_SCREEN:
                    p = s.params
                    grab = self._mss.grab({"left": p["left"], "top": p["top"],
                                           "width": p["width"], "height": p["height"]})
                    img = self._crop_img(Image.frombytes("RGB", grab.size, grab.bgra,
                                                         "raw", "BGRX"), t.crop)
                elif s.kind == scn.KIND_WINDOW:
                    got = self._grab_source_frame(s)   # WGC (=lo que graba el motor)
                    img = self._crop_img(got.convert("RGB"), t.crop) if got is not None else None
                elif s.kind == scn.KIND_IMAGE:
                    img = self._crop_img(Image.open(s.params["path"]).convert("RGBA"), t.crop)
                elif s.kind == scn.KIND_COLOR:
                    # el color se genera ya al tamano final (t.w/t.h o el lienzo),
                    # igual que hace _source_input.
                    img = Image.new("RGBA", (max(2, t.w or cw), max(2, t.h or ch)),
                                    fu._hex_to_rgba(s.params.get("color", "#1E3A5F")))
                elif s.kind == scn.KIND_TEXT:
                    img = Image.open(fu.render_text_png(
                        s.params.get("text", ""), int(s.params.get("size", 48)),
                        s.params.get("color", "#FFFFFF"), s.params.get("bg"),
                        work_dir(), name_hint=str(s.id),
                        bg_alpha=int(s.params.get("bg_alpha", 86)))).convert("RGBA")
                if img is None:                     # webcam/media/ventana no disponible
                    w, h = self._draw_placeholder(canvas, s, cw, ch)
                    boxes[s.id] = ((0, 0) if fu.fills_canvas(s) else (t.x, t.y)) + (w, h)
                    continue
                # 2) misma decision que build_scene: encajar en el lienzo o capa
                if fu.fills_canvas(s):
                    img = self._fit(img, cw, ch)
                    ox, oy = (cw - img.width) // 2, (ch - img.height) // 2
                else:
                    img = self._fit_preview(img, s)
                    ox, oy = t.x, t.y
                canvas.alpha_composite(img.convert("RGBA"), (ox, oy))
                boxes[s.id] = (ox, oy, img.width, img.height)
            except Exception as exc:  # noqa: BLE001
                logger.debug("preview src %s: %s", s.kind, exc)
        self._boxes = boxes

        ox, oy, pw, ph = self._pv_rect()
        prev = canvas.resize((pw, ph))
        self._preview_imgtk = ImageTk.PhotoImage(prev)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._preview_imgtk)
        self._draw_selection_handles()
        badge = None
        if self.engine and self.engine.state in ("recording", "paused"):
            t = self._fmt_elapsed(self._rec_t0)
            badge = ("● REC  " + t) if self.engine.state == "recording" else ("❚❚ PAUSA  " + t)
        elif self.replay and self.replay.state == "buffering":
            badge = "⏺ BUFFER"
        if badge:
            self.canvas.create_text(ox + 12, oy + 12, anchor="nw", text=badge, fill="#EF4444",
                                    font=(theme.FONT, 14, "bold"))
        if self.stream_engine and self.stream_engine.state == "streaming":
            drop = getattr(self.stream_engine, "dropped", 0)
            self.lbl_stream.config(
                text=f"● EN DIRECTO  {self._fmt_elapsed(self._stream_t0)} · caidos: {drop}",
                foreground=theme.REC)

    def _fmt_elapsed(self, t0) -> str:
        if not t0:
            return "00:00"
        s = int(max(0, time.time() - t0))
        h, m, sec = s // 3600, (s % 3600) // 60, s % 60
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    def _fit(self, img, cw, ch):
        r = min(cw / img.width, ch / img.height)
        return img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))))

    @staticmethod
    def _crop_img(img, crop):
        """Recorta (x,y,w,h) en pixeles de la fuente, acotado a la imagen."""
        if not crop:
            return img
        x, y, w, h = crop
        x = max(0, min(int(x), img.width - 1))
        y = max(0, min(int(y), img.height - 1))
        w = max(1, min(int(w), img.width - x))
        h = max(1, min(int(h), img.height - y))
        return img.crop((x, y, x + w, y + h))

    def _fit_preview(self, img, s):
        """Aplica a la imagen la MISMA geometria que aplicara FFmpeg al grabar
        (ver _layer_chain), para que la vista previa y el video coincidan: sin
        esto se ajustaba el tamano y el preview no cambiaba (y no se podia
        colocar la fuente porque su caja no era la real)."""
        from PIL import Image, ImageDraw
        t = s.transform
        tw, th = max(0, int(t.w or 0)), max(0, int(t.h or 0))   # igual que _layer_chain
        if tw > 0 and th > 0:
            if s.kind == scn.KIND_TEXT:
                # el texto se ajusta DENTRO de la caja y se centra: nunca se
                # recorta (antes se forzaba a la caja y se cortaban las letras).
                sc = min(tw / img.width, th / img.height)
                nw, nh = max(1, int(img.width * sc)), max(1, int(img.height * sc))
                fit = img.resize((nw, nh))
                box = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
                box.alpha_composite(fit, ((tw - nw) // 2, (th - nh) // 2))
                img = box
            else:
                # rellena la caja y recorta el sobrante (como object-fit: cover)
                sc = max(tw / img.width, th / img.height)
                nw, nh = max(1, int(img.width * sc)), max(1, int(img.height * sc))
                img = img.resize((nw, nh)).crop(((nw - tw) // 2, (nh - th) // 2,
                                                (nw - tw) // 2 + tw, (nh - th) // 2 + th))
        elif tw > 0:
            img = img.resize((tw, max(1, int(img.height * tw / img.width))))
        elif th > 0:
            img = img.resize((max(1, int(img.width * th / img.height)), th))
        # Mismas condiciones que _layer_chain: la mascara circular solo se aplica
        # con ancho Y alto fijados, y en ese caso el video NO aplica opacidad
        # (son ramas excluyentes). Replicarlo evita que el preview mienta.
        circulo = (t.shape == "circle" and tw > 0 and th > 0 and not t.chroma)
        if circulo:
            from PIL import ImageChops
            mask = Image.new("L", img.size, 0)
            ImageDraw.Draw(mask).ellipse([2, 2, img.width - 2, img.height - 2], fill=255)
            img = img.copy()
            # multiplica (no reemplaza) el alfa, igual que el video
            img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
        elif t.opacity < 1.0:
            op = max(0.0, min(1.0, t.opacity))
            img = img.copy()
            img.putalpha(img.getchannel("A").point(lambda v: int(v * op)))
        return img

    def _draw_placeholder(self, canvas, s, cw: int = 0, ch: int = 0):
        """Marcador para fuentes sin contenido en el preview (webcam, video, o una
        ventana no disponible). Respeta la misma decision que build_scene: si la
        fuente se encaja en el lienzo, el marcador lo ocupa entero."""
        from PIL import ImageDraw
        t = s.transform
        if fu.fills_canvas(s) and cw and ch:
            x, y, w, h = 0, 0, cw, ch
        else:
            x, y = t.x, t.y
            w = t.w or 340
            h = t.h or (w if t.shape == "circle" else int(w * 9 / 16))
        d = ImageDraw.Draw(canvas)
        box = [x, y, x + w, y + h]
        if t.shape == "circle":
            d.ellipse(box, fill=(206, 110, 97, 90), outline=(206, 110, 97, 255), width=4)
        else:
            d.rounded_rectangle(box, radius=14, fill=(30, 58, 95, 110),
                                outline=(110, 193, 228, 255), width=4)
        d.text((x + 14, y + h // 2 - 10), s.label(), fill=(255, 255, 255, 255))
        return w, h

    def _pv_rect(self) -> tuple[int, int, int, int]:
        """(ox, oy, ancho, alto) del preview DENTRO del widget: escalado al
        tamano real del canvas manteniendo la proporcion y CENTRADO. Antes se
        dibujaba fijo a 640x360 anclado arriba-izquierda, y al maximizar la
        ventana quedaba 'pegado a la esquina' con un mar oscuro alrededor."""
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 40 or ch < 40:      # el layout aun no dio tamano: usa el minimo
            return 0, 0, PREVIEW_W, PREVIEW_H
        r = min(cw / self.scene.canvas_w, ch / self.scene.canvas_h)
        pw = max(1, int(self.scene.canvas_w * r))
        ph = max(1, int(self.scene.canvas_h * r))
        return (cw - pw) // 2, (ch - ph) // 2, pw, ph

    # -- preview interactivo (arrastrar / redimensionar) ------------------
    def _to_canvas(self, px, py):
        ox, oy, pw, ph = self._pv_rect()
        return ((px - ox) * self.scene.canvas_w / pw, (py - oy) * self.scene.canvas_h / ph)

    def _to_preview(self, cx, cy):
        ox, oy, pw, ph = self._pv_rect()
        return (cx * pw / self.scene.canvas_w + ox, cy * ph / self.scene.canvas_h + oy)

    def _draw_selection_handles(self) -> None:
        if not self._sel_id or self._sel_id not in self._boxes:
            return
        x, y, w, h = self._boxes[self._sel_id]
        x0, y0 = self._to_preview(x, y)
        x1, y1 = self._to_preview(x + w, y + h)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=theme.PRIMARY, width=2)
        for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            self.canvas.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4,
                                         fill=theme.PRIMARY, outline=theme.WHITE)

    def _on_canvas_press(self, e) -> None:
        cx, cy = self._to_canvas(e.x, e.y)
        if self._sel_id and self._sel_id in self._boxes:
            bx, by, bw, bh = self._boxes[self._sel_id]
            hx, hy = self._to_preview(bx + bw, by + bh)
            if abs(e.x - hx) <= 8 and abs(e.y - hy) <= 8:    # esquina = redimensionar
                self._drag = ("resize", self._sel_id, cx, cy, bw, bh)
                return
        for s in sorted(self.scene.visible_sorted(), key=lambda z: -z.z):  # topmost primero
            if s.id in self._boxes:
                bx, by, bw, bh = self._boxes[s.id]
                if bx <= cx <= bx + bw and by <= cy <= by + bh:
                    self._select_id(s.id)
                    self._drag = ("move", s.id, cx, cy, s.transform.x, s.transform.y)
                    return
        self._drag = None

    def _on_canvas_drag(self, e) -> None:
        if not self._drag:
            return
        mode, sid, cx0, cy0, a, b = self._drag
        cx, cy = self._to_canvas(e.x, e.y)
        s = next((x for x in self.scene.sources if x.id == sid), None)
        if not s:
            return
        if mode == "move":
            s.transform.x = int(a + (cx - cx0))
            s.transform.y = int(b + (cy - cy0))
        else:  # resize
            neww = max(40, int(a + (cx - cx0)))
            s.transform.w = neww
            if s.transform.shape == "circle":
                s.transform.h = neww
            elif s.kind == scn.KIND_COLOR:
                s.transform.h = max(40, int(b + (cy - cy0)))
        self._preview_dirty = True

    def _on_canvas_release(self, e) -> None:
        if self._drag:
            self._drag = None
            s = self._selected()
            if s:
                self._load_inspector(s)

    # -- grabacion ---------------------------------------------------------
    def _toggle_record(self) -> None:
        if self.engine and self.engine.state in ("recording", "paused"):
            self._set_status("Finalizando…")
            self.btn_rec.config(state="disabled")
            self.btn_pause.config(state="disabled")
            self.engine.stop()
            return
        if not self.ffmpeg:
            messagebox.showerror(APP_NAME, "FFmpeg no disponible."); return
        if not self.scene.visible_sorted():
            messagebox.showwarning(APP_NAME, "Anade al menos una fuente."); return
        # Una fuente de VENTANA no localizable (minimizada, cerrada o con el titulo
        # cambiado) haria fallar a gdigrab y, como todas las fuentes van en UN solo
        # proceso FFmpeg, se perderia TODA la grabacion. Se avisa antes de empezar.
        # se valida CAPTURABILIDAD (area cliente real), no mera existencia:
        # client_rect devuelve None para ventanas minimizadas -> asi SI se avisa
        # (antes una minimizada pasaba y acababa grabando la esquina del escritorio).
        faltan = [s.params.get("title", "") for s in self.scene.visible_sorted()
                  if s.kind == scn.KIND_WINDOW
                  and not winlist.client_rect(winlist.resolve_window(s.params))]
        if faltan:
            messagebox.showwarning(APP_NAME, "No se puede grabar: la(s) ventana(s) "
                                   + ", ".join(f"«{t}»" for t in faltan)
                                   + " no estan disponibles (¿minimizadas o cerradas?). "
                                   "Restauralas o quitalas de la escena.")
            return
        if self._webcam_conflict("rec"):
            return
        self._pause_monitor_for_capture()
        self._start_cursor_logger_if_needed()
        enc = fu.resolve_encoder(self.var_enc.get(), self.encoders, self.ffmpeg)
        self.engine = RecordEngine(
            ffmpeg_path=self.ffmpeg, scene=self.scene, encoder=enc,
            quality_key=self.var_quality.get(), container=self.cfg.container,
            cursor=self.cfg.capture_cursor, audio_system=bool(self.var_sys.get()),
            audio_mic_device=self.var_micdev.get() if self.var_mic.get() else "",
            denoise=self.cfg.audio_denoise, out_dir=self.cfg.videos_dir,
            on_state=lambda st, p: self.after(0, self._on_engine_state, st, p),
            on_error=lambda m: self.after(0, self._on_engine_error, m))
        try:
            self.engine.start()
        except Exception as exc:  # noqa: BLE001
            self._on_engine_error(str(exc))

    def _toggle_pause(self) -> None:
        if not self.engine:
            return
        if self.engine.state == "recording":
            self.engine.pause()
        elif self.engine.state == "paused":
            self.engine.resume()

    def _on_engine_state(self, state: str, path) -> None:
        if state == "recording":
            if self._rec_t0 is None:
                self._rec_t0 = time.time()
            self.btn_rec.config(text="⏹  Detener", state="normal")
            self.btn_pause.config(text="⏸ Pausa", state="normal")
            self._set_status("Grabando…")
        elif state == "paused":
            self.btn_pause.config(text="▶ Reanudar")
            self._set_status("En pausa.")
        elif state == "saved":
            self.btn_rec.config(text="●  Grabar", state="normal")
            self.btn_pause.config(state="disabled", text="⏸ Pausa")
            # Avisos de audio (p.ej. el micro elegido no se pudo abrir): antes se
            # perdian en el log y el usuario descubria el silencio al reproducir.
            problems = self.engine.audio_problems if self.engine else []
            self.engine = None
            self._rec_t0 = None
            if problems:
                messagebox.showwarning(APP_NAME, "El video se guardo, pero:\n\n"
                                       + "\n".join(f"• {p}" for p in problems))
            # si habia una parada programada pendiente, ya no aplica
            if self._sched_stop_id:
                try:
                    self.after_cancel(self._sched_stop_id)
                except (tk.TclError, ValueError):
                    pass
                self._sched_stop_id = None
            self.last_recording = path
            self._set_status(f"Guardado: {path}")
            # En Docente/Curso: pasar la grabacion al panel de pulido (con el
            # registro del raton si se grabo con zoom-al-cursor).
            samples = region = None
            if self._cursor_logger is not None:
                try:
                    samples = self._cursor_logger.stop()
                except Exception:  # noqa: BLE001
                    samples = None
                region = self._cursor_region
                self._cursor_logger = None
            if self._polish_panel is not None:
                self._polish_panel.set_source(path, cursor_samples=samples, cursor_region=region)
                self._set_status(f"Grabacion lista. Marca mejoras y pulsa 'Pulir leccion'.")
                return
            if messagebox.askyesno(APP_NAME, f"Guardado en:\n{path}\n\nAbrir la carpeta?"):
                try:
                    os.startfile(str(Path(path).parent))
                except OSError:
                    pass

    def _on_engine_error(self, msg: str) -> None:
        self.btn_rec.config(text="●  Grabar", state="normal")
        self.btn_pause.config(state="disabled")
        self.engine = None
        self._rec_t0 = None
        if self._cursor_logger is not None:
            try:
                self._cursor_logger.stop()
            except Exception:  # noqa: BLE001
                pass
            self._cursor_logger = None
        self._set_status("Error en la grabacion.")
        messagebox.showerror(APP_NAME, f"No se pudo grabar:\n\n{msg}")

    # -- programar grabacion ----------------------------------------------
    def _schedule_dialog(self) -> None:
        from datetime import datetime, timedelta
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title("Programar grabacion")
        win.configure(bg=theme.BG)
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        if self._sched_start_id or self._sched_stop_id:
            ttk.Label(frm, text="Programacion activa:", style="H.TLabel").pack(anchor="w")
            ttk.Label(frm, text=self._sched_desc, style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
            ttk.Button(frm, text="Cancelar programacion", style="Primary.TButton",
                       command=lambda: (self._cancel_schedule(), win.destroy())).pack(fill="x")
            ttk.Button(frm, text="Cerrar", command=win.destroy).pack(anchor="e", pady=(10, 0))
            win.grab_set()
            return

        mode = tk.StringVar(value="in")
        var_in = tk.IntVar(value=5)
        var_at = tk.StringVar(value="")
        var_dur = tk.IntVar(value=0)
        ttk.Radiobutton(frm, text="Empezar dentro de (min):", variable=mode,
                        value="in").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(frm, from_=1, to=1440, textvariable=var_in,
                    width=6).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Radiobutton(frm, text="Empezar a las (HH:MM):", variable=mode,
                        value="at").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frm, textvariable=var_at, width=8).grid(row=1, column=1, sticky="w",
                                                          padx=8, pady=(8, 0))
        ttk.Label(frm, text="Detener tras (min, 0 = manual):",
                  style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(frm, from_=0, to=1440, textvariable=var_dur,
                    width=6).grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))

        def ok():
            if not self.ffmpeg:
                messagebox.showerror(APP_NAME, "FFmpeg no disponible: no se puede programar la grabacion.",
                                     parent=win)
                return
            try:
                if mode.get() == "in":
                    start_dt = datetime.now() + timedelta(minutes=max(1, int(var_in.get())))
                else:
                    hh, mm = (int(x) for x in var_at.get().strip().split(":"))
                    now = datetime.now()
                    start_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if start_dt <= now:
                        start_dt += timedelta(days=1)
                dur = max(0, int(var_dur.get()))
            except (ValueError, TypeError):
                messagebox.showerror(APP_NAME, "Revisa los valores (hora en formato HH:MM).",
                                     parent=win)
                return
            delay_ms = max(0, int((start_dt - datetime.now()).total_seconds() * 1000))
            self._arm_schedule(delay_ms, dur, start_dt.strftime("%H:%M"))
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Cancelar", command=win.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Programar", style="Primary.TButton", command=ok).pack(side="right")
        win.grab_set()

    def _arm_schedule(self, delay_ms: int, duration_min: int, when_label: str) -> None:
        self._cancel_schedule()
        self._sched_start_id = self.after(delay_ms, self._fire_scheduled_start, duration_min)
        dur_txt = f", parar tras {duration_min} min" if duration_min else ""
        self._sched_desc = f"inicio a las {when_label}{dur_txt}"
        self.btn_sched.config(text="⏰ Programada ✓")
        self._set_status(f"Grabacion programada: {self._sched_desc}.")

    def _fire_scheduled_start(self, duration_min: int) -> None:
        self._sched_start_id = None
        self.btn_sched.config(text="⏰ Programar")
        if self.engine and self.engine.state in ("recording", "paused"):
            self._set_status("Programacion: ya habia una grabacion en curso.")
            return
        self._toggle_record()
        if duration_min and self.engine:
            self._sched_stop_id = self.after(int(duration_min * 60_000), self._fire_scheduled_stop)

    def _fire_scheduled_stop(self) -> None:
        self._sched_stop_id = None
        if self.engine and self.engine.state in ("recording", "paused"):
            self._toggle_record()  # detiene la grabacion

    def _cancel_schedule(self) -> None:
        for attr in ("_sched_start_id", "_sched_stop_id"):
            tid = getattr(self, attr)
            if tid:
                try:
                    self.after_cancel(tid)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)
        self._sched_desc = ""
        try:
            self.btn_sched.config(text="⏰ Programar")
            self._set_status("Programacion cancelada.")
        except tk.TclError:
            pass

    # -- menu / post-produccion IA ----------------------------------------
    def _build_menubar(self) -> None:
        mb = tk.Menu(self)
        ia = tk.Menu(mb, tearoff=0)
        ia.add_command(label="Generar subtitulos (Whisper)…", command=self._ai_subtitles)
        ia.add_command(label="Quitar silencios…", command=self._ai_cut_silences)
        ia.add_command(label="Editar borrando palabras (texto)…", command=self._ai_word_edit)
        ia.add_command(label="Auto-encuadre (seguir al sujeto)…", command=self._ai_autoframe)
        ia.add_command(label="Capitulos automaticos + indice…", command=self._ai_chapters)
        ia.add_command(label="Buscador dentro del video…", command=self._ai_search)
        ia.add_command(label="Auto-apuntes (PDF)…", command=self._ai_notes)
        ia.add_command(label="Resumen + autoexamen…", command=self._ai_study)
        ia.add_command(label="Control de calidad (auto-auditoria)…", command=self._ai_quality)
        ia.add_separator()
        ia.add_command(label="Generar paquete de contenido…", command=self._ai_content_package)
        ia.add_command(label="Escudo de privacidad (censurar zona)…", command=self._ai_privacy)
        ia.add_command(label="Foco de ventana (oscurecer el resto)…", command=self._ai_focus)
        ia.add_command(label="Quitar fondo de una imagen (IA)…", command=self._ai_remove_bg)
        mb.add_cascade(label="Post-produccion IA", menu=ia)
        mb.add_command(label="🚀 Modos", command=self._show_mode_chooser)
        esc = tk.Menu(mb, tearoff=0)
        esc.add_command(label="Abrir proyecto…", command=self._open_scene)
        esc.add_command(label="Guardar proyecto…", command=self._save_scene)
        mb.add_cascade(label="Proyecto", menu=esc)
        ayuda = tk.Menu(mb, tearoff=0)
        ayuda.add_command(label="🤖 Asistente IA…", command=self._show_assistant)
        ayuda.add_command(label="⚙ Configurar IA…", command=self._show_ollama_config)
        ayuda.add_command(label="Carpeta de salida…", command=self._set_output_dir)
        ayuda.add_command(label="Abrir carpeta de videos", command=self._open_videos)
        ayuda.add_command(label="Atajos de teclado", command=self._show_hotkeys)
        ayuda.add_separator()
        ayuda.add_command(label=f"{APP_NAME} {APP_VERSION}", state="disabled")
        mb.add_cascade(label="Ayuda", menu=ayuda)
        self.config(menu=mb)

    def _show_assistant(self) -> None:
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title("🤖 Asistente IA")
        win.configure(bg=theme.BG)
        win.transient(self)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        has_ollama = llm.available(timeout=1.5)
        head = ("Preguntame como hacer cualquier cosa en CapturaStudio."
                if has_ollama else
                "El asistente conversacional necesita Ollama (gratis, local) en localhost:11434.\n"
                "Sin el, aqui tienes la guia rapida. Instala Ollama y un modelo para chatear.")
        ttk.Label(frm, text=head, style="Muted.TLabel", justify="left").pack(anchor="w")

        txt = tk.Text(frm, width=74, height=20, wrap="word", bg=theme.WHITE, fg=theme.TEXT,
                      relief="flat", highlightthickness=1, highlightbackground=theme.BORDER,
                      font=(theme.FONT, 10))
        txt.pack(fill="both", expand=True, pady=10)
        txt.insert("end", APP_GUIDE if not has_ollama else
                   "Hola 👋 Escribe tu pregunta abajo (p.ej. '¿como pongo subtitulos?').\n")
        txt.config(state="disabled")

        bar = ttk.Frame(frm)
        bar.pack(fill="x")
        var_q = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=var_q)
        ent.pack(side="left", fill="x", expand=True)
        btn = ttk.Button(bar, text="Preguntar", style="Primary.TButton")
        btn.pack(side="left", padx=(8, 0))

        def append(who, text):
            txt.config(state="normal")
            txt.insert("end", f"\n{who}: {text}\n")
            txt.see("end")
            txt.config(state="disabled")

        def ask(_e=None):
            q = var_q.get().strip()
            if not q or not has_ollama:
                return
            var_q.set("")
            append("Tu", q)
            btn.config(state="disabled")

            def work():
                return llm.generate(q, system="Eres el asistente de CapturaStudio. Responde breve "
                                    "y practico, en espanol, guiando por los menus. " + APP_GUIDE)

            def done(ans):
                try:
                    btn.config(state="normal")
                except tk.TclError:
                    pass
                append("Asistente", ans or "No pude generar respuesta. ¿Esta Ollama activo?")
                return None

            self._run_with_progress("Consultando al asistente…", work, done)

        btn.config(command=ask)
        ent.bind("<Return>", ask)
        if not has_ollama:
            ent.config(state="disabled")
            btn.config(state="disabled")
        win.grab_set()

    def _show_ollama_config(self) -> None:
        # Dialogo de IA UNIFICADO de la suite: Ollama local (gratis) o una API
        # potente (OpenAI/Gemini/Anthropic). Se configura una vez para las 5 apps.
        from octonove_core.ai_dialog import show_ai_dialog
        show_ai_dialog(self, on_saved=lambda: self._set_status("IA configurada."))

    def _open_teacher_mode(self, profile: str = "docente") -> None:
        # compatibilidad: ahora cambia el MODO de la ventana, no abre un pop-up
        self._set_mode(profile)

    def _open_videos(self) -> None:
        try:
            os.startfile(self.cfg.videos_dir)
        except OSError:
            pass

    def _set_output_dir(self) -> None:
        d = filedialog.askdirectory(title="Carpeta donde guardar los videos",
                                    initialdir=self.cfg.videos_dir, parent=self)
        if d:
            self.cfg.videos_dir = d
            save_config(self.cfg)
            self._set_status(f"Carpeta de salida: {d}")

    def _show_hotkeys(self) -> None:
        if self._hotkeys_win is not None:        # ya abierto: traerlo al frente
            try:
                self._hotkeys_win.lift()
                self._hotkeys_win.focus_set()
                return
            except tk.TclError:
                self._hotkeys_win = None
        win = tk.Toplevel(self)
        theme.center_window(win)
        self._hotkeys_win = win
        win.title("Atajos de teclado")
        win.configure(bg=theme.BG)
        win.transient(self)
        win.resizable(False, False)

        def close():
            self._end_capture()
            self._hotkeys_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Atajos globales (funcionan sin tener el foco):",
                  style="H.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        cur = dict(DEFAULT_HOTKEYS)
        cur.update(self.cfg.hotkeys or {})
        vars_: dict[str, tk.StringVar] = {}
        for i, (action, label, _m) in enumerate(HOTKEY_ACTIONS, start=1):
            ttk.Label(frm, text=label, style="Muted.TLabel").grid(row=i, column=0, sticky="w",
                                                                  pady=3, padx=(0, 12))
            var = tk.StringVar(value=cur.get(action, DEFAULT_HOTKEYS[action]))
            vars_[action] = var
            ent = ttk.Entry(frm, textvariable=var, width=20, state="readonly", justify="center")
            ent.grid(row=i, column=1, sticky="w")
            btn = ttk.Button(frm, text="Capturar", width=10)
            btn.grid(row=i, column=2, padx=(8, 0))
            btn.config(command=lambda v=var, b=btn: self._capture_hotkey(v, b))

        msg = ttk.Label(frm, text="Pulsa 'Capturar' y luego la combinacion (con Ctrl/Alt/Shift).",
                        style="Muted.TLabel")
        msg.grid(row=len(HOTKEY_ACTIONS) + 1, column=0, columnspan=3, sticky="w", pady=(12, 0))

        def restore():
            for action, var in vars_.items():
                var.set(DEFAULT_HOTKEYS[action])

        def save():
            chosen = {a: vars_[a].get().strip() for a in vars_}
            ok, err = validate_hotkey_map(chosen)
            if not ok:
                messagebox.showerror(APP_NAME, err, parent=win)
                return
            self.cfg.hotkeys = chosen
            save_config(self.cfg)
            self._apply_hotkeys()
            close()
            self._set_status("Atajos actualizados.")

        btns = ttk.Frame(frm)
        btns.grid(row=len(HOTKEY_ACTIONS) + 2, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Restaurar", command=restore).pack(side="left")
        ttk.Button(btns, text="Cancelar", command=close).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Guardar", style="Primary.TButton", command=save).pack(side="right")
        win.grab_set()

    def _end_capture(self) -> None:
        """Cancela cualquier captura de atajo en curso y restaura el boton."""
        btn = self._capturing_btn
        if btn is not None:
            try:
                btn.unbind("<KeyPress>")
                btn.unbind("<FocusOut>")
                btn.config(text="Capturar")
            except tk.TclError:
                pass
        self._capturing_btn = None

    def _capture_hotkey(self, var: "tk.StringVar", btn) -> None:
        """Pone el boton en modo escucha y captura la siguiente combinacion valida."""
        self._end_capture()              # cierra cualquier captura previa
        self._capturing_btn = btn
        btn.config(text="Pulsa…")

        def on_key(e):
            mods = 0
            if e.state & 0x0004:
                mods |= MOD_CONTROL
            if e.state & 0x0001:
                mods |= MOD_SHIFT
            if e.state & 0x20000 or e.state & 0x0008:   # Alt (Mod1 segun teclado)
                mods |= MOD_ALT
            vk = keysym_to_vk(e.keysym)
            if vk is None:
                return "break"           # solo modificadores aun: seguir esperando
            if mods == 0:
                return "break"           # exigir al menos un modificador
            var.set(format_hotkey(mods, vk))
            self._end_capture()
            return "break"

        btn.bind("<KeyPress>", on_key)
        btn.bind("<FocusOut>", lambda e: self._end_capture())
        btn.focus_set()

    def _scene_has_webcam(self) -> bool:
        return any(s.kind == scn.KIND_WEBCAM for s in self.scene.visible_sorted())

    def _capture_busy_other_than(self, who: str) -> bool:
        active = []
        if self.engine and self.engine.state in ("recording", "paused"):
            active.append("rec")
        if self.stream_engine and self.stream_engine.state == "streaming":
            active.append("stream")
        if self.replay and self.replay.state == "buffering":
            active.append("replay")
        return any(a != who for a in active)

    def _webcam_conflict(self, who: str) -> bool:
        if self._scene_has_webcam() and self._capture_busy_other_than(who):
            messagebox.showwarning(APP_NAME, "La webcam ya esta en uso por otra captura "
                                   "(grabacion, directo o buffer). Detenla primero.")
            return True
        return False

    # -- escenas (guardar / cargar) ---------------------------------------
    def _last_scene_path(self):
        return get_data_dir() / "last_scene.json"

    def _scenes_from_data(self, data) -> list:
        return scn.scenes_from_data(data)

    def _load_last_scene(self) -> None:
        import json
        p = self._last_scene_path()
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            scenes = self._scenes_from_data(data)
            if scenes:
                self.scenes = scenes
                self._scene_i = min(max(0, int(data.get("active", 0)) if isinstance(data, dict) else 0),
                                    len(scenes) - 1)
                self.scene = self.scenes[self._scene_i]
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo cargar la ultima escena: %s", exc)

    def _collection_dict(self) -> dict:
        return scn.collection_to_dict(self.scenes, self._scene_i)

    def _save_last_scene(self) -> None:
        import json
        try:
            self._last_scene_path().write_text(
                json.dumps(self._collection_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _sync_canvas_from_scene(self) -> None:
        for label, (w, h) in CANVAS_PRESETS.items():
            if (w, h) == (self.scene.canvas_w, self.scene.canvas_h):
                self.var_canvas.set(label)
                break
        self.var_fps.set(self.scene.fps)

    def _open_scene(self) -> None:
        import json
        path = filedialog.askopenfilename(title="Abrir proyecto",
                                          filetypes=[("Proyecto CapturaStudio", "*.json")])
        if not path:
            return
        try:
            scenes = self._scenes_from_data(json.loads(Path(path).read_text(encoding="utf-8")))
            if not scenes:
                raise ValueError("sin escenas")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"No se pudo abrir el proyecto:\n{exc}")
            return
        self.scenes = scenes
        self._scene_i = 0
        self.scene = self.scenes[0]
        self._sel_id = None
        self._set_inspector_enabled(False)
        self._sync_canvas_from_scene()
        self._refresh_source_list()
        self._refresh_scene_combo()
        self._preview_dirty = True
        self._set_status(f"Proyecto cargado: {Path(path).name}")

    def _save_scene(self) -> None:
        import json
        path = filedialog.asksaveasfilename(title="Guardar proyecto", defaultextension=".json",
                                            filetypes=[("Proyecto CapturaStudio", "*.json")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._collection_dict(), indent=2, ensure_ascii=False),
                                  encoding="utf-8")
            self._set_status(f"Proyecto guardado: {Path(path).name}")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"No se pudo guardar:\n{exc}")

    # -- multiples escenas (slots) ----------------------------------------
    def _any_capture_active(self) -> bool:
        return bool((self.engine and self.engine.state in ("recording", "paused")) or
                    (self.stream_engine and self.stream_engine.state == "streaming") or
                    (self.replay and self.replay.state == "buffering"))

    def _unique_name(self, base: str, exclude=None) -> str:
        existing = {s.name for s in self.scenes if s is not exclude}
        if base not in existing:
            return base
        i = 2
        while f"{base} {i}" in existing:
            i += 1
        return f"{base} {i}"

    def _refresh_scene_combo(self) -> None:
        self.scene_combo["values"] = [s.name for s in self.scenes]
        self.scene_combo.set(self.scene.name)

    def _switch_scene(self, i: int) -> None:
        if not (0 <= i < len(self.scenes)):
            return
        if self._any_capture_active():
            messagebox.showinfo(APP_NAME, "Deten la captura antes de cambiar de escena.")
            self._refresh_scene_combo()
            return
        self._scene_i = i
        self.scene = self.scenes[i]
        self._sel_id = None
        self._set_inspector_enabled(False)
        self._sync_canvas_from_scene()
        self._refresh_source_list()
        self._refresh_scene_combo()
        self._preview_dirty = True

    def _on_scene_combo(self, _e=None) -> None:
        name = self.scene_combo.get()
        for i, s in enumerate(self.scenes):
            if s.name == name:
                self._switch_scene(i)
                return

    def _new_scene(self) -> None:
        if self._any_capture_active():
            messagebox.showinfo(APP_NAME, "Deten la captura antes de cambiar de escena.")
            return
        cw, ch = CANVAS_PRESETS.get(self.var_canvas.get(), (1920, 1080))
        sc = scn.Scene(name=self._unique_name("Escena"), canvas_w=cw, canvas_h=ch,
                       fps=int(self.var_fps.get()))
        mon = primary_monitor()
        sc.add(scn.screen_source(mon.region, name=mon.label.split(":")[0]))
        self.scenes.append(sc)
        self._switch_scene(len(self.scenes) - 1)

    def _dup_scene(self) -> None:
        if self._any_capture_active():
            messagebox.showinfo(APP_NAME, "Deten la captura antes de cambiar de escena.")
            return
        sc = scn.Scene.from_dict(self.scene.to_dict())
        sc.name = self._unique_name(self.scene.name + " copia")
        self.scenes.append(sc)
        self._switch_scene(len(self.scenes) - 1)

    def _del_scene(self) -> None:
        if len(self.scenes) <= 1:
            messagebox.showinfo(APP_NAME, "Debe haber al menos una escena.")
            return
        if self._any_capture_active():
            messagebox.showinfo(APP_NAME, "Deten la captura primero.")
            return
        del self.scenes[self._scene_i]
        self._switch_scene(min(self._scene_i, len(self.scenes) - 1))

    def _rename_scene(self) -> None:
        name = simpledialog.askstring("Renombrar escena", "Nuevo nombre:",
                                      initialvalue=self.scene.name, parent=self)
        if name and name.strip():
            self.scene.name = self._unique_name(name.strip(), exclude=self.scene)
            self._refresh_scene_combo()

    # -- medidores VU -----------------------------------------------------
    VU_W, VU_H = 84, 9

    def _make_vu(self, parent, label: str):
        ttk.Label(parent, text=label, style="Muted.TLabel").pack(side="left", padx=(0, 3))
        cv = tk.Canvas(parent, width=self.VU_W, height=self.VU_H, bg="#0B1118",
                       highlightthickness=1, highlightbackground=theme.BORDER)
        cv.pack(side="left", padx=(0, 8))
        return cv

    def _draw_vu(self, canvas, level: float) -> None:
        canvas.delete("all")
        w = int(max(0.0, min(1.0, level)) * self.VU_W)
        if w <= 0:
            return
        # verde hasta 70%, ambar 70-90%, rojo por encima (escala perceptual)
        if level < 0.7:
            color = "#3FB950"
        elif level < 0.9:
            color = "#D29922"
        else:
            color = "#F85149"
        canvas.create_rectangle(0, 0, w, self.VU_H, fill=color, width=0)

    def _toggle_monitor(self) -> None:
        if self.var_monitor.get():
            sys_on = bool(self.var_sys.get())
            mic_dev = self.var_micdev.get() if self.var_mic.get() else None
            if not sys_on and not mic_dev:
                self.var_monitor.set(False)
                messagebox.showinfo(APP_NAME,
                                    "Activa 'Audio sistema' o 'Micro' para monitorizar el nivel.")
                return
            # ffmpeg: respaldo DirectShow para el VU de micros que WASAPI no abre
            self.meter = meters.AudioMeter(sys_on, mic_dev, ffmpeg=self.ffmpeg)
            self.meter.start()
            self._meter_gen += 1          # nueva generacion del bucle de refresco
            self._tick_meters(self._meter_gen)
        else:
            self._meter_gen += 1          # invalida cualquier bucle en curso
            if self.meter:
                self.meter.stop()
                self.meter = None
            self._vu_sys = self._vu_mic = 0.0
            self._draw_vu(self.vu_sys, 0.0)
            self._draw_vu(self.vu_mic, 0.0)

    def _stop_monitor(self) -> None:
        """Para el monitor VU y deja la casilla en su sitio (uso interno)."""
        self._meter_gen += 1
        if self.meter:
            self.meter.stop()
            self.meter = None
        try:
            if hasattr(self, "var_monitor"):
                self.var_monitor.set(False)
            self._vu_sys = self._vu_mic = 0.0
            self._draw_vu(self.vu_sys, 0.0)
            self._draw_vu(self.vu_mic, 0.0)
        except tk.TclError:
            pass

    def _pause_monitor_for_capture(self) -> None:
        """El monitor abre el mismo loopback/micro (soundcard) que la captura en
        vivo; se apaga al empezar a grabar/emitir para no competir por el audio."""
        if self.meter:
            self._stop_monitor()
            self._set_status("Monitor de audio pausado durante la captura.")

    def _start_cursor_logger_if_needed(self) -> None:
        """En Docente/Curso, si el panel de pulido pide 'zoom que sigue el cursor',
        registra la posicion del raton durante la grabacion para aplicar el zoom
        despues. La region es la del primer source de pantalla de la escena."""
        self._cursor_logger = None
        self._cursor_region = None
        panel = self._polish_panel
        if panel is None or not getattr(panel, "var_cursorzoom", None) or not panel.var_cursorzoom.get():
            return
        region = None
        for s in self.scene.visible_sorted():
            if s.kind == scn.KIND_SCREEN:
                p = s.params
                region = (int(p.get("left", 0)), int(p.get("top", 0)),
                          int(p.get("width", 1920)), int(p.get("height", 1080)))
                break
        if not region:
            return   # sin captura de pantalla no hay nada a lo que seguir el cursor
        try:
            self._cursor_logger = cursorzoom.MouseLogger()
            self._cursor_logger.start()
            self._cursor_region = region
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo iniciar el registro del cursor: %s", exc)
            self._cursor_logger = None
            self._cursor_region = None

    def _tick_meters(self, gen: int) -> None:
        # Si la generacion cambio (toggle/cierre), este bucle quedo obsoleto.
        if gen != self._meter_gen or not self.meter:
            return
        # decay visual: sube al instante, baja suave (lectura legible del pico)
        ns = meters.db_to_unit(self.meter.sys)
        nm = meters.db_to_unit(self.meter.mic)
        self._vu_sys = max(ns, self._vu_sys * 0.80)
        self._vu_mic = max(nm, self._vu_mic * 0.80)
        self._draw_vu(self.vu_sys, self._vu_sys)
        self._draw_vu(self.vu_mic, self._vu_mic)
        self.after(50, self._tick_meters, gen)

    def _pick_video(self, title: str) -> str | None:
        init = self.last_recording or self.cfg.videos_dir
        return filedialog.askopenfilename(
            title=title, initialdir=str(Path(init).parent if self.last_recording else init),
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi")]) or None

    def _ai_subtitles(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video para subtitular")
        if not video:
            return
        key = models.first_available()
        if not key:
            key = self.cfg.whisper_model
            if not messagebox.askyesno(APP_NAME,
                    f"Hay que descargar el modelo Whisper '{key}' (~{models.MODELS[key][1]} MB) "
                    "una sola vez.\n\nDescargar ahora?"):
                return
        burn = messagebox.askyesno(APP_NAME,
            "Quemar los subtitulos en el video?\n\nSi = nuevo video con subtitulos incrustados.\n"
            "No = solo el archivo .srt junto al video.")

        def work():
            mp = str(models.model_path(key)) if models.is_downloaded(key) else models.download(key)
            srt = str(Path(video).with_suffix(".srt"))
            ai_post.transcribe_srt(self.ffmpeg, mp, video, "es", srt)
            if burn:
                enc = fu.resolve_encoder(self.var_enc.get(), self.encoders, self.ffmpeg)
                out = str(Path(video).with_name(Path(video).stem + "_subtitulado.mp4"))
                ai_post.burn_subtitles(self.ffmpeg, video, srt, out, encoder=enc,
                                       quality_key=self.var_quality.get())
                return out
            return srt

        self._run_with_progress("Generando subtitulos con IA…", work,
                                lambda r: f"Subtitulos listos:\n{r}")

    def _ai_cut_silences(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video para quitar silencios")
        if not video:
            return

        def work():
            enc = fu.resolve_encoder(self.var_enc.get(), self.encoders, self.ffmpeg)
            out = str(Path(video).with_name(Path(video).stem + "_sin_silencios.mp4"))
            info = ai_post.cut_silences(self.ffmpeg, video, out, encoder=enc,
                                        quality_key=self.var_quality.get())
            return out, info

        self._run_with_progress("Quitando silencios…", work,
                                lambda r: f"Listo: {r[1]['orig']:.0f}s -> {r[1]['final']:.0f}s "
                                          f"({r[1]['segmentos']} tramos)\n{r[0]}")

    def _ai_word_edit(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video para editar por texto")
        if not video:
            return
        if not self._ensure_whisper_model():
            return

        def work():
            key = models.first_available() or self.cfg.whisper_model
            mp = str(models.model_path(key)) if models.is_downloaded(key) else models.download(key)
            wsrt = str(Path(video).with_suffix(".words.srt"))
            txt = ai_post.transcribe_srt(self.ffmpeg, mp, video, "es", wsrt, max_len=1)
            return wordedit.words_from_srt(txt)

        def done(words):
            if not words:
                messagebox.showinfo(APP_NAME, "No se detecto texto en el audio.")
            else:
                self._open_word_editor(video, words)
            return None

        self._run_with_progress("Transcribiendo palabra por palabra…", work, done)

    def _open_word_editor(self, video: str, words) -> None:
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title("Editar borrando palabras")
        win.configure(bg=theme.BG)
        win.transient(self)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Haz clic en una palabra para quitarla (se tacha). El video se "
                  "recortara quitando esos tramos.", style="Muted.TLabel").pack(anchor="w")
        txt = tk.Text(frm, width=80, height=22, wrap="word", bg=theme.WHITE, fg=theme.TEXT,
                      relief="flat", highlightthickness=1, highlightbackground=theme.BORDER,
                      font=(theme.FONT, 12), cursor="hand2", spacing3=4)
        txt.pack(fill="both", expand=True, pady=10)
        txt.tag_configure("del", overstrike=True, foreground="#F85149")
        deleted: set[int] = set()

        def toggle(i):
            tag = f"w{i}"
            if i in deleted:
                deleted.discard(i)
                txt.tag_remove("del", *txt.tag_ranges(tag))
            else:
                deleted.add(i)
                rng = txt.tag_ranges(tag)
                if rng:
                    txt.tag_add("del", *rng)

        for i, (_s, _e, w) in enumerate(words):
            tag = f"w{i}"
            txt.insert("end", w + " ", (tag,))
            txt.tag_bind(tag, "<Button-1>", lambda _e, k=i: toggle(k))
        txt.config(state="disabled")

        bar = ttk.Frame(frm)
        bar.pack(fill="x")
        lbl = ttk.Label(bar, text="", style="Muted.TLabel")
        lbl.pack(side="left")

        def apply():
            if not deleted:
                messagebox.showinfo(APP_NAME, "No has marcado ninguna palabra.", parent=win)
                return
            out = str(Path(video).with_name(Path(video).stem + "_editado.mp4"))
            enc = "libx264"
            win.destroy()

            def work():
                wordedit.apply_cut(self.ffmpeg, video, out, words, deleted, encoder=enc,
                                   quality_key=self.cfg.video_quality)
                return out

            self._run_with_progress("Aplicando el corte por texto…", work,
                                    lambda r: f"Video editado:\n{r}")
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(bar, text="✂ Aplicar corte", style="Primary.TButton",
                   command=apply).pack(side="right")
        win.grab_set()

    def _ai_autoframe(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video para auto-encuadrar")
        if not video:
            return
        vertical = messagebox.askyesno(APP_NAME,
            "¿Formato vertical 9:16 (Shorts/Reels)?\n\n"
            "Si = vertical 9:16 siguiendo al sujeto.\n"
            "No = mismo formato, con recorte dinamico que sigue al sujeto.")
        aspect = "vertical" if vertical else "keep"
        suffix = "_vertical" if vertical else "_encuadrado"

        def work():
            out = str(Path(video).with_name(Path(video).stem + suffix + ".mp4"))
            autoframe.autoframe(self.ffmpeg, video, out, aspect=aspect)
            return out

        self._run_with_progress("Auto-encuadrando (seguir al sujeto)…", work,
                                lambda r: f"Listo:\n{r}")

    def _ensure_whisper_model(self) -> str | None:
        """Devuelve la clave de un modelo Whisper disponible, descargandolo si hace
        falta (con permiso). None si el usuario cancela."""
        key = models.first_available()
        if key:
            return key
        key = self.cfg.whisper_model
        if not messagebox.askyesno(APP_NAME, f"Hay que descargar el modelo Whisper '{key}' "
                                   "una sola vez.\n\nDescargar ahora?"):
            return None
        return key

    def _srt_text_for(self, video: str) -> str:
        """Devuelve la transcripcion (.srt): reusa la existente o la genera con
        Whisper. Pensado para ejecutarse dentro del hilo de _run_with_progress."""
        srt_path = Path(video).with_suffix(".srt")
        if srt_path.is_file():
            return srt_path.read_text(encoding="utf-8", errors="replace")
        key = models.first_available() or self.cfg.whisper_model
        mp = str(models.model_path(key)) if models.is_downloaded(key) else models.download(key)
        return ai_post.transcribe_srt(self.ffmpeg, mp, video, "es", str(srt_path))

    def _pick_video_with_srt(self, title: str):
        """Elige un video y asegura el modelo Whisper si no hay .srt. None si cancela."""
        if not self.ffmpeg:
            return None
        video = self._pick_video(title)
        if not video:
            return None
        if not Path(video).with_suffix(".srt").is_file() and not self._ensure_whisper_model():
            return None
        return video

    def _ai_chapters(self) -> None:
        video = self._pick_video_with_srt("Elige el video para generar capitulos")
        if not video:
            return
        out_dir = str(Path(video).with_name(Path(video).stem + "_capitulos"))

        def work():
            return chapters.make_chapters(self.ffmpeg, video, self._srt_text_for(video),
                                          out_dir, embed=True)

        self._run_with_progress("Generando capitulos por tema…", work,
                                lambda r: f"{len(r['chapters'])} capitulos generados.\n\n"
                                          f"Archivos en:\n{out_dir}")

    def _ai_search(self) -> None:
        video = self._pick_video_with_srt("Elige el video para el buscador")
        if not video:
            return
        out = str(Path(video).with_name(Path(video).stem + "_buscar.html"))

        def work():
            segs = chapters.parse_srt(self._srt_text_for(video))
            Path(out).write_text(chapters.search_html(segs, Path(video).name, Path(video).stem),
                                 encoding="utf-8")
            try:
                os.startfile(out)
            except OSError:
                pass
            return out

        self._run_with_progress("Generando el buscador…", work,
                                lambda r: f"Buscador listo (se abrira en tu navegador):\n{r}")

    def _ai_notes(self) -> None:
        video = self._pick_video_with_srt("Elige el video para los apuntes")
        if not video:
            return
        out = str(Path(video).with_name(Path(video).stem + "_apuntes.pdf"))

        def work():
            notes.make_notes_pdf(self.ffmpeg, video, self._srt_text_for(video), out,
                                 title=Path(video).stem)
            return out

        self._run_with_progress("Generando los apuntes en PDF…", work,
                                lambda r: f"Apuntes listos:\n{r}")

    def _ai_study(self) -> None:
        video = self._pick_video_with_srt("Elige el video para resumen + autoexamen")
        if not video:
            return
        out = str(Path(video).with_name(Path(video).stem + "_estudio.html"))
        hint = " (mejorado con Ollama)" if llm.available(timeout=1.5) else ""

        def work():
            segs = chapters.parse_srt(self._srt_text_for(video))
            html = study.material_html(study.summarize(segs), study.quiz(segs), Path(video).stem)
            Path(out).write_text(html, encoding="utf-8")
            try:
                os.startfile(out)
            except OSError:
                pass
            return out

        self._run_with_progress(f"Generando resumen y autoexamen{hint}…", work,
                                lambda r: f"Material de estudio listo:\n{r}")

    def _ai_quality(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video a revisar")
        if not video:
            return

        def work():
            return quality_check.analyze(self.ffmpeg, video)

        self._run_with_progress("Revisando la grabacion…", work,
                                lambda issues: self._show_quality_report(video, issues))

    def _show_quality_report(self, video: str, issues) -> None:
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title("Control de calidad")
        win.configure(bg=theme.BG)
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"Revision de: {Path(video).name}", style="H.TLabel").pack(anchor="w")
        icon = {"alerta": "⛔", "aviso": "⚠", "ok": "✓"}
        for it in issues:
            ttk.Label(frm, text=f"{icon.get(it.level, '•')}  {it.message}",
                      style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        can_fix = any(getattr(it, "fix", None) == "normalizar" for it in issues)
        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(16, 0))
        ttk.Button(bar, text="Cerrar", command=win.destroy).pack(side="right")
        if can_fix:
            def fix():
                win.destroy()
                out = str(Path(video).with_name(Path(video).stem + "_audiook.mp4"))

                def work():
                    quality_check.normalize_audio(self.ffmpeg, video, out)
                    return out
                self._run_with_progress("Normalizando el audio…", work,
                                        lambda r: f"Audio normalizado:\n{r}")
            ttk.Button(bar, text="🔧 Normalizar audio", style="Primary.TButton",
                       command=fix).pack(side="right", padx=(0, 8))
        win.grab_set()
        return None

    def _ai_focus(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video para enfocar una ventana")
        if not video:
            return
        region = self._ask_region()
        if not region:
            return

        def work():
            out = str(Path(video).with_name(Path(video).stem + "_foco.mp4"))
            privacy_shield.focus_region(
                self.ffmpeg, video, out, (region.x, region.y, region.w, region.h),
                start=region.start, end=region.end)
            return out

        self._run_with_progress("Aplicando foco de ventana…", work,
                                lambda r: f"Video con foco:\n{r}")

    def _ai_content_package(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video para generar el paquete")
        if not video:
            return
        opts = self._ask_package_options()
        if not opts:
            return
        # Si pide subtitulos pero no hay modelo Whisper, no los omitimos en silencio:
        # ofrecemos descargarlo y, si el usuario no quiere, avisamos de que el paquete
        # ira SIN subtitulos antes de generarlo.
        model_key = None
        if opts["subs"]:
            model_key = self._ensure_whisper_model()
            if not model_key:
                if not messagebox.askyesno(
                        APP_NAME, "No hay modelo Whisper disponible, asi que el paquete se "
                        "generara SIN subtitulos.\n\nContinuar de todos modos?"):
                    return
                opts["subs"] = False
        out_dir = str(Path(video).with_name(Path(video).stem + "_paquete"))

        def work():
            model = None
            if opts["subs"] and model_key:
                # el modelo se descarga aqui (en el hilo) si aun no estaba, para no
                # congelar la ventana.
                model = (str(models.model_path(model_key)) if models.is_downloaded(model_key)
                         else models.download(model_key))
            enc = fu.resolve_encoder(self.var_enc.get(), self.encoders, self.ffmpeg)
            files = content_factory.make_package(
                self.ffmpeg, video, out_dir, vertical=opts["vertical"], audio=opts["audio"],
                gif=opts["gif"], subtitles=opts["subs"], model_file=model,
                encoder=enc, quality_key=self.var_quality.get())
            return out_dir, files

        self._run_with_progress("Generando paquete de contenido…", work,
                                lambda r: f"{len(r[1])} entregables en:\n{r[0]}")

    def _ask_package_options(self) -> dict | None:
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title("Paquete de contenido")
        win.transient(self)
        win.resizable(False, False)
        win.grab_set()
        ttk.Label(win, text="Que entregables generar?", style="H.TLabel").pack(padx=20, pady=(16, 8))
        v = {"vertical": tk.BooleanVar(value=True), "audio": tk.BooleanVar(value=True),
             "gif": tk.BooleanVar(value=True), "subs": tk.BooleanVar(value=False)}
        labels = {"vertical": "Vertical 9:16 (Reels/Shorts/TikTok)", "audio": "Audio MP3 (podcast)",
                  "gif": "GIF de un fragmento", "subs": "Subtitulos .srt (Whisper)"}
        for k in ("vertical", "audio", "gif", "subs"):
            ttk.Checkbutton(win, text=labels[k], variable=v[k]).pack(anchor="w", padx=24, pady=2)
        res: dict = {}

        def ok():
            res.update({k: v[k].get() for k in v})
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(pady=14)
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(bar, text="Generar", style="Primary.TButton", command=ok).pack(side="left", padx=6)
        win.wait_window()
        return res or None

    def _ai_privacy(self) -> None:
        if not self.ffmpeg:
            return
        video = self._pick_video("Elige el video a censurar")
        if not video:
            return
        region = self._ask_region()
        if not region:
            return

        def work():
            enc = fu.resolve_encoder(self.var_enc.get(), self.encoders, self.ffmpeg)
            out = str(Path(video).with_name(Path(video).stem + "_censurado.mp4"))
            privacy_shield.blur_regions(self.ffmpeg, video, out, [region], encoder=enc,
                                        quality_key=self.var_quality.get())
            return out

        self._run_with_progress("Aplicando escudo de privacidad…", work,
                                lambda r: f"Video censurado:\n{r}")

    def _ask_region(self) -> "privacy_shield.BlurRegion | None":
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title("Censurar zona")
        win.transient(self)
        win.resizable(False, False)
        win.grab_set()
        ttk.Label(win, text="Rectangulo a difuminar (pixeles del video)",
                  style="H.TLabel").pack(padx=20, pady=(16, 8))
        frm = ttk.Frame(win)
        frm.pack(padx=20)
        vars_ = {}
        for i, (k, default) in enumerate([("x", 100), ("y", 100), ("w", 400), ("h", 150)]):
            ttk.Label(frm, text=k.upper()).grid(row=0, column=i * 2, padx=(8, 2))
            vv = tk.IntVar(value=default)
            vars_[k] = vv
            ttk.Spinbox(frm, from_=0, to=8000, textvariable=vv, width=7).grid(row=0, column=i * 2 + 1)
        tr = ttk.Frame(win)
        tr.pack(padx=20, pady=(10, 0))
        ttk.Label(tr, text="Desde (s, vacio=todo):").pack(side="left")
        s_var = tk.StringVar()
        ttk.Entry(tr, textvariable=s_var, width=7).pack(side="left", padx=(4, 12))
        ttk.Label(tr, text="Hasta (s):").pack(side="left")
        e_var = tk.StringVar()
        ttk.Entry(tr, textvariable=e_var, width=7).pack(side="left", padx=4)

        def pick_window():
            title = simpledialog.askstring("Ventana", "Parte del titulo de la ventana a censurar:",
                                           parent=win)
            if not title:
                return
            rect = privacy_shield.window_rect(title)
            if rect:
                for k, val in zip(("x", "y", "w", "h"), rect):
                    vars_[k].set(val)
            else:
                messagebox.showinfo(APP_NAME, "No se encontro esa ventana.")
        ttk.Button(win, text="Rellenar desde una ventana…", command=pick_window).pack(pady=(10, 0))
        res: dict = {}

        def to_f(s):
            try:
                return float(s) if s.strip() else None
            except ValueError:
                return None

        def ok():
            res["r"] = privacy_shield.BlurRegion(
                vars_["x"].get(), vars_["y"].get(), vars_["w"].get(), vars_["h"].get(),
                to_f(s_var.get()), to_f(e_var.get()))
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(pady=14)
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(bar, text="Censurar", style="Primary.TButton", command=ok).pack(side="left", padx=6)
        win.wait_window()
        return res.get("r")

    def _ai_remove_bg(self) -> None:
        if not bg_removal.available():
            messagebox.showinfo(APP_NAME, "La funcion de quitar fondo necesita el componente de IA "
                                "'rembg' (no incluido por su tamano). Instalalo con:\n\n"
                                "pip install rembg onnxruntime")
            return
        path = filedialog.askopenfilename(
            title="Elige una imagen", filetypes=[("Imagenes", "*.png *.jpg *.jpeg *.bmp *.webp")])
        if not path:
            return
        if not bg_removal.model_ready():
            if not messagebox.askyesno(APP_NAME, "Se descargara el modelo de IA u2net (~176 MB) "
                                       "una sola vez.\n\nDescargar ahora?"):
                return
        _rgb, hx = colorchooser.askcolor(
            title="Color de fondo (Cancelar = PNG transparente)", parent=self)
        out = str(Path(path).with_name(Path(path).stem + "_sinfondo" + (".jpg" if hx else ".png")))

        def work():
            return bg_removal.replace_bg(path, out, hx) if hx else bg_removal.remove_bg(path, out)

        self._run_with_progress("Quitando el fondo con IA…", work, lambda r: f"Listo:\n{r}")

    def _run_with_progress(self, title: str, work, done_msg, always=None) -> None:
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title(title)
        win.transient(self)
        win.resizable(False, False)
        win.configure(bg=theme.BG)
        ttk.Label(win, text=title).pack(padx=24, pady=(20, 8))
        pb = ttk.Progressbar(win, mode="indeterminate", length=340)
        pb.pack(padx=24, pady=(0, 20))
        pb.start(12)
        win.update_idletasks()
        result: dict = {}

        def finish():
            try:
                pb.stop()
                win.destroy()
            except tk.TclError:
                pass
            try:
                if "err" in result:
                    messagebox.showerror(APP_NAME, f"No se pudo completar:\n\n{result['err']}")
                else:
                    msg = done_msg(result["ok"])
                    if msg:   # si el caller pinta su propia UI, devuelve None
                        messagebox.showinfo(APP_NAME, msg)
            except Exception as exc:  # noqa: BLE001
                logger.exception("callback de fin fallo")
                messagebox.showerror(APP_NAME, f"Error mostrando el resultado:\n{exc}")
            finally:
                if always:
                    try:
                        always()
                    except Exception:  # noqa: BLE001
                        logger.exception("always() fallo")

        def runner():
            try:
                result["ok"] = work()
            except Exception as exc:  # noqa: BLE001
                logger.exception("post-produccion fallo")
                result["err"] = str(exc)
            self.after(0, finish)

        threading.Thread(target=runner, daemon=True).start()

    # -- streaming ---------------------------------------------------------
    def _toggle_stream(self) -> None:
        if self.stream_engine and self.stream_engine.state == "streaming":
            self.lbl_stream.config(text="Deteniendo…")
            self.stream_engine.stop()
            return
        if getattr(self, "_mode", "studio") != "studio":
            self._set_status("Cambia a 'Streamer / Estudio' para emitir en directo.")
            return
        if not self.ffmpeg or not self.scene.visible_sorted():
            messagebox.showwarning(APP_NAME, "Anade fuentes y comprueba FFmpeg.")
            return
        key = self.var_streamkey.get().strip()
        if not key:
            messagebox.showwarning(APP_NAME, "Pega tu clave de stream (o la URL completa de ingest).")
            return
        if self._webcam_conflict("stream"):
            return
        self._pause_monitor_for_capture()
        ingest = stream.ingest_url(self.var_service.get(), key)
        self.cfg.stream_service = self.var_service.get()
        self.cfg.stream_key = key
        save_config(self.cfg)
        enc = fu.resolve_encoder(self.var_enc.get(), self.encoders, self.ffmpeg)
        vod = None
        if self.var_vod.get():
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            vod = str(Path(self.cfg.videos_dir) / f"Directo_{ts}.mkv")
        self.stream_engine = stream.StreamEngine(
            ffmpeg_path=self.ffmpeg, scene=self.scene, encoder=enc,
            bitrate_k=VIDEO_QUALITY[self.var_quality.get()]["bitrate_k"],
            audio_system=bool(self.var_sys.get()),
            audio_mic_device=self.var_micdev.get() if self.var_mic.get() else "",
            ingest=ingest, vod_path=vod, cursor=self.cfg.capture_cursor,
            extra_ingests=list(self.extra_dests),
            on_state=lambda s, p: self.after(0, self._on_stream_state, s, p),
            on_error=lambda m: self.after(0, self._on_stream_error, m))
        self.stream_engine.start()

    def _on_stream_state(self, state, info) -> None:
        if state == "streaming":
            if self._stream_t0 is None:
                self._stream_t0 = time.time()
            self.btn_stream.config(text="⏹ Detener directo")
            self.lbl_stream.config(text="● EN DIRECTO", foreground=theme.REC)
        elif state == "reconnecting":
            self.lbl_stream.config(text=f"Reconectando ({info})…", foreground=theme.PRIMARY_DARK)
        elif state == "stopped":
            self._stream_t0 = None
            self.btn_stream.config(text="▶ Emitir en directo")
            self.lbl_stream.config(text="Directo finalizado.", foreground=theme.MUTED)
            self.stream_engine = None

    def _on_stream_error(self, msg) -> None:
        self._stream_t0 = None
        self.btn_stream.config(text="▶ Emitir en directo")
        self.lbl_stream.config(text="Error de directo.", foreground=theme.DANGER)
        self.stream_engine = None
        messagebox.showerror(APP_NAME, f"Problema con el directo:\n\n{msg}")

    def _ask_extra_destinations(self) -> None:
        win = tk.Toplevel(self)
        theme.center_window(win)
        win.title("Destinos adicionales (multistream)")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text="URLs RTMP completas (una por linea):",
                  style="H.TLabel").pack(padx=20, pady=(16, 6))
        ttk.Label(win, text="Emite a varias plataformas a la vez. Ej.: rtmp://host/app/clave",
                  style="Muted.TLabel").pack(padx=20)
        txt = tk.Text(win, width=58, height=6, font=(theme.FONT, 9), relief="solid", borderwidth=1)
        txt.pack(padx=20, pady=10)
        txt.insert("1.0", "\n".join(self.extra_dests))

        def ok():
            self.extra_dests = [ln.strip() for ln in txt.get("1.0", "end").splitlines() if ln.strip()]
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(pady=12)
        ttk.Button(bar, text="Cancelar", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(bar, text="Guardar", style="Primary.TButton", command=ok).pack(side="left", padx=6)

    def _test_rtmp(self) -> None:
        if not self.ffmpeg:
            return
        key = self.var_streamkey.get().strip()
        if not key:
            messagebox.showwarning(APP_NAME, "Pega tu clave o URL de ingest primero.")
            return
        ingest = stream.ingest_url(self.var_service.get(), key)

        def work():
            cmd = stream.build_test_command(self.ffmpeg, ingest, duration=2)
            r = subprocess.run(cmd, capture_output=True, timeout=30, **fu.subprocess_kwargs())
            if r.returncode == 0:
                return "Conexion correcta: la URL y la clave funcionan."
            raise RuntimeError(fu._decode(r.stderr)[-300:] or "No se pudo conectar al servidor.")

        self._run_with_progress("Probando conexion (2 s de prueba)…", work, lambda r: r)

    # -- replay buffer (time machine) -------------------------------------
    def _toggle_replay(self) -> None:
        if self.replay and self.replay.state == "buffering":
            self.replay.stop()
            return
        if not self.ffmpeg or not self.scene.visible_sorted():
            messagebox.showwarning(APP_NAME, "Anade fuentes a la escena.")
            return
        if self._webcam_conflict("replay"):
            return
        enc = fu.resolve_encoder(self.var_enc.get(), self.encoders, self.ffmpeg)
        self.replay = ReplayBuffer(
            ffmpeg_path=self.ffmpeg, scene=self.scene, encoder=enc,
            bitrate_k=VIDEO_QUALITY[self.var_quality.get()]["bitrate_k"],
            audio_system=bool(self.var_sys.get()),
            audio_mic_device=self.var_micdev.get() if self.var_mic.get() else "",
            out_dir=self.cfg.videos_dir, buffer_seconds=120, seg_seconds=4,
            cursor=self.cfg.capture_cursor,
            on_state=lambda s, p: self.after(0, self._on_replay_state, s, p),
            on_error=lambda m: self.after(0, self._on_replay_error, m))
        self.replay.start()

    def _on_replay_state(self, state, info) -> None:
        if state == "buffering":
            self.btn_replay.config(text="⏹ Detener buffer")
            self._set_status("Buffer de replay activo · Ctrl+Shift+M guarda los ultimos 120 s.")
        elif state == "stopped":
            self.btn_replay.config(text="⏺ Buffer replay")
            self.replay = None
            self._set_status("Buffer de replay detenido.")

    def _on_replay_error(self, msg) -> None:
        self.btn_replay.config(text="⏺ Buffer replay")
        self.replay = None
        messagebox.showerror(APP_NAME, f"Replay: {msg}")

    def _save_replay_moment(self) -> None:
        if not (self.replay and self.replay.state == "buffering"):
            return

        def work():
            return self.replay.save_moment(120)

        self._run_with_progress("Guardando el momento…", work,
                                lambda r: f"Momento guardado:\n{r}")

    def _set_status(self, text: str) -> None:
        try:
            self.status.config(text=text)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        if self.engine and self.engine.state in ("recording", "paused"):
            if not messagebox.askyesno(APP_NAME, "Hay una grabacion en curso. Salir igualmente?"):
                return
        if self.stream_engine and self.stream_engine.state == "streaming":
            if not messagebox.askyesno(APP_NAME, "Hay un directo en curso. Salir igualmente?"):
                return
            try:
                self.stream_engine.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.replay and self.replay.state == "buffering":
            try:
                self.replay.stop()
            except Exception:  # noqa: BLE001
                pass
        self._on_setting()
        self._save_last_scene()
        self._cancel_schedule()
        if self.meter:
            try:
                self.meter.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._cursor_logger is not None:
            try:
                self._cursor_logger.stop()
            except Exception:  # noqa: BLE001
                pass
            self._cursor_logger = None
        if self.hotkeys:
            try:
                self.hotkeys.stop()
            except Exception:  # noqa: BLE001
                pass
        for gr in list(self._win_grabbers.values()):
            try:
                gr.stop()
            except Exception:  # noqa: BLE001
                pass
        self._win_grabbers = {}
        try:
            if self._mss:
                self._mss.close()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def main() -> None:
    from .config import setup_logging
    from .monitors import set_dpi_awareness
    set_dpi_awareness()
    setup_logging()
    logger.info("Captura de ventana WGC: %s",
                "disponible (a prueba de oclusion)" if wincap.available()
                else "no disponible; se usara gdigrab de region")
    App().mainloop()
