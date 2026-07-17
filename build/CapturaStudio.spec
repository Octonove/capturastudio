# -*- mode: python ; coding: utf-8 -*-
"""Spec de PyInstaller para CapturaStudio (onedir, ventana sin consola).

La ruta de ffmpeg.exe se pasa por la variable de entorno FFMPEG_SRC (ver
build.ps1). ffprobe.exe NO se empaqueta a proposito: en el build full de Gyan
pesa ~213 MB (casi duplicaria el paquete) y la app no lo necesita, porque mide
duraciones y dimensiones parseando la salida de FFmpeg (ver fu.ffprobe_from y
los fallbacks en ai_post.py y autoframe.py). Si hay un ffprobe en el PATH del
usuario, se usara; si no, los fallbacks cubren el caso."""

import os
from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None

binaries = []
ffmpeg_src = os.environ.get("FFMPEG_SRC", "")
if ffmpeg_src and os.path.isfile(ffmpeg_src):
    # Solo ffmpeg.exe: la duracion se mide parseando su salida (sin ffprobe),
    # ahorrando ~210 MB en el paquete final.
    binaries.append((ffmpeg_src, "."))

# Captura de ventana WGC (wincap.py): el .pyd nativo Rust de windows-capture.
# Su import es perezoso, asi que hay que declararlo. cv2 se EXCLUYE: el
# __init__.py lo importa pero solo lo usa save_as_image (no lo usamos); wincap
# inyecta un stub de cv2 en runtime -> se evita empaquetar opencv (~44 MB).
binaries += collect_dynamic_libs("windows_capture")

icon_path = os.environ.get("APP_ICON", "")
icon_arg = icon_path if (icon_path and os.path.isfile(icon_path)) else None

a = Analysis(
    ['..\\CapturaStudio.py'],
    pathex=[],
    binaries=binaries,
    datas=[],
    hiddenimports=['PIL._tkinter_finder', 'soundcard', 'soundcard.mediafoundation',
                   '_cffi_backend', 'octonove_core.dshow',
                   'windows_capture', 'windows_capture.windows_capture', 'numpy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['scipy', 'pandas', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide6',
              # rembg y sus dependencias pesadas NO se empaquetan (~500 MB);
              # la funcion 'quitar fondo' degrada con un aviso si no estan.
              'rembg', 'onnxruntime', 'numba', 'llvmlite', 'skimage', 'scikit-image',
              'pymatting', 'jsonschema', 'jsonschema_specifications', 'pooch',
              'imageio', 'tifffile', 'scikit_image',
              # cv2: windows_capture lo importa pero wincap inyecta un stub en
              # runtime (solo lo usaria save_as_image, que no llamamos).
              'cv2'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CapturaStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CapturaStudio',
)
