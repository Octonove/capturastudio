"""Valida el streaming: (1) pipeline de audio EN VIVO por stdin -> fichero;
(2) push RTMP real a un servidor FFmpeg local (-listen 1)."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

from capturastudio import scene as scn
from capturastudio import ffmpeg_utils as fu
from capturastudio import streaming as st

OUT = Path(__file__).resolve().parent / "prototype" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def region():
    try:
        u = ctypes.windll.user32
        return (0, 0, int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1)))
    except (AttributeError, OSError):
        return (0, 0, 1920, 1080)


def make_scene():
    sc = scn.Scene(canvas_w=1280, canvas_h=720, fps=30)
    sc.add(scn.screen_source(region()))
    sc.add(scn.text_source("CapturaStudio · EN DIRECTO", x=380, y=620, size=34))
    cams = fu.list_video_devices(fu.find_ffmpeg())
    cam = next((c for c in cams if "broadcast" not in c.lower()), None)
    if cam:
        sc.add(scn.webcam_source(cam, x=970, y=440, size=240, circle=True))
    return sc


def probe(ffprobe, path):
    return fu._decode(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "default=nw=1", str(path)], capture_output=True, timeout=20,
        **fu.subprocess_kwargs()).stdout)


def run_pipeline_to(ffmpeg, scene, enc, output=None, ingest=None, secs=5):
    cmd = st.build_stream_command(
        ffmpeg_path=ffmpeg, scene=scene, encoder=enc, bitrate_k=4000,
        has_audio=True, ingest=ingest or "", output_override=output, duration=secs,
        tmp=fu.work_dir())
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, **fu.subprocess_kwargs())
    ap = st.AudioPipe(system=True, mic_name=None)
    ap.start(proc.stdin)
    try:
        proc.wait(timeout=secs + 20)
    except subprocess.TimeoutExpired:
        proc.terminate()
    ap.stop()
    err = fu._decode(proc.stderr.read()) if proc.stderr else ""
    return proc.returncode, err


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    ffprobe = fu.ffprobe_from(ffmpeg)
    enc = fu.resolve_encoder("auto", fu.list_encoders(ffmpeg))
    scene = make_scene()
    print(f"Encoder: {enc}")

    # --- TEST 1: pipeline audio en vivo -> fichero mkv ---
    print("\n[1] Pipeline compositing + audio en vivo (pipe) -> fichero...")
    f1 = OUT / "stream_pipeline.mkv"
    rc, err = run_pipeline_to(ffmpeg, scene, enc, output=str(f1), secs=5)
    info = probe(ffprobe, f1) if f1.is_file() else ""
    ok1 = f1.is_file() and "codec_type=video" in info and "codec_type=audio" in info
    print(f"    rc={rc} -> {f1.name} ({f1.stat().st_size // 1024 if f1.is_file() else 0} KB)")
    print("    streams:", info.replace("\n", " ").strip())
    if not ok1:
        print("    err:", err[-300:])

    # --- TEST 2: push RTMP real a servidor local ---
    print("\n[2] Push RTMP real a servidor FFmpeg local (rtmp://127.0.0.1:1935)...")
    url = "rtmp://127.0.0.1:1935/live/test"
    server_out = OUT / "stream_rtmp_server.mkv"
    server = subprocess.Popen(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "flv",
         "-listen", "1", "-i", url, "-c", "copy", "-t", "12", "-f", "matroska", str(server_out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **fu.subprocess_kwargs())
    time.sleep(1.5)  # que el servidor empiece a escuchar
    ok2 = False
    try:
        rc, err = run_pipeline_to(ffmpeg, scene, enc, ingest=url, secs=5)
        time.sleep(1.0)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate()
            server.wait(timeout=5)
        info2 = probe(ffprobe, server_out) if server_out.is_file() else ""
        ok2 = server_out.is_file() and "codec_type=video" in info2 and "codec_type=audio" in info2
        print(f"    cliente rc={rc} | servidor grabo: {server_out.name} "
              f"({server_out.stat().st_size // 1024 if server_out.is_file() else 0} KB)")
        print("    streams recibidos:", info2.replace("\n", " ").strip())
        if not ok2:
            print("    client err:", err[-200:])
            print("    server err:", fu._decode(server.stderr.read())[-200:] if server.stderr else "")
    finally:
        if server.poll() is None:
            server.kill()

    print("\nRESUMEN:")
    print(f"  [{'PASA' if ok1 else 'FALLA'}] pipeline_audio_en_vivo")
    print(f"  [{'PASA' if ok2 else 'FALLA'}] push_rtmp_real")
    return 0 if ok1 else 1


if __name__ == "__main__":
    sys.exit(main())
