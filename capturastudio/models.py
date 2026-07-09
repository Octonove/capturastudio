"""Modelos Whisper (GGML de whisper.cpp). Reutiliza los de TranscriptorIA si
ya estan descargados (config.models_dir apunta alli cuando existe)."""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

from .config import models_dir

logger = logging.getLogger(__name__)

_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
MODELS = {
    "tiny":  ("ggml-tiny.bin",  75,  "Rapido · menos preciso"),
    "base":  ("ggml-base.bin",  142, "Equilibrado (recomendado)"),
    "small": ("ggml-small.bin", 466, "Mas preciso · mas lento"),
}
ORDER = ["tiny", "base", "small"]

# SHA-256 oficiales para verificar integridad tras la descarga. Solo se fijan los
# confirmados; el resto cae a la comprobacion de tamano hasta conocer su hash.
MODEL_SHA256 = {
    "tiny": "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
}


def sha256_of(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_path(key: str) -> Path:
    return models_dir() / MODELS[key][0]


def is_downloaded(key: str) -> bool:
    p = model_path(key)
    return p.is_file() and p.stat().st_size > 1_000_000


def first_available() -> str | None:
    for k in ("base", "small", "tiny"):
        if is_downloaded(k):
            return k
    return None


def label(key: str) -> str:
    fname, mb, desc = MODELS[key]
    estado = "descargado" if is_downloaded(key) else f"~{mb} MB a descargar"
    return f"{key.capitalize()} — {desc} ({estado})"


def _download_curl(url: str, tmp: str) -> bool:
    """Descarga con curl.exe (Windows 10+). Usa Schannel = almacen de certificados
    del sistema, por lo que funciona aunque un antivirus/proxy intercepte TLS
    (donde urllib falla con CERTIFICATE_VERIFY_FAILED)."""
    import shutil
    import subprocess
    curl = shutil.which("curl") or r"C:\Windows\System32\curl.exe"
    if not Path(curl).is_file():
        return False
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000
    try:
        # --ssl-no-revoke: necesario cuando un antivirus/proxy intercepta TLS y
        # Schannel no puede comprobar la revocacion (CRYPT_E_NO_REVOCATION_CHECK).
        r = subprocess.run([curl, "-fL", "--ssl-no-revoke", "--retry", "2",
                            "--connect-timeout", "30", "-A", "CapturaStudio", "-o", tmp, url],
                           capture_output=True, timeout=900, **kw)
        return r.returncode == 0 and Path(tmp).is_file() and Path(tmp).stat().st_size > 1_000_000
    except (OSError, subprocess.SubprocessError):
        return False


def _download_powershell(url: str, tmp: str) -> bool:
    """Fallback con Invoke-WebRequest (.NET): usa el almacen de certificados de
    Windows y no hace revocacion estricta, asi que funciona tras proxies/AV."""
    import subprocess
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000
    ps = ("[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
          f"Invoke-WebRequest -Uri '{url}' -OutFile '{tmp}' -TimeoutSec 900 -UseBasicParsing")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=950, **kw)
        return r.returncode == 0 and Path(tmp).is_file() and Path(tmp).stat().st_size > 1_000_000
    except (OSError, subprocess.SubprocessError):
        return False


def _download_urllib(url: str, tmp: str, progress_cb=None) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CapturaStudio"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            got = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(0.999, got / total))
        return Path(tmp).is_file() and Path(tmp).stat().st_size > 1_000_000
    except Exception as exc:  # noqa: BLE001
        logger.warning("Descarga urllib fallo: %s", exc)
        return False


def download(key: str, progress_cb=None) -> str:
    dst = model_path(key)
    if is_downloaded(key):
        return str(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    url = _BASE_URL + MODELS[key][0]
    tmp = str(dst) + ".part"
    try:
        Path(tmp).unlink(missing_ok=True)
    except OSError:
        pass
    logger.info("Descargando modelo %s desde %s", key, url)
    # curl (--ssl-no-revoke) -> PowerShell/.NET -> urllib. Cubre entornos con
    # antivirus/proxy que interceptan TLS y rompen la verificacion estandar.
    if not (_download_curl(url, tmp) or _download_powershell(url, tmp)
            or _download_urllib(url, tmp, progress_cb)):
        raise RuntimeError("No se pudo descargar el modelo (revisa tu conexion/antivirus).")
    expected = MODEL_SHA256.get(key)
    if expected:
        got = sha256_of(tmp)
        if got != expected:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("El modelo descargado no coincide con el hash esperado "
                               "(posible corrupcion o manipulacion).")
    os.replace(tmp, dst)
    if progress_cb:
        progress_cb(1.0)
    return str(dst)
