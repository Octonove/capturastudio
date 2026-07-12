"""Tests automatizados de logica pura (sin ejecutar FFmpeg). Ejecutar con:
    python -m pytest tests/ -q   (desde la carpeta CapturaStudio)"""

from capturastudio import ffmpeg_utils as fu
from capturastudio import ai_post, scene as scn, streaming as st
from capturastudio import hotkeys as hk
from capturastudio import privacy_shield as ps
from capturastudio import autoframe as afr
from capturastudio import chapters as ch
from capturastudio import quality_check as qc
from capturastudio import study, llm, wordedit, cursorzoom as cz, livecam


# --- Anti-inyeccion -------------------------------------------------------
def test_safe_color_valido():
    assert fu.safe_color("#1E3A5F") == "0x1E3A5F"
    assert fu.safe_color("1e3a5f") == "0x1e3a5f"
    assert fu.safe_color("0xAABBCC") == "0xAABBCC"
    assert fu.safe_color("black") == "black"


def test_safe_color_inyeccion():
    assert fu.safe_color("red:s=2x2[x];movie=evil") == "0x101418"
    assert fu.safe_color("'; drop") == "0x101418"
    assert fu.safe_color("") == "0x101418"


def test_safe_lang():
    assert ai_post._safe_lang("es") == "es"
    assert ai_post._safe_lang("AUTO") == "auto"
    assert ai_post._safe_lang("es:use_gpu=1") == "auto"
    assert ai_post._safe_lang("") == "auto"


# --- Streaming ------------------------------------------------------------
def test_ingest_url():
    assert st.ingest_url("Twitch", "KEY") == "rtmp://live.twitch.tv/app/KEY"
    assert st.ingest_url("YouTube", "k") == "rtmp://a.rtmp.youtube.com/live2/k"
    assert st.ingest_url("Personalizado (URL completa)", "rtmp://x/y/z") == "rtmp://x/y/z"


def test_stream_video_args_cbr():
    args = st.stream_video_args("h264_nvenc", 6000, 30)
    assert "cbr" in args and "6000k" in args


# --- Escena ---------------------------------------------------------------
def test_scene_roundtrip():
    sc = scn.Scene(canvas_w=1280, canvas_h=720, fps=24)
    sc.add(scn.screen_source((0, 0, 1920, 1080)))
    sc.add(scn.webcam_source("Cam", 100, 100, 300, circle=True))
    sc.add(scn.text_source("hola", 10, 10))
    sc2 = scn.Scene.from_dict(sc.to_dict())
    assert sc2.canvas_w == 1280 and sc2.fps == 24
    assert len(sc2.sources) == 3
    assert sc2.sources[1].transform.shape == "circle"


def test_collection_roundtrip():
    a = scn.Scene(name="Intro"); a.add(scn.screen_source((0, 0, 1920, 1080)))
    b = scn.Scene(name="Pausa"); b.add(scn.color_source("#111111"))
    data = scn.collection_to_dict([a, b], active=1)
    assert data["active"] == 1 and len(data["scenes"]) == 2
    back = scn.scenes_from_data(data)
    assert [s.name for s in back] == ["Intro", "Pausa"]
    assert len(back[0].sources) == 1


def test_collection_compat_escena_antigua():
    # Un proyecto guardado en el formato viejo (una sola escena con "sources").
    old = scn.Scene(name="Vieja").to_dict()
    assert "sources" in old and "scenes" not in old
    back = scn.scenes_from_data(old)
    assert len(back) == 1 and back[0].name == "Vieja"


def test_scene_reorder():
    sc = scn.Scene()
    a = sc.add(scn.color_source("#111111"))
    b = sc.add(scn.text_source("t"))
    assert b.z > a.z
    sc.lower(b.id)
    assert sc.sources[0].z != sc.sources[1].z


# --- build_scene ----------------------------------------------------------
def test_build_scene_filtergraph():
    sc = scn.Scene()
    sc.add(scn.screen_source((0, 0, 1920, 1080)))
    sc.add(scn.text_source("x", 10, 10))
    inputs, fc, vout = fu.build_scene(sc)
    assert vout == "[vout]"
    assert "overlay" in fc
    assert inputs.count("-i") >= 2


