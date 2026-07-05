@echo off
REM ========================================
REM  Byte Transcode Node - headless console
REM ========================================
REM Runs the node in the terminal (no GUI). Use this for a server/headless
REM machine. For the desktop GUI with the update bell, use run_node.bat.

set "TOOLS_DIR=%~dp0tools"
set "PATH=%TOOLS_DIR%;%PATH%"

py "%~dp0byte_node_v2.py" ^
    --server http://192.168.3.13:5800 ^
    --name DoVi-5080 ^
    --gpu "RTX 5080" ^
    --nas-drive Z: ^
    --nas-prefix /media ^
    --temp-dir "F:\Byte_Engine_temp"

echo.
echo Node stopped. Press any key to exit.
pause
