$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$port = 8000
$baseUrl = "http://127.0.0.1:$port"
$healthUrl = "$baseUrl/health"
$readyUrl = "$baseUrl/ready"
$dashboardUrl = "$baseUrl/app/"
$pidFile = Join-Path $root '.smarttech-backend.pid'
$logDir = Join-Path $root 'scratch\logs'
$stdoutLog = Join-Path $logDir 'backend.out.log'
$stderrLog = Join-Path $logDir 'backend.err.log'

function Stop-StartedProcess([int]$ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -like "*$backend*" -and $process.CommandLine -like '*uvicorn*') {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Open-Dashboard([string]$Url) {
    try {
        Start-Process $Url -ErrorAction Stop
        Write-Host 'Opened the dashboard in the default browser.'
    } catch {
        Write-Host "Default browser launch was blocked by Windows; open $Url manually." -ForegroundColor Yellow
    }
}

function Test-SmartTechReady([string]$Url) {
    # HttpWebRequest is available in Windows PowerShell 5.1's .NET Framework.
    # Disable proxies explicitly so system proxy settings cannot intercept loopback.
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Proxy = $null
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $response = $null
    $reader = $null
    try {
        try {
            $response = $request.GetResponse()
        } catch [System.Net.WebException] {
            # HTTP errors, connection failures, and request timeouts mean not ready.
            return $false
        }
        if ([int]$response.StatusCode -lt 200 -or [int]$response.StatusCode -ge 300) { return $false }
        $reader = New-Object -TypeName System.IO.StreamReader -ArgumentList @($response.GetResponseStream())
        $body = $reader.ReadToEnd()
        try {
            $payload = ConvertFrom-Json -InputObject $body -ErrorAction Stop
        } catch {
            # A malformed readiness response is not a ready backend.
            return $false
        }
        return ($null -ne $payload -and $payload.ready -is [bool] -and $payload.ready)
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
        $request.Abort()
    }
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Required backend environment is missing: backend\.venv. Run scripts\setup.ps1, then start again."
}

Set-Location $backend
$env:PYTHONPATH = $backend
& $python -c "import fastapi, uvicorn, sqlalchemy, alembic, googleapiclient, langchain, langchain_openai, pgvector" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'A required backend dependency is missing. Run scripts\setup.ps1 to install requirements.' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency check failed. Repair the existing backend\.venv before starting.' }

$configCheck = @'
from app.core.config import settings
from pathlib import Path
url = settings.database_url.strip()
if not url or any(token in url.upper() for token in ("USERNAME:", ":PASSWORD@", "@HOST:", "/DATABASE_NAME")):
    raise SystemExit("Database configuration is missing or still uses example placeholders.")
if not settings.jwt_secret or settings.jwt_secret == "CHANGE_ME_LONG_RANDOM_STRING":
    raise SystemExit("JWT_SECRET must be configured before startup.")
print("Configuration check passed (secret values hidden).")
print("Groq:", "configured" if settings.groq_api_key else "not configured")
print("Gmail OAuth client:", "configured" if Path(settings.gmail_credentials_file).is_file() else "not configured")
'@
$configCheck | & $python -
if ($LASTEXITCODE -ne 0) { throw 'Required backend configuration is incomplete; see the message above.' }

# Validate the database and pgvector extension before launching. Never seed or
# rewrite the database from startup. Pending schema changes require the explicit
# scripts\migrate.ps1 command after reviewing the migration files.
$dbCheck = @'
from sqlalchemy import text
from app.db.session import engine
with engine.connect() as conn:
    conn.execute(text("SELECT 1"))
    vector = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar()
    if not vector:
        raise SystemExit("PostgreSQL is reachable, but the pgvector extension is missing.")
print("PostgreSQL connectivity and pgvector check passed.")
'@
$dbCheck | & $python -
if ($LASTEXITCODE -ne 0) { throw 'Database readiness check failed. No server was started.' }

$schemaCheck = @'
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from app.db.session import engine
cfg = Config("alembic.ini")
heads = ScriptDirectory.from_config(cfg).get_heads()
with engine.connect() as conn:
    current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
if len(heads) != 1:
    raise SystemExit("Migration history has multiple heads; review migrations before startup.")
if current != heads[0]:
    raise SystemExit(f"Database schema is not current (current={current}, head={heads[0]}). Review migrations, then run scripts\\migrate.ps1.")
print("Database schema revision is current.")
'@
$schemaCheck | & $python -
if ($LASTEXITCODE -ne 0) { throw 'Could not verify the current migration revision. No server was started.' }

$existingReady = Test-SmartTechReady $readyUrl

if ($existingReady) {
    Write-Host "SmartTech backend is already running and ready at $baseUrl" -ForegroundColor Green
    Write-Host "Dashboard: $dashboardUrl"
    Open-Dashboard $dashboardUrl
} else {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) { throw "Port $port is occupied by PID $($listener.OwningProcess), and it did not answer SmartTech /ready. No process was stopped." }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $server = Start-Process -FilePath $python `
        -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$port") `
        -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    Set-Content -LiteralPath $pidFile -Value $server.Id -NoNewline

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if ($server.HasExited) { break }
    if (Test-SmartTechReady $readyUrl) { $ready = $true; break }
    Start-Sleep -Milliseconds 750
}

    if (-not $ready) {
        Stop-StartedProcess $server.Id
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        Write-Host "Backend did not become ready. Logs: $stdoutLog and $stderrLog" -ForegroundColor Red
        throw 'SmartTech startup failed before readiness. Review the backend logs (secrets are not printed by this script).'
    }

    Write-Host ''
    Write-Host 'SmartTech Support Agent is ready.' -ForegroundColor Green
    Write-Host "Dashboard: $dashboardUrl" -ForegroundColor Cyan
    Write-Host "API docs:  $baseUrl/docs"
    Write-Host "Health:    $healthUrl"
    Write-Host "Backend PID: $($server.Id)"
    Write-Host "Logs:      $stdoutLog"
    Write-Host 'Automatic email sending remains controlled by backend configuration.'
    Open-Dashboard $dashboardUrl
}
