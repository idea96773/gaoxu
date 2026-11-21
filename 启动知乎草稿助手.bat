@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-zhihu-drafts.ps1" -Guided
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
