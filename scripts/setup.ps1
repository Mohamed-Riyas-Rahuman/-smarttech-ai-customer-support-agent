# Creates venv, installs backend deps, creates .env from example (never overwrites).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location "$root\backend"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python not found on PATH." }
if (-not (Test-Path ".venv")) { python -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created backend\.env - edit DATABASE_URL and JWT_SECRET." }
else { Write-Host "backend\.env already exists - left untouched." }
Write-Host "Setup done. Next: .\scripts\start.ps1"
