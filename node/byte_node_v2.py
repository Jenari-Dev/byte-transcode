#!/usr/bin/env python3
"""
Byte Transcode Node v2
======================
Worker node that connects to the Byte Transcode server.
Runs on a machine with GPU (inside Docker with ffmpeg/dovi_tool/mkvmerge).

Features:
  - Registers with server and sends heartbeats
  - Polls for transcode jobs
  - Full DoVi P7→P8 pipeline + standard NVENC transcode
  - Real-time FPS/progress/ETA parsing from ffmpeg output
  - C%/T% compression ratio reporting
  - Cancel polling with ffmpeg process kill
  - mkvmerge I/O watchdog (kills stuck remux)
  - Temp file cleanup on failure
  - Sends detailed log lines to server

Usage:
  python3 byte_node.py --server http://192.168.3.13:5800 --name DoVi-5080 --gpu "RTX 5080"
"""

import sys, os, time, json, hashlib, shutil, subprocess, threading, signal, re, socket, platform, argparse
from datetime import datetime

# Auto-install requests if missing
try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q", "--break-system-packages"])
    import requests


class ByteNode:
    def __init__(self, server_url, name, gpu, poll_interval=10):
        self.server = server_url.rstrip('/')
        self.name = name
        self.gpu = gpu
        self.poll_interval = poll_interval
        self.worker_id = hashlib.md5(f"{name}-{socket.gethostname()}".encode()).hexdigest()[:12]
        self.host = socket.gethostname()
        self.running = True
        self.job_procs = {}   # job_id -> subprocess.Popen (per-job process tracking)
        self.job_cancel = {}  # job_id -> bool (per-job cancel flag)
        self.job_lock = threading.Lock()
        self.current_job_id = None  # Legacy — used by GUI for display
        self.hc_active = 0  # Track active health check count
        self.hc_lock = threading.Lock()
        self.active_dovi = 0  # Track active DoVi transcode count
        self.dovi_lock = threading.Lock()
        self.max_dovi_concurrent = 2  # Updated by fetch_worker_counts()

        # Path translation: NAS paths → local paths (set by GUI or CLI args)
        self.native_mode = False
        self.nas_prefix = "/media"
        self.nas_drive = ""
        self.temp_base = "/temp/byte_work"

        # Find tools
        self.ffmpeg = self._find_tool("tdarr-ffmpeg", ["ffmpeg"])
        self.ffprobe = self._find_tool("ffprobe", ["tdarr-ffprobe"])
        self.dovi_tool = self._find_tool("dovi_tool")
        self.mkvmerge = self._find_tool("mkvmerge")

        self.log(f"Byte Node v2 initialized")
        self.log(f"  Worker ID: {self.worker_id}")
        self.log(f"  Server: {self.server}")
        self.log(f"  GPU: {self.gpu}")
        self.log(f"  ffmpeg: {self.ffmpeg}")
        self.log(f"  dovi_tool: {self.dovi_tool}")
        self.log(f"  mkvmerge: {self.mkvmerge}")

    def translate_path(self, server_path):
        """Convert server NAS path to local path. In native mode, maps /media → Z:\\ (or configured drive)."""
        if self.native_mode and server_path and server_path.startswith(self.nas_prefix):
            rel = server_path[len(self.nas_prefix):]
            local = self.nas_drive + rel.replace("/", os.sep)
            return local
        return server_path

    def reverse_translate_path(self, local_path):
        """Convert local Windows path back to server NAS path. Z:\\ → /media."""
        if self.native_mode and local_path:
            # Normalize separators
            normalized = local_path.replace(os.sep, "/").replace("\\", "/")
            nas_drive_fwd = self.nas_drive.replace(os.sep, "/").replace("\\", "/")
            if normalized.startswith(nas_drive_fwd):
                rel = normalized[len(nas_drive_fwd):]
                return self.nas_prefix + rel
        return local_path

    def fetch_worker_counts(self):
        """Fetch worker count settings from server."""
        r = self.api("GET", "/api/worker-counts")
        if r:
            tc_gpu = int(r.get("transcode_gpu_count", "1") or "1")
            hc_gpu = int(r.get("healthcheck_gpu_count", "3") or "3")
            self.max_dovi_concurrent = int(r.get("max_dovi_concurrent", "2") or "2")
            return {"transcode_gpu": max(1, tc_gpu), "healthcheck_gpu": max(1, hc_gpu)}
        self.max_dovi_concurrent = 2
        return {"transcode_gpu": 1, "healthcheck_gpu": 3}

    def _find_tool(self, name, alternatives=None):
        for n in [name] + (alternatives or []):
            try:
                r = subprocess.run([n, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                if r.returncode == 0:
                    return n
            except:
                pass
        return name

    def log(self, msg, level="INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{level}] {msg}", flush=True)

    def api(self, method, path, data=None, timeout=30):
        """Make API call to server with error handling."""
        try:
            url = f"{self.server}{path}"
            if method == "GET":
                r = requests.get(url, timeout=timeout)
            else:
                r = requests.post(url, json=data or {}, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            else:
                self.log(f"API {method} {path} returned {r.status_code}", "WARN")
                return None
        except requests.exceptions.Timeout:
            self.log(f"API timeout: {path}", "WARN")
            return None
        except requests.exceptions.ConnectionError:
            self.log(f"API connection failed: {path}", "ERROR")
            return None
        except Exception as e:
            self.log(f"API error: {path} — {e}", "ERROR")
            return None

    def send_log(self, job_id, lines):
        """Send log lines to server for real-time display."""
        if isinstance(lines, str):
            lines = [lines]
        self.api("POST", f"/api/jobs/{job_id}/log", {"lines": lines})

    def register(self):
        """Register this node with the server."""
        r = self.api("POST", "/api/workers/register", {
            "id": self.worker_id,
            "name": self.name,
            "host": self.host,
            "gpu": self.gpu,
        })
        if r and r.get("ok"):
            self.log(f"Registered with server as {self.name} ({self.worker_id})")
            # Run crash recovery to handle leftover state from previous session
            self.recover_from_crash()
            return True
        self.log("Failed to register with server", "ERROR")
        return False

    def heartbeat(self):
        """Send heartbeat to server."""
        self.api("POST", "/api/workers/heartbeat", {
            "id": self.worker_id,
            "cpu": 0,  # TODO: get actual CPU usage
            "ram": 0,
            "gpu_usage": 0,
            "vram": 0,
        })

    def check_cancel(self, job_id):
        """Check if a job has been cancelled by the user."""
        r = self.api("GET", f"/api/jobs/{job_id}/check-cancel")
        if r and r.get("cancel"):
            self.log(f"Job #{job_id} cancelled by user — killing process", "WARN")
            self.job_cancel[job_id] = True
            proc = self.job_procs.get(job_id)
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                    self.log(f"Killed active subprocess (PID {proc.pid})")
                except:
                    pass
            return True
        return False

    def update_progress(self, job_id, progress, step, eta="", fps=0, compression=0):
        """Send progress update to server."""
        self.api("POST", f"/api/jobs/{job_id}/progress", {
            "progress": progress,
            "step": step,
            "eta": eta,
            "fps": str(int(fps)) if fps else "",
            "compression": compression,
        })

    def run_cmd(self, cmd, description, job_id, parse_progress=False, input_size_gb=0, timeout_minutes=180, total_duration_sec=0):
        """
        Run a command with logging, cancel polling, and optional ffmpeg progress parsing.
        Returns (success, output_text)
        """
        self.log(f"[CMD] {description}")
        self.send_log(job_id, f"[CMD] {description}")
        self.send_log(job_id, f"  $ {' '.join(cmd)}")

        self.job_cancel[job_id] = False
        start_time = time.time()

        try:
            self.job_procs[job_id] = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0
            )

            stderr_lines = []
            last_progress_time = time.time()
            last_eta = ""
            last_compression = 0

            # Cancel polling thread
            def cancel_poller():
                # Wait for process to actually start before polling
                for _ in range(30):
                    if self.job_procs.get(job_id):
                        break
                    time.sleep(0.5)
                while self.job_procs.get(job_id) and self.job_procs[job_id].poll() is None and not self.job_cancel.get(job_id, False):
                    time.sleep(2)
                    self.check_cancel(job_id)

            cancel_thread = threading.Thread(target=cancel_poller, daemon=True)
            cancel_thread.start()

            # Read stderr byte-by-byte to handle \r progress lines
            line_buf = b''
            while True:
                chunk = self.job_procs[job_id].stderr.read(1)
                if not chunk:
                    break
                if chunk in (b'\n', b'\r'):
                    if line_buf:
                        try:
                            line = line_buf.decode('utf-8', errors='replace').strip()
                        except:
                            line = ''
                        line_buf = b''
                        if not line:
                            continue
                        stderr_lines.append(line)

                        # Parse ffmpeg progress output
                        if parse_progress and 'frame=' in line:
                            fps_match = re.search(r'fps=\s*([\d.]+)', line)
                            speed_match = re.search(r'speed=\s*([\d.]+)x', line)
                            size_match = re.search(r'size=\s*([\d]+)\s*[kK][iI]?[bB]', line)
                            time_match = re.search(r'time=(\d+):(\d+):(\d+)', line)
                            bitrate_match = re.search(r'bitrate=\s*([\d.]+)\s*[kK]', line)

                            fps = float(fps_match.group(1)) if fps_match else 0
                            speed = float(speed_match.group(1)) if speed_match else 0
                            current_kb = int(size_match.group(1)) if size_match else 0
                            current_gb = current_kb / (1024**2)

                            # Progress from encoded time vs total duration
                            progress = 0
                            eta_str = ""
                            if time_match:
                                encoded_sec = int(time_match.group(1)) * 3600 + int(time_match.group(2)) * 60 + int(time_match.group(3))
                                if total_duration_sec > 0:
                                    progress = min(99, (encoded_sec / total_duration_sec) * 100)
                                if speed > 0 and total_duration_sec > 0:
                                    remaining_media_sec = total_duration_sec - encoded_sec
                                    remaining_wall_sec = remaining_media_sec / speed
                                    if remaining_wall_sec > 3600:
                                        eta_str = str(int(remaining_wall_sec/3600)) + 'h ' + str(int((remaining_wall_sec%3600)/60)) + 'm'
                                    elif remaining_wall_sec > 60:
                                        eta_str = str(int(remaining_wall_sec/60)) + 'm ' + str(int(remaining_wall_sec%60)) + 's'
                                    else:
                                        eta_str = str(int(remaining_wall_sec)) + 's'

                            # Carry forward last known good ETA when current line has no speed data
                            if eta_str:
                                last_eta = eta_str
                            else:
                                eta_str = last_eta

                            # Compression ratio — estimate final output size from current progress
                            c_ratio = 0
                            if input_size_gb > 0 and current_gb > 0 and progress > 5:
                                estimated_final_gb = current_gb / (progress / 100)
                                c_ratio = (1 - estimated_final_gb / input_size_gb) * 100

                            # Carry forward compression when current value is 0
                            if c_ratio != 0:
                                last_compression = c_ratio
                            else:
                                c_ratio = last_compression

                            step_info = description + ' — ' + str(int(fps)) + ' fps, ' + str(round(speed, 1)) + 'x'
                            self.update_progress(job_id, progress, step_info, eta_str, fps=fps, compression=c_ratio)
                            last_progress_time = time.time()

                        # Watchdog
                        if time.time() - last_progress_time > timeout_minutes * 60:
                            self.log(f"WATCHDOG: No progress for {timeout_minutes} minutes — killing", "ERROR")
                            self.send_log(job_id, "[ERROR] Watchdog timeout: no progress for " + str(timeout_minutes) + " minutes")
                            self.job_procs[job_id].kill()
                            return False, "Watchdog timeout"
                else:
                    line_buf += chunk

            self.job_procs[job_id].wait()
            rc = self.job_procs[job_id].returncode
            self.job_procs.pop(job_id, None)

            if self.job_cancel.get(job_id, False):
                return False, "Cancelled by user"

            if rc != 0:
                err = '\n'.join(stderr_lines[-10:])
                self.log(f"[FAILED] {description} (exit {rc})", "ERROR")
                self.send_log(job_id, f"[ERROR] {description} failed (exit {rc})")
                self.send_log(job_id, f"[ERROR] {err[:500]}")
                return False, err

            self.send_log(job_id, f"[OK] {description} completed")
            return True, '\n'.join(stderr_lines)

        except Exception as e:
            self.log(f"[EXCEPTION] {description}: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] Exception: {e}")
            self.job_procs.pop(job_id, None)
            return False, str(e)

    def run_cmd_with_watchdog(self, cmd, description, job_id, stale_timeout=300):
        """
        Run a command with I/O watchdog — monitors output file size.
        If no growth for stale_timeout seconds, kill the process.
        Used for mkvmerge which can hang on network I/O.
        """
        self.log(f"[CMD+WATCHDOG] {description}")
        self.send_log(job_id, f"[CMD+WATCHDOG] {description} (stale timeout: {stale_timeout}s)")

        self.job_cancel[job_id] = False

        try:
            self.job_procs[job_id] = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
            )

            # Monitor in background
            last_size = -1
            last_change_time = time.time()

            def watchdog():
                nonlocal last_size, last_change_time
                while self.job_procs.get(job_id) and self.job_procs[job_id].poll() is None:
                    # Check cancel
                    self.check_cancel(job_id)
                    if self.job_cancel.get(job_id, False):
                        return

                    # Check output file growth (look for output file in cmd)
                    out_file = cmd[cmd.index("-o") + 1] if "-o" in cmd else None
                    if out_file and os.path.exists(out_file):
                        sz = os.path.getsize(out_file)
                        if sz != last_size:
                            last_size = sz
                            last_change_time = time.time()
                        elif time.time() - last_change_time > stale_timeout:
                            self.log(f"WATCHDOG: Output stale for {stale_timeout}s — killing mkvmerge", "ERROR")
                            self.send_log(job_id, f"[ERROR] I/O stale for {stale_timeout}s — killing process")
                            try:
                                self.job_procs[job_id].kill()
                            except:
                                pass
                            return
                    time.sleep(3)

            wt = threading.Thread(target=watchdog, daemon=True)
            wt.start()

            stdout, stderr = self.job_procs[job_id].communicate()
            rc = self.job_procs[job_id].returncode
            self.job_procs.pop(job_id, None)

            if self.job_cancel.get(job_id, False):
                return False, "Cancelled by user"
            if rc != 0:
                self.log(f"[FAILED] {description} (exit {rc})", "ERROR")
                self.send_log(job_id, f"[ERROR] {description} failed (exit {rc})")
                if stderr:
                    self.send_log(job_id, f"[ERROR] {stderr[:500]}")
                return False, stderr or "Failed"

            self.send_log(job_id, f"[OK] {description} completed")
            return True, stdout or ""

        except Exception as e:
            self.log(f"[EXCEPTION] {description}: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] Exception: {e}")
            self.job_procs.pop(job_id, None)
            return False, str(e)

    def cleanup_workdir(self, work_dir):
        """Clean up temp files on failure."""
        if work_dir and os.path.isdir(work_dir):
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
                self.log(f"Cleaned up work dir: {work_dir}")
            except Exception as e:
                self.log(f"Cleanup failed: {e}", "WARN")

    def transcode_dovi(self, job, settings):
        """Full Dolby Vision preserving transcode pipeline."""
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]
        cq = int(settings.get("cq", "18"))
        preset = settings.get("preset", "slow")

        basename = os.path.splitext(filename)[0]
        work_dir = os.path.join(self.temp_base, f"job_{job_id}")
        os.makedirs(work_dir, exist_ok=True)

        raw_hevc = os.path.join(work_dir, f"{basename}.hevc")
        rpu_bin = os.path.join(work_dir, f"{basename}.rpu.bin")
        transcoded_hevc = os.path.join(work_dir, f"{basename}_transcoded.hevc")
        injected_hevc = os.path.join(work_dir, f"{basename}_injected.hevc")
        profile8_hevc = os.path.join(work_dir, f"{basename}_profile8.hevc")
        output_mkv = os.path.join(work_dir, f"{basename}_byte.mkv")

        total_steps = 6
        dovi_profile = job.get("dovi_profile")
        duration_sec = float(job.get("duration_min", 0)) * 60

        try:
            # Step 1: Extract HEVC bitstream
            step = f"[Step 1/{total_steps}] Extracting HEVC bitstream"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-i", filepath,
                "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", raw_hevc
            ], "Extract HEVC", job_id)
            if not ok:
                return False, f"HEVC extraction failed: {err[:200]}", None, work_dir

            # Verify extracted file
            if not os.path.exists(raw_hevc) or os.path.getsize(raw_hevc) < 1024:
                return False, "HEVC extraction produced empty/tiny file", None, work_dir

            raw_size_gb = os.path.getsize(raw_hevc) / (1024**3)
            self.send_log(job_id, f"  Extracted HEVC: {raw_size_gb:.2f} GB")
            self.save_checkpoint(job_id, "step1_hevc_extracted", work_dir)

            # Step 2: Extract RPU
            step = f"[Step 2/{total_steps}] Extracting DoVi RPU metadata"
            self.update_progress(job_id, 15, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd([
                self.dovi_tool, "extract-rpu", "-i", raw_hevc, "-o", rpu_bin
            ], "Extract RPU", job_id)
            if not ok:
                return False, f"RPU extraction failed: {err[:200]}", None, work_dir

            if not os.path.exists(rpu_bin) or os.path.getsize(rpu_bin) == 0:
                return False, "RPU extraction produced empty file", None, work_dir

            rpu_kb = os.path.getsize(rpu_bin) / 1024
            self.send_log(job_id, f"  RPU size: {rpu_kb:.1f} KB")
            self.save_checkpoint(job_id, "step2_rpu_extracted", work_dir)

            # Step 3: NVENC Transcode
            step = f"[Step 3/{total_steps}] NVENC Transcode CQ{cq} ({preset})"
            self.update_progress(job_id, 20, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-hwaccel", "cuda", "-f", "hevc", "-i", raw_hevc,
                "-c:v", "hevc_nvenc", "-preset", preset, "-cq", str(cq),
                "-f", "hevc", transcoded_hevc
            ], f"NVENC Transcode CQ{cq}", job_id, parse_progress=True, input_size_gb=raw_size_gb, total_duration_sec=duration_sec)
            if not ok:
                return False, f"NVENC transcode failed: {err[:200]}", None, work_dir

            if not os.path.exists(transcoded_hevc) or os.path.getsize(transcoded_hevc) < 1024:
                return False, "NVENC produced empty output", None, work_dir

            transcoded_gb = os.path.getsize(transcoded_hevc) / (1024**3)
            reduction = (1 - transcoded_gb / raw_size_gb) * 100 if raw_size_gb > 0 else 0
            self.send_log(job_id, f"  Video: {raw_size_gb:.2f} GB → {transcoded_gb:.2f} GB ({reduction:.1f}% reduction)")

            # Free disk: delete raw HEVC
            os.remove(raw_hevc)
            self.send_log(job_id, f"  Freed {raw_size_gb:.1f} GB (deleted raw HEVC)")
            self.save_checkpoint(job_id, "step3_nvenc_complete", work_dir)

            # Step 4: Inject RPU
            step = f"[Step 4/{total_steps}] Injecting DoVi RPU"
            self.update_progress(job_id, 70, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd([
                self.dovi_tool, "inject-rpu",
                "-i", transcoded_hevc, "--rpu-in", rpu_bin, "-o", injected_hevc
            ], "Inject RPU", job_id)
            if not ok:
                return False, f"RPU injection failed: {err[:200]}", None, work_dir

            os.remove(transcoded_hevc)
            os.remove(rpu_bin)

            # Step 5: P7→P8 conversion
            source_hevc = injected_hevc
            if dovi_profile == 7 or str(dovi_profile) == "7":
                step = f"[Step 5/{total_steps}] Converting DoVi P7 → P8"
                self.update_progress(job_id, 80, step)
                self.send_log(job_id, step)
                ok, err = self.run_cmd([
                    self.dovi_tool, "-m", "2", "convert", "--discard",
                    "-i", injected_hevc, "-o", profile8_hevc
                ], "P7→P8 Convert", job_id)
                if not ok:
                    return False, f"P7→P8 failed: {err[:200]}", None, work_dir
                source_hevc = profile8_hevc
                os.remove(injected_hevc)
            else:
                step = f"[Step 5/{total_steps}] Profile {dovi_profile} — no conversion needed"
                self.update_progress(job_id, 80, step)
                self.send_log(job_id, step)

            # Step 6: mkvmerge remux (with I/O watchdog)
            step = f"[Step 6/{total_steps}] mkvmerge Remux (all audio/subs/chapters)"
            self.update_progress(job_id, 85, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd_with_watchdog([
                self.mkvmerge, "-o", output_mkv,
                source_hevc, "--no-video", filepath
            ], "mkvmerge Remux", job_id, stale_timeout=300)
            if not ok:
                return False, f"mkvmerge failed: {err[:200]}", None, work_dir

            # Cleanup source HEVC
            if os.path.exists(source_hevc):
                os.remove(source_hevc)

            # Verify output
            if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                return False, "mkvmerge produced empty output", None, work_dir

            output_gb = os.path.getsize(output_mkv) / (1024**3)
            total_reduction = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0

            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id, f"  COMPLETE: {file_size_gb:.2f} GB → {output_gb:.2f} GB ({total_reduction:.1f}% reduction)")

            return True, None, {
                "output_path": output_mkv,
                "output_size_gb": output_gb,
                "reduction_pct": total_reduction,
                "saved_gb": file_size_gb - output_gb,
            }, work_dir

        except Exception as e:
            self.log(f"Pipeline exception: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] Pipeline exception: {e}")
            return False, str(e), None, work_dir

    def transcode_standard(self, job, settings):
        """Simple NVENC transcode for SDR/HDR10/HLG content."""
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]
        cq = int(settings.get("cq", "18"))
        preset = settings.get("preset", "slow")

        basename = os.path.splitext(filename)[0]
        work_dir = os.path.join(self.temp_base, f"job_{job_id}")
        os.makedirs(work_dir, exist_ok=True)
        output_mkv = os.path.join(work_dir, f"{basename}_byte.mkv")
        duration_sec = float(job.get("duration_min", 0)) * 60

        try:
            step = f"[Step 1/1] NVENC Transcode CQ{cq} ({preset}) — {job.get('hdr_type', 'SDR')}"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)

            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", filepath,
                "-map", "0", "-map", "-0:d",
                "-c:v", "hevc_nvenc", "-preset", preset, "-cq", str(cq),
                "-c:a", "copy", "-c:s", "copy",
                "-map_chapters", "0", "-map_metadata", "0",
                output_mkv
            ], f"NVENC Transcode CQ{cq}", job_id, parse_progress=True, input_size_gb=file_size_gb, total_duration_sec=duration_sec)

            if not ok:
                return False, f"NVENC failed: {err[:200]}", None, work_dir

            if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                return False, "NVENC produced empty output", None, work_dir

            output_gb = os.path.getsize(output_mkv) / (1024**3)
            reduction = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0

            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id, f"  COMPLETE: {file_size_gb:.2f} GB → {output_gb:.2f} GB ({reduction:.1f}% reduction)")

            return True, None, {
                "output_path": output_mkv,
                "output_size_gb": output_gb,
                "reduction_pct": reduction,
                "saved_gb": file_size_gb - output_gb,
            }, work_dir

        except Exception as e:
            self.send_log(job_id, f"[ERROR] Exception: {e}")
            return False, str(e), None, work_dir

    def replace_original(self, job, result, settings, work_dir):
        """Replace original file with transcoded output, with safety checks."""
        job_id = job["id"]
        filepath = job["file_path"]
        output_path = result["output_path"]

        if settings.get("replace_original", "true") != "true":
            # Keep both mode: copy output back to NAS next to original with _byte suffix
            try:
                original_dir = os.path.dirname(filepath)
                basename = os.path.splitext(os.path.basename(filepath))[0]
                dest_path = os.path.join(original_dir, f"{basename}_byte.mkv")
                self.send_log(job_id, f"  Keep both: copying output to NAS...")
                self.send_log(job_id, f"  → {dest_path}")
                shutil.copy2(output_path, dest_path)
                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                    self.send_log(job_id, f"  ✓ Output copied to NAS ({os.path.getsize(dest_path)/(1024**3):.2f} GB)")
                    self.cleanup_workdir(work_dir)
                    # Return NAS server path so server can find it for accept
                    return self.reverse_translate_path(dest_path)
                else:
                    self.send_log(job_id, f"  [WARN] Copy may have failed — keeping temp file")
                    return output_path
            except Exception as e:
                self.send_log(job_id, f"  [ERROR] Failed to copy to NAS: {e}")
                self.send_log(job_id, f"  Output remains at: {output_path}")
                return output_path

        try:
            # Safety check 1: Verify original still exists
            if not os.path.exists(filepath):
                self.send_log(job_id, f"  [WARN] Original already missing — keeping output in work dir")
                return output_path

            # Safety check 2: Output size must be reasonable (>5% of original)
            out_size = os.path.getsize(output_path)
            orig_size = os.path.getsize(filepath)
            if out_size < orig_size * 0.05:
                self.send_log(job_id, f"  [SAFETY] Output only {out_size/(1024**3):.2f}GB vs {orig_size/(1024**3):.2f}GB original — NOT replacing")
                return output_path

            # Safety check 3: Output must not be larger than original (unless forced)
            if out_size > orig_size * 1.1:
                self.send_log(job_id, f"  [WARN] Output is {(out_size/orig_size*100):.0f}% of original — larger than expected")

            # Safety check 4: Verify output has valid streams via ffprobe
            probe = self.probe_file(output_path, job_id)
            if probe:
                vid_count = len([s for s in probe.get("streams", []) if s.get("codec_type") == "video"])
                if vid_count == 0:
                    self.send_log(job_id, f"  [SAFETY] Output has no video stream — NOT replacing original")
                    return output_path

            # All checks passed — replace
            base, _ = os.path.splitext(filepath)
            final_path = base + ".mkv"

            self.send_log(job_id, f"  Deleting original: {os.path.basename(filepath)}")
            os.remove(filepath)

            self.send_log(job_id, f"  Moving output to: {os.path.basename(final_path)}")
            shutil.move(output_path, final_path)

            # Verify the move worked
            if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                self.send_log(job_id, f"  ✓ Successfully replaced: {os.path.basename(final_path)}")
                self.cleanup_workdir(work_dir)
                return final_path
            else:
                self.send_log(job_id, f"  [ERROR] Move may have failed — check {final_path}")
                return final_path

        except Exception as e:
            self.send_log(job_id, f"  [ERROR] Replace failed: {e}")
            return output_path

    def probe_file(self, filepath, job_id=None):
        """Probe a media file to get codec, streams, duration etc."""
        try:
            cmd = [self.ffprobe, "-v", "quiet", "-print_format", "json",
                   "-show_format", "-show_streams", filepath]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            if r.returncode != 0 or not r.stdout:
                if job_id:
                    self.send_log(job_id, f"[WARN] FFprobe failed on {os.path.basename(filepath)}")
                return None
            return json.loads(r.stdout)
        except Exception as e:
            if job_id:
                self.send_log(job_id, f"[WARN] FFprobe error: {e}")
            return None

    def verify_output(self, output_path, job_id, min_streams=1):
        """Verify output file is valid media with ffprobe."""
        self.send_log(job_id, f"  Verifying output with FFprobe...")
        probe = self.probe_file(output_path, job_id)
        if not probe:
            self.send_log(job_id, f"  [ERROR] Output failed FFprobe validation")
            return False
        streams = probe.get("streams", [])
        fmt = probe.get("format", {})
        duration = float(fmt.get("duration", 0))
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

        if len(video_streams) == 0:
            self.send_log(job_id, f"  [ERROR] Output has no video stream")
            return False
        if duration < 10:
            self.send_log(job_id, f"  [WARN] Output duration very short ({duration:.0f}s)")
        self.send_log(job_id, f"  Output valid: {len(video_streams)} video, {len(audio_streams)} audio, {duration/60:.1f}min")
        return True

    def save_checkpoint(self, job_id, step, work_dir):
        """Save checkpoint so interrupted jobs can report where they stopped."""
        try:
            cp_file = os.path.join(work_dir, "checkpoint.json")
            with open(cp_file, "w") as f:
                json.dump({"job_id": job_id, "step": step, "time": datetime.now().isoformat()}, f)
            self.save_session()  # Update session state too
        except:
            pass

    def remux_container(self, job, settings):
        """Remux only — change container format without re-encoding. Ultra fast, no GPU needed."""
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]
        target = settings.get("container", "mkv")

        basename = os.path.splitext(filename)[0]
        current_ext = os.path.splitext(filename)[1].lstrip(".").lower()

        # Skip if already in target container
        if current_ext == target:
            self.send_log(job_id, f"  Already in {target.upper()} container — skipping remux")
            return True, None, {
                "output_path": filepath,
                "output_size_gb": file_size_gb,
                "reduction_pct": 0,
            }, None

        work_dir = os.path.join(self.temp_base, f"job_{job_id}")
        os.makedirs(work_dir, exist_ok=True)
        output_file = os.path.join(work_dir, f"{basename}_byte.{target}")
        duration_sec = float(job.get("duration_min", 0)) * 60

        try:
            step = f"[Step 1/1] Remux {current_ext.upper()} → {target.upper()}"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)

            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-i", filepath,
                "-map", "0", "-c", "copy",
                "-map_chapters", "0", "-map_metadata", "0",
                output_file
            ], f"Remux to {target.upper()}", job_id, parse_progress=True, input_size_gb=file_size_gb, total_duration_sec=duration_sec)

            if not ok:
                return False, f"Remux failed: {err[:200]}", None, work_dir

            if not os.path.exists(output_file) or os.path.getsize(output_file) < 1024:
                return False, "Remux produced empty output", None, work_dir

            output_gb = os.path.getsize(output_file) / (1024**3)
            diff_pct = abs(1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0

            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id, f"  COMPLETE: Remuxed {current_ext.upper()} → {target.upper()} ({file_size_gb:.2f} GB → {output_gb:.2f} GB)")

            return True, None, {
                "output_path": output_file,
                "output_size_gb": output_gb,
                "reduction_pct": diff_pct,
            }, work_dir

        except Exception as e:
            self.send_log(job_id, f"[ERROR] Remux exception: {e}")
            return False, str(e), None, work_dir

    def process_job(self, job_data):
        """Process a single transcode job."""
        job = job_data["job"]
        settings = job.get("settings", {})
        job_id = job["id"]
        filename = job["file_name"]

        # Translate NAS path → local path (e.g. /media/... → Z:\...)
        server_path = job["file_path"]
        filepath = self.translate_path(server_path)
        job["file_path"] = filepath  # Update dict so child methods (transcode_dovi, etc.) get translated path

        has_dovi = job.get("has_dovi", 0)
        video_codec = job.get("video_codec", "")
        self.current_job_id = job_id
        self.job_cancel[job_id] = False

        self.log(f"Processing job #{job_id}: {filename}")
        self.send_log(job_id, f"Job #{job_id} started: {filename}")
        if filepath != server_path:
            self.send_log(job_id, f"  NAS path: {server_path} → {filepath}")
        else:
            self.send_log(job_id, f"  Path: {filepath}")
        self.send_log(job_id, f"  Size: {job.get('file_size_gb', 0):.2f} GB")
        self.send_log(job_id, f"  HDR: {job.get('hdr_type', 'SDR')}")
        self.send_log(job_id, f"  Codec: {video_codec}")
        self.send_log(job_id, f"  DoVi: {bool(has_dovi)} (Profile {job.get('dovi_profile', 'N/A')})")
        self.send_log(job_id, f"  CQ: {settings.get('cq', '18')}, Preset: {settings.get('preset', 'slow')}")

        # Verify file exists
        if not os.path.exists(filepath):
            self.send_log(job_id, f"[ERROR] File not found: {filepath}")
            self.api("POST", f"/api/jobs/{job_id}/error", {
                "worker_id": self.worker_id, "error": f"File not found: {filepath}"
            })
            self.job_cancel.pop(job_id, None)
            self.job_procs.pop(job_id, None)
            return

        # Pre-flight probe: verify codec before choosing pipeline
        self.send_log(job_id, f"  Pre-flight: Probing source file...")
        probe = self.probe_file(filepath, job_id)
        if probe:
            streams = probe.get("streams", [])
            vid = next((s for s in streams if s.get("codec_type") == "video"), None)
            if vid:
                actual_codec = vid.get("codec_name", "")
                self.send_log(job_id, f"  Detected codec: {actual_codec}")
                # DoVi pipeline requires HEVC — if codec is h264, use standard pipeline
                if has_dovi and actual_codec != "hevc":
                    self.send_log(job_id, f"  [WARN] DoVi flagged but codec is {actual_codec}, not HEVC — using standard pipeline")
                    has_dovi = 0
            else:
                self.send_log(job_id, f"  [WARN] No video stream found in probe — proceeding anyway")

        # Choose pipeline
        processing_mode = settings.get("processing_mode", "transcode")
        start_time = time.time()
        if processing_mode == "remux":
            self.send_log(job_id, f"  Mode: Remux only (container conversion)")
            success, error, result, work_dir = self.remux_container(job, settings)
        elif has_dovi:
            success, error, result, work_dir = self.transcode_dovi(job, settings)
        else:
            success, error, result, work_dir = self.transcode_standard(job, settings)

        elapsed = time.time() - start_time
        elapsed_str = f"{int(elapsed/60)}m {int(elapsed%60)}s"

        if success and result:
            # Feature 29: Verify output with ffprobe before accepting
            if not self.verify_output(result["output_path"], job_id):
                self.send_log(job_id, f"[ERROR] Output verification failed — marking as error")
                self.cleanup_workdir(work_dir)
                self.api("POST", f"/api/jobs/{job_id}/error", {
                    "worker_id": self.worker_id, "error": "Output file failed FFprobe verification"
                })
                self.current_job_id = None
                return

            # Handle file replacement
            final_path = self.replace_original(job, result, settings, work_dir)
            result["output_path"] = final_path

            self.send_log(job_id, f"  Total time: {elapsed_str}")
            self.api("POST", f"/api/jobs/{job_id}/complete", {
                "worker_id": self.worker_id,
                "output_path": result["output_path"],
                "output_size_gb": result["output_size_gb"],
                "reduction_pct": result["reduction_pct"],
                "saved_gb": result["saved_gb"],
            })
            self.log(f"Job #{job_id} complete: {result['reduction_pct']:.0f}% reduction in {elapsed_str}")
        else:
            # Cleanup temp files on failure
            self.cleanup_workdir(work_dir)

            error_msg = error or "Unknown error"
            self.send_log(job_id, f"[ERROR] Job #{job_id} failed: {error_msg}")
            self.send_log(job_id, f"  Cleaned up temp files")
            self.api("POST", f"/api/jobs/{job_id}/error", {
                "worker_id": self.worker_id, "error": error_msg[:500]
            })
            self.log(f"Job #{job_id} failed: {error_msg[:100]}")

        self.job_cancel.pop(job_id, None)
        self.job_procs.pop(job_id, None)


    # ─── Session State & Crash Recovery ───────────────────────────────────────
    def _session_path(self):
        return os.path.join(self.temp_base, "byte_session.json")

    def save_session(self):
        """Save current session state for crash recovery."""
        try:
            os.makedirs(self.temp_base, exist_ok=True)
            state = {
                "worker_id": self.worker_id,
                "active_jobs": list(self.job_procs.keys()),
                "timestamp": datetime.now().isoformat(),
            }
            with open(self._session_path(), "w") as f:
                json.dump(state, f)
        except Exception as e:
            self.log(f"Session save failed: {e}", "WARN")

    def clear_session(self):
        """Clear session state file."""
        try:
            sp = self._session_path()
            if os.path.exists(sp):
                os.remove(sp)
        except:
            pass

    def recover_from_crash(self):
        """Check for leftover work dirs from a crashed session and handle recovery."""
        if not os.path.isdir(self.temp_base):
            return

        # Find any job_XXX directories
        job_dirs = []
        try:
            for d in os.listdir(self.temp_base):
                if d.startswith("job_") and os.path.isdir(os.path.join(self.temp_base, d)):
                    try:
                        job_id = int(d.split("_")[1])
                        job_dirs.append((job_id, os.path.join(self.temp_base, d)))
                    except (ValueError, IndexError):
                        pass
        except:
            return

        if not job_dirs:
            return

        self.log(f"Found {len(job_dirs)} leftover work dir(s) from previous session")

        # Check each work dir — ask server before deleting
        for job_id, work_dir in job_dirs:
            checkpoint = None
            cp_file = os.path.join(work_dir, "checkpoint.json")
            if os.path.exists(cp_file):
                try:
                    with open(cp_file) as f:
                        checkpoint = json.load(f)
                except:
                    pass

            step = checkpoint.get("step", "unknown") if checkpoint else "unknown"
            self.log(f"  Job #{job_id}: checkpoint={step}")

            # Check with server if this job is complete/accepted — don't delete output files for those
            skip_cleanup = False
            try:
                r = self.api("GET", f"/api/queue/{job_id}")
                if r and r.get("status") in ("complete",):
                    # Job completed — output may still be needed (keep-both mode)
                    # Only clean up if output was already copied to NAS
                    output = r.get("output_path", "")
                    if output and os.path.exists(output):
                        self.log(f"  Job #{job_id}: completed, output exists on NAS — cleaning temp")
                    elif r.get("accepted"):
                        self.log(f"  Job #{job_id}: accepted — cleaning temp")
                    else:
                        self.log(f"  Job #{job_id}: completed but output may be in temp — PRESERVING")
                        skip_cleanup = True
            except:
                pass

            if not skip_cleanup:
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                    self.log(f"  Cleaned up job #{job_id} work dir")
                except Exception as e:
                    self.log(f"  Cleanup failed for job #{job_id}: {e}", "WARN")

        # Tell the server to reset any stuck jobs for this worker
        r = self.api("POST", f"/api/workers/{self.worker_id}/reset-jobs")
        if r and r.get("reset", 0) > 0:
            self.log(f"Server reset {r['reset']} stuck job(s) back to queue")
        elif r is None:
            self.log("Server reset-jobs endpoint not available — jobs will auto-recover", "WARN")

        self.clear_session()
        self.log("Crash recovery complete")

    def graceful_shutdown(self, finish_current=True):
        """Initiate graceful shutdown."""
        self.log("Shutdown requested...")
        self.running = False
        if finish_current and self.job_procs:
            active = list(self.job_procs.keys())
            self.log(f"Waiting for {len(active)} active job(s) to finish...")
            # Jobs will finish naturally since self.running=False stops new polls
            # The transcode_worker_loop checks self.running before polling
        else:
            # Kill active processes immediately
            for jid, proc in list(self.job_procs.items()):
                try:
                    if proc and proc.poll() is None:
                        proc.kill()
                        self.log(f"Killed job #{jid} subprocess")
                except:
                    pass
            # Reset killed jobs on server
            self.api("POST", f"/api/workers/{self.worker_id}/reset-jobs")
        self.clear_session()

    # ─── Health Check Workers (Tdarr-style, parallel with transcoding) ────────
    def run_health_check(self, job):
        """Run health check on a single file — file exists, read test, FFprobe validation."""
        job_id = job["id"]
        server_path = job["file_path"]
        filepath = self.translate_path(server_path)  # Convert NAS path → local Windows path
        filename = job["file_name"]
        status = "healthy"
        error = None

        self.send_log(job_id, f"[Health Check] Starting: {filename}")
        if filepath != server_path:
            self.send_log(job_id, f"[Health Check] Path: {server_path} → {filepath}")

        # Step 1: File exists
        self.send_log(job_id, f"[Health Check] Step 1: Checking file exists")
        if not os.path.exists(filepath):
            status = "missing"
            error = f"File not found: {filepath}"
            self.send_log(job_id, f"[Health Check] FAILED: {error}")
        elif os.path.getsize(filepath) == 0:
            status = "corrupt"
            error = "File is empty (0 bytes)"
            self.send_log(job_id, f"[Health Check] FAILED: {error}")
        else:
            file_size = os.path.getsize(filepath)
            self.send_log(job_id, f"[Health Check] Step 1: File exists ({file_size/(1024**3):.2f} GB)")

            # Step 2: Read test
            self.send_log(job_id, f"[Health Check] Step 2: Read test")
            try:
                with open(filepath, "rb") as f:
                    f.read(65536)  # Read 64KB
                self.send_log(job_id, f"[Health Check] Step 2: Read test passed")
            except Exception as e:
                status = "unreadable"
                error = f"Cannot read file: {str(e)}"
                self.send_log(job_id, f"[Health Check] FAILED: {error}")

            # Step 3: FFprobe validation (lightweight — only check format)
            if status == "healthy":
                # Skip FFprobe if probe data already exists from scan
                if job.get("probe_data"):
                    self.send_log(job_id, f"[Health Check] Step 3: Skipped FFprobe (already probed during scan)")
                else:
                    self.send_log(job_id, f"[Health Check] Step 3: FFprobe media validation")
                    try:
                        cmd = [self.ffprobe, "-v", "error", "-show_entries",
                               "format=duration,size,nb_streams", "-of", "json", filepath]
                        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                        if r.returncode != 0:
                            status = "corrupt"
                            error = f"FFprobe failed: {r.stderr[:200] if r.stderr else 'Unknown error'}"
                            self.send_log(job_id, f"[Health Check] FAILED: {error}")
                        else:
                            probe = json.loads(r.stdout)
                            streams = int(probe.get("format", {}).get("nb_streams", 0))
                            duration = float(probe.get("format", {}).get("duration", 0))
                            if streams == 0:
                                status = "corrupt"
                                error = "No media streams found"
                                self.send_log(job_id, f"[Health Check] FAILED: {error}")
                            elif duration < 1:
                                status = "corrupt"
                                error = "Duration is 0 — file may be corrupt"
                                self.send_log(job_id, f"[Health Check] FAILED: {error}")
                            else:
                                self.send_log(job_id, f"[Health Check] Step 3: Valid ({streams} streams, {duration/60:.1f} min)")
                    except subprocess.TimeoutExpired:
                        status = "timeout"
                        error = "FFprobe timed out after 120s"
                        self.send_log(job_id, f"[Health Check] FAILED: {error}")
                    except Exception as e:
                        # FFprobe not available — pass anyway with warning
                        self.send_log(job_id, f"[Health Check] Step 3: FFprobe unavailable, skipping ({e})")

        # Report result to server
        result_msg = "PASSED" if status == "healthy" else f"FAILED — {error}"
        self.send_log(job_id, f"[Health Check] {result_msg}: {filename}")
        self.api("POST", f"/api/jobs/{job_id}/health-result", {
            "status": status,
            "error": error,
        })

    def healthcheck_worker_loop(self, worker_num):
        """Health check worker thread — polls for pending health checks and processes them."""
        self.log(f"Health check worker #{worker_num} started")
        fails = 0
        while self.running:
            try:
                r = self.api("POST", "/api/jobs/next-healthcheck", {"worker_id": self.worker_id})
                if r and r.get("job"):
                    fails = 0
                    job = r["job"]
                    self.log(f"HC#{worker_num}: checking {job.get('file_name','?')}")
                    with self.hc_lock:
                        self.hc_active += 1
                    try:
                        self.run_health_check(job)
                    finally:
                        with self.hc_lock:
                            self.hc_active -= 1
                elif r:
                    reason = r.get("reason", "unknown")
                    if fails < 3:
                        self.log(f"HC#{worker_num}: no job — {reason}")
                    fails += 1
                    time.sleep(5)
                    continue
                else:
                    if fails < 3:
                        self.log(f"HC#{worker_num}: API returned None (server unreachable?)", "WARN")
                    fails += 1
                    time.sleep(10)
                    continue
            except Exception as e:
                self.log(f"HC#{worker_num} error: {e}", "ERROR")
                time.sleep(5)
            time.sleep(1)
        self.log(f"HC#{worker_num} stopped")

    def start_healthcheck_workers(self, count=3):
        """Start health check worker threads. Can be called independently by GUI wrappers."""
        self.log(f"Starting {count} health check workers...")
        for i in range(count):
            t = threading.Thread(target=self.healthcheck_worker_loop, args=(i,), daemon=True)
            t.start()

    def start_all_workers(self):
        """Start all workers (HC + transcode) using counts from server settings.
        Call this from the GUI after configuring the node."""
        counts = self.fetch_worker_counts()
        self.log(f"Worker counts: {counts['transcode_gpu']} transcode GPU, {counts['healthcheck_gpu']} health check GPU")
        self.start_healthcheck_workers(counts["healthcheck_gpu"])
        self.start_transcode_workers(counts["transcode_gpu"])

    def transcode_worker_loop(self, worker_num):
        """Transcode worker thread — polls for jobs and processes them."""
        self.log(f"Transcode worker #{worker_num} started")
        idle_count = 0
        while self.running:
            try:
                # Check if DoVi slots are full — ask server for non-DoVi if so
                with self.dovi_lock:
                    dovi_count = self.active_dovi
                max_dovi = getattr(self, 'max_dovi_concurrent', 2)
                prefer_non_dovi = dovi_count >= max_dovi

                r = self.api("POST", "/api/jobs/next", {
                    "worker_id": self.worker_id,
                    "prefer_non_dovi": prefer_non_dovi
                })
                if r and r.get("job"):
                    idle_count = 0
                    job = r["job"]
                    is_dovi = bool(job.get("has_dovi", 0))
                    if is_dovi:
                        with self.dovi_lock:
                            self.active_dovi += 1
                        self.log(f"TW#{worker_num}: starting DoVi {job.get('file_name','?')} (DoVi active: {self.active_dovi})")
                    else:
                        self.log(f"TW#{worker_num}: starting {job.get('file_name','?')}")
                    try:
                        self.process_job(r)
                    finally:
                        if is_dovi:
                            with self.dovi_lock:
                                self.active_dovi = max(0, self.active_dovi - 1)
                elif r:
                    reason = r.get("reason", "")
                    idle_count += 1
                    # Log first occurrence and then every 30 polls (~5 min at 10s interval)
                    if idle_count == 1 or idle_count % 30 == 0:
                        self.log(f"TW#{worker_num}: waiting — {reason}")
                else:
                    idle_count += 1
                    if idle_count == 1:
                        self.log(f"TW#{worker_num}: server unreachable", "WARN")
                time.sleep(self.poll_interval)
            except Exception as e:
                self.log(f"TW#{worker_num} error: {e}", "ERROR")
                time.sleep(10)
        self.log(f"Transcode worker #{worker_num} stopped")

    def start_transcode_workers(self, count=1):
        """Start transcode worker threads. Default 1 (optimal for single GPU)."""
        self.log(f"Starting {count} transcode worker(s)...")
        for i in range(count):
            t = threading.Thread(target=self.transcode_worker_loop, args=(i,), daemon=True)
            t.start()

    def run(self):
        """Main loop: register, heartbeat, start all workers."""
        if not self.register():
            self.log("Cannot start without server connection", "ERROR")
            return

        # Heartbeat thread
        def hb_loop():
            while self.running:
                self.heartbeat()
                time.sleep(15)

        hb_thread = threading.Thread(target=hb_loop, daemon=True)
        hb_thread.start()
        self.log(f"Heartbeat thread started (every 15s)")

        # Fetch worker counts from server settings
        counts = self.fetch_worker_counts()
        self.log(f"Worker counts from server: {counts['transcode_gpu']} transcode GPU, {counts['healthcheck_gpu']} health check GPU")

        # Start health check worker threads (Tdarr-style — run parallel with transcoding)
        self.start_healthcheck_workers(counts["healthcheck_gpu"])

        # Start transcode worker threads
        self.start_transcode_workers(counts["transcode_gpu"])

        # Keep main thread alive
        while self.running:
            time.sleep(1)

            time.sleep(self.poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Byte Transcode Node v2")
    parser.add_argument("--server", required=True, help="Server URL (e.g., http://192.168.3.13:5800)")
    parser.add_argument("--name", default="ByteNode", help="Node name")
    parser.add_argument("--gpu", default="GPU", help="GPU name")
    parser.add_argument("--poll", type=int, default=10, help="Poll interval in seconds")
    parser.add_argument("--path-from", default="/media", help="NAS path prefix to translate from")
    parser.add_argument("--path-to", default="", help="Local drive/path to translate to (e.g., Z:\\)")
    parser.add_argument("--temp-dir", default="", help="Local temp directory for job files")
    args = parser.parse_args()

    node = ByteNode(args.server, args.name, args.gpu, args.poll)

    # Configure path translation
    if args.path_to:
        node.native_mode = True
        node.nas_drive = args.path_to.rstrip("\\/")
        node.nas_prefix = args.path_from
        node.log(f"  Path mapping: {node.nas_prefix} → {node.nas_drive}")
    if args.temp_dir:
        node.temp_base = args.temp_dir
        node.log(f"  Temp dir: {node.temp_base}")

    def shutdown(sig, frame):
        print("\nShutting down gracefully — finishing active jobs...")
        node.graceful_shutdown(finish_current=True)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    node.run()


if __name__ == "__main__":
    main()
