$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$pidFile = Join-Path $root '.smarttech-backend.pid'

if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    Write-Host 'No SmartTech PID file exists. The managed backend is not running.' -ForegroundColor Yellow
    exit 0
}

$rawPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
$pidValue = 0
if (-not [int]::TryParse($rawPid, [ref]$pidValue) -or $pidValue -le 0) {
    throw 'The SmartTech PID file is invalid; it was left untouched.'
}

$process = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host 'The managed backend has already stopped.' -ForegroundColor Yellow
    exit 0
}

$expectedPython = Join-Path $backend '.venv\Scripts\python.exe'
if ($process.ExecutablePath -ne $expectedPython -or $process.CommandLine -notlike '*uvicorn*app.main:app*') {
    throw "PID $pidValue no longer identifies this project's backend; refusing to stop it."
}

if (-not ('SmartTechConsoleSignal' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SmartTechConsoleSignal {
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint processId);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GenerateConsoleCtrlEvent(uint eventType, uint processGroupId);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);
}
'@ -ErrorAction SilentlyContinue
}

# Ask Uvicorn to handle Ctrl+C so FastAPI's lifespan can stop the sync worker.
[void][SmartTechConsoleSignal]::FreeConsole()
[void][SmartTechConsoleSignal]::SetConsoleCtrlHandler([IntPtr]::Zero, $true)
$attached = [SmartTechConsoleSignal]::AttachConsole([uint32]$pidValue)
if ($attached) {
    [void][SmartTechConsoleSignal]::GenerateConsoleCtrlEvent(0, 0)
    [void][SmartTechConsoleSignal]::FreeConsole()
}

$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 250
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
} while ($process -and (Get-Date) -lt $deadline)

if ($process) {
    Write-Warning 'Graceful shutdown timed out; stopping the verified SmartTech backend process.'
    Stop-Process -Id $pidValue -Force
}

Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Write-Host 'SmartTech backend stopped.' -ForegroundColor Green
