$root = Split-Path -Parent $PSScriptRoot
Set-Location "$root\backend"
$env:PYTHONPATH = "."
& ".\.venv\Scripts\python.exe" -m pytest tests -q
