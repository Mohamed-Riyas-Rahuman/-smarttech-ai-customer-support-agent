# Runs database migrations using Alembic
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location "$root\backend"

$alembicExe = ".\.venv\Scripts\alembic.exe"
if (-not (Test-Path $alembicExe)) {
    throw "Alembic executable not found at $alembicExe. Please run scripts\setup.ps1 first."
}

Write-Host "Applying database migrations..." -ForegroundColor Cyan
& $alembicExe upgrade head
if ($LASTEXITCODE -ne 0) {
    throw "Database migration failed with exit code $LASTEXITCODE."
}
Write-Host "Database migrations applied successfully." -ForegroundColor Green
