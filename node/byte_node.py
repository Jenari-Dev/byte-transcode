#!/usr/bin/env python3
"""
Byte Transcode Node
===================
Worker node that connects to the Byte Transcode Server,
pulls jobs from the queue, and executes transcodes using
the local GPU.

Runs on the machine with the GPU (e.g., Windows Docker container
with NVENC access).

Usage:
  python3 byte_node.py --server http://192.168.3.13:5800
  python3 byte_node.py --server http://192.168.3.13:5800 --name "DoVi-5080" --gpu "RTX 5080"
"""

import os
import sys
import json
import time
import socket
import hashlib
import logging
import signal
import subprocess
import shutil
import threading
import argparse
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("Installing requests...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests


# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("byte-node")


# ─── Tool Discovery ─────────────────────────────────────────────────────────

def find_tool(name, alternatives=None):
    candidates = [name] + (alternatives or [])
    for c in candidates:
        result = subprocess.run(["which", c], capture_output=True, text=True)
        if result.returncode == 0:
            return c
    return name


FFMPEG = find_tool("tdarr-ffmpeg", ["ffmpeg"])
FFPROBE = find_tool("ffprobe", ["tdarr-ffprobe"])
DOVI_TOOL = find_tool("dovi_tool")
MKVMERGE = find_tool("mkvmerge")


# ─── Transcode Pipelines ────────────────────────────────────────────────────

def run_cmd(cmd, description):
    """Run a command, return (success, stderr_tail)."""
    log.info(f"  [CMD] {description}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_tail = "\n".join(result.stderr.strip().split("\n")[-10:]) if result.stderr else ""
        log.error(f"  [FAILED] {description}: {stderr_tail[:200]}")
        return False, stderr_tail
    return True, ""


def transcode_dovi(file_path, settings, work_dir, progress_callback):
    """DoVi-preserving transcode pipeline."""
    cq = int(settings.get("cq", "18"))
    preset = settings.get("preset", "slow")
    convert_p8 = settings.get("dovi_convert_p8", "true") == "true"
    replace = settings.get("replace_original", "true") == "true"

    basename = os.path.splitext(os.path.basename(file_path))[0]
    raw_hevc = os.path.join(work_dir, f"{basename}.hevc")
    rpu_bin = os.path.join(work_dir, f"{basename}.rpu.bin")
    transcoded_hevc = os.path.join(work_dir, f"{basename}_transcoded.hevc")
    injected_hevc = os.path.join(work_dir, f"{basename}_injected.hevc")
    profile8_hevc = os.path.join(work_dir, f"{basename}_profile8.hevc")
    output_path = os.path.join(work_dir, f"{basename}_sentinel.mkv")

    total_steps = 6 if convert_p8 else 5

    # Step 1: Extract HEVC
    progress_callback(5, f"Step 1/{total_steps}: Extracting HEVC bitstream")
    ok, err = run_cmd([
        FFMPEG, "-y", "-i", file_path,
        "-map", "0:v:0", "-c:v", "copy",
        "-bsf:v", "hevc_mp4toannexb",
        "-f", "hevc", raw_hevc
    ], "Extract HEVC")
    if not ok:
        return None, f"HEVC extraction failed: {err[:100]}"

    # Step 2: Extract RPU
    progress_callback(15, f"Step 2/{total_steps}: Extracting DoVi RPU")
    ok, err = run_cmd([
        DOVI_TOOL, "extract-rpu", "-i", raw_hevc, "-o", rpu_bin
    ], "Extract RPU")
    if not ok:
        return None, f"RPU extraction failed: {err[:100]}"

    # Step 3: NVENC Transcode
    progress_callback(20, f"Step 3/{total_steps}: NVENC Transcode (CQ {cq})")

    # Start transcode in background and monitor progress
    proc = subprocess.Popen(
        [FFMPEG, "-y", "-i", raw_hevc,
         "-c:v", "hevc_nvenc", "-preset", preset, "-cq", str(cq),
         "-f", "hevc", transcoded_hevc],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    # Monitor progress by checking file size growth
    input_size = os.path.getsize(raw_hevc)
    while proc.poll() is None:
        time.sleep(5)
        if os.path.exists(transcoded_hevc):
            out_size = os.path.getsize(transcoded_hevc)
            # Estimate progress (rough: output is typically 20-35% of input)
            estimated_ratio = 0.28  # average compression
            estimated_final = input_size * estimated_ratio
            pct = min(85, 20 + (out_size / max(estimated_final, 1)) * 55)
            progress_callback(pct, f"Step 3/{total_steps}: Transcoding... ({out_size/(1024**3):.1f} GB)")

    if proc.returncode != 0:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        return None, f"NVENC transcode failed: {stderr[-200:]}"

    transcoded_gb = os.path.getsize(transcoded_hevc) / (1024**3)
    original_gb = os.path.getsize(raw_hevc) / (1024**3)
    log.info(f"  Video: {original_gb:.2f} GB → {transcoded_gb:.2f} GB ({(1 - transcoded_gb/original_gb)*100:.1f}%)")

    # Free space
    os.remove(raw_hevc)

    # Step 4: Inject RPU
    progress_callback(80, f"Step 4/{total_steps}: Injecting DoVi RPU")
    ok, err = run_cmd([
        DOVI_TOOL, "inject-rpu",
        "-i", transcoded_hevc, "--rpu-in", rpu_bin,
        "-o", injected_hevc
    ], "Inject RPU")
    if not ok:
        return None, f"RPU injection failed: {err[:100]}"

    os.remove(transcoded_hevc)
    os.remove(rpu_bin)

    # Step 5: P7→P8 Convert (optional)
    source_hevc = injected_hevc
    if convert_p8:
        progress_callback(88, f"Step 5/{total_steps}: Converting P7 → P8")
        ok, err = run_cmd([
            DOVI_TOOL, "-m", "2", "convert", "--discard",
            "-i", injected_hevc, "-o", profile8_hevc
        ], "P7→P8 Convert")
        if not ok:
            return None, f"P7→P8 conversion failed: {err[:100]}"
        source_hevc = profile8_hevc
        os.remove(injected_hevc)

    # Step 6: mkvmerge Remux
    step_num = total_steps
    progress_callback(92, f"Step {step_num}/{total_steps}: Remuxing with mkvmerge")
    ok, err = run_cmd([
        MKVMERGE, "-o", output_path,
        source_hevc,
        "--no-video", file_path
    ], "mkvmerge Remux")
    if not ok:
        return None, f"mkvmerge remux failed: {err[:100]}"

    # Cleanup source hevc
    if os.path.exists(source_hevc):
        os.remove(source_hevc)

    progress_callback(98, "Finalizing...")

    # Replace original
    if replace:
        final_path = os.path.splitext(file_path)[0] + ".mkv"
        try:
            os.remove(file_path)
            shutil.move(output_path, final_path)
            output_path = final_path
        except Exception as e:
            log.error(f"  Failed to replace original: {e}")

    return output_path, None


def transcode_standard(file_path, settings, work_dir, progress_callback):
    """Standard SDR/HDR10/HDR10+ transcode."""
    cq = int(settings.get("cq", "18"))
    preset = settings.get("preset", "slow")
    replace = settings.get("replace_original", "true") == "true"

    basename = os.path.splitext(os.path.basename(file_path))[0]
    output_path = os.path.join(work_dir, f"{basename}_sentinel.mkv")

    progress_callback(10, "NVENC Transcode starting...")

    proc = subprocess.Popen(
        [FFMPEG, "-y", "-i", file_path,
         "-map", "0", "-map", "-0:d",
         "-c:v", "hevc_nvenc", "-preset", preset, "-cq", str(cq),
         "-c:a", "copy", "-c:s", "copy",
         "-map_chapters", "0", "-map_metadata", "0",
         output_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    input_size = os.path.getsize(file_path)
    while proc.poll() is None:
        time.sleep(5)
        if os.path.exists(output_path):
            out_size = os.path.getsize(output_path)
            pct = min(90, 10 + (out_size / max(input_size * 0.5, 1)) * 75)
            progress_callback(pct, f"Transcoding... ({out_size/(1024**3):.1f} GB)")

    if proc.returncode != 0:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        return None, f"Transcode failed: {stderr[-200:]}"

    progress_callback(95, "Finalizing...")

    if replace:
        final_path = os.path.splitext(file_path)[0] + ".mkv"
        try:
            os.remove(file_path)
            shutil.move(output_path, final_path)
            output_path = final_path
        except Exception as e:
            log.error(f"  Failed to replace original: {e}")

    return output_path, None


# ─── Node Client ─────────────────────────────────────────────────────────────

class ByteNode:
    """Worker node that connects to Byte Server and processes jobs."""

    def __init__(self, server_url, name, gpu, poll_interval=10):
        self.server_url = server_url.rstrip("/")
        self.name = name
        self.gpu = gpu
        self.poll_interval = poll_interval
        self.worker_id = hashlib.md5(
            f"{name}-{socket.gethostname()}-{gpu}".encode()
        ).hexdigest()[:12]
        self.shutdown = False
        self.current_job = None

        log.info(f"Byte Node initialized")
        log.info(f"  Server:    {self.server_url}")
        log.info(f"  Node:      {self.name}")
        log.info(f"  GPU:       {self.gpu}")
        log.info(f"  Worker ID: {self.worker_id}")
        log.info(f"  Tools:     ffmpeg={FFMPEG} dovi_tool={DOVI_TOOL} mkvmerge={MKVMERGE}")

    def api(self, method, endpoint, data=None):
        """Make API call to server."""
        url = f"{self.server_url}/api{endpoint}"
        try:
            if method == "GET":
                resp = requests.get(url, timeout=30)
            elif method == "POST":
                resp = requests.post(url, json=data or {}, timeout=30)
            elif method == "PUT":
                resp = requests.put(url, json=data or {}, timeout=30)
            else:
                return None

            if resp.status_code >= 400:
                log.error(f"API error {resp.status_code}: {endpoint}")
                return None
            return resp.json()
        except requests.exceptions.ConnectionError:
            log.warning(f"Cannot connect to server at {self.server_url}")
            return None
        except Exception as e:
            log.error(f"API error: {e}")
            return None

    def register(self):
        """Register this node with the server."""
        result = self.api("POST", "/workers/register", {
            "id": self.worker_id,
            "name": self.name,
            "host": socket.gethostname(),
            "gpu": self.gpu,
        })
        if result:
            log.info("Registered with server")
            return True
        return False

    def heartbeat(self):
        """Send heartbeat to server."""
        self.api("POST", "/workers/heartbeat", {"id": self.worker_id})

    def pull_job(self):
        """Request next available job from server."""
        result = self.api("POST", "/jobs/next", {"worker_id": self.worker_id})
        if result and result.get("job"):
            return result["job"]
        return None

    def report_progress(self, job_id, progress, step, eta=""):
        """Report progress to server."""
        self.api("POST", f"/jobs/{job_id}/progress", {
            "progress": progress,
            "step": step,
            "eta": eta,
        })

    def report_complete(self, job_id, output_path, output_size_gb, reduction_pct, saved_gb):
        """Report job completion to server."""
        self.api("POST", f"/jobs/{job_id}/complete", {
            "worker_id": self.worker_id,
            "output_path": output_path,
            "output_size_gb": output_size_gb,
            "reduction_pct": reduction_pct,
            "saved_gb": saved_gb,
        })

    def report_error(self, job_id, error):
        """Report job failure to server."""
        self.api("POST", f"/jobs/{job_id}/error", {
            "worker_id": self.worker_id,
            "error": error,
        })

    def process_job(self, job):
        """Process a single transcode job."""
        job_id = job["id"]
        file_path = job["file_path"]
        file_name = job["file_name"]
        hdr_type = job["hdr_type"]
        has_dovi = job.get("has_dovi", 0)
        settings = job.get("settings", {})

        log.info(f"\n{'='*60}")
        log.info(f"Processing: {file_name}")
        log.info(f"  Type: {hdr_type} | Size: {job['file_size_gb']:.1f} GB")
        log.info(f"{'='*60}")

        # Create work directory
        work_dir = os.path.join(
            settings.get("temp_path", "/temp/byte_work"),
            f"job_{job_id}"
        )
        os.makedirs(work_dir, exist_ok=True)

        # Progress callback
        def progress_cb(pct, step, eta=""):
            self.report_progress(job_id, pct, step, eta)
            log.info(f"  [{pct:.0f}%] {step}")

        start_time = time.time()
        input_size_gb = job["file_size_gb"]

        try:
            if has_dovi:
                output_path, error = transcode_dovi(
                    file_path, settings, work_dir, progress_cb
                )
            else:
                output_path, error = transcode_standard(
                    file_path, settings, work_dir, progress_cb
                )

            if error:
                self.report_error(job_id, error)
                log.error(f"  ❌ FAILED: {error}")
                return

            # Calculate results
            output_size_gb = os.path.getsize(output_path) / (1024**3) if output_path and os.path.exists(output_path) else 0
            reduction_pct = (1 - output_size_gb / input_size_gb) * 100 if input_size_gb > 0 else 0
            saved_gb = input_size_gb - output_size_gb
            elapsed = time.time() - start_time

            self.report_complete(job_id, output_path, output_size_gb, reduction_pct, saved_gb)

            log.info(f"\n  ✅ COMPLETE: {file_name}")
            log.info(f"     {input_size_gb:.1f} GB → {output_size_gb:.1f} GB ({reduction_pct:.0f}% reduction)")
            log.info(f"     Time: {elapsed/60:.1f} minutes | Saved: {saved_gb:.1f} GB")

        except Exception as e:
            self.report_error(job_id, str(e))
            log.error(f"  ❌ EXCEPTION: {e}")

        finally:
            # Cleanup work dir
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass

    def run(self):
        """Main loop — register, poll for jobs, process."""
        # Register with server (retry until connected)
        while not self.shutdown:
            if self.register():
                break
            log.warning(f"Cannot reach server at {self.server_url}, retrying in 10s...")
            time.sleep(10)

        log.info("Node running — waiting for jobs...")

        # Heartbeat thread
        def heartbeat_loop():
            while not self.shutdown:
                self.heartbeat()
                time.sleep(30)

        hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        hb_thread.start()

        # Main polling loop
        while not self.shutdown:
            job = self.pull_job()
            if job:
                self.current_job = job
                self.process_job(job)
                self.current_job = None
            else:
                time.sleep(self.poll_interval)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Byte Transcode Node")
    parser.add_argument("--server", required=True,
                       help="Byte Server URL (e.g., http://192.168.3.13:5800)")
    parser.add_argument("--name", default="ByteNode-1",
                       help="Node name (default: ByteNode-1)")
    parser.add_argument("--gpu", default="RTX 5080",
                       help="GPU name (default: RTX 5080)")
    parser.add_argument("--poll", type=int, default=10,
                       help="Poll interval in seconds (default: 10)")

    args = parser.parse_args()

    node = ByteNode(
        server_url=args.server,
        name=args.name,
        gpu=args.gpu,
        poll_interval=args.poll,
    )

    # Graceful shutdown
    def shutdown_handler(sig, frame):
        log.info("\n⚠️  Shutdown requested...")
        node.shutdown = True
    signal.signal(signal.SIGINT, shutdown_handler)

    node.run()


if __name__ == "__main__":
    main()
