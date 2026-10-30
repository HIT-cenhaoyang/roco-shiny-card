@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -Command "if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 1 }"
if errorlevel 1 (
    echo Requesting administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" manual_count.py %*
    goto :end
)

if exist "%USERPROFILE%\anaconda3\envs\kingdom\python.exe" (
    "%USERPROFILE%\anaconda3\envs\kingdom\python.exe" manual_count.py %*
    goto :end
)

if exist "%USERPROFILE%\anaconda3\python.exe" (
    "%USERPROFILE%\anaconda3\python.exe" manual_count.py %*
    goto :end
)

python manual_count.py %*

:end
if errorlevel 1 pause
