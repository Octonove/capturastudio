"""Cifrado de secretos en reposo con DPAPI de Windows (CryptProtectData),
ligado al usuario actual. Sin dependencias externas. Se usa para no guardar la
stream_key en claro en config.json."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_encrypt(text: str) -> str:
    """Cifra un texto y devuelve base64. Lanza OSError si DPAPI falla."""
    data = (text or "").encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData fallo")
    try:
        enc = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return base64.b64encode(enc).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(b64: str) -> str:
    """Descifra un base64 producido por dpapi_encrypt. Lanza OSError si falla."""
    enc = base64.b64decode(b64)
    buf = ctypes.create_string_buffer(enc, len(enc))
    blob_in = _DATA_BLOB(len(enc), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData fallo")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
