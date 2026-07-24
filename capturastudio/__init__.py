"""CapturaStudio — estudio de grabacion y streaming local con IA.

Evolucion de CapturaPro: compositing de varias fuentes (pantalla, webcam,
imagen, texto, media) en el render con FFmpeg, grabacion y streaming, y
superpoderes de IA local (subtitulos Whisper, recorte de silencios, etc.).
100% local, sin marca de agua, sin limites.
"""

from __future__ import annotations

APP_NAME = "CapturaStudio"
APP_VERSION = "1.7.0"   # fuente unica de version: build-installer.ps1 la inyecta al .iss
