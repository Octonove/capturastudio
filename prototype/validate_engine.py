"""Prototipo de validacion del MOTOR de CapturaStudio (opcion 3).

Valida el riesgo tecnico nº1 del proyecto: componer en UN solo proceso FFmpeg
(filter_complex) varias fuentes en vivo -> pantalla real + webcam (recorte
circular con alpha) + logo PNG con transparencia + subtitulo dinamico (drawtext)
-> codificar a MP4. Si sale bien, el modelo "compositing en el render" del MVP
es viable. Ademas comprueba el filtro whisper (subtitulos IA local) si hay modelo.

Ejecutar:  python validate_engine.py
Salida en: prototype/out/
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def sp_kwargs() -> dict:
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kw["startupinfo"] = si
    return kw


def decode(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Localizacion de FFmpeg / ffprobe
# --------------------------------------------------------------------------
def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for p in Path(local, "Microsoft", "WinGet", "Packages").glob(
            "Gyan.FFmpeg*/**/bin/ffmpeg.exe"
        ):
            return str(p)
    raise SystemExit("FFmpeg no encontrado.")


def ffprobe_from(ffmpeg: str) -> str | None:
    p = Path(ffmpeg).with_name("ffprobe.exe")
    return str(p) if p.is_file() else shutil.which("ffprobe")


def list_encoders(ffmpeg: str) -> set[str]:
    out = decode(subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                capture_output=True, timeout=20, **sp_kwargs()).stdout)
    return {e for e in ("libx264", "h264_nvenc", "h264_amf", "h264_qsv")
            if re.search(rf"\b{e}\b", out)} | {"libx264"}


def first_webcam(ffmpeg: str) -> str | None:
    proc = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true",
                           "-f", "dshow", "-i", "dummy"],
                          capture_output=True, timeout=20, **sp_kwargs())
    text = decode(proc.stderr) + decode(proc.stdout)
    vids = re.findall(r'"([^"]+)"\s*\(video\)', text)
    # Preferimos una webcam "real" antes que filtros virtuales.
    for v in vids:
        if "broadcast" not in v.lower() and "virtual" not in v.lower():
            return v
    return vids[0] if vids else None


def primary_size() -> tuple[int, int]:
    try:
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except (AttributeError, OSError):
        return 1920, 1080


def even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def quality_args(enc: str) -> list[str]:
    if enc == "libx264":
        return ["-preset", "veryfast", "-crf", "18"]
    if enc == "h264_nvenc":
        return ["-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    if enc == "h264_amf":
        return ["-quality", "quality", "-rc", "cqp", "-qp_i", "19", "-qp_p", "19"]
    if enc == "h264_qsv":
        return ["-global_quality", "19", "-preset", "veryfast"]
    return ["-crf", "18"]


# --------------------------------------------------------------------------
# Assets (logo + mascara circular) generados con Pillow
# --------------------------------------------------------------------------
def make_assets() -> tuple[Path, Path]:
    from PIL import Image, ImageDraw, ImageFont

    # Logo con alpha: insignia redondeada navy con texto.
    logo = Image.new("RGBA", (420, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo)
    d.rounded_rectangle([0, 0, 419, 119], radius=24, fill=(30, 58, 95, 235))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    d.ellipse([18, 28, 80, 90], fill=(206, 110, 97, 255))
    d.text((96, 32), "CapturaStudio", fill=(255, 255, 255, 255), font=font)
    logo_path = OUT / "logo.png"
    logo.save(logo_path)

    # Mascara circular (blanco sobre negro) para el recorte de la webcam.
    size = 360
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([6, 6, size - 6, size - 6], fill=255)
    mask_path = OUT / "circle_mask.png"
    mask.save(mask_path)
    return logo_path, mask_path


# --------------------------------------------------------------------------
# Compositing
# --------------------------------------------------------------------------
def build_composite(ffmpeg: str, enc: str, cam_spec: list[str], logo: Path,
                    mask: Path, pw: int, ph: int, out_path: Path,
                    seconds: int = 6) -> list[str]:
    font = "C\\:/Windows/Fonts/segoeui.ttf"
    caption = "CapturaStudio  -  subtitulos en vivo (demo)"
    graph = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[bg];"
        "[1:v]scale=360:360:force_original_aspect_ratio=increase,crop=360:360,"
        "format=rgba[cam];"
        "[3:v]format=gray,scale=360:360[m];"
        "[cam][m]alphamerge[camc];"
        "[bg][camc]overlay=W-w-40:H-h-40:format=auto[v1];"
        "[2:v]scale=320:-1[logo];"
        "[v1][logo]overlay=40:40[v2];"
        f"[v2]drawtext=fontfile='{font}':text='{caption}':fontcolor=white:"
        "fontsize=40:box=1:boxcolor=0x1E3A5F@0.85:boxborderw=20:"
        "x=(w-text_w)/2:y=h-130[vout]"
    )
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
           "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "1",
           "-offset_x", "0", "-offset_y", "0",
           "-video_size", f"{even(pw)}x{even(ph)}", "-i", "desktop"]
    cmd += cam_spec
    cmd += ["-loop", "1", "-i", str(logo)]
    cmd += ["-loop", "1", "-i", str(mask)]
    cmd += ["-filter_complex", graph, "-map", "[vout]"]
    cmd += ["-t", str(seconds), "-c:v", enc]
    cmd += quality_args(enc)
    cmd += ["-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", str(out_path)]
    return cmd


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **sp_kwargs())
    return proc.returncode, decode(proc.stderr)[-1500:]


def ffprobe_info(ffprobe: str, path: Path) -> str:
    if not ffprobe or not path.is_file():
        return "(sin ffprobe o sin archivo)"
    out = decode(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,duration",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, timeout=20, **sp_kwargs()).stdout)
    return out.strip()


# --------------------------------------------------------------------------
# Whisper (subtitulos IA local) - opcional si hay modelo
# --------------------------------------------------------------------------
def find_whisper_model() -> Path | None:
    base = Path(os.environ.get("APPDATA", "")) / "TranscriptorIA" / "models"
    for name in ("ggml-base.bin", "ggml-small.bin", "ggml-tiny.bin"):
        p = base / name
        if p.is_file() and p.stat().st_size > 1_000_000:
            return p
    return None


def tts_wav(text: str, out_wav: Path) -> bool:
    """Genera voz en espanol con SAPI (PowerShell) para validar Whisper."""
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SetOutputToWaveFile('{out_wav}');"
        f"$s.Speak('{text}');$s.Dispose()"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60, **sp_kwargs())
        return r.returncode == 0 and out_wav.is_file() and out_wav.stat().st_size > 1024
    except (OSError, subprocess.SubprocessError):
        return False


def whisper_srt(ffmpeg: str, model: Path, wav: Path, out_srt: Path) -> bool:
    model_dir = str(model.parent)
    tmp = f".proto_{os.getpid()}.srt"
    filt = (f"aresample=16000,whisper=model={model.name}:language=es"
            f":use_gpu=false:destination={tmp}:format=srt")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(wav), "-af", filt, "-f", "null", "-"]
    subprocess.run(cmd, cwd=model_dir, capture_output=True, timeout=180, **sp_kwargs())
    src = Path(model_dir) / tmp
    if src.is_file():
        shutil.move(str(src), str(out_srt))
        return out_srt.is_file() and out_srt.stat().st_size > 0
    return False


# --------------------------------------------------------------------------
def main() -> int:
    print("=" * 60)
    print(" PROTOTIPO MOTOR CapturaStudio - validacion")
    print("=" * 60)
    ffmpeg = find_ffmpeg()
    ffprobe = ffprobe_from(ffmpeg)
    print(f"FFmpeg: {ffmpeg}")
    encs = list_encoders(ffmpeg)
    enc = next((e for e in ("h264_nvenc", "h264_amf", "libx264") if e in encs), "libx264")
    print(f"Encoders: {sorted(encs)}  ->  usando: {enc}")
    cam = first_webcam(ffmpeg)
    print(f"Webcam detectada: {cam!r}")
    pw, ph = primary_size()
    print(f"Pantalla principal: {pw}x{ph}")
    logo, mask = make_assets()
    print(f"Assets: {logo.name}, {mask.name}")

    results: dict[str, bool] = {}

    # --- TEST 1: compositing con webcam real ---
    print("\n[1] Compositing pantalla + webcam(circular) + logo + subtitulo...")
    out1 = OUT / "composite_webcam.mp4"
    ok1 = False
    if cam:
        cam_spec = ["-f", "dshow", "-rtbufsize", "256M", "-i", f"video={cam}"]
        cmd = build_composite(ffmpeg, enc, cam_spec, logo, mask, pw, ph, out1)
        rc, err = run(cmd)
        ok1 = rc == 0 and out1.is_file() and out1.stat().st_size > 10_000
        if not ok1:
            print(f"    webcam real fallo (rc={rc}). Detalle: {err[-400:]}")
    results["compositing_webcam_real"] = ok1
    if ok1:
        print(f"    OK -> {out1.name} ({out1.stat().st_size // 1024} KB)")
        print(f"    ffprobe: {ffprobe_info(ffprobe, out1)}")

    # --- TEST 2: compositing con fuente sintetica (robustez del grafo) ---
    print("\n[2] Compositing con fuente de prueba (valida el grafo sin depender de webcam)...")
    out2 = OUT / "composite_testsrc.mp4"
    cam_spec = ["-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30"]
    cmd = build_composite(ffmpeg, enc, cam_spec, logo, mask, pw, ph, out2)
    rc, err = run(cmd)
    ok2 = rc == 0 and out2.is_file() and out2.stat().st_size > 10_000
    results["compositing_grafo"] = ok2
    if ok2:
        print(f"    OK -> {out2.name} ({out2.stat().st_size // 1024} KB)")
        print(f"    ffprobe: {ffprobe_info(ffprobe, out2)}")
    else:
        print(f"    FALLO (rc={rc}). Detalle: {err[-400:]}")

    # --- TEST 3: Whisper (subtitulos IA local) ---
    print("\n[3] Subtitulos IA local (filtro whisper)...")
    model = find_whisper_model()
    if model is None:
        print("    (omitido: no hay modelo Whisper en %APPDATA%/TranscriptorIA/models;")
        print("     el filtro whisper ya esta validado en TranscriptorIA. Se integrara igual.)")
        results["whisper"] = None  # type: ignore
    else:
        wav = OUT / "tts.wav"
        srt = OUT / "subs.srt"
        if tts_wav("Hola, esto es una prueba de subtitulos automaticos en espanol "
                   "generados de forma local con inteligencia artificial.", wav):
            ok3 = whisper_srt(ffmpeg, model, wav, srt)
            results["whisper"] = ok3
            if ok3:
                print(f"    OK -> {srt.name}:")
                print("    " + srt.read_text(encoding="utf-8", errors="replace").strip()[:300].replace("\n", "\n    "))
            else:
                print("    FALLO al generar SRT.")
        else:
            print("    (no se pudo generar TTS de prueba; Whisper no validado aqui)")
            results["whisper"] = None  # type: ignore

    # --- Resumen ---
    print("\n" + "=" * 60)
    print(" RESUMEN")
    print("=" * 60)
    for k, v in results.items():
        tag = "OMITIDO" if v is None else ("PASA" if v else "FALLA")
        print(f"  [{tag}] {k}")
    critical = results.get("compositing_grafo")
    print("\nVEREDICTO:", "MOTOR VIABLE (compositing OK)" if critical else "REVISAR (compositing fallo)")
    return 0 if critical else 1


if __name__ == "__main__":
    sys.exit(main())
