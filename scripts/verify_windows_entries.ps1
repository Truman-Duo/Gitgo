param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][string]$Directory
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$product = Get-Content -LiteralPath (Join-Path $root 'packaging/product.json') -Raw | ConvertFrom-Json
$archiveHash = (Get-FileHash -LiteralPath $Archive).Hash

function Assert-ResourceHash($assembly, [string]$resource, [string]$expected) {
    $stream = $assembly.GetManifestResourceStream($resource)
    if ($null -eq $stream) { throw "Missing resource: $resource" }
    try {
        if ((Get-FileHash -InputStream $stream).Hash -ine $expected) { throw "Resource digest differs: $resource" }
    } finally { $stream.Dispose() }
}

foreach ($stem in @('GitgoSetup', 'GitgoPortable')) {
    $name = "$stem-$($product.version)-win-x64.exe"
    $path = Join-Path $Directory $name
    $record = ((Get-Content -LiteralPath "$path.sha256" -Raw).Trim() -split '\s+', 2)
    if ($record.Count -ne 2 -or $record[1] -cne $name -or (Get-FileHash -LiteralPath $path).Hash -ine $record[0]) {
        throw "Invalid Windows entry checksum: $name"
    }
    $assembly = [Reflection.Assembly]::LoadFrom($path)
    Assert-ResourceHash $assembly 'Gitgo.Payload' $archiveHash
    $stored = $assembly.GetManifestResourceStream('Gitgo.PayloadSha256')
    if ($null -eq $stored) { throw "Missing embedded payload digest: $name" }
    $reader = [IO.StreamReader]::new($stored)
    try {
        if ($reader.ReadToEnd().Trim() -ine $archiveHash) { throw "Embedded payload digest differs: $name" }
    } finally { $reader.Dispose() }
    if ($stem -eq 'GitgoSetup') {
        foreach ($pair in @(@('zh', 'preview-notice.txt'), @('en', 'preview-notice.en.txt'))) {
            $terms = (Get-FileHash -LiteralPath (Join-Path $root "packaging/windows/$($pair[1])")).Hash
            Assert-ResourceHash $assembly "Gitgo.PreviewNotice.$($pair[0])" $terms
        }
        $uninstaller = $assembly.GetManifestResourceStream('Gitgo.Uninstaller')
        if ($null -eq $uninstaller) { throw 'Missing Setup uninstaller' }
        $bytes = [IO.MemoryStream]::new()
        try {
            $uninstaller.CopyTo($bytes)
            $inner = [Reflection.Assembly]::Load($bytes.ToArray())
            $helper = (Get-FileHash -LiteralPath (Join-Path $root 'packaging/uninstall-data.py')).Hash
            Assert-ResourceHash $inner 'Gitgo.UninstallData' $helper
        } finally { $uninstaller.Dispose(); $bytes.Dispose() }
    }
    Write-Host "$name embeds the checked final ZIP"
}
