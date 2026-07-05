@echo off
REM ========================================
REM  Byte Transcode Node - launches the GUI
REM ========================================
REM Opens the desktop GUI (with the update bell + live status). Your settings
REM below are passed through so the GUI matches this machine. To run the old
REM headless console instead, use run_node_console.bat.

set "TOOLS_DIR=%~dp0tools"
set "PATH=%TOOLS_DIR%;%PATH%"

set "SERVER=http://192.168.3.13:5800"
set "NAME=DoVi-5080"
set "GPU=RTX 5080"
set "NASDRIVE=Z:"
set "NASPREFIX=/media"
set "TEMPDIR=F:\Byte_Engine_temp"

REM Launch the GUI without a lingering console (pythonw = no console window).
start "" pythonw "%~dp0byte_node_gui.py" ^
    --server %SERVER% --name "%NAME%" --gpu "%GPU%" ^
    --nas-drive %NASDRIVE% --nas-prefix %NASPREFIX% --temp-dir "%TEMPDIR%"

REM If pythonw isn't available or the GUI fails instantly, fall back to a
REM visible console run so errors are readable.
if errorlevel 1 (
  echo pythonw failed - starting GUI with py so you can see any error...
  py "%~dp0byte_node_gui.py" ^
    --server %SERVER% --name "%NAME%" --gpu "%GPU%" ^
    --nas-drive %NASDRIVE% --nas-prefix %NASPREFIX% --temp-dir "%TEMPDIR%"
  pause
)
