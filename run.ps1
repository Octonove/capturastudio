# Ejecuta CapturaStudio en modo desarrollo usando el venv de CapturaPro
# (comparte dependencias: Pillow, mss, numpy, soundcard).
$venv = "..\CapturaPro\.venv\Scripts\python.exe"
if (-not (Test-Path $venv)) { $venv = "python" }
& $venv "$PSScriptRoot\CapturaStudio.py"