def test_build_scene_bg_injection_safe():
    sc = scn.Scene(bg_color="red:s=2x2[x];anullsrc")
    sc.add(scn.color_source("blue:evil[x]"))
    _inputs, fc, _vout = fu.build_scene(sc)
    assert "anullsrc" not in fc and "evil" not in fc


# --- Encoder --------------------------------------------------------------
def test_resolve_encoder_sin_probe():
    assert fu.resolve_encoder("auto", {"libx264"}) == "libx264"
    assert fu.resolve_encoder("h264_inexistente", {"libx264"}) in {"libx264"}


def test_chroma_en_filtergraph():
    sc = scn.Scene()
    sc.add(scn.screen_source((0, 0, 1920, 1080)))
    cam = scn.webcam_source("Cam", 100, 100, 300, circle=False)
    cam.transform.chroma = "#00D000"
    sc.add(cam)
    _i, fc, _v = fu.build_scene(sc)
    assert "chromakey" in fc


def test_chroma_roundtrip():
    sc = scn.Scene()
    s = scn.image_source("x.png")
    s.transform.chroma = "#00FF00"
    sc.add(s)
    sc2 = scn.Scene.from_dict(sc.to_dict())
    assert sc2.sources[0].transform.chroma == "#00FF00"


def test_build_test_command():
    cmd = st.build_test_command("ffmpeg", "rtmp://x/y/k", 2)
    assert "flv" in cmd and "rtmp://x/y/k" in cmd


# --- Atajos (remapeo) -----------------------------------------------------
def test_parse_hotkey_valido():
    assert hk.parse_hotkey("Ctrl+Shift+R") == (hk.MOD_CONTROL | hk.MOD_SHIFT, ord("R"))
    assert hk.parse_hotkey("alt+f5") == (hk.MOD_ALT, hk.NAME_VKS["F5"])
    assert hk.parse_hotkey("CTRL+1") == (hk.MOD_CONTROL, ord("1"))


def test_parse_hotkey_invalido():
    assert hk.parse_hotkey("") is None
    assert hk.parse_hotkey("R") is None          # sin modificador
    assert hk.parse_hotkey("Ctrl+Shift") is None  # sin tecla
    assert hk.parse_hotkey("Ctrl+Ñ") is None      # tecla no soportada
    assert hk.parse_hotkey("Ctrl+R+P") is None    # dos teclas


def test_hotkey_roundtrip():
    for combo in ("Ctrl+Shift+R", "Alt+F5", "Ctrl+Win+3"):
        mods, vk = hk.parse_hotkey(combo)
        assert hk.parse_hotkey(hk.format_hotkey(mods, vk)) == (mods, vk)


def test_keysym_to_vk():
    assert hk.keysym_to_vk("r") == ord("R")
    assert hk.keysym_to_vk("F12") == hk.NAME_VKS["F12"]
    assert hk.keysym_to_vk("Shift_L") is None


def test_validate_hotkey_map():
    ok, _ = hk.validate_hotkey_map({"a": "Ctrl+R", "b": "Alt+P"})
    assert ok
    bad, msg = hk.validate_hotkey_map({"a": "Ctrl+R", "b": "Ctrl+R"})  # duplicado
    assert not bad and "repetido" in msg
    bad2, _ = hk.validate_hotkey_map({"a": "R"})  # invalido (sin modificador)
    assert not bad2
    # duplicado SEMANTICO: misma combinacion en distinto orden de texto
    dup, _ = hk.validate_hotkey_map({"a": "Ctrl+Shift+R", "b": "Shift+Ctrl+R"})
    assert not dup


SRT_SAMPLE = """1
00:00:00,000 --> 00:00:03,000
Hola, bienvenidos a la clase de hoy.

2
00:00:03,400 --> 00:00:06,000
Vamos a empezar con la introduccion.

3
00:00:20,000 --> 00:00:23,500
Ahora pasamos al segundo tema importante.
"""


def test_livecam_crop_box():
    x, y, w, h = livecam.crop_box(1280, 720, 0.5, 0.5, 1.6)
    assert 0 <= x and x + w <= 1280 and 0 <= y and y + h <= 720
    assert w % 2 == 0 and h % 2 == 0
    # centro a la derecha -> clamp dentro del cuadro
    x2, _y2, w2, _h2 = livecam.crop_box(1280, 720, 0.99, 0.5, 1.6)
    assert x2 + w2 <= 1280


