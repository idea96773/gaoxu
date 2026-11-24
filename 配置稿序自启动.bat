@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure-startup.ps1"
set "gaoxu_result=%errorlevel%"
pause
exit /b %gaoxu_result%
