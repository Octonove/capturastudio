# Genera el instalador unico con Inno Setup (requiere ISCC.exe).
#
# Por defecto RECONSTRUYE el dist (build.ps1) para que el instalador refleje
# siempre el codigo actual. Usa -SkipBuild para reutilizar un dist ya fresco.
# La version se toma de capturastudio\__init__.py (APP_VERSION): fuente unica,
# asi el instalador nunca queda desincronizado con la app.
param([switch]$SkipBuild)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# 1) Reconstruir el ejecutable (salvo que se pida saltarlo y ya exista)
$distExe = Join-Path $root "dist\CapturaStudio\CapturaStudio.exe"
if ($SkipBuild -and (Test-Path $distExe)) {
    Write-Host "Reutilizando dist existente (-SkipBuild)." -ForegroundColor DarkGray
} else {
    Write-Host "== Reconstruyendo dist (build.ps1) ==" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) { Write-Host "Fallo el build." -ForegroundColor Red; exit 1 }
}
if (-not (Test-Path $distExe)) {
    Write-Host "No existe el dist tras el build; abortando." -ForegroundColor Red; exit 1
}

# 2) Leer la version desde APP_VERSION (fuente unica)
$initPy = Join-Path $root "capturastudio\__init__.py"
$m = Select-String -Path $initPy -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
if (-not $m) {
    Write-Host "ERROR: no se encontro APP_VERSION en $initPy." -ForegroundColor Red
    Write-Host "Abortando: generar el instalador con una version incorrecta seria peor que fallar." -ForegroundColor Red
    exit 1
}
$ver = $m.Matches[0].Groups[1].Value
Write-Host "Version (APP_VERSION): $ver" -ForegroundColor Green

# 3) Localizar Inno Setup
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                     "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                     "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if (-not $iscc) {
    Write-Host "No se encontro Inno Setup (ISCC.exe). Instalalo con:" -ForegroundColor Red
    Write-Host "  winget install JRSoftware.InnoSetup --source winget"
    exit 1
}

# 4) Compilar el instalador inyectando la version
Write-Host "Compilando instalador con: $iscc" -ForegroundColor Cyan
& $iscc "/DMyAppVersion=$ver" (Join-Path $PSScriptRoot "CapturaStudio.iss")
if ($LASTEXITCODE -eq 0) {
    $out = Join-Path $root ("installer\CapturaStudio-Setup-$ver.exe")
    Write-Host "`nInstalador: $out" -ForegroundColor Green
} else {
    Write-Host "Fallo la creacion del instalador (codigo $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}
