@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh-grok.ps1"
exit /b %ERRORLEVEL%
