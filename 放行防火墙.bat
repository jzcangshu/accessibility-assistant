@echo off
rem One-click firewall fix for the phone-connection problem.
rem Thin wrapper: all real logic lives in tools\allow_firewall.ps1
rem (PowerShell handles the JSON config and the Chinese text far better than cmd).
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\allow_firewall.ps1"
echo.
pause
