@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
set "exitCode=%ERRORLEVEL%"
endlocal & exit /b %exitCode%
