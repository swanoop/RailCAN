@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -m railcan ui
) else (
    py -3 -m railcan ui
)
if errorlevel 1 (
    echo.
    echo RailCAN requires Python 3.10 or newer. See README.md for setup.
    pause
)
