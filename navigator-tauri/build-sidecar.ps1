<#
.SYNOPSIS
    Freeze the Python bridge into a standalone executable and install it
    where Tauri expects an `externalBin`:
    src-tauri/binaries/nebula-bridge-<triple>.exe

.DESCRIPTION
    Windows counterpart to build-sidecar.sh -- see that file for the full
    rationale. Tauri requires the target-triple suffix so a bundle can carry
    per-platform binaries, plus a `.exe` extension on Windows; it strips both
    when copying into the app. Run this before `npm run build` (and before
    `npm run dev` if you want to exercise the frozen bridge in development).

    Override the interpreter that gets frozen with $env:NEBULA_PYTHON --
    whatever Python builds the sidecar is the Python the app will ship.
#>

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Set-Location $PSScriptRoot

$Python = if ($env:NEBULA_PYTHON) { $env:NEBULA_PYTHON } else { "python" }
$Name = "nebula-bridge"

# Match the triple rustc is building for, not the host -- they differ when
# cross-compiling or targeting a different arch than the host.
if ($env:TAURI_TARGET_TRIPLE) {
    $Triple = $env:TAURI_TARGET_TRIPLE
} else {
    $hostLine = (& rustc -vV | Select-String '^host:') 2>$null
    if (-not $hostLine) {
        Write-Error "error: could not determine the target triple (is rustc on PATH?)"
        exit 1
    }
    $Triple = ($hostLine -split '\s+')[1]
}
if (-not $Triple) {
    Write-Error "error: could not determine the target triple (is rustc on PATH?)"
    exit 1
}

Write-Host "==> freezing $Name for $Triple using $Python"

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error @"
error: PyInstaller not available for $Python. Install it with:
         $Python -m pip install pyinstaller
"@
    exit 1
}

# --paths ..\src: import `nebula` from the repo checkout rather than relying
#   on it being pip-installed for this interpreter.
# --hidden-import yaml: nebula's only runtime dependency; it is imported
#   lazily in places PyInstaller's static analysis does not follow.
& $Python -m PyInstaller `
    --onefile `
    --console `
    --clean `
    --noconfirm `
    --name $Name `
    --paths ..\src `
    --hidden-import yaml `
    --distpath build\sidecar\dist `
    --workpath build\sidecar\work `
    --specpath build\sidecar `
    sidecar\bridge.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "error: PyInstaller failed"
    exit 1
}

New-Item -ItemType Directory -Force -Path src-tauri\binaries | Out-Null
$Dest = "src-tauri\binaries\$Name-$Triple.exe"
Copy-Item -Force "build\sidecar\dist\$Name.exe" $Dest

Write-Host "==> smoke test"
'{"id":1,"op":"ping","args":null}' | & $Dest
if ($LASTEXITCODE -ne 0) {
    Write-Error "error: smoke test failed"
    exit 1
}

Write-Host "==> installed $Dest"
