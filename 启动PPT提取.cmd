@echo off
chcp 65001 >nul
setlocal
py -3 --version >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0extract_course.py" --extract ppt %*
    goto done
)
python --version >nul 2>&1
if not errorlevel 1 (
    python "%~dp0extract_course.py" --extract ppt %*
    goto done
)
echo Python 3.10+ is required. Run extract_ppt.py with your existing Python interpreter.
:done
echo.
pause
