@echo off
setlocal
cd /d "%~dp0"

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
