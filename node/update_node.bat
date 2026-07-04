@echo off
REM Byte Transcode - Windows Node updater
REM Close the node first (exit the GUI / stop run_node.bat), then run this.
REM Downloads the latest node code from GitHub into this folder. Your config
REM (byte_node_config.json) and tools\ are left untouched.
setlocal
set "DIR=%~dp0"
set "RAW=https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main/node"

echo == Byte Transcode node update ==
echo Folder: %DIR%
echo.
echo Make sure the node is CLOSED before continuing.
pause

echo Backing up current files...
if exist "%DIR%byte_node_v2.py"  copy /Y "%DIR%byte_node_v2.py"  "%DIR%byte_node_v2.py.bak"  >nul
if exist "%DIR%byte_node_gui.py" copy /Y "%DIR%byte_node_gui.py" "%DIR%byte_node_gui.py.bak" >nul

echo Downloading latest node files...
curl -fsSL "%RAW%/byte_node_v2.py"  -o "%DIR%byte_node_v2.py"
curl -fsSL "%RAW%/byte_node_gui.py" -o "%DIR%byte_node_gui.py"
curl -fsSL "%RAW%/setup_tools.py"   -o "%DIR%setup_tools.py"
curl -fsSL "%RAW%/run_node.bat"     -o "%DIR%run_node.bat"

echo.
echo Done. New tools (if any) can be fetched with:  py setup_tools.py
echo Restart the node (START_NODE.bat, run_node.bat, or byte_node_gui.py).
pause
