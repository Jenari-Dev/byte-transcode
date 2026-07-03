#!/usr/bin/env python3
"""
Byte Transcode Node — Windows Tool Setup
Downloads ffmpeg (NVENC), dovi_tool, and mkvmerge.
Run: python setup_tools.py
"""
import os, sys, zipfile, tarfile, io, shutil, subprocess

TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")

def download(url, desc):
    """Download a URL with progress."""
    print(f"  Downloading {desc}...")
    print(f"  URL: {url}")
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "ByteTranscode/1.0"})
    resp = urllib.request.urlopen(req, timeout=120)
    total = int(resp.headers.get("Content-Length", 0))
    data = b""
    while True:
        chunk = resp.read(1024 * 1024)
        if not chunk:
            break
        data += chunk
        if total > 0:
            pct = len(data) / total * 100
            mb = len(data) / (1024 * 1024)
            print(f"\r  {mb:.1f} MB ({pct:.0f}%)", end="", flush=True)
    print(f"\r  Done — {len(data) / (1024*1024):.1f} MB")
    return data


def setup_ffmpeg():
    """Download ffmpeg with NVENC support (BtbN GPL build)."""
    ffmpeg_exe = os.path.join(TOOLS_DIR, "ffmpeg.exe")
    ffprobe_exe = os.path.join(TOOLS_DIR, "ffprobe.exe")
    if os.path.exists(ffmpeg_exe) and os.path.exists(ffprobe_exe):
        print("[ffmpeg] Already installed")
        return True

    print("[ffmpeg] Downloading BtbN GPL build (includes NVENC)...")
    url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    data = download(url, "ffmpeg")

    print("  Extracting...")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            basename = os.path.basename(member)
            if basename in ("ffmpeg.exe", "ffprobe.exe"):
                with zf.open(member) as src, open(os.path.join(TOOLS_DIR, basename), "wb") as dst:
                    dst.write(src.read())
                    print(f"  Extracted {basename}")

    if os.path.exists(ffmpeg_exe):
        print("[ffmpeg] Installed successfully")
        return True
    else:
        print("[ffmpeg] ERROR: ffmpeg.exe not found after extraction")
        return False


def setup_dovi_tool():
    """Download dovi_tool Windows binary."""
    exe = os.path.join(TOOLS_DIR, "dovi_tool.exe")
    if os.path.exists(exe):
        print("[dovi_tool] Already installed")
        return True

    print("[dovi_tool] Finding latest release...")
    import urllib.request, json
    req = urllib.request.Request("https://api.github.com/repos/quietvoid/dovi_tool/releases/latest",
                                 headers={"User-Agent": "ByteTranscode/1.0"})
    resp = urllib.request.urlopen(req, timeout=30)
    release = json.loads(resp.read())
    tag = release["tag_name"]

    # Find Windows x86_64 asset
    asset_url = None
    for asset in release.get("assets", []):
        name = asset["name"].lower()
        if "x86_64" in name and "windows" in name and name.endswith(".zip"):
            asset_url = asset["browser_download_url"]
            break

    if not asset_url:
        print("[dovi_tool] ERROR: No Windows x86_64 asset found")
        return False

    print(f"[dovi_tool] Version {tag}")
    data = download(asset_url, "dovi_tool")

    print("  Extracting...")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            if os.path.basename(member) == "dovi_tool.exe":
                with zf.open(member) as src, open(exe, "wb") as dst:
                    dst.write(src.read())
                print("  Extracted dovi_tool.exe")

    if os.path.exists(exe):
        print("[dovi_tool] Installed successfully")
        return True
    else:
        print("[dovi_tool] ERROR: extraction failed")
        return False


MKVTOOLNIX_EXES = ("mkvmerge.exe", "mkvpropedit.exe", "mkvextract.exe")

