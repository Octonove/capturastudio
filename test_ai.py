"""Valida la post-produccion IA: descarga modelo tiny, crea un video de prueba
con voz + silencio, y prueba transcribe_srt + cut_silences + burn_subtitles."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from capturastudio import ffmpeg_utils as fu
from capturastudio import models, ai_post

OUT = Path(__file__).resolve().parent / "prototype" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def tts(text: str, wav: Path) -> bool:
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SetOutputToWaveFile('{wav}');$s.Speak('{text}');$s.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=60, **fu.subprocess_kwargs())
    return r.returncode == 0 and wav.is_file()


def main() -> int:
    ffmpeg = fu.find_ffmpeg()
    enc = fu.resolve_encoder("auto", fu.list_encoders(ffmpeg))

    print("[1] Modelo Whisper...")
    key = models.first_available() or "tiny"
    if not models.is_downloaded(key):
        print(f"    descargando {key} (~75MB)...")
        models.download(key, progress_cb=lambda f: None)
    model = str(models.model_path(key))
    print(f"    modelo: {key} -> {model}")

    print("[2] Generando voz de prueba + silencio...")
    tts1 = OUT / "v1.wav"
    if not tts(("Hola, esto es CapturaStudio. Vamos a probar los subtitulos "
                "automaticos y el recorte de silencios."), tts1):
        print("    no se pudo generar TTS"); return 1
    audio_full = OUT / "audio_full.wav"
    # voz + 2.5s de silencio + voz  (para que haya un silencio detectable)
    sub = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(tts1), "-f", "lavfi", "-t", "2.5", "-i", "anullsrc=r=16000:cl=mono",
        "-i", str(tts1),
        "-filter_complex", "[0:a]aresample=16000[a0];[1:a]aresample=16000[a1];"
        "[2:a]aresample=16000[a2];[a0][a1][a2]concat=n=3:v=0:a=1[a]",
        "-map", "[a]", str(audio_full)], capture_output=True, timeout=60, **fu.subprocess_kwargs())
    if sub.returncode != 0:
        print("    fallo audio:", fu._decode(sub.stderr)[-300:]); return 1

    test_video = OUT / "ai_test_video.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-i", str(audio_full),
        "-map", "0:v", "-map", "1:a", "-shortest", "-c:v", "libx264", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(test_video)],
        capture_output=True, timeout=120, **fu.subprocess_kwargs())
    dur = ai_post.get_duration(ffmpeg, str(test_video))
    print(f"    video de prueba: {dur:.1f}s")

    print("[3] Subtitulos (Whisper)...")
    srt = OUT / "ai_subs.srt"
    text = ai_post.transcribe_srt(ffmpeg, model, str(test_video), "es", str(srt))
    print("    SRT:\n      " + text.strip()[:240].replace("\n", "\n      "))
    ok_srt = srt.is_file() and len(text.strip()) > 0

    print("[4] Recorte de silencios (auto-jumpcut)...")
    cut = OUT / "ai_cut.mp4"
    info = ai_post.cut_silences(ffmpeg, str(test_video), str(cut), noise_db=-30,
                                min_silence=0.6, padding=0.1, encoder=enc, quality_key="media")
    print(f"    {info['orig']:.1f}s -> {info['final']:.1f}s en {info['segmentos']} tramos")
    ok_cut = cut.is_file() and info["final"] < info["orig"] - 1.0

    print("[5] Quemar subtitulos...")
    burned = OUT / "ai_burned.mp4"
    ai_post.burn_subtitles(ffmpeg, str(cut), str(srt), str(burned), encoder=enc, quality_key="media")
    ok_burn = burned.is_file() and burned.stat().st_size > 10_000
    print(f"    -> {burned.name} ({burned.stat().st_size // 1024} KB)" if ok_burn else "    FALLO")

    print("\nRESUMEN:")
    for k, v in [("subtitulos", ok_srt), ("recorte_silencios", ok_cut), ("quemado", ok_burn)]:
        print(f"  [{'PASA' if v else 'FALLA'}] {k}")
    return 0 if (ok_srt and ok_cut and ok_burn) else 1


if __name__ == "__main__":
    sys.exit(main())
