@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-service.ps1"
exit /b %ERRORLEVEL%
