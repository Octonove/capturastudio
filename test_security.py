"""Valida la Fase A: anti-inyeccion, secretos (DPAPI), redaccion de logs y
robustez del encoder (probe + fallback)."""

import sys

from capturastudio import ffmpeg_utils as fu
from capturastudio import ai_post, secrets
from capturastudio.config import _RedactingFilter


def main() -> int:
    ok = True

    # --- safe_color ---
    cases = {
        "#1E3A5F": "0x1E3A5F", "1e3a5f": "0x1e3a5f", "0xAABBCC": "0xAABBCC",
        "black": "black",
        "red:s=2x2[x];movie=evil.mp4": "0x101418",   # inyeccion -> default
        "'; drop": "0x101418", "../../x": "0x101418",
    }
    for inp, exp in cases.items():
        got = fu.safe_color(inp)
        if got != exp:
            print(f"  [FALLA] safe_color({inp!r}) = {got!r}, esperado {exp!r}"); ok = False
    print("safe_color: OK" if ok else "safe_color: FALLA")

    # --- _safe_lang ---
    langs = {"es": "es", "en": "en", "auto": "auto", "AUTO": "auto",
             "es:use_gpu=true": "auto", "../x": "auto", "e": "auto", "": "auto"}
    lok = True
    for inp, exp in langs.items():
        if ai_post._safe_lang(inp) != exp:
            print(f"  [FALLA] _safe_lang({inp!r}) = {ai_post._safe_lang(inp)!r}"); lok = False; ok = False
    print("_safe_lang: OK" if lok else "_safe_lang: FALLA")

    # --- DPAPI round-trip ---
    secret = "live_1234567890_ABCdefKEY"
    try:
        enc = secrets.dpapi_encrypt(secret)
        dec = secrets.dpapi_decrypt(enc)
        dpok = (dec == secret) and (secret not in enc)
    except Exception as exc:  # noqa: BLE001
        print("  DPAPI error:", exc); dpok = False
    print(f"DPAPI cifrado/descifrado: {'OK' if dpok else 'FALLA'} (cifrado != claro: {secret not in enc})")
    ok = ok and dpok

    # --- redaccion de logs ---
    rf = _RedactingFilter()
    import logging
    rec = logging.LogRecord("x", logging.INFO, "f", 1,
                            "Stream: ffmpeg -f flv rtmp://live.twitch.tv/app/live_SECRETO123", (), None)
    rf.filter(rec)
    redok = "SECRETO123" not in rec.getMessage() and "***" in rec.getMessage()
    print(f"redaccion de stream_key en logs: {'OK' if redok else 'FALLA'} -> {rec.getMessage()[-40:]}")
    ok = ok and redok

    # --- encoder probe + fallback ---
    ffmpeg = fu.find_ffmpeg()
    encs = fu.list_encoders(ffmpeg)
    chosen = fu.resolve_encoder("auto", encs, ffmpeg)
    opens = fu.encoder_opens(ffmpeg, chosen)
    fake = fu.resolve_encoder("h264_nvenc_inexistente", encs, ffmpeg)  # no en available -> auto
    print(f"resolve_encoder(auto) -> {chosen} (abre={opens}); fallback de encoder invalido -> {fake}")
    encok = opens and fake in encs
    ok = ok and encok

    print("\nVEREDICTO:", "FASE A OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
