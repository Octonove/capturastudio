"""Valida el replay buffer (Time Machine): buffer rotatorio + guardar momento."""

import ctypes
import subprocess
import sys
import time
from pathlib import Path

from capturastudio import scene as scn
from capturastudio import ffmpeg_utils as fu
from capturastudio import ai_post
from capturastudio.replay import ReplayBuffer

OUT = Path(__file__).resolve().parent / "prototype" / "out"


def region():
    u = ctypes.windll.user32
    return (0, 0, int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1)))


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    ffprobe = fu.ffprobe_from(ffmpeg)
    enc = fu.resolve_encoder("auto", fu.list_encoders(ffmpeg))
    sc = scn.Scene(canvas_w=1280, canvas_h=720, fps=30)
    sc.add(scn.screen_source(region()))
    sc.add(scn.text_source("REPLAY BUFFER", x=480, y=620, size=34))

    rb = ReplayBuffer(ffmpeg_path=ffmpeg, scene=sc, encoder=enc, bitrate_k=5000,
                      audio_system=True, audio_mic_device="", out_dir=str(OUT),
                      seg_seconds=4, buffer_seconds=40)
    print("Arrancando buffer (4s/segmento)...")
    rb.start()
    if rb.state != "buffering":
        print("No arranco."); return 1
    print("Acumulando 14s de buffer...")
    time.sleep(14)
    print("Guardando los ultimos ~8s...")
    clip = rb.save_moment(8)
    rb.stop()

    dur = ai_post.get_duration(ffmpeg, clip)
    info = fu._decode(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "default=nw=1", clip],
        capture_output=True, timeout=20, **fu.subprocess_kwargs()).stdout) if ffprobe else ""
    has_v = "codec_type=video" in info
    has_a = "codec_type=audio" in info
    p = Path(clip)
    print(f"\nClip: {p.name} ({p.stat().st_size // 1024} KB) | dur={dur:.1f}s | video={has_v} audio={has_a}")
    ok = p.is_file() and 4 <= dur <= 13 and has_v
    print("VEREDICTO:", "TIME MACHINE OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
