@echo off
title Byte Transcode Node
echo.
echo  ╔══════════════════════════════════════╗
echo  ║     BYTE TRANSCODE NODE LAUNCHER     ║
echo  ╚══════════════════════════════════════╝
echo.

:: Try 'py' first (Python Launcher — official installer)
py --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo  [OK] Found Python via 'py' launcher
    echo.
    py "%~dp0byte_node_gui.py"
    goto :end
)

:: Try 'python' (Microsoft Store or PATH install)
python --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    :: Verify it's Python 3, not Python 2
    python -c "import sys; exit(0 if sys.version_info[0]>=3 else 1)" >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        echo  [OK] Found Python via 'python'
        echo.
        python "%~dp0byte_node_gui.py"
        goto :end
    ) else (
        echo  [WARN] 'python' found but it's Python 2 — need Python 3
    )
)

:: Try 'python3' (some custom installs)
python3 --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo  [OK] Found Python via 'python3'
    echo.
    python3 "%~dp0byte_node_gui.py"
    goto :end
)

:: Nothing found
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║  ERROR: Python 3 not found!                         ║
echo  ║                                                     ║
echo  ║  Install Python 3.10+ from one of:                  ║
echo  ║    - https://www.python.org/downloads/              ║
echo  ║    - Microsoft Store (search "Python 3.12")         ║
echo  ║                                                     ║
echo  ║  During install, CHECK "Add Python to PATH"         ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

:end
pause
