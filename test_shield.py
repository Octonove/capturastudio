"""Valida el escudo de privacidad: difumina una region fija y otra temporal."""

import subprocess
import sys
from pathlib import Path

from capturastudio import ffmpeg_utils as fu
from capturastudio import privacy_shield as ps

OUT = Path(__file__).resolve().parent / "prototype" / "out"


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    ffprobe = fu.ffprobe_from(ffmpeg)
    enc = fu.resolve_encoder("auto", fu.list_encoders(ffmpeg))

    src = OUT / "shield_src.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "5",
        "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, timeout=60, **fu.subprocess_kwargs())

    out = OUT / "shield_out.mp4"
    regions = [
        ps.BlurRegion(x=80, y=60, w=400, h=120),                    # fija (todo el video)
        ps.BlurRegion(x=700, y=400, w=300, h=200, start=1, end=3),  # solo 1-3s
    ]
    ps.blur_regions(ffmpeg, str(src), str(out), regions, encoder=enc, quality_key="media")
    info = fu._decode(subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,duration", "-of", "default=nw=1", str(out)],
        capture_output=True, timeout=20, **fu.subprocess_kwargs()).stdout).strip()
    ok = out.is_file() and out.stat().st_size > 5000 and "width=1280" in info

    # extrae un frame en t=2 (deberia tener ambas zonas difuminadas)
    frame = OUT / "shield_frame.jpg"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", "2",
                    "-i", str(out), "-frames:v", "1", str(frame)],
                   capture_output=True, timeout=30, **fu.subprocess_kwargs())

    print(f"salida: {out.name} ({out.stat().st_size // 1024} KB) | {info.replace(chr(10),' ')}")
    print(f"frame t=2: {frame.name} ({'OK' if frame.is_file() else 'no'})")
    print("\nVEREDICTO:", "ESCUDO OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
