# build-exe.ps1 - package the Zcode Gateway desktop client as a single .exe
#
#   .\build-exe.ps1              build
#   .\build-exe.ps1 -Clean       wipe build/ dist/ first
#   .\build-exe.ps1 -Console     keep a console window (useful for debugging)
#
# Output: .\dist\ZcodeGateway.exe   (~single file, no Python needed to run)

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

Write-Host "Zcode Gateway - exe build" -ForegroundColor Cyan
Write-Host "  workdir: $Here"

if ($Clean) {
    foreach ($d in @("build", "dist")) {
        $buildTarget = [System.IO.Path]::GetFullPath((Join-Path $Here $d))
        $buildRoot = [System.IO.Path]::GetFullPath($Here).TrimEnd('\') + '\'
        if (-not $buildTarget.StartsWith($buildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a path outside this project"
        }
        if (Test-Path -LiteralPath $buildTarget) { Remove-Item -LiteralPath $buildTarget -Recurse -Force }
    }
}

# find a python (prefer the venv; a PATH python is used read-only, never pip-installed into)
$py = Join-Path $Here ".venv\Scripts\python.exe"
$usingVenv = Test-Path $py
if (-not $usingVenv) { $py = (Get-Command python -ErrorAction Stop).Source }
Write-Host "  python : $py$(if (-not $usingVenv) { '  (no .venv — will NOT pip install)' })"

# make sure the build deps are present (install only into the project venv —
# installing into a shared/system Python mutates the user's machine)
& $py -c "import PyInstaller" 2>$null
$needInstaller = $LASTEXITCODE -ne 0
& $py -c "import yaml, httpx, fastapi, uvicorn" 2>$null
$needRuntime = $LASTEXITCODE -ne 0
if (($needInstaller -or $needRuntime) -and -not $usingVenv) {
    throw "build deps missing and no .venv found at $Here\.venv - " +
          "create it first:  python -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt pyinstaller"
}
if ($needInstaller) {
    Write-Host "  installing pyinstaller (venv)..." -ForegroundColor Yellow
    & $py -m pip install --upgrade pyinstaller
}
if ($needRuntime) {
    Write-Host "  installing runtime deps (venv)..." -ForegroundColor Yellow
    & $py -m pip install -r (Join-Path $Here "requirements.txt")
}

$mode = if ($Console) { "--console" } else { "--windowed" }

# icon: official Zcode logo, extracted to assets/ by tools/make_icon.py
$icon = Join-Path $Here "assets\zcode.ico"
$iconArg = @()
if (Test-Path $icon) { $iconArg = @("--icon", $icon) }

# exe properties (right-click -> Details) with the branding line
$versionFile = Join-Path $Here "tools\exe_version.txt"
$versionArg = @()
if (Test-Path $versionFile) { $versionArg = @("--version-file", $versionFile) }

$buildArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    $mode,
    "--name", "ZcodeGateway",
    # uvicorn loads these dynamically, so PyInstaller cannot see them
    "--collect-submodules", "uvicorn",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols",
    "--hidden-import", "uvicorn.protocols.http",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.http.h11_impl",
    "--hidden-import", "uvicorn.protocols.websockets",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.lifespan",
    "--hidden-import", "uvicorn.lifespan.on",
    "--hidden-import", "uvicorn.lifespan.off",
    "--hidden-import", "anyio._backends._asyncio",
    "--hidden-import", "httpcore",
    "--hidden-import", "h11",
    "--hidden-import", "yaml",
    # pywebview + its Windows WebView2 backend
    "--collect-submodules", "webview",
    "--hidden-import", "clr",
    "--hidden-import", "pythonnet",
    # account-generator integration: imported lazily inside api_dash handlers,
    # so PyInstaller cannot see them
    "--hidden-import", "zcode_gateway.generator",
    "--hidden-import", "zcode_gateway.accounts_dir_bridge",
    # ship the example config, the app icon assets, the dashboard page, and the
    # Cloudflare mail-worker template used by the one-click deploy
    "--add-data", "config.example.yaml;.",
    "--add-data", "assets;assets",
    "--add-data", "zcode_gateway/dashboard;zcode_gateway/dashboard",
    "--add-data", "mail-worker;mail-worker",
    "--paths", $Here,
    "--specpath", $Here
) + $iconArg + $versionArg + @("gui_main.py")

Write-Host "`n  building (this takes a minute)...`n" -ForegroundColor Cyan
& $py @buildArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$exe = Join-Path $Here "dist\ZcodeGateway.exe"
if (-not (Test-Path $exe)) { throw "expected output missing: $exe" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "`n  built: $exe  ($size MB)" -ForegroundColor Green
Write-Host "`nRun it, or copy it next to your accounts/ folder." -ForegroundColor Cyan
Write-Host "It writes config.yaml beside itself on first launch." -ForegroundColor Cyan
