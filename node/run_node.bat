@echo off
echo ========================================
echo  Byte Transcode Node - Native Windows
echo ========================================
echo.

set TOOLS_DIR=%~dp0tools
set PATH=%TOOLS_DIR%;%PATH%

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
