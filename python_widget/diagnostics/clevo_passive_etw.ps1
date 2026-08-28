#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$win32kSession = 'ClevoLightingWin32k'
$wmiSession = 'ClevoLightingWmi'
$captureRoot = [System.IO.Path]::GetTempPath().TrimEnd('\')
$win32kPath = Join-Path $captureRoot 'ClevoLighting_Win32k_20260827.etl'
$wmiPath = Join-Path $captureRoot 'ClevoLighting_Wmi_20260827.etl'

function Test-TraceSession {
    param([Parameter(Mandatory = $true)][string]$Name)

    & logman.exe query $Name -ets *> $null
    return $LASTEXITCODE -eq 0
}

function Stop-TraceSessionIfPresent {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (Test-TraceSession -Name $Name) {
        & logman.exe stop $Name -ets
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to stop trace session '$Name'."
        }
    }
}

switch ($Action) {
    'Status' {
        [pscustomobject]@{
            Win32kSessionRunning = Test-TraceSession -Name $win32kSession
            WmiSessionRunning = Test-TraceSession -Name $wmiSession
            Win32kCapture = $win32kPath
            WmiCapture = $wmiPath
        }
        break
    }

    'Start' {
        if (Test-TraceSession -Name $win32kSession) {
            throw "Trace session '$win32kSession' is already running."
        }
        if (Test-TraceSession -Name $wmiSession) {
            throw "Trace session '$wmiSession' is already running."
        }
        if (Test-Path -LiteralPath $win32kPath) {
            throw "Capture file already exists: $win32kPath"
        }
        if (Test-Path -LiteralPath $wmiPath) {
            throw "Capture file already exists: $wmiPath"
        }

        $win32kStarted = $false
        try {
            & logman.exe create trace $win32kSession `
                -p Microsoft-Windows-Win32k 0x0400004000C00400 5 `
                -o $win32kPath -f bincirc -max 128 -ets
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to start trace session '$win32kSession'."
            }
            $win32kStarted = $true

            & logman.exe create trace $wmiSession `
                -p Microsoft-Windows-WMI-Activity 0xE000000000000000 4 `
                -o $wmiPath -f bincirc -max 64 -ets
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to start trace session '$wmiSession'."
            }
        }
        catch {
            if ($win32kStarted) {
                Stop-TraceSessionIfPresent -Name $win32kSession
            }
            throw
        }

        Write-Host 'Passive Clevo trace is running.'
        Write-Host "Win32k: $win32kPath"
        Write-Host "WMI:    $wmiPath"
        break
    }

    'Stop' {
        Stop-TraceSessionIfPresent -Name $wmiSession
        Stop-TraceSessionIfPresent -Name $win32kSession
        Write-Host 'Passive Clevo trace is stopped.'
        Write-Host "Win32k: $win32kPath"
        Write-Host "WMI:    $wmiPath"
        break
    }
}
