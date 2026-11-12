[CmdletBinding()]
param(
    [string]$CertificateThumbprint = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$brokerRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageRoot = Join-Path $brokerRoot "Package"
$buildRoot = Join-Path $brokerRoot "build"
$publishRoot = Join-Path $buildRoot "publish"
$stagingRoot = Join-Path $buildRoot "staging"
$msixPath = Join-Path $buildRoot "HRWidget.ClevoBroker.Experimental_0.2.7.0_x64.msix"
$sdkRoot = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64"
$makeAppx = Join-Path $sdkRoot "makeappx.exe"
$signTool = Join-Path $sdkRoot "signtool.exe"

if (-not (Test-Path -LiteralPath $makeAppx) -or -not (Test-Path -LiteralPath $signTool)) {
    throw "Windows SDK packaging tools were not found at the allowlisted location."
}

New-Item -ItemType Directory -Path $publishRoot, $stagingRoot -Force | Out-Null
Get-ChildItem -LiteralPath $publishRoot -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stagingRoot -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force

dotnet publish (Join-Path $brokerRoot "ClevoBroker.csproj") -c Release -r win-x64 --self-contained false -o $publishRoot
if ($LASTEXITCODE -ne 0) {
    throw "The read-only broker build failed."
}

Copy-Item -Path (Join-Path $publishRoot "*") -Destination $stagingRoot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $packageRoot "AppxManifest.xml") -Destination $stagingRoot -Force
Copy-Item -LiteralPath (Join-Path $packageRoot "Assets") -Destination $stagingRoot -Recurse -Force

& $makeAppx pack /d $stagingRoot /p $msixPath /o
if ($LASTEXITCODE -ne 0) {
    throw "MSIX packaging failed."
}

if ($CertificateThumbprint) {
    & $signTool sign /sha1 $CertificateThumbprint /fd SHA256 $msixPath
    if ($LASTEXITCODE -ne 0) {
        throw "MSIX signing failed."
    }
}

Get-FileHash -Algorithm SHA256 -LiteralPath $msixPath | Select-Object Path, Hash
