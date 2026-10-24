param(
    [string]$Python = "",
    [string]$Bun = "$env:USERPROFILE\.bun\bun.exe",
    [string]$PyInstallerPackages = "",
    [string]$TestPackages = "",
    [string]$Ripgrep = "",
    [string]$RipgrepNotices = "",
    [string]$Output = "",
    [string]$ArtifactOutput = "",
    [string]$InnoSetupCompiler = "",
    [string]$BaseRef = "origin/master",
    [string]$SettingsFile = "",
    [string]$TemporaryRoot = "",
    [switch]$VerifyOnly,
    [switch]$BuildOnly,
    [switch]$BuildInstaller,
    [switch]$ManagedUpdate,
    [string]$UpdateReleasePolicy = "",
    [switch]$Sign,
    [string]$SigningKey = "",
    [string]$KeyId = "",
    [ValidateSet("stable", "preview")][string]$Channel = "preview",
    [long]$Sequence = 1,
    [string]$ReleaseTag = "",
    [switch]$AllowDirty
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ($BuildOnly -and $VerifyOnly) { throw "Choose -BuildOnly or -VerifyOnly" }
if ($Sign -and ($BuildOnly -or $VerifyOnly -or $AllowDirty -or -not $ManagedUpdate)) {
    throw "Signing requires a clean managed release with full validation"
}
if ($ManagedUpdate -and $BuildInstaller) { throw "Managed releases use the complete ZIP and install-managed.ps1, not Inno" }

# Local settings contain tool paths, never private-key bytes. Only explicit
# parameters may enable signing, dirty builds or test omission.
$explicitSettings = [bool]$SettingsFile
if (-not $SettingsFile) { $SettingsFile = Join-Path $root "dev-artifacts\release-settings.local.json" }
if (-not [IO.Path]::IsPathRooted($SettingsFile)) { $SettingsFile = Join-Path $root $SettingsFile }
$settingNames = @("Python", "Bun", "PyInstallerPackages", "TestPackages", "Ripgrep",
    "RipgrepNotices", "UpdateReleasePolicy", "SigningKey", "KeyId", "Channel", "TemporaryRoot")
if (Test-Path -LiteralPath $SettingsFile -PathType Leaf) {
    if ((Get-Item -LiteralPath $SettingsFile).Length -gt 65536) { throw "Release settings exceed 64 KiB" }
    $settings = Get-Content -LiteralPath $SettingsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (($settings.schema_version -isnot [int] -and $settings.schema_version -isnot [long]) -or $settings.schema_version -ne 1) { throw "Unsupported release settings schema" }
    foreach ($property in $settings.PSObject.Properties) {
        if ($property.Name -eq "schema_version") { continue }
        if ($property.Name -notin $settingNames -or $property.Value -isnot [string]) {
            throw "Unknown or invalid release setting: $($property.Name)"
        }
        if (-not $PSBoundParameters.ContainsKey($property.Name)) { Set-Variable -Name $property.Name -Value $property.Value }
    }
} elseif ($explicitSettings) { throw "Release settings file does not exist" }
if ($Channel -notin @("stable", "preview")) { throw "Invalid release channel" }
function Invoke-Checked {
    param([string]$Label, [scriptblock]$Command)
    Write-Host "`n== $Label ==" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}
if (-not $Python -and $env:GITGO_PYTHON) { $Python = $env:GITGO_PYTHON }
if (-not $Python) {
    $Python = @((Join-Path $env:USERPROFILE ".gitgo\runtime\python\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Gitgo\runtime\python\python.exe")) |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
foreach ($name in @("Python", "Bun", "PyInstallerPackages", "TestPackages", "Ripgrep",
    "RipgrepNotices", "Output", "ArtifactOutput", "InnoSetupCompiler", "UpdateReleasePolicy", "SigningKey", "TemporaryRoot")) {
    $value = Get-Variable -Name $name -ValueOnly
    if ($value -and -not [IO.Path]::IsPathRooted($value)) { Set-Variable -Name $name -Value (Join-Path $root $value) }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Gitgo Python runtime not found; pass -Python or configure the ignored local settings"
}
if (-not (Test-Path -LiteralPath $Bun -PathType Leaf)) { throw "Bun runtime not found; pass -Bun" }
if (-not $TestPackages) {
    $candidate = Join-Path $root "dev-artifacts\test-packages"
    if (Test-Path -LiteralPath (Join-Path $candidate "pytest\__init__.py") -PathType Leaf) { $TestPackages = $candidate }
}
if ($ManagedUpdate -and -not $UpdateReleasePolicy) {
    $UpdateReleasePolicy = Join-Path $PSScriptRoot "release-trust\windows-x86_64.json"
}
if ($UpdateReleasePolicy -and (-not $ManagedUpdate -or -not (Test-Path -LiteralPath $UpdateReleasePolicy -PathType Leaf))) {
    throw "Release trust requires a managed build and an existing public policy file"
}
if ($Sign) {
    if (-not $SigningKey -or -not $KeyId -or -not (Test-Path -LiteralPath $SigningKey -PathType Leaf)) {
        throw "Signing requires an external private-key file and its public key id"
    }
    $keyPath = [IO.Path]::GetFullPath($SigningKey)
    if ($keyPath.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The signing key must stay outside the repository"
    }
    if ($Sequence -lt 1 -or $Sequence -gt 9007199254740991) { throw "Invalid release sequence" }
}
$environmentNames = @("GITGO_BUN", "TEMP", "TMP", "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE")
$previousEnvironment = @{}
foreach ($name in $environmentNames) { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
Push-Location $root
try {
    if (-not $AllowDirty) {
        $dirty = @(git status --porcelain --untracked-files=normal)
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect Git worktree" }
        if ($dirty.Count -gt 0) { throw "Commit intended changes, or use -AllowDirty for an unsigned local rehearsal" }
    }
    git rev-parse --verify "$BaseRef^{commit}" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Base ref is unavailable: $BaseRef" }
    $messages = @(git log --no-merges --format=%s "$BaseRef..HEAD")
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect release commit messages" }
    $pattern = '^\[GITGO-[0-9]+\] (feat|fix|docs|style|refactor|perf|test|chore)\([a-z0-9_-]+\): .{1,60}$'
    if (@($messages | Where-Object { $_ -notmatch $pattern }).Count -gt 0) {
        throw "Release commit messages do not follow commit-config.json"
    }
    $head = (git rev-parse --short=8 HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect build source" }
    $runRoot = Join-Path $root ("dev-artifacts\windows-packaging\" + (Get-Date -Format "yyyyMMdd-HHmmss") +
        "-" + $head + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    # Create only the developer parent; the existing builder owns private stage ACLs.
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    if (-not $Output) { $Output = Join-Path $runRoot "stage" }
    if (-not $ArtifactOutput) { $ArtifactOutput = Join-Path $runRoot "archives" }
    if (-not $TemporaryRoot) { $TemporaryRoot = Join-Path $root ("dev-artifacts\pack-tmp-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)) }
    $env:GITGO_BUN = $Bun
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    Invoke-Checked "Private developer temporary directory" {
        & $Python -B -c "from pathlib import Path; import sys; from backend.core.update.windows_storage import create_private_directory, validate_staging; p=Path(sys.argv[1]); create_private_directory(p) if not p.exists() else None; validate_staging(p)" $TemporaryRoot
    }
    $env:TEMP = $TemporaryRoot
    $env:TMP = $TemporaryRoot
    Write-Host "Developer temporary files: $TemporaryRoot"
    Invoke-Checked "SQLite runtime safety" {
        & $Python -B -c "import sqlite3; from backend.core.storage.runtime import validate_sqlite_runtime; validate_sqlite_runtime(); print('SQLite', sqlite3.sqlite_version)"
    }
    Invoke-Checked "Product version consistency" { & $Python -B scripts\release_artifacts.py --check }
    Invoke-Checked "Tracked-file privacy boundary" { & $Python -B scripts\verify_release_privacy.py --root $root }
    Push-Location (Join-Path $root "cli\dashboard")
    try { Invoke-Checked "Locked Dashboard dependencies" { & $Bun install --frozen-lockfile } }
    finally { Pop-Location }
    if (-not $BuildOnly) {
        Invoke-Checked "Python test suite" {
            if ($TestPackages) { & $Python -B scripts\run_source_tests.py --packages $TestPackages tests -q }
            else { & $Python -B scripts\run_source_tests.py tests -q }
        }
        Push-Location (Join-Path $root "cli\dashboard")
        try {
            Invoke-Checked "Dashboard test suite" { & $Bun test }
            # The package builder performs this build during packaging.
            if ($VerifyOnly) { Invoke-Checked "Dashboard production build" { & $Bun run build } }
        } finally { Pop-Location }
    }
    if (-not $VerifyOnly) {
        $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            (Join-Path $PSScriptRoot "build_windows.ps1"), "-Python", $Python, "-Bun", $Bun,
            "-Output", $Output, "-ArtifactOutput", $ArtifactOutput)
        if ($PyInstallerPackages) { $arguments += @("-PyInstallerPackages", $PyInstallerPackages) }
        if ($Ripgrep) { $arguments += @("-Ripgrep", $Ripgrep) }
        if ($RipgrepNotices) { $arguments += @("-RipgrepNotices", $RipgrepNotices) }
        if ($ManagedUpdate) { $arguments += "-ManagedUpdate" }
        if ($UpdateReleasePolicy) { $arguments += @("-UpdateReleasePolicy", $UpdateReleasePolicy) }
        if ($BuildInstaller) {
            $arguments += "-BuildInstaller"
            if ($InnoSetupCompiler) { $arguments += @("-InnoSetupCompiler", $InnoSetupCompiler) }
        }
        Invoke-Checked "Windows package build and actual runtime smoke" { & powershell.exe @arguments }
        if (-not $BuildOnly) {
            $oldHost = $env:GITGO_SANDBOX_TEST_HOST
            try {
                $env:GITGO_SANDBOX_TEST_HOST = Join-Path $Output "internal\gitgo-host\gitgo-host.exe"
                Invoke-Checked "Actual packaged native boundaries" {
                    if ($TestPackages) {
                        & $Python -B scripts\run_source_tests.py --packages $TestPackages tests\test_native_sandbox.py tests\test_windows_sandbox_minimum.py -k packaged -q
                    } else {
                        & $Python -B scripts\run_source_tests.py tests\test_native_sandbox.py tests\test_windows_sandbox_minimum.py -k packaged -q
                    }
                }
            } finally { $env:GITGO_SANDBOX_TEST_HOST = $oldHost }
        }
        if ($Sign) {
            $archives = @(Get-ChildItem -LiteralPath $ArtifactOutput -Filter "*.zip" -File)
            if ($archives.Count -ne 1) { throw "Signing requires exactly one complete archive" }
            if (-not $ReleaseTag) {
                $product = Get-Content -LiteralPath (Join-Path $PSScriptRoot "product.json") -Raw | ConvertFrom-Json
                $ReleaseTag = "v" + $product.version
            }
            $signArguments = @("--stage", [IO.Path]::GetFullPath($Output), "--archive", $archives[0].FullName,
                "--private-key", $SigningKey, "--key-id", $KeyId, "--channel", $Channel, "--sequence", $Sequence,
                "--release-tag", $ReleaseTag, "--output", (Join-Path $ArtifactOutput "signed-release"))
            Invoke-Checked "Publisher-signed manifest preparation" {
                & $Python -B scripts\prepare_update_release.py @signArguments
            }
        }
    }
    if ($VerifyOnly) { Write-Host "Source verification passed. No package was built." -ForegroundColor Green }
    elseif ($BuildOnly) { Write-Host "Local package built. Full release tests were not run. Artifacts: $ArtifactOutput" -ForegroundColor Green }
    else { Write-Host "Source and package verification passed. Artifacts: $ArtifactOutput" -ForegroundColor Green }
    Write-Host "Nothing was published or installed."
} finally {
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process") }
    Pop-Location
}