def test_livecam_process_frame_follows():
    import numpy as np
    W, H = 320, 180
    state = {}

    def fr(cx):
        f = np.zeros((H, W, 3), np.uint8)
        x = int(cx * W)
        f[60:120, max(0, x - 25):min(W, x + 25)] = 255
        return f

    livecam.process_frame(fr(0.2), state)
    start = state["center"][0]
    for cx in np.linspace(0.2, 0.85, 30):
        crop = livecam.process_frame(fr(float(cx)), state)
    assert state["center"][0] > start + 0.08      # siguio el movimiento
    assert crop.ndim == 3 and crop.shape[2] == 3   # recorte valido


def test_wordedit_kept_intervals():
    words = [(0.0, 1.0, "a"), (1.0, 2.0, "b"), (2.0, 3.0, "c"), (3.0, 4.0, "d")]
    # borrar 'b' (idx 1) -> dos tramos
    assert len(wordedit.kept_intervals(words, {1}, pad=0.0)) == 2
    # no borrar nada -> un tramo continuo
    assert len(wordedit.kept_intervals(words, set(), pad=0.0)) == 1
    # borrar todo -> vacio
    assert wordedit.kept_intervals(words, {0, 1, 2, 3}) == []


def test_cursor_log_to_trajectory():
    traj = cz.log_to_trajectory([(0.0, 100, 100), (2.0, 1060, 640)], (100, 100, 960, 540))
    assert traj[0][1] == 0.0 and traj[0][2] == 0.0
    assert traj[1][1] == 1.0 and traj[1][2] == 1.0
    # fuera de la region -> clamped a [0,1]
    t2 = cz.log_to_trajectory([(0.0, -500, -500)], (0, 0, 100, 100))
    assert t2[0][1] == 0.0 and t2[0][2] == 0.0


def test_study_summary_quiz():
    segs = ch.parse_srt(SRT_SAMPLE)
    res = study.extractive_summary(segs, n=2)
    assert 1 <= len(res) <= 2 and all(isinstance(s, str) for s in res)
    q = study.cloze_quiz(segs, n=3)
    assert all("______" in it["pregunta"] and it["respuesta"] for it in q)


def test_study_material_html():
    html = study.material_html("- Punto uno\n- Punto dos",
                               [{"pregunta": "2+2?", "respuesta": "4"}], "Mates")
    assert "Mates" in html and "Autoexamen" in html and "2+2?" in html


def test_llm_offline_safe():
    # sin Ollama corriendo, no debe lanzar: available False o int; generate -> str|None
    assert isinstance(llm.available(timeout=1.0), bool)


def test_llm_recommend_model():
    # poca RAM -> modelo ligero; equilibrio -> llama3.2; mucha RAM/GPU -> grande
    assert llm.recommend_model(4, False)[0] == "qwen2.5:1.5b"
    assert llm.recommend_model(8, False)[0] == "llama3.2"
    assert llm.recommend_model(16, False)[0] == "qwen2.5:7b"
    assert llm.recommend_model(32, True)[0] == "llama3.1:8b"
    # RAM desconocida (0) -> fallback seguro, y siempre devuelve (modelo, tamano, motivo)
    rec = llm.recommend_model(0, False)
    assert rec[0] and len(rec) == 3


def test_quality_parsers():
    vol = qc.parse_volumedetect("...\n[Parsed] mean_volume: -23.4 dB\n[Parsed] max_volume: -2.1 dB\n")
    assert vol["mean"] == -23.4 and vol["max"] == -2.1
    bl = qc.parse_blackdetect("black_start:1.0 black_end:3.5 black_duration:2.5\n"
                              "black_start:10 black_end:12")
    assert bl == [(1.0, 3.5), (10.0, 12.0)]


def test_quality_evaluate():
    # micro apagado: max muy bajo -> alerta
    iss = qc.evaluate({"mean": -70, "max": -60}, [], 100, True)
    assert any(i.level == "alerta" for i in iss)
    # audio bajo -> aviso normalizable
    iss = qc.evaluate({"mean": -40, "max": -10}, [], 100, True)
    assert any(i.fix == "normalizar" for i in iss)
    # clipping -> aviso normalizable
    iss = qc.evaluate({"mean": -12, "max": -0.1}, [], 100, True)
    assert any(i.fix == "normalizar" for i in iss)
    # sin audio -> alerta
    assert any(i.level == "alerta" for i in qc.evaluate({}, [], 100, False))
    # todo bien -> ok
    iss = qc.evaluate({"mean": -16, "max": -3}, [], 100, True)
    assert any(i.level == "ok" for i in iss)


