param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$BinDir,
    [string[]]$ProtectedPath = @()
)
$ErrorActionPreference = 'Stop'
# Fresh opt-in installation only. No elevation, PATH/registry edits, global
# execution-policy changes, adoption of Inno installs, or profile deletion.
$hostPath = Join-Path $PSScriptRoot 'internal\gitgo-host\gitgo-host.exe'
if (!(Test-Path -LiteralPath $hostPath -PathType Leaf)) { throw 'Complete extracted managed package required' }
$installerArguments = @('--gitgo-internal-role', 'managed-install', '--root', $Root, '--bin-dir', $BinDir)
foreach ($path in $ProtectedPath) { $installerArguments += @('--protected-path', $path) }
& $hostPath @installerArguments
if ($LASTEXITCODE -ne 0) { throw 'Managed installation rejected; existing files and user data retained' }
Write-Output 'Managed installation registered. Launch the new gitgo.ps1 command from PowerShell. Updates require pinned release trust and explicit confirmation.'
