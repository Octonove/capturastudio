"""Quitar el fondo con IA LOCAL (rembg / u2net), 100% offline.

Caso practico y rapido: quitar el fondo a una IMAGEN (foto de webcam, captura,
logo) y, opcionalmente, ponerle un fondo de color o imagen. Procesar cada frame
de un video en CPU es demasiado lento, asi que esto opera sobre imagenes/clips
cortos. Alternativa local a remove.bg, sin marca de agua, sin limite ni pago.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .models import _download_curl, _download_powershell

logger = logging.getLogger(__name__)

_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"
_MODEL_SHA256 = "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491"


class BgError(Exception):
    pass


def available() -> bool:
    """rembg/onnxruntime no se empaquetan por defecto (pesan ~500 MB). Esta
    funcion comprueba si estan instalados (dev o instalacion manual)."""
    try:
        import rembg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _u2net_dir() -> Path:
    return Path(os.environ.get("U2NET_HOME", str(Path.home() / ".u2net")))


def model_ready() -> bool:
    p = _u2net_dir() / "u2net.onnx"
    return p.is_file() and p.stat().st_size > 1_000_000


def ensure_model() -> str:
    """Descarga u2net.onnx (~176 MB) a ~/.u2net si falta. Robusto frente a
    antivirus que interceptan TLS (curl --ssl-no-revoke -> PowerShell/.NET)."""
    d = _u2net_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "u2net.onnx"
    if model_ready():
        return str(p)
    tmp = str(p) + ".part"
    try:
        Path(tmp).unlink(missing_ok=True)
    except OSError:
        pass
    if not (_download_curl(_MODEL_URL, tmp) or _download_powershell(_MODEL_URL, tmp)):
        raise BgError("No se pudo descargar el modelo de IA (revisa tu conexion/antivirus).")
    from .models import sha256_of
    if sha256_of(tmp) != _MODEL_SHA256:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise BgError("El modelo descargado no coincide con el hash esperado.")
    os.replace(tmp, p)
    return str(p)


def _session():
    ensure_model()
    from rembg import new_session
    return new_session("u2net")


def remove_bg(input_path: str, out_png: str, session=None) -> str:
    """Quita el fondo y guarda PNG con transparencia."""
    from rembg import remove
    from PIL import Image
    session = session or _session()
    img = Image.open(input_path).convert("RGBA")
    out = remove(img, session=session)
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    out.save(out_png)
    return out_png


def _hex_rgba(color: str):
    c = (color or "#1E3A5F").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)


def replace_bg(input_path: str, out_path: str, background: str, session=None) -> str:
    """Quita el fondo y compone sobre un color (#hex) o una imagen de fondo."""
    from rembg import remove
    from PIL import Image
    session = session or _session()
    cut = remove(Image.open(input_path).convert("RGBA"), session=session)
    if background.startswith("#") or len(background) in (6, 7):
        bg = Image.new("RGBA", cut.size, _hex_rgba(background))
    else:
        bg = Image.open(background).convert("RGBA").resize(cut.size)
    bg.alpha_composite(cut)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if out_path.lower().endswith((".jpg", ".jpeg")):
        bg.convert("RGB").save(out_path, quality=95)
    else:
        bg.save(out_path)
    return out_path