def test_parse_srt():
    segs = ch.parse_srt(SRT_SAMPLE)
    assert len(segs) == 3
    assert segs[0][0] == 0.0 and abs(segs[0][1] - 3.0) < 1e-6
    assert "bienvenidos" in segs[0][2]


def test_group_chapters():
    segs = ch.parse_srt(SRT_SAMPLE)
    # hueco de ~14s entre seg 2 (fin 6.0) y seg 3 (inicio 20.0) -> nueva frontera
    chs = ch.group_chapters(segs, min_gap=2.5, min_len=10)
    assert chs[0][0] == 0.0
    assert len(chs) == 2
    assert chs[1][0] == 20.0


def test_youtube_txt_y_ffmetadata():
    chs = [(0.0, "Intro"), (20.0, "Tema 2")]
    txt = ch.youtube_txt(chs)
    assert txt.splitlines()[0].startswith("0:00")
    meta = ch.ffmetadata(chs, total=40.0)
    assert meta.count("[CHAPTER]") == 2 and "title=Tema 2" in meta


def test_autoframe_crop_size():
    # keep: mantiene el aspecto del origen y recorta (zoom)
    cw, ch = afr._crop_size(1920, 1080, "keep", 1.5)
    assert abs((cw / ch) - (1920 / 1080)) < 0.05
    assert cw < 1920 and ch < 1080
    assert cw % 2 == 0 and ch % 2 == 0
    # vertical: aspecto 9:16 dentro del cuadro
    cw2, ch2 = afr._crop_size(1920, 1080, "vertical", 1.3)
    assert abs((cw2 / ch2) - (9 / 16)) < 0.06
    assert cw2 <= 1920 and ch2 <= 1080


def test_autoframe_motion_centers_static():
    import numpy as np
    flat = np.zeros((6, 20, 30), dtype=np.uint8)   # sin movimiento
    cs = afr._motion_centers(flat)
    assert all(abs(c[0] - 0.5) < 1e-6 and abs(c[1] - 0.5) < 1e-6 for c in cs)


def test_autoframe_smooth_bordes():
    # sujeto centrado y quieto -> el suavizado NO debe derivar en los extremos
    s = afr._smooth([0.5] * 20, 4)
    assert abs(s[0] - 0.5) < 1e-9 and abs(s[-1] - 0.5) < 1e-9
    assert all(abs(v - 0.5) < 1e-9 for v in s)


def test_clamp_region():
    # dentro: se mantiene (con w,h pares)
    assert ps.clamp_region(10, 10, 100, 80, 1920, 1080) == (10, 10, 100, 80)
    # se sale por la derecha -> recorta w
    cl = ps.clamp_region(1900, 10, 400, 80, 1920, 1080)
    assert cl is not None and cl[0] + cl[2] <= 1920
    # totalmente fuera -> None
    assert ps.clamp_region(5000, 5000, 100, 80, 1920, 1080) is None


def test_ffmeta_escape():
    meta = ch.ffmetadata([(0.0, "A=B; C#D\nE")], total=10.0)
    assert "title=A\\=B\\; C\\#D E" in meta


def test_quality_no_falso_ok():
    # hay audio pero no se pudo medir -> NO debe decir 'todo bien'
    iss = qc.evaluate({}, [], 100, True)
    assert not any(i.level == "ok" for i in iss)
    assert any(i.level == "aviso" for i in iss)
    # negro detectable aunque la duracion sea 0 (sondeo fallido)
    iss2 = qc.evaluate({"mean": -16, "max": -3}, [(0.0, 5.0)], 0, True)
    assert any("negro" in i.message for i in iss2)


def test_safe_blur_radius():
    # region grande: respeta la fuerza pedida
    assert ps.safe_blur_radius(800, 600, 24) == 24
    # region pequena (la que rompia el filtro): croma limita a min(w,h)//4
    assert ps.safe_blur_radius(200, 80, 24) == 20      # 80//4
    assert ps.safe_blur_radius(12, 8, 24) == 2         # clampado pero valido
    # zona diminuta -> radio < 2 => la capa de blur cae a pixelado
    assert ps.safe_blur_radius(6, 4, 24) < 2