def setup_mkvmerge():
    """Download MKVToolNix portable (mkvmerge + mkvpropedit + mkvextract)."""
    exe = os.path.join(TOOLS_DIR, "mkvmerge.exe")
    if all(os.path.exists(os.path.join(TOOLS_DIR, e)) for e in MKVTOOLNIX_EXES):
        print("[mkvtoolnix] Already installed")
        return True

    print("[mkvmerge] Finding latest portable release...")
    import urllib.request, json, re
    # Get latest version from MKVToolNix
    req = urllib.request.Request("https://mkvtoolnix.download/latest-release.xml",
                                 headers={"User-Agent": "ByteTranscode/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        content = resp.read().decode()
        ver_match = re.search(r'<version>([\d.]+)</version>', content)
        if ver_match:
            ver = ver_match.group(1)
        else:
            ver = "89.0"  # fallback
    except:
        ver = "89.0"

    print(f"[mkvmerge] Version {ver}")
    url = f"https://mkvtoolnix.download/windows/releases/{ver}/mkvtoolnix-64-bit-{ver}.7z"
    # Try zip first since 7z needs special handling
    url_zip = f"https://mkvtoolnix.download/windows/releases/{ver}/mkvtoolnix-64-bit-{ver}.zip"

    try:
        data = download(url_zip, "mkvtoolnix (zip)")
        print("  Extracting...")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                basename = os.path.basename(member)
                if basename in MKVTOOLNIX_EXES:
                    with zf.open(member) as src, open(os.path.join(TOOLS_DIR, basename), "wb") as dst:
                        dst.write(src.read())
                    print(f"  Extracted {basename}")
    except Exception as e:
        print(f"  ZIP download failed ({e}), trying portable installer...")
        print(f"  Please download MKVToolNix manually from https://mkvtoolnix.download/downloads.html")
        print(f"  Then copy mkvmerge.exe, mkvpropedit.exe and mkvextract.exe to: {TOOLS_DIR}")
        return False

    if os.path.exists(exe):
        print("[mkvmerge] Installed successfully")
        return True
    else:
        print("[mkvmerge] ERROR: mkvmerge.exe not found")
        print(f"  Download manually from https://mkvtoolnix.download/downloads.html")
        print(f"  Copy mkvmerge.exe to: {TOOLS_DIR}")
        return False


def verify_tools():
    """Verify all tools work."""
    print("\n=== Verifying tools ===")
    ok = True
    for tool in ["ffmpeg", "ffprobe", "dovi_tool", "mkvmerge", "mkvpropedit", "mkvextract"]:
        exe = os.path.join(TOOLS_DIR, f"{tool}.exe")
        if os.path.exists(exe):
            try:
                r = subprocess.run([exe, "--version" if tool != "dovi_tool" else "--version"],
                                   capture_output=True, text=True, timeout=10)
                ver = r.stdout.split("\n")[0][:80] if r.stdout else r.stderr.split("\n")[0][:80]
                print(f"  ✓ {tool}: {ver}")
            except Exception as e:
                print(f"  ✓ {tool}: found (version check: {e})")
        else:
            print(f"  ✗ {tool}: NOT FOUND")
            ok = False
    return ok


def create_run_bat():
    """Create run_node.bat launcher."""
    bat_path = os.path.join(os.path.dirname(TOOLS_DIR), "run_node.bat")
    content = f'''@echo off
echo ========================================
echo  Byte Transcode Node — Native Windows
echo ========================================
echo.

set TOOLS_DIR=%~dp0tools
set PATH=%TOOLS_DIR%;%PATH%

python "%~dp0byte_node_v2.py" ^
    --server http://192.168.3.13:5800 ^
    --name DoVi-5080 ^
    --gpu "RTX 5080" ^
    --nas-drive Z: ^
    --nas-prefix /media ^
    --temp-dir "F:\\Byte_Engine_temp"

echo.
echo Node stopped. Press any key to exit.
pause
'''
    with open(bat_path, "w") as f:
        f.write(content)
    print(f"\nCreated: {bat_path}")
    print("  Edit this file to change server URL, node name, GPU, or drive letter.")


def main():
    print("=" * 50)
    print(" Byte Transcode Node — Tool Setup")
    print("=" * 50)
    print(f"Tools directory: {TOOLS_DIR}\n")

    os.makedirs(TOOLS_DIR, exist_ok=True)

    # Install pip packages
    print("[pip] Installing requests...")
    subprocess.run([sys.executable, "-m", "pip", "install", "requests", "-q"], check=False)

    ok = True
    ok = setup_ffmpeg() and ok
    ok = setup_dovi_tool() and ok
    ok = setup_mkvmerge() and ok

    verify_tools()
    create_run_bat()

    print("\n" + "=" * 50)
    if ok:
        print(" Setup complete! Run: run_node.bat")
    else:
        print(" Some tools need manual installation (see above)")
    print("=" * 50)


if __name__ == "__main__":
    main()
