param(
    [string]$Python = "",
    [string]$Bun = "$env:USERPROFILE\.bun\bun.exe",
    [string]$PyInstallerPackages = "",
    [string]$Output = "",
    [string]$InnoSetupCompiler = "",
    [switch]$BuildInstaller
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Output) { $Output = Join-Path $root "dist-terminal" }
$stage = [System.IO.Path]::GetFullPath($Output)
$internal = Join-Path $stage "internal"
$productPath = Join-Path $PSScriptRoot "product.json"
$product = Get-Content -LiteralPath $productPath -Raw | ConvertFrom-Json
if ($product.primary_command -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    throw "Unsafe primary command in product.json: $($product.primary_command)"
}

if (-not $Python -and $env:GITGO_PYTHON) { $Python = $env:GITGO_PYTHON }
if (-not $Python) {
    $runtimeCandidates = @(
        (Join-Path $env:USERPROFILE ".gitgo\runtime\python\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Gitgo\runtime\python\python.exe")
    )
    $Python = $runtimeCandidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python runtime not found: $Python"
}
if (-not (Test-Path -LiteralPath $Bun -PathType Leaf)) {
    throw "Bun runtime not found: $Bun"
}

& $Python -B -c (
    "import sqlite3; " +
    "from backend.core.storage.runtime import validate_sqlite_runtime; " +
    "validate_sqlite_runtime(); print('Build runtime SQLite', sqlite3.sqlite_version)"
)
if ($LASTEXITCODE -ne 0) {
    throw "Python runtime failed the SQLite WAL-safety check"
}
$pyInstallerRunner = Join-Path $root "scripts\run_pyinstaller.py"
$useExternalPyInstaller = $false
# This is an availability probe, so ImportError is expected when PyInstaller
# lives in the separate build-only package directory. Windows PowerShell turns
# native stderr into a terminating ErrorRecord under ErrorActionPreference=Stop;
# temporarily downgrade only this probe and restore fail-closed behavior after.
$savedErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python -B -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)" 2>$null
$pyInstallerImportExit = $LASTEXITCODE
$ErrorActionPreference = $savedErrorActionPreference
if ($pyInstallerImportExit -ne 0) {
    if (-not $PyInstallerPackages) {
        $packageCandidates = @(
            (Join-Path (Split-Path -Parent $Python) "packages"),
            (Join-Path $env:USERPROFILE ".gitgo\runtime\python\packages"),
            (Join-Path $env:LOCALAPPDATA "Gitgo\runtime\python\packages")
        )
        $PyInstallerPackages = $packageCandidates | Where-Object {
            Test-Path -LiteralPath (Join-Path $_ "PyInstaller") -PathType Container
        } | Select-Object -First 1
    }
    if (-not $PyInstallerPackages -or
        -not (Test-Path -LiteralPath (Join-Path $PyInstallerPackages "PyInstaller") -PathType Container)) {
        throw "PyInstaller is not installed in the selected build runtime; pass -PyInstallerPackages"
    }
    & $Python -B $pyInstallerRunner $PyInstallerPackages --version
    if ($LASTEXITCODE -ne 0) { throw "External PyInstaller package set is invalid" }
    $useExternalPyInstaller = $true
}

New-Item -ItemType Directory -Force -Path $stage, $internal | Out-Null

Push-Location (Join-Path $root "cli\dashboard")
try {
    & $Bun test src\input\runtime.test.tsx src\backend\client.test.ts
    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard Unicode/input integrity release gate failed"
    }
} finally {
    Pop-Location
}

try {
    & $Bun build (Join-Path $root "cli\dashboard\src\main.tsx") `
        --compile --outfile (Join-Path $stage "$($product.primary_command).exe")
    if ($LASTEXITCODE -ne 0) { throw "Dashboard compilation failed" }
} finally {
    # Bun can leave its full-size atomic output beside the repository when an
    # existing Windows executable was briefly locked. These files are build
    # products, never source inputs, and would otherwise dirty every release.
    Get-ChildItem -LiteralPath $root -File -Filter ".*.bun-build" `
        -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
}

$pyInstallerArguments = @(
    "--noconfirm", "--clean", "--onedir",
    "--name", "gitgo-host",
    "--distpath", $internal,
    "--workpath", (Join-Path $root "build\terminal-host"),
    "--specpath", (Join-Path $root "build\terminal-host"),
    (Join-Path $root "backend\core\native_host_entry.py")
)
if ($useExternalPyInstaller) {
    & $Python -B $pyInstallerRunner $PyInstallerPackages @pyInstallerArguments
} else {
    & $Python -B -m PyInstaller @pyInstallerArguments
}
if ($LASTEXITCODE -ne 0) { throw "Native Host compilation failed" }

Copy-Item -LiteralPath $productPath `
    -Destination (Join-Path $stage "product.json") -Force

$packagedHost = Join-Path $internal "gitgo-host\gitgo-host.exe"
& $Python -B (Join-Path $root "scripts\smoke_packaged_runtime.py") --host $packagedHost
if ($LASTEXITCODE -ne 0) {
    throw "Packaged Host/Daemon/tool-runner smoke test failed"
}
Write-Host "Terminal release staged at $stage"

foreach ($alias in @($product.command_aliases)) {
    if ($alias -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
        throw "Unsafe command alias in product.json: $alias"
    }
    $aliasPath = Join-Path $stage "$alias.cmd"
    Set-Content -LiteralPath $aliasPath -Encoding ascii `
        -Value "@`"%~dp0$($product.primary_command).exe`" %*"
}

if ($BuildInstaller) {
    if (-not $InnoSetupCompiler) {
        $candidate = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $InnoSetupCompiler = $candidate
        }
    }
    if (-not $InnoSetupCompiler -or -not (Test-Path -LiteralPath $InnoSetupCompiler -PathType Leaf)) {
        throw "Inno Setup compiler not found; pass -InnoSetupCompiler or omit -BuildInstaller"
    }
    $installerOutput = Join-Path $root "dist-installer"
    New-Item -ItemType Directory -Force -Path $installerOutput | Out-Null
    & $InnoSetupCompiler `
        "/DStageDir=$stage" `
        "/DOutputDir=$installerOutput" `
        "/DProductName=$($product.display_name)" `
        "/DProductId=$($product.product_id)" `
        "/DPrimaryCommand=$($product.primary_command)" `
        (Join-Path $PSScriptRoot "windows\installer.iss")
    if ($LASTEXITCODE -ne 0) { throw "Installer compilation failed" }
    Write-Host "Windows installer written to $installerOutput"
}
