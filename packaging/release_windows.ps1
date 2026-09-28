param(
    [string]$Python = "",
    [string]$Bun = "$env:USERPROFILE\.bun\bun.exe",
    [string]$PyInstallerPackages = "",
    [string]$Output = "",
    [string]$InnoSetupCompiler = "",
    [string]$BaseRef = "origin/master",
    [switch]$VerifyOnly,
    [switch]$BuildInstaller,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Invoke-Checked {
    param([string]$Label, [scriptblock]$Command)
    Write-Host "`n== $Label ==" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}
if (-not $Python -and $env:GITGO_PYTHON) { $Python = $env:GITGO_PYTHON }
if (-not $Python) {
    $Python = @(
        (Join-Path $env:USERPROFILE ".gitgo\runtime\python\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Gitgo\runtime\python\python.exe")
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Gitgo Python runtime not found; pass -Python"
}
if (-not (Test-Path -LiteralPath $Bun -PathType Leaf)) {
    throw "Bun runtime not found; pass -Bun"
}

Push-Location $root
try {
    if (-not $AllowDirty) {
        $dirty = @(git status --porcelain --untracked-files=normal)
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect Git worktree" }
        if ($dirty.Count -gt 0) {
            throw "Release worktree is not clean. Commit intended changes or use -AllowDirty for a non-release rehearsal."
        }
    }

    git rev-parse --verify "$BaseRef^{commit}" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Base ref is unavailable: $BaseRef" }
    $messages = @(git log --format=%s "$BaseRef..HEAD")
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect release commit messages" }
    $messagePattern = '^\[GITGO-[0-9]+\] (feat|fix|docs|style|refactor|perf|test|chore)\([a-z0-9_-]+\): .{1,60}$'
    $invalid = @($messages | Where-Object { $_ -notmatch $messagePattern })
    if ($invalid.Count -gt 0) {
        throw "Release contains commit messages outside commit-config.json convention: $($invalid.Count)"
    }

    Invoke-Checked "SQLite runtime safety" {
        & $Python -B -c "import sqlite3; from backend.core.storage.runtime import validate_sqlite_runtime; validate_sqlite_runtime(); print('SQLite', sqlite3.sqlite_version)"
    }
    Invoke-Checked "Tracked-file privacy boundary" {
        & $Python -B scripts\verify_release_privacy.py --root $root
    }
    Invoke-Checked "Python test suite" {
        & $Python -B -m pytest tests -q
    }

    Push-Location (Join-Path $root "cli\dashboard")
    try {
        Invoke-Checked "Dashboard test suite" { & $Bun test }
        Invoke-Checked "Dashboard production build" { & $Bun run build }
    } finally {
        Pop-Location
    }

    if (-not $VerifyOnly) {
        $arguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            (Join-Path $PSScriptRoot "build_windows.ps1"),
            "-Python", $Python, "-Bun", $Bun
        )
        if ($PyInstallerPackages) { $arguments += @("-PyInstallerPackages", $PyInstallerPackages) }
        if ($Output) { $arguments += @("-Output", $Output) }
        if ($BuildInstaller) {
            $arguments += "-BuildInstaller"
            if ($InnoSetupCompiler) { $arguments += @("-InnoSetupCompiler", $InnoSetupCompiler) }
        }
        Invoke-Checked "Windows release build" { & powershell.exe @arguments }
    }

    Write-Host "`nRelease verification passed. Nothing was pushed or installed." -ForegroundColor Green
} finally {
    Pop-Location
}
