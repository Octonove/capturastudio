# Construye el ejecutable de CapturaStudio con PyInstaller (onedir).
# Empaqueta ffmpeg.exe (+ffprobe) para que sea portable.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = Join-Path $root "..\CapturaPro\.venv\Scripts\python.exe" }
if (-not (Test-Path $py)) { $py = "python" }
Write-Host "Python: $py" -ForegroundColor DarkGray

Write-Host "== Generando icono ==" -ForegroundColor Cyan
& $py (Join-Path $PSScriptRoot "gen_icon.py")

Write-Host "== Localizando FFmpeg ==" -ForegroundColor Cyan
$ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ff) {
    $winget = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $winget) {
        $cand = Get-ChildItem -Path $winget -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -like "*Gyan.FFmpeg*" } | Select-Object -First 1
        if ($cand) { $ff = $cand.FullName }
    }
}
if ($ff) { Write-Host "FFmpeg: $ff" -ForegroundColor Green; $env:FFMPEG_SRC = $ff }
else { Write-Host "AVISO: FFmpeg no encontrado; se construira sin empaquetarlo." -ForegroundColor Yellow; $env:FFMPEG_SRC = "" }

$icon = Join-Path $PSScriptRoot "icon.ico"
if (Test-Path $icon) { $env:APP_ICON = $icon } else { $env:APP_ICON = "" }

Write-Host "== Compilando con PyInstaller ==" -ForegroundColor Cyan
Push-Location $root
& $py -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "CapturaStudio.spec")
$code = $LASTEXITCODE
Pop-Location

if ($code -eq 0) {
    Write-Host "`n== LISTO ==" -ForegroundColor Green
    Write-Host "Ejecutable: $(Join-Path $root 'dist\CapturaStudio\CapturaStudio.exe')"
} else {
    Write-Host "`nLa compilacion fallo (codigo $code)." -ForegroundColor Red
    exit $code
}
