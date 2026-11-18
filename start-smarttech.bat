@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
set "exitCode=%ERRORLEVEL%"
endlocal & exit /b %exitCode%
