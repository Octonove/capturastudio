"""Smoke test de RecordEngine: graba con pausa/reanudar + audio del sistema y
verifica el archivo final (video + audio). Ejecutar desde CapturaStudio/."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

from capturastudio import scene as scn
from capturastudio import ffmpeg_utils as fu
from capturastudio.engine import RecordEngine

OUT = Path(__file__).resolve().parent / "prototype" / "out"
RESULT: dict = {}


def primary_region():
    try:
        u = ctypes.windll.user32
        return (0, 0, int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1)))
    except (AttributeError, OSError):
        return (0, 0, 1920, 1080)


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    enc = fu.resolve_encoder("auto", fu.list_encoders(ffmpeg))
    cams = fu.list_video_devices(ffmpeg)
    cam = next((c for c in cams if "broadcast" not in c.lower()), None)

    scene = scn.Scene(canvas_w=1280, canvas_h=720, fps=30)
    scene.add(scn.screen_source(primary_region()))
    scene.add(scn.text_source("CapturaStudio · test grabacion", x=300, y=620, size=36))
    if cam:
        scene.add(scn.webcam_source(cam, x=950, y=440, size=240, circle=True))

    def on_state(state, path):
        print(f"  [estado] {state}" + (f" -> {path}" if path else ""))
        if state == "saved":
            RESULT["path"] = path
        if state in ("saved", "error"):
            RESULT["done"] = True

    def on_error(msg):
        print(f"  [ERROR] {msg}")
        RESULT["done"] = True
        RESULT["error"] = msg

    eng = RecordEngine(ffmpeg_path=ffmpeg, scene=scene, encoder=enc, quality_key="media",
                       container="mp4", cursor=True, audio_system=True,
                       audio_mic_device="", denoise=False, out_dir=str(OUT),
                       on_state=on_state, on_error=on_error)

    print(f"Encoder={enc} cam={cam!r}. Grabando 2s, pausa, 2s, stop...")
    eng.start()
    time.sleep(2.0)
    eng.pause()
    print("  (pausado 0.6s)")
    time.sleep(0.6)
    eng.resume()
    time.sleep(2.0)
    eng.stop()

    for _ in range(200):  # esperar finalizacion (max 20s)
        if RESULT.get("done"):
            break
        time.sleep(0.1)

    if RESULT.get("error") or not RESULT.get("path"):
        print("\nVEREDICTO: FALLO"); return 1
    final = Path(RESULT["path"])
    ffprobe = fu.ffprobe_from(ffmpeg)
    info = fu._decode(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration",
         "-of", "default=noprint_wrappers=1", str(final)],
        capture_output=True, timeout=20, **fu.subprocess_kwargs()).stdout).strip()
    has_audio = "codec_type=audio" in info
    has_video = "codec_type=video" in info
    print(f"\nFinal: {final.name} ({final.stat().st_size // 1024} KB)")
    print("streams:", info.replace("\n", " | "))
    print(f"video={has_video} audio={has_audio}")
    print("\nVEREDICTO:", "GRABACION OK" if (has_video and has_audio) else "REVISAR (falta pista)")
    return 0 if (has_video and has_audio) else 1


if __name__ == "__main__":
    sys.exit(main())
