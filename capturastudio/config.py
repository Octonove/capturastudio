"""Configuracion, rutas de datos y presets de calidad de CapturaStudio (shim del
nucleo compartido octonove_core.config + lo propio de esta app: presets de video,
hotkeys, cifrado DPAPI de la stream_key y redaccion de la clave en logs)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from octonove_core.config import RedactingFilter as _CoreRedactingFilter
from octonove_core.config import default_videos_dir as _default_videos_dir
from octonove_core.config import get_data_dir as _get_data_dir
from octonove_core.config import load_config as _load_config
from octonove_core.config import models_dir as _models_dir
from octonove_core.config import save_config as _save_config
from octonove_core.config import setup_logging as _setup_logging
from octonove_core.config import work_dir as _work_dir

from . import APP_NAME

logger = logging.getLogger(__name__)


def get_data_dir():
    return _get_data_dir(APP_NAME)


def default_videos_dir() -> Path:
    return _default_videos_dir(APP_NAME)


def work_dir() -> Path:
    """Carpeta de trabajo temporal (segmentos, audio, mascaras)."""
    return _work_dir(APP_NAME)


def models_dir() -> Path:
    """Modelos de IA (Whisper). Reutiliza los de TranscriptorIA si de verdad
    contienen modelos (fix del core: una carpeta vacia ajena ya no 'secuestra'
    las descargas propias)."""
    appdata = os.environ.get("APPDATA", "")
    return _models_dir(APP_NAME, [Path(appdata) / "TranscriptorIA" / "models"])


CONFIG_PATH = get_data_dir() / "config.json"
LOG_PATH = get_data_dir() / "capturastudio.log"

# Presets de calidad (CRF para CPU, CQ para GPU; mas bajo = mejor).
VIDEO_QUALITY = {
    "alta":  {"label": "Alta — casi sin perdida", "x264_crf": 18, "x264_preset": "veryfast",
              "nvenc_cq": 19, "nvenc_preset": "p5", "bitrate_k": 12000},
    "media": {"label": "Media — equilibrado", "x264_crf": 23, "x264_preset": "veryfast",
              "nvenc_cq": 25, "nvenc_preset": "p4", "bitrate_k": 6000},
    "baja":  {"label": "Baja — archivo pequeno", "x264_crf": 30, "x264_preset": "superfast",
              "nvenc_cq": 32, "nvenc_preset": "p4", "bitrate_k": 3000},
}
QUALITY_ORDER = ["alta", "media", "baja"]

# Lienzos disponibles (ancho x alto del render).
CANVAS_PRESETS = {
    "1080p (1920x1080)": (1920, 1080),
    "1440p (2560x1440)": (2560, 1440),
    "720p (1280x720)": (1280, 720),
    "Vertical 9:16 (1080x1920)": (1080, 1920),
}


# Atajos globales por defecto (accion -> combinacion). Remapeables por el usuario.
DEFAULT_HOTKEYS = {
    "record": "Ctrl+Shift+R",
    "pause": "Ctrl+Shift+P",
    "stream": "Ctrl+Shift+D",
    "moment": "Ctrl+Shift+M",
}


@dataclass
class AppConfig:
    videos_dir: str = field(default_factory=lambda: str(default_videos_dir()))
    canvas: str = "1080p (1920x1080)"
    fps: int = 30
    video_quality: str = "alta"
    encoder: str = "auto"            # auto | libx264 | h264_nvenc | h264_amf | h264_qsv
    container: str = "mp4"           # mp4 | mkv
    capture_cursor: bool = True

    # Audio
    audio_system: bool = True
    audio_mic: bool = False
    audio_mic_device: str = ""
    audio_denoise: bool = False

    # IA
    whisper_model: str = "base"      # tiny | base | small
    burn_subs: bool = False

    # Streaming
    stream_service: str = "Personalizado"
    stream_url: str = ""
    stream_key: str = ""
    stream_bitrate_k: int = 6000

    # Avanzado
    ffmpeg_path: str = ""
    hotkeys_enabled: bool = True
    hotkeys: dict = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))
    seen_welcome: bool = False       # onboarding mostrado una sola vez
    ollama_model: str = ""           # modelo Ollama preferido ("" = auto)

    def ensure_dirs(self) -> None:
        try:
            Path(self.videos_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("No se pudo crear %s: %s", self.videos_dir, exc)


def _post_load(data: dict, cfg: AppConfig) -> None:
    # Descifrar la stream_key (guardada con DPAPI, nunca en claro).
    enc = data.get("stream_key_enc")
    if enc and not cfg.stream_key:
        try:
            from . import secrets as sec
            cfg.stream_key = sec.dpapi_decrypt(enc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo descifrar la clave: %s", exc)


def _pre_save(data: dict) -> None:
    key = data.get("stream_key", "") or ""
    data["stream_key"] = ""        # nunca en claro en disco
    data["stream_key_enc"] = ""
    if key:
        try:
            from . import secrets as sec
            data["stream_key_enc"] = sec.dpapi_encrypt(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo cifrar la clave: %s", exc)


def load_config() -> AppConfig:
    return _load_config(CONFIG_PATH, AppConfig, post_load=_post_load)


def save_config(cfg: AppConfig) -> None:
    _save_config(cfg, CONFIG_PATH, pre_save=_pre_save)


class _RedactingFilter(_CoreRedactingFilter):
    """Enmascara la stream_key (ultimo segmento de URLs rtmp/rtmps) en los logs.
    Defensa en profundidad: aunque algun modulo loggee un comando con la clave."""
    _rx_rtmp = re.compile(r'(rtmps?://[^\s"\'/]+/[^\s"\'/]+/)[^\s"\']+', re.IGNORECASE)

    def __init__(self):
        super().__init__(self._rx_rtmp)


def setup_logging() -> None:
    _setup_logging(LOG_PATH, filters=[_RedactingFilter()])