def test_scene_from_dict_robusto():
    # fuente sin 'kind' -> se omite, no aborta; crop malformado -> None
    d = {"name": "X", "sources": [
        {"name": "rota"},                                   # sin kind
        {"kind": "color", "params": {"color": "#111"},
         "transform": {"crop": [1, 2, 3]}},                  # crop de 3 -> None
    ]}
    sc = scn.Scene.from_dict(d)
    assert len(sc.sources) == 1
    assert sc.sources[0].transform.crop is None


# --- Audio: apertura robusta del microfono (feedback tester: mic USB) -------
class _FakeRecorder:
    def __init__(self):
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        return False


class _FakeDevice:
    """Simula un micro USB mono que rechaza 48 kHz estereo (el caso del tester:
    aparecia en la lista pero la pista fallaba y se perdia en silencio)."""

    def __init__(self, name="Micrófono (2- USB PnP Sound Device)", accepts=((44100, 1),)):
        self.name = name
        self.channels = 1
        self._accepts = set(accepts)

    def recorder(self, samplerate, channels, blocksize):
        if (samplerate, channels) not in self._accepts:
            raise RuntimeError(f"formato no soportado: {samplerate}/{channels}")
        return _FakeRecorder()


def test_open_recorder_fallback_usb_mono():
    from capturastudio import audio_capture as cap
    dev = _FakeDevice(accepts=((44100, 1),))
    rec, sr, chn = cap.open_recorder(dev)
    assert (sr, chn) == (44100, 1) and rec.entered


def test_open_recorder_prefiere_config_nativa():
    from capturastudio import audio_capture as cap
    dev = _FakeDevice(accepts=((48000, 1), (44100, 1)))
    rec, sr, chn = cap.open_recorder(dev)
    assert (sr, chn) == (48000, 1)      # respeta 48k si el dispositivo lo acepta


def test_open_recorder_todo_falla():
    from capturastudio import audio_capture as cap
    import pytest
    with pytest.raises(RuntimeError):
        cap.open_recorder(_FakeDevice(accepts=()))


def test_find_microphone_matching():
    from capturastudio import audio_capture as cap

    class _SC:
        def all_microphones(self, include_loopback=False):
            return [_FakeDevice("Micrófono (Razer Seiren X)"),
                    _FakeDevice("Micrófono (2- USB PnP Sound Device)")]

        def default_microphone(self):
            return _FakeDevice("DEFAULT")

    sc = _SC()
    assert cap.find_microphone(sc, "Micrófono (Razer Seiren X)").name.endswith("Seiren X)")
    # subcadena: el sufijo WASAPI puede variar entre sesiones
    assert cap.find_microphone(sc, "2- USB PnP").name.endswith("Sound Device)")
    # no encontrado -> predeterminado (con warning, no en silencio)
    assert cap.find_microphone(sc, "No Existe").name == "DEFAULT"


# --- Preview centrado (feedback tester: se veia pegada a la esquina) --------
def test_pv_rect_centra_y_escala():
    from types import SimpleNamespace
    from capturastudio.app import App

    class _FakeCanvas:
        def __init__(self, w, h):
            self._w, self._h = w, h

        def winfo_width(self):
            return self._w

        def winfo_height(self):
            return self._h

    fake = SimpleNamespace(canvas=_FakeCanvas(1280, 500),
                           scene=SimpleNamespace(canvas_w=1920, canvas_h=1080))
    ox, oy, pw, ph = App._pv_rect(fake)
    assert (pw, ph) == (888, 500)            # limita el alto y mantiene 16:9
    assert ox == (1280 - 888) // 2 and oy == 0   # centrado horizontal
    # widget aun sin layout -> tamano minimo historico, sin division por cero
    fake2 = SimpleNamespace(canvas=_FakeCanvas(1, 1),
                            scene=SimpleNamespace(canvas_w=1920, canvas_h=1080))
    assert App._pv_rect(fake2) == (0, 0, 640, 360)


def test_audiopipe_stereo_upmix():
    # un buffer mono debe duplicarse a estereo antes de escribir al pipe s16le
    import numpy as np
    mono = np.ones((256, 1), dtype="float32") * 0.5
    if mono.ndim == 1:
        mono = mono[:, None]
    if mono.shape[1] == 1:
        up = np.repeat(mono, 2, axis=1)
    assert up.shape == (256, 2) and float(up[0, 1]) == 0.5
