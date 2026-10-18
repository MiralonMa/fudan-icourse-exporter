@echo off
chcp 65001 >nul
setlocal
py -3 --version >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0extract_subtitles.py" %*
    goto done
)
python --version >nul 2>&1
if not errorlevel 1 (
    python "%~dp0extract_subtitles.py" %*
    goto done
)
echo Python 3.10+ is required. Install Python or run this script with your existing Python interpreter.
:done
echo.
pause
