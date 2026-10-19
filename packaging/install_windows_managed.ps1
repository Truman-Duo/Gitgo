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
# A native Host can be classified as a GUI subsystem executable. Invocation
# alone may return before exit and leave LASTEXITCODE unset. Wait on the actual
# process, preserving stdio and the native argument boundary on PS 5.1 and 7.
function ConvertTo-GitgoNativeArgument([string]$Value) {
    '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}
$nativeArguments = ($installerArguments | ForEach-Object { ConvertTo-GitgoNativeArgument $_ }) -join ' '
$installer = Start-Process -FilePath $hostPath -ArgumentList $nativeArguments -NoNewWindow -Wait -PassThru
try { $installerExit = $installer.ExitCode } finally { $installer.Dispose() }
if ($installerExit -ne 0) { throw "Managed installation rejected (Host exit $installerExit); existing files and user data retained" }
Write-Output 'Managed installation registered. Launch the new gitgo.ps1 command from PowerShell. Updates require pinned release trust and explicit confirmation.'
