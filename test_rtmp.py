"""Push RTMP real a un servidor FFmpeg local (aislado, proceso limpio)."""

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


def region():
    u = ctypes.windll.user32
    return (0, 0, int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1)))


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    ffprobe = fu.ffprobe_from(ffmpeg)
    enc = fu.resolve_encoder("auto", fu.list_encoders(ffmpeg))
    sc = scn.Scene(canvas_w=1280, canvas_h=720, fps=30)
    sc.add(scn.screen_source(region()))
    sc.add(scn.text_source("EN DIRECTO", x=520, y=620, size=34))

    url = "rtmp://127.0.0.1:1936/live/test"
    server_out = OUT / "rtmp_server.mkv"
    server_out.unlink(missing_ok=True)
    print("Arrancando servidor RTMP local...")
    server = subprocess.Popen(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-f", "flv",
         "-listen", "1", "-i", url, "-c", "copy", "-f", "matroska", str(server_out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **fu.subprocess_kwargs())
    time.sleep(2.0)

    print("Emitiendo 5s al servidor...")
    cmd = st.build_stream_command(ffmpeg_path=ffmpeg, scene=sc, encoder=enc,
                                  bitrate_k=3500, has_audio=True, ingest=url,
                                  duration=5, tmp=fu.work_dir())
    client = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, **fu.subprocess_kwargs())
    ap = st.AudioPipe(system=True, mic_name=None)
    ap.start(client.stdin)
    try:
        client.wait(timeout=30)
    except subprocess.TimeoutExpired:
        client.terminate()
    ap.stop()
    time.sleep(1.5)
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=6)
        except subprocess.TimeoutExpired:
            server.kill()

    info = ""
    if server_out.is_file():
        info = fu._decode(subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "default=nw=1", str(server_out)],
            capture_output=True, timeout=20, **fu.subprocess_kwargs()).stdout)
    ok = server_out.is_file() and "codec_type=video" in info and "codec_type=audio" in info
    size = server_out.stat().st_size // 1024 if server_out.is_file() else 0
    print(f"\nServidor recibio: {size} KB | streams: {info.replace(chr(10), ' ').strip()}")
    if not ok:
        print("client err:", fu._decode(client.stderr.read())[-250:] if client.stderr else "")
        print("server err:", fu._decode(server.stderr.read())[-250:] if server.stderr else "")
    print("\nVEREDICTO:", "PUSH RTMP OK" if ok else "RTMP no validado localmente")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
