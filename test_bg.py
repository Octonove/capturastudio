"""Valida quitar fondo con IA local (rembg/u2net), incluida la descarga robusta
del modelo. Usa una imagen real como entrada."""

import subprocess
import sys
from pathlib import Path

from capturastudio import ffmpeg_utils as fu
from capturastudio import bg_removal as bg

OUT = Path(__file__).resolve().parent / "prototype" / "out"


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    # entrada: un frame real de la pantalla
    src = OUT / "bg_src.png"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "gdigrab", "-video_size", "1280x720", "-i", "desktop",
                    "-frames:v", "1", str(src)], capture_output=True, timeout=30,
                   **fu.subprocess_kwargs())
    if not src.is_file():
        print("No se pudo capturar una imagen de prueba."); return 1
    print(f"entrada: {src.name}")

    print("Modelo presente:", bg.model_ready())
    print("Quitando fondo (descarga modelo si falta)...")
    out_png = OUT / "bg_cutout.png"
    bg.remove_bg(str(src), str(out_png))

    from PIL import Image
    im = Image.open(out_png)
    alphas = im.getchannel("A").getextrema() if im.mode == "RGBA" else (255, 255)
    has_transparency = im.mode == "RGBA" and alphas[0] < 250
    print(f"salida: {out_png.name} | modo={im.mode} | alpha_min={alphas[0]} | "
          f"transparencia={has_transparency} ({out_png.stat().st_size // 1024} KB)")

    print("Reemplazando fondo por color navy...")
    out_jpg = OUT / "bg_replaced.jpg"
    bg.replace_bg(str(src), str(out_jpg), "#1E3A5F")
    ok2 = out_jpg.is_file() and out_jpg.stat().st_size > 5000

    ok = out_png.is_file() and im.mode == "RGBA" and ok2
    print("\nVEREDICTO:", "FONDO IA OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
