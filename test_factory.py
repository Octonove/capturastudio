"""Valida la fabrica de contenido: vertical 9:16 + audio MP3 + GIF."""

import subprocess
import sys
from pathlib import Path

from capturastudio import ffmpeg_utils as fu
from capturastudio import content_factory as cf

OUT = Path(__file__).resolve().parent / "prototype" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    ffprobe = fu.ffprobe_from(ffmpeg)
    enc = fu.resolve_encoder("auto", fu.list_encoders(ffmpeg))

    src = OUT / "factory_src.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "5", "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(src)], capture_output=True, timeout=60, **fu.subprocess_kwargs())
    print(f"fuente: {src.name} ({cf.ai_post.get_duration(ffmpeg, str(src)):.0f}s)")

    files = cf.make_package(ffmpeg, str(src), str(OUT / "paquete"),
                            vertical=True, audio=True, gif=True, subtitles=False,
                            encoder=enc, quality_key="media")
    print("\nEntregables generados:")
    ok = True
    for f in files:
        p = Path(f)
        exists = p.is_file() and p.stat().st_size > 1000
        dims = ""
        if p.suffix == ".mp4" and ffprobe:
            dims = fu._decode(subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0:s=x", str(p)],
                capture_output=True, timeout=20, **fu.subprocess_kwargs()).stdout).strip()
        print(f"  [{'OK' if exists else 'FALLA'}] {p.name}  {dims} ({p.stat().st_size // 1024 if p.is_file() else 0} KB)")
        ok = ok and exists
        if p.name.endswith("_vertical_9x16.mp4") and dims and dims != "1080x1920":
            print(f"      AVISO: vertical no es 1080x1920 ({dims})"); ok = False

    print("\nVEREDICTO:", "FABRICA OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
