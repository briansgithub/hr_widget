#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_-]{1,32}$')]
    [string]$CaptureTag
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sessionName = 'ClevoUsbHidPayloadTrace'
$captureRoot = [System.IO.Path]::GetTempPath().TrimEnd('\')
$captureBaseName = "ClevoUsbHid_${CaptureTag}"
$capturePath = Join-Path $captureRoot "${captureBaseName}.etl"

function Get-CaptureFiles {
    $versionedPattern = '^' + [regex]::Escape($captureBaseName) + '_[0-9]{6}\.etl$'
    @(Get-ChildItem -LiteralPath $captureRoot -File -ErrorAction Stop | Where-Object {
        $_.Name -eq "${captureBaseName}.etl" -or $_.Name -match $versionedPattern
    })
}

function Test-TraceSession {
    & logman.exe query $sessionName -ets *> $null
    return $LASTEXITCODE -eq 0
}

function Test-TraceDefinition {
    & logman.exe query $sessionName *> $null
    return $LASTEXITCODE -eq 0
}

function Stop-TraceSessionIfPresent {
    if (Test-TraceSession) {
        & logman.exe stop $sessionName
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to stop trace session '$sessionName'."
        }
    }
}

function Remove-TraceDefinitionIfPresent {
    if (Test-TraceDefinition) {
        & logman.exe delete $sessionName
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to remove trace definition '$sessionName'."
        }
    }
}

function Add-TraceProvider {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Keywords,
        [Parameter(Mandatory = $true)][int]$Level
    )

    & logman.exe update trace $sessionName -p $Name $Keywords $Level
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to add ETW provider '$Name'."
    }
}

switch ($Action) {
    'Status' {
        [pscustomobject]@{
            SessionRunning = Test-TraceSession
            DefinitionPresent = Test-TraceDefinition
            CaptureFiles = @((Get-CaptureFiles).FullName)
            CaptureBase = $capturePath
        }
        break
    }

    'Start' {
        if (Test-TraceSession) {
            throw "Trace session '$sessionName' is already running."
        }
        if (Test-TraceDefinition) {
            throw "Trace definition '$sessionName' already exists. Run Stop once to clean it up."
        }
        $existingCaptureFiles = @(Get-CaptureFiles)
        if ($existingCaptureFiles.Count -ne 0) {
            throw "Capture file already exists: $($existingCaptureFiles[0].FullName)"
        }

        $definitionCreated = $false
        try {
            # Configure every provider before starting the collector. Default +
            # FullDataBusTrace (0x101) records transfer metadata and full payloads;
            # the 64 MiB circular cap bounds local data.
            & logman.exe create trace $sessionName `
                -o $capturePath -f bincirc -max 64 -nb 128 640 -bs 128
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to create trace definition '$sessionName'."
            }
            $definitionCreated = $true

            Add-TraceProvider -Name 'Microsoft-Windows-USB-USBXHCI' `
                -Keywords '0x0000000000000101' -Level 4
            Add-TraceProvider -Name 'Microsoft-Windows-USB-UCX' `
                -Keywords '0x0000000000000101' -Level 4
            Add-TraceProvider -Name 'Microsoft-Windows-USB-USBHUB3' `
                -Keywords '0x0000000000000101' -Level 4
            Add-TraceProvider -Name 'Microsoft-Windows-Kernel-IoTrace' `
                -Keywords '0x0000000000000000' -Level 2

            & logman.exe start $sessionName
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to start trace session '$sessionName'."
            }
            if (-not (Test-TraceSession)) {
                throw "Trace session '$sessionName' did not remain active after Start."
            }
        }
        catch {
            Stop-TraceSessionIfPresent
            if ($definitionCreated) {
                Remove-TraceDefinitionIfPresent
            }
            throw
        }

        Write-Host "Passive USB HID payload trace '$CaptureTag' is running."
        Write-Host "Capture base: $capturePath (Windows may append _000001)"
        Write-Host 'Do not type sensitive information while this trace is active.'
        break
    }

    'Stop' {
        Stop-TraceSessionIfPresent
        Remove-TraceDefinitionIfPresent

        $captureFiles = @(Get-CaptureFiles)
        if ($captureFiles.Count -ne 1) {
            throw "Expected exactly one completed capture for '$CaptureTag'; found $($captureFiles.Count)."
        }
        $captureFile = $captureFiles[0]
        if ($captureFile.Length -lt 65536) {
            throw "Capture is unexpectedly small ($($captureFile.Length) bytes): $($captureFile.FullName)"
        }
        $captureHash = Get-FileHash -Algorithm SHA256 -LiteralPath $captureFile.FullName

        Write-Host "Passive USB HID payload trace '$CaptureTag' is stopped."
        Write-Host "Capture: $($captureFile.FullName)"
        Write-Host "Bytes:   $($captureFile.Length)"
        Write-Host "SHA256:  $($captureHash.Hash)"
        break
    }
}
