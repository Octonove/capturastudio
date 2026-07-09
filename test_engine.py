"""Test del motor generalizado: construye una Escena (modelo de datos) con varias
fuentes y la graba 5s usando build_record_command. Valida que el builder de
escena equivale al prototipo, y prueba la serializacion JSON de la escena.

Ejecutar desde la carpeta CapturaStudio:  python test_engine.py
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path

from capturastudio import scene as scn
from capturastudio import ffmpeg_utils as fu
from capturastudio.config import work_dir

OUT = Path(__file__).resolve().parent / "prototype" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def primary_region() -> tuple[int, int, int, int]:
    try:
        u = ctypes.windll.user32
        return (0, 0, int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1)))
    except (AttributeError, OSError):
        return (0, 0, 1920, 1080)


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    if not ffmpeg:
        print("FFmpeg no encontrado."); return 1
    ffprobe = fu.ffprobe_from(ffmpeg)
    encs = fu.list_encoders(ffmpeg)
    enc = fu.resolve_encoder("auto", encs)
    cams = fu.list_video_devices(ffmpeg)
    cam = next((c for c in cams if "broadcast" not in c.lower()), cams[0] if cams else None)
    print(f"Encoder: {enc} | Webcam: {cam!r}")

    # Reutiliza el logo del prototipo como fuente de imagen.
    logo = OUT / "logo.png"

    # --- Construir la escena con el modelo de datos ---
    scene = scn.Scene(name="Demo", canvas_w=1920, canvas_h=1080, fps=30)
    scene.add(scn.screen_source(primary_region()))                       # base
    if logo.is_file():
        scene.add(scn.image_source(str(logo), x=40, y=40, w=300))        # logo
    scene.add(scn.text_source("CapturaStudio · grabando en vivo", x=480, y=950,
                              size=44, color="#FFFFFF", bg="#1E3A5F"))    # texto
    if cam:
        scene.add(scn.webcam_source(cam, x=1500, y=680, size=360, circle=True))  # webcam circular

    print(f"Fuentes: {[s.label() for s in scene.visible_sorted()]}")

    # --- Serializacion round-trip ---
    d = scene.to_dict()
    scene2 = scn.Scene.from_dict(json.loads(json.dumps(d)))
    assert len(scene2.sources) == len(scene.sources), "fallo round-trip de escena"
    print("Serializacion JSON de la escena: OK")

    # --- Construir y ejecutar el comando ---
    out = OUT / "engine_scene.mp4"
    cmd = fu.build_record_command(ffmpeg_path=ffmpeg, scene=scene, encoder=enc,
                                  quality_key="alta", output_path=str(out),
                                  cursor=True, duration=5, tmp=work_dir())
    # Muestra el filter_complex generado (para revision)
    fc_idx = cmd.index("-filter_complex") + 1
    print("\nfilter_complex generado:\n  " + cmd[fc_idx].replace(";", ";\n  "))

    print("\nGrabando 5s...")
    proc = subprocess.run(cmd, capture_output=True, timeout=120, **fu.subprocess_kwargs())
    ok = proc.returncode == 0 and out.is_file() and out.stat().st_size > 10_000
    if not ok:
        print("FALLO:", fu._decode(proc.stderr)[-600:]); return 1

    info = ""
    if ffprobe:
        info = fu._decode(subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height,duration", "-of",
             "default=noprint_wrappers=1", str(out)],
            capture_output=True, timeout=20, **fu.subprocess_kwargs()).stdout).strip()
    print(f"\nOK -> {out.name} ({out.stat().st_size // 1024} KB)")
    print("ffprobe:", info.replace("\n", " | "))
    print("\nVEREDICTO: MOTOR DE ESCENA OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
