#!/usr/bin/env python3
"""
Byte Transcode — Tool Setup
Downloads ffmpeg, ffprobe, dovi_tool, and mkvmerge into the tools/ folder.
Run once after cloning the repo: py setup_tools.py
"""

import os, sys, shutil, zipfile, tarfile, io, platform

# Ensure we have urllib
from urllib.request import urlopen, Request
from urllib.error import URLError
import json

TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
HEADERS = {"User-Agent": "Byte-Transcode-Setup/1.0"}

def download(url, desc=""):
    """Download a URL and return bytes."""
    print(f"  Downloading {desc or url}...")
    req = Request(url, headers=HEADERS)
    try:
        resp = urlopen(req, timeout=120)
        data = resp.read()
        size_mb = len(data) / (1024 * 1024)
        print(f"  Downloaded {size_mb:.1f} MB")
        return data
    except URLError as e:
        print(f"  ERROR: {e}")
        return None

def get_latest_github_release(repo):
    """Get latest release tag from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = Request(url, headers=HEADERS)
    try:
        resp = urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return data.get("tag_name", ""), data.get("assets", [])
    except Exception as e:
        print(f"  ERROR fetching release info for {repo}: {e}")
        return "", []

def setup_ffmpeg():
    """Download BtbN GPL ffmpeg build with NVENC support."""
    print("\n[1/3] ffmpeg + ffprobe (BtbN GPL build with NVENC)")

    ffmpeg_path = os.path.join(TOOLS_DIR, "ffmpeg.exe")
    ffprobe_path = os.path.join(TOOLS_DIR, "ffprobe.exe")

    if os.path.exists(ffmpeg_path) and os.path.exists(ffprobe_path):
        print("  Already exists — skipping. Delete tools/ffmpeg.exe to re-download.")
        return True

    url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    data = download(url, "ffmpeg GPL build")
    if not data:
        return False

    print("  Extracting ffmpeg.exe and ffprobe.exe...")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            extracted = 0
            for name in zf.namelist():
                basename = os.path.basename(name)
                if basename in ("ffmpeg.exe", "ffprobe.exe"):
                    target = os.path.join(TOOLS_DIR, basename)
                    with open(target, "wb") as f:
                        f.write(zf.read(name))
                    extracted += 1
                    print(f"  Extracted {basename}")
            if extracted < 2:
                print("  WARNING: Could not find ffmpeg.exe/ffprobe.exe in ZIP")
                return False
    except Exception as e:
        print(f"  ERROR extracting: {e}")
        return False

    return True

def setup_dovi_tool():
    """Download dovi_tool from GitHub releases."""
    print("\n[2/3] dovi_tool (Dolby Vision metadata)")

    dovi_path = os.path.join(TOOLS_DIR, "dovi_tool.exe")
    if os.path.exists(dovi_path):
        print("  Already exists — skipping. Delete tools/dovi_tool.exe to re-download.")
        return True

    tag, assets = get_latest_github_release("quietvoid/dovi_tool")
    if not tag:
        print("  ERROR: Could not fetch latest release")
        return False

    print(f"  Latest release: {tag}")

    # Find Windows x86_64 asset
    target_asset = None
    for a in assets:
        name = a.get("name", "")
        if "x86_64" in name and "windows" in name and name.endswith(".zip"):
            target_asset = a
            break

    if not target_asset:
        print("  ERROR: No Windows x86_64 ZIP found in release assets")
        print("  Available:", [a["name"] for a in assets])
        return False

    data = download(target_asset["browser_download_url"], target_asset["name"])
    if not data:
        return False

    print("  Extracting dovi_tool.exe...")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if os.path.basename(name) == "dovi_tool.exe":
                    with open(dovi_path, "wb") as f:
                        f.write(zf.read(name))
                    print(f"  Extracted dovi_tool.exe")
                    return True
        # If not in zip, try as single file
        print("  Not found in ZIP, trying as raw binary...")
    except zipfile.BadZipFile:
        pass

    # Some releases use .tar.gz
    for a in assets:
        name = a.get("name", "")
        if "x86_64" in name and "windows" in name and ".tar.gz" in name:
            data = download(a["browser_download_url"], name)
            if data:
                try:
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                        for m in tf.getmembers():
                            if m.name.endswith("dovi_tool.exe"):
                                with open(dovi_path, "wb") as f:
                                    f.write(tf.extractfile(m).read())
                                print(f"  Extracted dovi_tool.exe")
                                return True
                except Exception as e:
                    print(f"  ERROR extracting tar.gz: {e}")

    print("  ERROR: Could not extract dovi_tool.exe")
    return False

def setup_mkvmerge():
    """Download portable MKVToolNix."""
    print("\n[3/3] mkvmerge (MKVToolNix)")

    mkvmerge_path = os.path.join(TOOLS_DIR, "mkvmerge.exe")
    if os.path.exists(mkvmerge_path):
        print("  Already exists — skipping. Delete tools/mkvmerge.exe to re-download.")
        return True

    # Try GitHub releases for MKVToolNix portable
    tag, assets = get_latest_github_release("nmaier/mkvtoolnix")
    if not tag:
        # Fallback: try direct fosshub mirror or official site
        tag, assets = get_latest_github_release("morkt/mkvtoolnix")

    # MKVToolNix doesn't always have GitHub releases — try direct download
    # The portable version is available from the official site
    print("  MKVToolNix must be downloaded manually:")
    print("  1. Go to: https://mkvtoolnix.download/downloads.html#windows")
    print("  2. Download the 'Portable' 64-bit version")
    print("  3. Extract mkvmerge.exe to: " + TOOLS_DIR)
    print()
    print("  Alternatively, install MKVToolNix via the installer and")
    print("  copy mkvmerge.exe from C:\\Program Files\\MKVToolNix\\")
    return False

def main():
    print("╔══════════════════════════════════════════╗")
    print("║   Byte Transcode — Tool Setup            ║")
    print("╚══════════════════════════════════════════╝")

    if platform.system() != "Windows":
        print("\nWARNING: This script is designed for Windows.")
        print("On Linux/Docker, tools are installed via the Dockerfile.")

    os.makedirs(TOOLS_DIR, exist_ok=True)
    print(f"\nTools directory: {TOOLS_DIR}")

    results = {}
    results["ffmpeg"] = setup_ffmpeg()
    results["dovi_tool"] = setup_dovi_tool()
    results["mkvmerge"] = setup_mkvmerge()

    print("\n" + "=" * 50)
    print("RESULTS:")
    for tool, ok in results.items():
        status = "OK" if ok else "MANUAL DOWNLOAD NEEDED"
        print(f"  {tool:15s} — {status}")

    # Verify all tools
    print("\nVerifying tools...")
    all_ok = True
    for exe in ["ffmpeg.exe", "ffprobe.exe", "dovi_tool.exe", "mkvmerge.exe"]:
        path = os.path.join(TOOLS_DIR, exe)
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            print(f"  [OK] {exe} ({size_mb:.1f} MB)")
        else:
            print(f"  [MISSING] {exe}")
            all_ok = False

    if all_ok:
        print("\nAll tools ready! Run 'py byte_node_gui.py' or double-click run_node.bat")
    else:
        print("\nSome tools are missing — see instructions above.")

    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
