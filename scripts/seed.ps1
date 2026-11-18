# Seeds database with safe demo data
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location "$root\backend"

$pythonExe = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at $pythonExe. Please run scripts\setup.ps1 first."
}

Write-Host "Seeding database with demo data..." -ForegroundColor Cyan
& $pythonExe -m app.db.seed
if ($LASTEXITCODE -ne 0) {
    throw "Database seeding failed with exit code $LASTEXITCODE."
}
Write-Host "Database seeded successfully." -ForegroundColor Green
