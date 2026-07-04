#!/usr/bin/env python3
"""
Byte Transcode Node v2.9
========================
v2.5 — Server→Node path translation
v2.6 — Critical fixes:
        - _find_tool() uses shutil.which (cross-platform); previous version
          called Linux `which` command on Windows so it always silently
          fell through, returning the first arg verbatim even if the binary
          didn't exist — leading to subprocess WinError 2.
        - All handlers now use _work_dir(job_id, settings) to get the
          OS-appropriate temp directory, replacing 5 sites that hardcoded
          /temp/byte_work (a Linux path that doesn't exist on Windows).
        - Loud WARN log if a tool isn't found, so future config issues
          are visible at startup.
v2.7 — Multi-node / multi-worker correctness:
        - Per-job state registry: cancelled flag and subprocess handle are
          now tracked per job instead of on the shared instance. With
          transcode_gpu_count > 1, concurrent jobs previously clobbered
          each other's cancel flags and process handles.
        - Per-node setting overrides: local overrides (GUI fields / CLI
          flags) and server-side worker config (worker_config_<id>, edited
          from the web UI) are merged over global settings, so two nodes
          with different temp drives and mounts can share one server.
        - start_workers() (non-blocking) split out of start_all_workers()
          (blocking); the GUI previously deadlocked calling the blocking
          variant mid-setup, which was the "stuck on Connecting" bug.
        - Heartbeat sends real CPU/RAM (psutil) and GPU/VRAM (nvidia-smi)
          metrics instead of hardcoded zeros.
        - CLI accepts --nas-prefix/--nas-drive/--temp-dir/--workers, which
          run_node.bat was already passing (it crashed argparse before).
        - All text-mode subprocess captures use encoding="utf-8": Windows
          Python otherwise decodes ffprobe/mkvmerge JSON as cp1252, which
          raises on non-Latin track titles (e.g. Russian) and made those
          files silently unprocessable.
        - DV7→8 Only pipeline actually works now: it previously ran
          `dovi_tool convert` on an extracted RPU .bin, which dovi_tool
          rejects ("Invalid input file type" — convert operates on HEVC
          streams). Now converts the extracted HEVC directly, same as the
          transcode pipeline's P7→P8 step. 3 steps instead of 5.
v2.8 — Universal DV converter + Compatibility pipeline:
        - DV → P8 handles ALL source profiles, not just 7: P5 (IPTPQc2,
          the "purple and green" one) via dovi_tool mode 3, P7 via mode 2
          --discard, others best-effort. Same generalization applied to
          the transcode pipeline's Step 5.
        - New compat_fix handler (job_type 'compatfix'): fixes files the
          server's Compatibility scan flags as playback risks — rewrap to
          MKV (strategy remux) or NVENC re-encode to compat_target with
          optional deinterlace and subtitle-track filtering (strategy
          reencode). HDR/DV sources are guarded from SDR re-encoding.
v2.9 — TRUE DV Profile 5 → 8 conversion (_dv5_true_convert): metadata-only
        relabeling (dovi_tool mode 3) left IPTPQc2 pixels in the base
        layer, which still played purple/green everywhere in practice.
        P5 sources are now DV-decoded via libplacebo (Vulkan) and
        re-encoded to a genuine PQ BT.2020 HDR10 base layer, then the
        8.1 RPU is injected — correct on DV and non-DV devices alike.
        Old fast path available via dv5_mode=relabel.
"""

import sys, os, time, json, hashlib, shutil, subprocess, threading, signal, re, socket, platform, argparse, random
from datetime import datetime

# Auto-install requests if missing
try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q", "--break-system-packages"])
    import requests

# psutil is optional (heartbeat CPU/RAM metrics) — try to install once, but
# never block startup on it; heartbeat degrades to zeros without it.
try:
    import psutil  # noqa: F401
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil", "-q", "--break-system-packages"])
    except Exception:
        pass


class ByteNode:
    def __init__(self, server_url, name, gpu, poll_interval=10, local_overrides=None):
        self.server = server_url.rstrip('/')
        self.name = name
        self.gpu = gpu
        self.poll_interval = poll_interval
        self.worker_id = hashlib.md5(f"{name}-{socket.gethostname()}".encode()).hexdigest()[:12]
        self.host = socket.gethostname()
        self.running = True

        # v2.7 — per-job state registry (thread-safe multi-worker).
        # Each concurrently-processing job gets its own cancelled flag and
        # subprocess handle. Worker threads point at their job via
        # thread-local storage, so the legacy self.cancelled /
        # self.current_process accessors keep working unchanged inside
        # handlers, while helper threads (cancel pollers, watchdogs) look
        # state up by job_id.
        self._tls = threading.local()
        self._jobs_lock = threading.Lock()
        self._jobs = {}  # job_id -> {"cancelled": bool, "process": Popen|None, "file": str}

        # v2.7 — per-node setting overrides. Local (GUI/CLI) values beat
        # server-side worker config, which beats global settings.
        self.local_overrides = dict(local_overrides or {})
        self._worker_cfg = {}
        self._worker_cfg_at = 0.0

        # v2.2 — lazy-loaded Whisper model state
        self._whisper_model = None
        self._whisper_model_name = None
        self._whisper_device = None
        self._whisper_compute = None

        # v2.4 — GUI status flags. byte_node_gui.py polls these to
        # update its top-bar indicator from "Connecting" → "Connected".
        # Multiple aliases set so different GUI versions all see truth.
        self.connected = False
        self.registered = False
        self.is_running = False
        self.is_connected = False  # alias

        # Find tools — try a list of common names per tool. shutil.which
        # handles .exe extensions on Windows automatically.
        self.ffmpeg = self._find_tool("ffmpeg",
            ["jellyfin-ffmpeg7", "jellyfin-ffmpeg", "tdarr-ffmpeg", "ffmpeg.exe"])
        self.ffprobe = self._find_tool("ffprobe",
            ["jellyfin-ffprobe", "tdarr-ffprobe", "ffprobe.exe"])
        self.dovi_tool = self._find_tool("dovi_tool", ["dovi_tool.exe"])
        self.mkvmerge = self._find_tool("mkvmerge", ["mkvmerge.exe"])
        self.mkvpropedit = self._find_tool("mkvpropedit", ["mkvpropedit.exe"])
        self.mkvextract = self._find_tool("mkvextract", ["mkvextract.exe"])

        # Verify required tools were actually located. Anything unresolved
        # gets a loud WARN — silent fallback was the bug behind v2.5's
        # "WinError 2: cannot find the file specified" error.
        for tool_name, tool_path in [
            ("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe),
            ("dovi_tool", self.dovi_tool), ("mkvmerge", self.mkvmerge),
            ("mkvpropedit", self.mkvpropedit), ("mkvextract", self.mkvextract),
        ]:
            if not tool_path or not shutil.which(tool_path):
                self.log(f"  WARN: {tool_name} not found in PATH (fallback: {tool_path!r}). "
                         f"Install it or add its directory to PATH.", "WARN")

        self.log(f"Byte Node v2.9 initialized")
        self.log(f"  Worker ID: {self.worker_id}")
        self.log(f"  Server: {self.server}")
        self.log(f"  GPU: {self.gpu}")
        self.log(f"  ffmpeg: {self.ffmpeg}")
        self.log(f"  dovi_tool: {self.dovi_tool}")
        self.log(f"  mkvmerge: {self.mkvmerge}")
        self.log(f"  mkvpropedit: {self.mkvpropedit}")
        self.log(f"  mkvextract: {self.mkvextract}")

    def _find_tool(self, name, alternatives=None):
        """
        Cross-platform tool lookup. Uses shutil.which (which handles
        Windows .exe extensions automatically) instead of the Linux
        `which` command. Returns the resolved absolute path on success,
        or the first candidate name as a last-resort fallback.
        """
        candidates = [name] + (alternatives or [])
        for n in candidates:
            resolved = shutil.which(n)
            if resolved:
                return resolved
        # Nothing found — return the primary name as a fallback. The init
        # check below will WARN about it so the user knows.
        return name

    def _work_dir(self, job_id, settings):
        """
        Get the OS-appropriate temp work directory for a job. Honors
        node_temp_path setting; falls back to legacy temp_path; auto-detects
        if both are empty. Creates the directory if needed.

        v2.6: previously hardcoded /temp/byte_work which doesn't exist on
        Windows — every job's temp output silently failed to write.
        """
        base = (settings.get("node_temp_path") or "").strip()
        if not base:
            base = (settings.get("temp_path") or "").strip()
        if not base:
            # Last-resort auto-detect by OS
            if os.name == "nt":
                base = "C:\\Byte_Engine_temp"
            else:
                base = "/tmp/byte_work"
        work_dir = os.path.join(base, f"job_{job_id}")
        os.makedirs(work_dir, exist_ok=True)
        return work_dir

    # ── v2.7 per-job state plumbing ──────────────────────────────────────────
    def _job_entry(self, job_id=None):
        """Registry entry for a job (defaults to this thread's current job)."""
        if job_id is None:
            job_id = getattr(self._tls, "job_id", None)
        if job_id is None:
            return None
        with self._jobs_lock:
            return self._jobs.get(job_id)

    @property
    def current_job_id(self):
        return getattr(self._tls, "job_id", None)

    @current_job_id.setter
    def current_job_id(self, job_id):
        if job_id is None:
            old = getattr(self._tls, "job_id", None)
            if old is not None:
                with self._jobs_lock:
                    self._jobs.pop(old, None)
            self._tls.job_id = None
        else:
            self._tls.job_id = job_id
            with self._jobs_lock:
                self._jobs.setdefault(job_id, {"cancelled": False, "process": None, "file": ""})

    @property
    def cancelled(self):
        st = self._job_entry()
        return bool(st and st["cancelled"])

    @cancelled.setter
    def cancelled(self, value):
        st = self._job_entry()
        if st is not None:
            st["cancelled"] = bool(value)

    @property
    def current_process(self):
        st = self._job_entry()
        return st["process"] if st else None

    @current_process.setter
    def current_process(self, proc):
        st = self._job_entry()
        if st is not None:
            st["process"] = proc

    @property
    def active_jobs(self):
        """Snapshot of currently-processing jobs: {job_id: file_name}."""
        with self._jobs_lock:
            return {jid: st.get("file", "") for jid, st in self._jobs.items()}

    def _node_overrides(self, force=False):
        """
        v2.7 — effective per-node setting overrides, lowest priority first:
        server-side worker config (worker_config_<id>, editable from the web
        UI), then local overrides (GUI fields / CLI flags). Worker config is
        re-fetched at most once every 60s so UI edits apply to the next job
        without a node restart. Blank values fall through to global settings.
        """
        now = time.time()
        if force or now - self._worker_cfg_at > 60:
            cfg = self.api("GET", f"/api/workers/{self.worker_id}/config")
            if isinstance(cfg, dict):
                self._worker_cfg = cfg
            self._worker_cfg_at = now
        merged = {}
        merged.update(self._worker_cfg)
        merged.update(self.local_overrides)
        merged = {k: v for k, v in merged.items() if str(v).strip() != ""}
        # Normalize the path-prefix pair so translation joins cleanly
        # ("/media" → "/media/", "Z:" → "Z:\")
        rp = merged.get("node_path_remote_prefix")
        if rp and not rp.endswith("/"):
            merged["node_path_remote_prefix"] = rp + "/"
        lp = merged.get("node_path_local_prefix")
        if lp and not (lp.endswith("\\") or lp.endswith("/")):
            merged["node_path_local_prefix"] = lp + ("\\" if os.sep == "\\" else "/")
        return merged

    def log(self, msg, level="INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            # Console/pipe encoding can't render a character (e.g. cp1252
            # stdout with non-Latin file names) — degrade, never crash.
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(line.encode(enc, errors="replace").decode(enc), flush=True)

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
            return True
        self.log("Failed to register with server", "ERROR")
        return False

    def heartbeat(self):
        """Send heartbeat to server with live system metrics (v2.7)."""
        cpu = ram = gpu = vram = 0
        try:
            import psutil
            cpu = round(psutil.cpu_percent(interval=None))
            ram = round(psutil.virtual_memory().percent)
        except Exception:
            pass
        try:
            smi = shutil.which("nvidia-smi")
            if smi:
                out = subprocess.run(
                    [smi, "--query-gpu=utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5).stdout.strip().splitlines()
                if out:
                    parts = [p.strip() for p in out[0].split(",")]
                    if len(parts) >= 3:
                        gpu = round(float(parts[0]))
                        total = float(parts[2]) or 1.0
                        vram = round(float(parts[1]) / total * 100)
        except Exception:
            pass
        self.api("POST", "/api/workers/heartbeat", {
            "id": self.worker_id,
            "cpu": cpu,
            "ram": ram,
            "gpu_usage": gpu,
            "vram": vram,
        })

    def check_cancel(self, job_id):
        """Check if a job has been cancelled by the user."""
        r = self.api("GET", f"/api/jobs/{job_id}/check-cancel")
        if r and r.get("cancel"):
            self.log(f"Job #{job_id} cancelled by user — killing process", "WARN")
            st = self._job_entry(job_id)
            proc = None
            if st is not None:
                st["cancelled"] = True
                proc = st.get("process")
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

        # v2.7: don't reset the cancel flag here — a cancel that arrives
        # between pipeline steps must survive into the next run_cmd call.
        if self.cancelled:
            return False, "Cancelled by user"
        start_time = time.time()

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0
            )
            self.current_process = proc

            stderr_lines = []
            last_progress_time = time.time()

            # Cancel polling thread (v2.7: closure-captured proc + explicit
            # job_id — thread-local state isn't visible from helper threads)
            def cancel_poller():
                while proc.poll() is None:
                    time.sleep(5)
                    if self.check_cancel(job_id):
                        return

            cancel_thread = threading.Thread(target=cancel_poller, daemon=True)
            cancel_thread.start()

            # Read stderr byte-by-byte to handle \r progress lines
            line_buf = b''
            while True:
                chunk = proc.stderr.read(1)
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
                            size_match = re.search(r'size=\s*([\d]+)kB', line)
                            time_match = re.search(r'time=(\d+):(\d+):(\d+)', line)

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

                            # Compression ratio
                            c_ratio = 0
                            if input_size_gb > 0 and current_gb > 0:
                                c_ratio = (current_gb / input_size_gb) * 100

                            step_info = description + ' — ' + str(int(fps)) + ' fps, ' + str(round(speed, 1)) + 'x'
                            self.update_progress(job_id, progress, step_info, eta_str, fps=fps, compression=c_ratio)
                            last_progress_time = time.time()

                        # Watchdog
                        if time.time() - last_progress_time > timeout_minutes * 60:
                            self.log(f"WATCHDOG: No progress for {timeout_minutes} minutes — killing", "ERROR")
                            self.send_log(job_id, "[ERROR] Watchdog timeout: no progress for " + str(timeout_minutes) + " minutes")
                            proc.kill()
                            return False, "Watchdog timeout"
                else:
                    line_buf += chunk

            proc.wait()
            rc = proc.returncode
            self.current_process = None

            if self.cancelled:
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
            self.current_process = None
            return False, str(e)

    def run_cmd_with_watchdog(self, cmd, description, job_id, stale_timeout=300):
        """
        Run a command with I/O watchdog — monitors output file size.
        If no growth for stale_timeout seconds, kill the process.
        Used for mkvmerge which can hang on network I/O.
        """
        self.log(f"[CMD+WATCHDOG] {description}")
        self.send_log(job_id, f"[CMD+WATCHDOG] {description} (stale timeout: {stale_timeout}s)")

        # v2.7: don't reset the cancel flag — see run_cmd
        if self.cancelled:
            return False, "Cancelled by user"

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
            )
            self.current_process = proc

            # Monitor in background
            last_size = -1
            last_change_time = time.time()

            def watchdog():
                nonlocal last_size, last_change_time
                while proc.poll() is None:
                    # Check cancel (v2.7: closure proc + explicit job_id)
                    if self.check_cancel(job_id):
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
                                proc.kill()
                            except:
                                pass
                            return
                    time.sleep(10)

            wt = threading.Thread(target=watchdog, daemon=True)
            wt.start()

            stdout, stderr = proc.communicate()
            rc = proc.returncode
            self.current_process = None

            if self.cancelled:
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
            self.current_process = None
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
        work_dir = self._work_dir(job_id, settings)

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
                self.ffmpeg, "-y", "-f", "hevc", "-i", raw_hevc,
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

            # Step 5: DV profile → P8 conversion (v2.8: any profile, not just 7)
            source_hevc = injected_hevc
            try:
                src_profile = int(dovi_profile) if str(dovi_profile).strip() else 8
            except (TypeError, ValueError):
                src_profile = 8
            if src_profile != 8 and settings.get("dovi_convert_p8", "true") == "true":
                convert_args = self.DV_CONVERT_ARGS.get(src_profile, ["-m", "2", "convert", "--discard"])
                step = f"[Step 5/{total_steps}] Converting DoVi P{src_profile} → P8"
                self.update_progress(job_id, 80, step)
                self.send_log(job_id, step)
                ok, err = self.run_cmd(
                    [self.dovi_tool] + convert_args + ["-i", injected_hevc, "-o", profile8_hevc],
                    f"P{src_profile}→P8 Convert", job_id)
                if not ok:
                    return False, f"P{src_profile}→P8 failed: {err[:200]}", None, work_dir
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
        work_dir = self._work_dir(job_id, settings)
        output_mkv = os.path.join(work_dir, f"{basename}_byte.mkv")
        duration_sec = float(job.get("duration_min", 0)) * 60

        try:
            step = f"[Step 1/1] NVENC Transcode CQ{cq} ({preset}) — {job.get('hdr_type', 'SDR')}"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)

            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-i", filepath,
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
            self.send_log(job_id, f"  Keep both: original preserved, output at {output_path}")
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
            if r.returncode != 0:
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
        except:
            pass

    # ─── RemuxClean (job_type='remuxclean') ──────────────────────────────────
    KEEP_LANGS = {"eng", "en", "jpn", "ja", "und", ""}

    LANG_DISPLAY = {
        "eng": "English", "en": "English",
        "jpn": "Japanese", "ja": "Japanese",
        "und": "Undetermined",
    }

    DIRTY_KEYWORDS = [
        "blu-ray", "bluray", "blu ray", "uhd", "web-dl", "web dl", "webrip",
        "bdrip", "hdrip", "brrip", "remux", "1080p", "2160p", "720p", "4k",
        "x264", "x265", "hevc-", "ddp", "rarbg", "psa", "tigole", "cee",
        "subrip", "pgssub", "subtitleedit",
    ]

    def _is_dirty_name(self, name):
        if not name or not isinstance(name, str):
            return False
        if any(ord(c) > 127 for c in name):
            return True
        nl = name.lower()
        for kw in self.DIRTY_KEYWORDS:
            if kw in nl:
                return True
        if name.count("/") >= 2:
            return True
        if name.count(" - ") >= 4:
            return True
        return False

    def _codec_display(self, codec_name, codec_long_name="", profile=""):
        cn = (codec_name or "").lower()
        cln = (codec_long_name or "").lower()
        pf = (profile or "").lower()
        if cn == "aac":
            return "AAC"
        if cn == "ac3":
            return "Dolby Digital"
        if cn == "eac3":
            return "Dolby Digital Plus"
        if cn == "truehd":
            return "TrueHD Atmos" if "atmos" in cln or "atmos" in pf else "TrueHD"
        if cn == "dts":
            if "ma" in pf or "master audio" in cln:
                return "DTS-HD MA"
            if "x" in pf and (":" in pf or "x" == pf or "dts-x" in cln):
                return "DTS:X"
            if "hd" in pf or "high-resolution" in cln:
                return "DTS-HD"
            return "DTS"
        if cn == "flac":
            return "FLAC"
        if cn == "opus":
            return "Opus"
        if cn == "mp3":
            return "MP3"
        if cn == "vorbis":
            return "Vorbis"
        if cn == "pcm_s16le" or cn == "pcm_s24le":
            return "PCM"
        return (codec_name or "Audio").upper()

    def _channels_display(self, channels, channel_layout=""):
        cl = (channel_layout or "").lower()
        if cl:
            cl_clean = cl.split("(")[0].strip()
            mapping = {
                "stereo": "Stereo",
                "mono": "Mono",
                "5.1": "5.1",
                "7.1": "7.1",
                "5.0": "5.0",
                "7.0": "7.0",
                "2.1": "2.1",
                "downmix": "Stereo",
            }
            if cl_clean in mapping:
                return mapping[cl_clean]
        ch_map = {1: "Mono", 2: "Stereo", 3: "2.1", 4: "Quad", 6: "5.1", 7: "6.1", 8: "7.1"}
        try:
            return ch_map.get(int(channels), f"{int(channels)}ch")
        except (TypeError, ValueError):
            return "Stereo"

    def _is_commentary(self, title, disposition):
        if disposition and (disposition.get("commentary") or disposition.get("dub")):
            if disposition.get("commentary"):
                return True
        if title and "commentary" in title.lower():
            return True
        return False

    def _build_audio_name(self, lang, codec_name, codec_long_name, profile, channels, channel_layout, is_commentary):
        lang_disp = self.LANG_DISPLAY.get((lang or "und").lower(), (lang or "Undetermined").title())
        codec = self._codec_display(codec_name, codec_long_name, profile)
        chans = self._channels_display(channels, channel_layout)
        parts = [lang_disp, codec, chans]
        if is_commentary:
            parts.append("Commentary")
        return " - ".join(parts)

    def _build_subtitle_name(self, lang, disposition, is_commentary):
        lang_disp = self.LANG_DISPLAY.get((lang or "und").lower(), (lang or "Undetermined").title())
        parts = [lang_disp]
        d = disposition or {}
        if d.get("forced"):
            parts.append("Forced")
        if d.get("hearing_impaired"):
            parts.append("Hearing Impaired")
        elif d.get("sdh"):  # rare, but handle it
            parts.append("SDH")
        if is_commentary:
            parts.append("Commentary")
        return " - ".join(parts)

    def _analyze_mkv_for_clean(self, filepath, job_id):
        """Run mkvmerge -J for definitive track IDs + ffprobe for codec details. Returns merged structure."""
        # mkvmerge -J for track IDs and language
        try:
            r = subprocess.run([self.mkvmerge, "-J", filepath],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            if r.returncode != 0 and r.returncode != 1:
                # mkvmerge returns 1 for warnings but still produces JSON
                self.send_log(job_id, f"  [WARN] mkvmerge -J returned {r.returncode}")
                return None
            mkv_data = json.loads(r.stdout)
        except Exception as e:
            self.send_log(job_id, f"[ERROR] mkvmerge -J failed: {e}")
            return None

        # ffprobe for codec_long_name, profile, channel_layout, disposition
        probe = self.probe_file(filepath, job_id)
        if not probe:
            self.send_log(job_id, "[ERROR] ffprobe failed for cleanup analysis")
            return None

        # Build a stream-index → ffprobe-stream map (mkvmerge track ID == ffprobe stream index for MKV)
        ff_streams_by_index = {s.get("index", -1): s for s in probe.get("streams", [])}
        return {"mkv": mkv_data, "ffprobe": probe, "ff_by_index": ff_streams_by_index}

    def _plan_remux_clean(self, analysis, job_id):
        """
        Given analysis from _analyze_mkv_for_clean, decide which tracks to keep.
        Returns dict:
          - audio_keep_ids: list of mkvmerge track IDs (input file)
          - subtitle_keep_ids: list of mkvmerge track IDs (input file)
          - removed_audio, removed_subs (counts)
          - any_dirty_names (bool)
          - source_track_meta: list of track info dicts (lang, codec, etc.) used after remux
                              for naming
        """
        plan = {
            "audio_keep_ids": [],
            "subtitle_keep_ids": [],
            "video_track_ids": [],
            "removed_audio": 0,
            "removed_subs": 0,
            "kept_audio": 0,
            "kept_subs": 0,
            "any_dirty_names": False,
            "source_track_meta": [],  # ordered list of dicts for kept tracks (in output order)
        }
        mkv = analysis["mkv"]
        ff_by_index = analysis["ff_by_index"]

        # mkvmerge -J tracks have integer "id" — these are the IDs used in --audio-tracks etc.
        all_audio = []
        all_subs = []
        for t in mkv.get("tracks", []):
            tid = t.get("id")
            ttype = t.get("type")
            props = t.get("properties") or {}
            lang = (props.get("language") or "und").lower()
            track_name = props.get("track_name") or ""

            # Build a unified track metadata record (mkvmerge data + ffprobe details)
            ff = ff_by_index.get(tid, {})
            ff_tags = ff.get("tags") or {}
            ff_disp = ff.get("disposition") or {}

            # Prefer ffprobe-reported title if mkvmerge didn't have one
            title = track_name or ff_tags.get("title", "")

            meta = {
                "id": tid,
                "type": ttype,
                "lang": lang,
                "title": title,
                "codec_name": ff.get("codec_name") or t.get("codec", ""),
                "codec_long_name": ff.get("codec_long_name", ""),
                "profile": ff.get("profile", ""),
                "channels": ff.get("channels") or props.get("audio_channels") or 0,
                "channel_layout": ff.get("channel_layout", ""),
                "disposition": ff_disp,
                "props": props,
            }

            if self._is_dirty_name(title):
                plan["any_dirty_names"] = True

            if ttype == "video":
                plan["video_track_ids"].append(tid)
            elif ttype == "audio":
                all_audio.append(meta)
            elif ttype == "subtitles":
                all_subs.append(meta)

        # Filter audio
        for a in all_audio:
            if a["lang"] in self.KEEP_LANGS:
                plan["audio_keep_ids"].append(a["id"])

        # Safety: NEVER strip all audio. If filter removed everything, keep first audio track.
        if not plan["audio_keep_ids"] and all_audio:
            plan["audio_keep_ids"].append(all_audio[0]["id"])
            self.send_log(job_id,
                "  [SAFETY] No English/Japanese audio found — keeping first audio track")

        plan["removed_audio"] = len(all_audio) - len(plan["audio_keep_ids"])
        plan["kept_audio"] = len(plan["audio_keep_ids"])

        # Filter subs (no safety net — fine to remove all subs)
        for s in all_subs:
            if s["lang"] in self.KEEP_LANGS:
                plan["subtitle_keep_ids"].append(s["id"])
        plan["removed_subs"] = len(all_subs) - len(plan["subtitle_keep_ids"])
        plan["kept_subs"] = len(plan["subtitle_keep_ids"])

        # Build source_track_meta in OUTPUT order: video tracks first, then kept audio, then kept subs
        # (mkvmerge preserves this ordering by default)
        kept_audio_meta = [a for a in all_audio if a["id"] in plan["audio_keep_ids"]]
        kept_sub_meta = [s for s in all_subs if s["id"] in plan["subtitle_keep_ids"]]
        # Video metadata also needed (preserve the existing video track names — usually fine)
        video_meta = []
        for t in mkv.get("tracks", []):
            if t.get("type") == "video":
                tid = t.get("id")
                ff = ff_by_index.get(tid, {})
                video_meta.append({
                    "id": tid, "type": "video",
                    "title": (t.get("properties") or {}).get("track_name") or "",
                    "codec_name": ff.get("codec_name") or t.get("codec", ""),
                })
        plan["source_track_meta"] = video_meta + kept_audio_meta + kept_sub_meta
        return plan

    def _build_clean_names_for_output(self, output_path, source_meta, job_id):
        """
        After remux, run mkvmerge -J on output and pair tracks (in order) with source_meta
        to build clean names. Returns dict: {output_track_number_1based: clean_name}
        """
        try:
            r = subprocess.run([self.mkvmerge, "-J", output_path],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            if r.returncode not in (0, 1):
                self.send_log(job_id, f"  [WARN] mkvmerge -J on output returned {r.returncode}")
                return {}
            out_data = json.loads(r.stdout)
        except Exception as e:
            self.send_log(job_id, f"  [WARN] Could not analyze output for naming: {e}")
            return {}

        out_tracks = out_data.get("tracks", [])
        # mkvpropedit uses 1-indexed track numbers. We pair output tracks with source_meta
        # by ORDER (mkvmerge preserves order: video → kept audio → kept subs).
        result = {}
        if len(out_tracks) != len(source_meta):
            self.send_log(job_id,
                f"  [WARN] Output has {len(out_tracks)} tracks but expected {len(source_meta)} — "
                "naming might be off. Skipping name cleanup to be safe.")
            return {}

        for idx, (out_t, src) in enumerate(zip(out_tracks, source_meta)):
            tid_1based = idx + 1
            ttype = out_t.get("type")
            if ttype == "audio":
                is_comm = self._is_commentary(src.get("title", ""), src.get("disposition", {}))
                name = self._build_audio_name(
                    src.get("lang", "und"),
                    src.get("codec_name", ""),
                    src.get("codec_long_name", ""),
                    src.get("profile", ""),
                    src.get("channels", 0),
                    src.get("channel_layout", ""),
                    is_comm,
                )
                result[tid_1based] = name
            elif ttype == "subtitles":
                is_comm = self._is_commentary(src.get("title", ""), src.get("disposition", {}))
                name = self._build_subtitle_name(
                    src.get("lang", "und"),
                    src.get("disposition", {}),
                    is_comm,
                )
                result[tid_1based] = name
            else:
                # Video tracks: only rename if dirty; otherwise leave alone
                if src.get("title") and self._is_dirty_name(src["title"]):
                    result[tid_1based] = ""  # empty string clears the name
        return result

    def remux_clean(self, job, settings):
        """
        RemuxClean pipeline:
          1. Analyze source (mkvmerge -J + ffprobe)
          2. Plan track filter (keep eng/jpn/und audio+subs)
          3. mkvmerge -o output --audio-tracks X,Y --subtitle-tracks A,B input.mkv
          4. mkvpropedit on output to apply clean names
        Returns (success, error, result_dict, work_dir) — same shape as transcode_*.
        """
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]

        basename = os.path.splitext(filename)[0]
        work_dir = self._work_dir(job_id, settings)
        # Output is always MKV (cleanup is MKV-only)
        output_mkv = os.path.join(work_dir, f"{basename}_clean.mkv")
        total_steps = 4

        try:
            # Step 1: Analyze
            step = f"[Step 1/{total_steps}] Analyzing tracks (mkvmerge -J + ffprobe)"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)
            analysis = self._analyze_mkv_for_clean(filepath, job_id)
            if not analysis:
                return False, "Failed to analyze MKV tracks", None, work_dir

            # Step 2: Plan
            step = f"[Step 2/{total_steps}] Planning track filter"
            self.update_progress(job_id, 15, step)
            self.send_log(job_id, step)
            plan = self._plan_remux_clean(analysis, job_id)

            self.send_log(job_id,
                f"  Audio: keep {plan['kept_audio']}, remove {plan['removed_audio']}")
            self.send_log(job_id,
                f"  Subtitles: keep {plan['kept_subs']}, remove {plan['removed_subs']}")
            self.send_log(job_id,
                f"  Dirty track names found: {plan['any_dirty_names']}")

            nothing_to_remove = (plan["removed_audio"] == 0 and plan["removed_subs"] == 0)
            if nothing_to_remove and not plan["any_dirty_names"]:
                self.send_log(job_id, "  File already clean — no changes needed")
                # Mark complete with no file change
                return True, None, {
                    "output_path": filepath,
                    "output_size_gb": file_size_gb,
                    "reduction_pct": 0,
                    "saved_gb": 0,
                    "no_op": True,
                }, work_dir

            # Step 3: mkvmerge remux to filter tracks
            if not nothing_to_remove:
                step = f"[Step 3/{total_steps}] mkvmerge filtering tracks"
                self.update_progress(job_id, 25, step)
                self.send_log(job_id, step)

                cmd = [self.mkvmerge, "-o", output_mkv]
                if plan["audio_keep_ids"]:
                    cmd += ["--audio-tracks", ",".join(str(i) for i in plan["audio_keep_ids"])]
                else:
                    cmd += ["--no-audio"]
                if plan["subtitle_keep_ids"]:
                    cmd += ["--subtitle-tracks", ",".join(str(i) for i in plan["subtitle_keep_ids"])]
                else:
                    cmd += ["--no-subtitles"]
                cmd.append(filepath)

                ok, err = self.run_cmd_with_watchdog(
                    cmd, "mkvmerge Track Filter", job_id, stale_timeout=300)
                if not ok:
                    return False, f"mkvmerge failed: {err[:200]}", None, work_dir
                if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                    return False, "mkvmerge produced empty output", None, work_dir
                target_for_naming = output_mkv
                # source_meta is already in output-order from _plan_remux_clean
                source_meta = plan["source_track_meta"]
            else:
                # No tracks to remove — copy file to work_dir to apply names safely
                # (We don't want to mkvpropedit the original directly; replace_original
                # handles the swap.)
                step = f"[Step 3/{total_steps}] Copying file to apply name cleanup"
                self.update_progress(job_id, 25, step)
                self.send_log(job_id, step)
                shutil.copy2(filepath, output_mkv)
                target_for_naming = output_mkv
                # Build full source_meta (no filtering applied)
                # Walk all tracks in mkvmerge order: video, audio, subs
                source_meta = []
                # Need to collect ALL tracks since none were filtered
                # Re-derive from analysis
                mkv = analysis["mkv"]
                ff_by_index = analysis["ff_by_index"]
                for t_type in ("video", "audio", "subtitles"):
                    for t in mkv.get("tracks", []):
                        if t.get("type") != t_type:
                            continue
                        tid = t.get("id")
                        props = t.get("properties") or {}
                        ff = ff_by_index.get(tid, {})
                        source_meta.append({
                            "id": tid, "type": t_type,
                            "lang": (props.get("language") or "und").lower(),
                            "title": props.get("track_name") or (ff.get("tags") or {}).get("title", ""),
                            "codec_name": ff.get("codec_name") or t.get("codec", ""),
                            "codec_long_name": ff.get("codec_long_name", ""),
                            "profile": ff.get("profile", ""),
                            "channels": ff.get("channels") or props.get("audio_channels") or 0,
                            "channel_layout": ff.get("channel_layout", ""),
                            "disposition": ff.get("disposition") or {},
                        })

            # Step 4: mkvpropedit clean names
            step = f"[Step 4/{total_steps}] mkvpropedit clean track names"
            self.update_progress(job_id, 80, step)
            self.send_log(job_id, step)
            clean_names = self._build_clean_names_for_output(target_for_naming, source_meta, job_id)
            self.send_log(job_id, f"  Will rename {len(clean_names)} track(s)")

            if clean_names:
                ed_cmd = [self.mkvpropedit, target_for_naming]
                for tid_1based, name in clean_names.items():
                    ed_cmd += ["--edit", f"track:@{tid_1based}",
                               "--set", f"name={name}"]
                ok, err = self.run_cmd(ed_cmd, "mkvpropedit names", job_id)
                if not ok:
                    self.send_log(job_id,
                        f"  [WARN] Name cleanup failed but tracks were filtered. "
                        f"File still usable. {err[:200]}")
                    # Don't fail the whole job over a name cleanup error
                else:
                    for tid, name in list(clean_names.items())[:6]:
                        self.send_log(job_id, f"    track {tid}: '{name}'")
                    if len(clean_names) > 6:
                        self.send_log(job_id, f"    ... and {len(clean_names) - 6} more")

            # Verify output
            output_gb = os.path.getsize(output_mkv) / (1024**3)
            size_change_pct = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0

            self.update_progress(job_id, 100, "Complete")
            removed_total = plan["removed_audio"] + plan["removed_subs"]
            self.send_log(job_id,
                f"  COMPLETE: {file_size_gb:.2f} GB → {output_gb:.2f} GB "
                f"({removed_total} tracks removed, {len(clean_names)} names cleaned)")

            return True, None, {
                "output_path": output_mkv,
                "output_size_gb": output_gb,
                "reduction_pct": size_change_pct,
                "saved_gb": file_size_gb - output_gb,
            }, work_dir

        except Exception as e:
            self.log(f"RemuxClean exception: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] RemuxClean exception: {e}")
            return False, str(e), None, work_dir

    # ─── DV7→8 Only (job_type='dv78only') ────────────────────────────────────
    # v2.8 — dovi_tool conversion arguments per source DV profile.
    # Mode 2: convert to profile 8.1 (dual-layer P7 also discards the EL).
    # Mode 3: convert profile 5 (IPTPQc2, the "purple and green" one on
    #         unsupported devices) to profile 8.1.
    DV_CONVERT_ARGS = {
        7: ["-m", "2", "convert", "--discard"],
        5: ["-m", "3", "convert"],
        4: ["-m", "2", "convert", "--discard"],  # best effort for dual-layer P4
    }

    def _dv5_true_convert(self, job, settings):
        """
        v2.9 — REAL Profile 5 → Profile 8 conversion.

        The metadata-only path (dovi_tool -m 3 relabel) is not enough for
        P5: the base layer pixels stay IPTPQc2, so anything that plays the
        base layer without full DV composition — and in practice even DV
        TVs fed the relabeled stream — still shows purple/green.

        This path re-encodes the base layer to genuine PQ BT.2020 HDR10:
          1. Extract raw HEVC (copy) and pull the RPU converted to 8.1
             (dovi_tool -m 3 extract-rpu)
          2. Decode with Dolby Vision processing via libplacebo (Vulkan)
             and NVENC-encode a true PQ/BT.2020 10-bit base layer
          3. Inject the 8.1 RPU into the new base layer
          4. mkvmerge remux with original audio/subs/chapters
        Result: DV devices compose Profile 8 correctly, and non-DV devices
        get a real HDR10 picture. Verified frame-accurate against the
        purple/green dumb-player rendering of the source.
        """
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]
        cq = settings.get("cq", "16")
        preset = settings.get("preset", "p5")
        if preset not in ("p1", "p2", "p3", "p4", "p5", "p6", "p7"):
            preset = "p5"
        try:
            duration_sec = float(job.get("duration_min") or 0) * 60
        except (TypeError, ValueError):
            duration_sec = 0

        basename = os.path.splitext(filename)[0]
        work_dir = self._work_dir(job_id, settings)
        raw_hevc = os.path.join(work_dir, f"{basename}.hevc")
        rpu_bin = os.path.join(work_dir, f"{basename}.rpu8.bin")
        pq_hevc = os.path.join(work_dir, f"{basename}_pq.hevc")
        p8_hevc = os.path.join(work_dir, f"{basename}_p8.hevc")
        output_mkv = os.path.join(work_dir, f"{basename}_p8.mkv")
        total_steps = 4

        try:
            # Step 1: extract raw HEVC and the 8.1-converted RPU
            step = f"[Step 1/{total_steps}] Extracting RPU (P5 → P8.1)"
            self.update_progress(job_id, 3, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-i", filepath,
                "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", raw_hevc
            ], "Extract HEVC", job_id)
            if not ok:
                return False, f"HEVC extraction failed: {err[:200]}", None, work_dir
            ok, err = self.run_cmd([
                self.dovi_tool, "-m", "3", "extract-rpu", "-i", raw_hevc, "-o", rpu_bin
            ], "Extract RPU (mode 3)", job_id)
            if not ok:
                return False, f"RPU extraction failed: {err[:200]}", None, work_dir
            if not os.path.exists(rpu_bin) or os.path.getsize(rpu_bin) == 0:
                return False, "RPU extraction produced empty file", None, work_dir
            try:
                os.remove(raw_hevc)
            except Exception:
                pass

            # Step 2: DV-aware decode → genuine PQ BT.2020 base layer
            step = f"[Step 2/{total_steps}] Re-encoding base layer IPTPQc2 → PQ HDR10 (NVENC CQ{cq})"
            self.update_progress(job_id, 10, step)
            self.send_log(job_id, step)
            vf = ("libplacebo=apply_dolbyvision=true:tonemapping=clip:"
                  "colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084:"
                  "format=p010,hwdownload,format=p010le")
            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-init_hw_device", "vulkan",
                "-i", filepath, "-map", "0:v:0", "-vf", vf,
                "-c:v", "hevc_nvenc", "-preset", preset, "-cq", str(cq),
                "-profile:v", "main10",
                "-color_primaries", "bt2020", "-color_trc", "smpte2084",
                "-colorspace", "bt2020nc",
                "-f", "hevc", pq_hevc
            ], "DV5→HDR10 Re-encode", job_id, parse_progress=True,
               input_size_gb=file_size_gb, total_duration_sec=duration_sec)
            if not ok:
                return False, f"Base layer re-encode failed: {err[:200]}", None, work_dir
            if not os.path.exists(pq_hevc) or os.path.getsize(pq_hevc) < 1024:
                return False, "Base layer re-encode produced empty output", None, work_dir

            # Step 3: inject the P8.1 RPU into the new PQ base layer
            step = f"[Step 3/{total_steps}] Injecting P8 RPU"
            self.update_progress(job_id, 80, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd([
                self.dovi_tool, "inject-rpu",
                "-i", pq_hevc, "--rpu-in", rpu_bin, "-o", p8_hevc
            ], "Inject P8 RPU", job_id)
            if not ok:
                return False, f"RPU injection failed: {err[:200]}", None, work_dir
            try:
                os.remove(pq_hevc)
                os.remove(rpu_bin)
            except Exception:
                pass

            # Step 4: remux with original audio/subs/chapters
            step = f"[Step 4/{total_steps}] mkvmerge Remux (HEVC + audio/subs/chapters)"
            self.update_progress(job_id, 88, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd_with_watchdog([
                self.mkvmerge, "-o", output_mkv,
                p8_hevc, "--no-video", filepath
            ], "mkvmerge Remux", job_id, stale_timeout=300)
            if not ok:
                return False, f"mkvmerge failed: {err[:200]}", None, work_dir
            try:
                os.remove(p8_hevc)
            except Exception:
                pass
            if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                return False, "mkvmerge produced empty output", None, work_dir

            output_gb = os.path.getsize(output_mkv) / (1024**3)
            reduction = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0
            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id,
                f"  COMPLETE: {file_size_gb:.2f} GB → {output_gb:.2f} GB "
                f"(DV P5 → P8 with true HDR10 base layer)")
            return True, None, {
                "output_path": output_mkv,
                "output_size_gb": output_gb,
                "reduction_pct": reduction,
                "saved_gb": file_size_gb - output_gb,
            }, work_dir

        except Exception as e:
            self.log(f"DV5 true convert exception: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] DV5 true convert exception: {e}")
            return False, str(e), None, work_dir

    def dv78only_convert(self, job, settings):
        """
        Convert any Dolby Vision profile → Profile 8.
        - P7 (and other dual-layer profiles): metadata-only conversion, no
          re-encode — the base layer is already genuine HDR10.
          (v2.7 fixed `convert` being fed an RPU .bin; v2.8 generalized.)
        - P5: routed to _dv5_true_convert (v2.9) — re-encodes the IPTPQc2
          base layer to real PQ HDR10, because metadata-only relabeling
          leaves purple/green output on every playback path in practice.
          Set dv5_mode=relabel to force the old fast path for experiments.
        """
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]

        try:
            src_profile = int(job.get("dovi_profile") or 7)
        except (TypeError, ValueError):
            src_profile = 7

        if src_profile == 5 and settings.get("dv5_mode", "reencode") != "relabel":
            return self._dv5_true_convert(job, settings)

        convert_args = self.DV_CONVERT_ARGS.get(src_profile, ["-m", "2", "convert", "--discard"])

        basename = os.path.splitext(filename)[0]
        work_dir = self._work_dir(job_id, settings)

        raw_hevc = os.path.join(work_dir, f"{basename}.hevc")
        p8_hevc = os.path.join(work_dir, f"{basename}_p8.hevc")
        output_mkv = os.path.join(work_dir, f"{basename}_p8.mkv")
        total_steps = 3

        try:
            # Step 1: Extract HEVC bitstream (copy)
            step = f"[Step 1/{total_steps}] Extracting HEVC bitstream (copy)"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd([
                self.ffmpeg, "-y", "-i", filepath,
                "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", raw_hevc
            ], "Extract HEVC", job_id)
            if not ok:
                return False, f"HEVC extraction failed: {err[:200]}", None, work_dir
            if not os.path.exists(raw_hevc) or os.path.getsize(raw_hevc) < 1024:
                return False, "HEVC extraction produced empty/tiny file", None, work_dir
            raw_size_gb = os.path.getsize(raw_hevc) / (1024**3)
            self.send_log(job_id, f"  Extracted HEVC: {raw_size_gb:.2f} GB")

            # Step 2: Convert source profile → P8.1 on the HEVC stream
            step = f"[Step 2/{total_steps}] Converting DoVi P{src_profile} → P8 (dovi_tool {' '.join(convert_args[:2])})"
            self.update_progress(job_id, 35, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd(
                [self.dovi_tool] + convert_args + ["-i", raw_hevc, "-o", p8_hevc],
                f"P{src_profile}→P8 Convert", job_id)
            if not ok:
                return False, f"P{src_profile}→P8 convert failed: {err[:200]}", None, work_dir
            if not os.path.exists(p8_hevc) or os.path.getsize(p8_hevc) < 1024:
                return False, f"P{src_profile}→P8 conversion produced empty file", None, work_dir

            # Free disk: original extracted stream no longer needed
            try:
                os.remove(raw_hevc)
            except Exception:
                pass

            # Step 3: mkvmerge remux — new HEVC + original audio/subs/chapters
            step = f"[Step 3/{total_steps}] mkvmerge Remux (HEVC + audio/subs/chapters)"
            self.update_progress(job_id, 70, step)
            self.send_log(job_id, step)
            ok, err = self.run_cmd_with_watchdog([
                self.mkvmerge, "-o", output_mkv,
                p8_hevc, "--no-video", filepath
            ], "mkvmerge Remux", job_id, stale_timeout=300)
            if not ok:
                return False, f"mkvmerge failed: {err[:200]}", None, work_dir
            if os.path.exists(p8_hevc):
                try:
                    os.remove(p8_hevc)
                except Exception:
                    pass

            if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                return False, "mkvmerge produced empty output", None, work_dir

            output_gb = os.path.getsize(output_mkv) / (1024**3)
            size_change_pct = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0

            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id,
                f"  COMPLETE: {file_size_gb:.2f} GB → {output_gb:.2f} GB "
                f"(DV Profile {src_profile} → 8, no re-encode)")

            return True, None, {
                "output_path": output_mkv,
                "output_size_gb": output_gb,
                "reduction_pct": size_change_pct,
                "saved_gb": file_size_gb - output_gb,
            }, work_dir

        except Exception as e:
            self.log(f"DV78Only exception: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] DV78Only exception: {e}")
            return False, str(e), None, work_dir

    # ─── Compatibility Fix (job_type='compatfix') ─────────────────────────────
    def compat_fix(self, job, settings):
        """
        v2.8 — fix playback-compatibility issues flagged by the server's
        Compatibility scan. Strategy comes from probe_data["_compat"]:
          remux    — rewrap into MKV with mkvmerge (no re-encode). When the
                     source had an absurd number of subtitle tracks, only
                     eng/jpn/und subs are kept.
          reencode — NVENC re-encode video to compat_target (h264 = plays
                     on everything / hevc = smaller), 8-bit SDR pixel
                     format, optional deinterlace. Audio and subs copied.
        HDR/DV sources are never re-encoded here (that would destroy HDR
        metadata) — they fall back to remux, and the Transcode pipeline is
        the right tool for them.
        """
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]

        try:
            pd = json.loads(job.get("probe_data") or "{}")
        except Exception:
            pd = {}
        compat = pd.get("_compat") or {}
        strategy = compat.get("strategy") or "reencode"
        deinterlace = bool(compat.get("deinterlace"))
        filter_subs = bool(compat.get("filter_subs"))
        reasons = compat.get("reasons") or []

        # Never re-encode HDR/DV content through the SDR compat path
        hdr_type = (job.get("hdr_type") or "SDR").upper()
        if strategy == "reencode" and hdr_type not in ("SDR", ""):
            self.send_log(job_id, f"  [GUARD] {hdr_type} source — downgrading strategy to remux "
                                  f"(use the Transcode pipeline for HDR/DV re-encodes)")
            strategy = "remux"

        basename = os.path.splitext(filename)[0]
        work_dir = self._work_dir(job_id, settings)
        output_mkv = os.path.join(work_dir, f"{basename}_compat.mkv")

        if reasons:
            self.send_log(job_id, "  Flagged: " + "; ".join(reasons))
        self.send_log(job_id, f"  Strategy: {strategy}")

        try:
            if strategy == "remux":
                step = "[Step 1/1] mkvmerge rewrap to MKV" + (" (eng/jpn subs only)" if filter_subs else "")
                self.update_progress(job_id, 20, step)
                self.send_log(job_id, step)
                cmd = [self.mkvmerge, "-o", output_mkv]
                if filter_subs:
                    cmd += ["--subtitle-tracks", "eng,jpn,und"]
                cmd += [filepath]
                ok, err = self.run_cmd_with_watchdog(cmd, "mkvmerge Rewrap", job_id, stale_timeout=300)
                if not ok:
                    return False, f"mkvmerge rewrap failed: {err[:200]}", None, work_dir
            else:
                target = (settings.get("compat_target") or "h264").lower()
                encoder = "hevc_nvenc" if target == "hevc" else "h264_nvenc"
                cq = settings.get("cq", "18")
                try:
                    duration_sec = float(job.get("duration_min") or 0) * 60
                except (TypeError, ValueError):
                    duration_sec = 0

                base_cmd = [self.ffmpeg, "-y", "-i", filepath, "-map", "0:v:0", "-map", "0:a?"]
                sub_maps = (["-map", "0:s:m:language:eng?", "-map", "0:s:m:language:jpn?"]
                            if filter_subs else ["-map", "0:s?"])
                vf = ["-vf", "yadif"] if deinterlace else []
                enc_args = ["-c:v", encoder, "-preset", settings.get("preset", "p5"),
                            "-cq", str(cq), "-pix_fmt", "yuv420p",
                            "-c:a", "copy"]

                step = f"[Step 1/1] NVENC re-encode to {target.upper()} 8-bit" + (" + deinterlace" if deinterlace else "")
                self.update_progress(job_id, 5, step)
                self.send_log(job_id, step)

                # Subtitle handling fallback chain: copy → convert to srt → drop
                attempts = [
                    (sub_maps + ["-c:s", "copy"], "subs copied"),
                    (sub_maps + ["-c:s", "srt"], "subs converted to SRT"),
                    (["-sn"], "subs dropped (incompatible formats)"),
                ]
                ok, err = False, ""
                for sub_args, sub_note in attempts:
                    cmd = base_cmd + sub_args + vf + enc_args + ["-f", "matroska", output_mkv]
                    ok, err = self.run_cmd(cmd, f"Compat Re-encode ({sub_note})", job_id,
                                           parse_progress=True, input_size_gb=file_size_gb,
                                           total_duration_sec=duration_sec)
                    if ok:
                        self.send_log(job_id, f"  Subtitles: {sub_note}")
                        break
                    if self.cancelled:
                        return False, "Cancelled by user", None, work_dir
                if not ok:
                    return False, f"Compat re-encode failed: {err[:200]}", None, work_dir

            if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                return False, "Compat conversion produced empty output", None, work_dir

            output_gb = os.path.getsize(output_mkv) / (1024**3)
            reduction = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0

            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id, f"  COMPLETE: {file_size_gb:.2f} GB → {output_gb:.2f} GB ({strategy})")
            return True, None, {
                "output_path": output_mkv,
                "output_size_gb": output_gb,
                "reduction_pct": reduction,
                "saved_gb": file_size_gb - output_gb,
            }, work_dir

        except Exception as e:
            self.log(f"CompatFix exception: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] CompatFix exception: {e}")
            return False, str(e), None, work_dir

    # ─── Subtitle Generation (job_type='subgen') ─────────────────────────────
    def _ensure_whisper(self, model_name, device, compute_type, job_id):
        """Lazy-load faster-whisper model. Auto-installs if missing."""
        # Already loaded with the same params?
        if (self._whisper_model is not None
                and self._whisper_model_name == model_name
                and self._whisper_device == device
                and self._whisper_compute == compute_type):
            return self._whisper_model

        # Try to import; install if needed
        try:
            from faster_whisper import WhisperModel  # noqa
        except ImportError:
            self.send_log(job_id, "  faster-whisper not installed — installing now (one-time)")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install",
                                       "faster-whisper", "-q", "--break-system-packages"])
            except Exception as e:
                self.send_log(job_id, f"  [ERROR] faster-whisper install failed: {e}")
                return None
            try:
                from faster_whisper import WhisperModel  # noqa
            except ImportError as e:
                self.send_log(job_id, f"  [ERROR] faster-whisper still not importable: {e}")
                return None

        from faster_whisper import WhisperModel

        # Resolve auto values
        resolved_device = device
        resolved_compute = compute_type
        if resolved_device == "auto":
            # Try CUDA first, fall back to CPU
            try:
                test = WhisperModel("tiny", device="cuda", compute_type="int8")
                del test
                resolved_device = "cuda"
            except Exception:
                resolved_device = "cpu"
                self.send_log(job_id, "  CUDA not available for Whisper — falling back to CPU")
        if resolved_compute == "auto":
            resolved_compute = "float16" if resolved_device == "cuda" else "int8"

        self.send_log(job_id,
            f"  Loading Whisper model '{model_name}' on {resolved_device} ({resolved_compute})...")
        try:
            model = WhisperModel(model_name, device=resolved_device, compute_type=resolved_compute)
        except Exception as e:
            # Retry on CPU if CUDA load failed
            if resolved_device == "cuda":
                self.send_log(job_id, f"  CUDA load failed ({e}); retrying on CPU")
                resolved_device = "cpu"
                resolved_compute = "int8"
                try:
                    model = WhisperModel(model_name, device=resolved_device, compute_type=resolved_compute)
                except Exception as e2:
                    self.send_log(job_id, f"  [ERROR] Whisper CPU load also failed: {e2}")
                    return None
            else:
                self.send_log(job_id, f"  [ERROR] Whisper load failed: {e}")
                return None

        self._whisper_model = model
        self._whisper_model_name = model_name
        self._whisper_device = resolved_device
        self._whisper_compute = resolved_compute
        self.send_log(job_id, f"  Whisper ready: {model_name} on {resolved_device}")
        return model

    def _srt_timestamp(self, sec):
        """Format seconds as SRT timestamp 'HH:MM:SS,mmm'."""
        if sec < 0:
            sec = 0
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _parse_srt_ts(self, ts):
        """Parse 'HH:MM:SS,mmm' → seconds (float)."""
        try:
            ts = ts.replace(",", ".").strip()
            parts = ts.split(":")
            h = int(parts[0]); m = int(parts[1]); s = float(parts[2])
            return h * 3600 + m * 60 + s
        except Exception:
            return 0.0

    def _parse_srt(self, srt_text):
        """Parse SRT text into list of {idx, start, end, text} dicts."""
        entries = []
        # Normalize line endings
        srt_text = srt_text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n\n+", srt_text.strip())
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) < 2:
                continue
            # First line should be a number, second should be timestamps
            try:
                idx = int(lines[0].strip())
                ts_line = lines[1]
            except ValueError:
                # Some SRTs have no number, just use position
                idx = len(entries) + 1
                ts_line = lines[0]
                lines = [str(idx)] + lines

            m = re.match(r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)", ts_line)
            if not m:
                continue
            start = self._parse_srt_ts(m.group(1))
            end = self._parse_srt_ts(m.group(2))
            text = "\n".join(lines[2:]).strip()
            if text:
                entries.append({"idx": idx, "start": start, "end": end, "text": text})
        return entries

    def _serialize_srt(self, entries):
        """Build SRT text from entries (re-numbered 1-indexed)."""
        out = []
        for i, e in enumerate(entries, start=1):
            out.append(str(i))
            out.append(f"{self._srt_timestamp(e['start'])} --> {self._srt_timestamp(e['end'])}")
            out.append(e["text"])
            out.append("")
        return "\n".join(out)

    def _claude_translate_chunk(self, api_key, model, chunk, prev_context, title, job_id):
        """
        Send a chunk of SRT entries to Claude for Japanese translation.
        Returns translated chunk (list of dicts with 'text' replaced).
        """
        # Build the request payload
        chunk_for_api = [{"i": i, "t": e["text"]} for i, e in enumerate(chunk)]
        prev_lines = "\n".join(f"- {t}" for t in prev_context[-10:]) if prev_context else "(start of file)"

        system_prompt = (
            "You are translating English movie/TV subtitles to natural, native-sounding Japanese. "
            "Translate as if you were writing subtitles for a Japanese audience watching this content. "
            "Preserve tone, register, and character voice. Use natural Japanese sentence structure, "
            "not literal word-for-word translation. Keep idioms idiomatic. "
            "Do not add explanatory notes. Output ONLY a JSON array."
        )
        user_prompt = (
            f"Title: {title}\n\n"
            f"Previous context (already translated):\n{prev_lines}\n\n"
            f"Translate the following English subtitle entries to Japanese. "
            f"Each has an index 'i' and English text 't'. "
            f"Return JSON array of objects with 'i' (same index) and 't' (Japanese translation). "
            f"Output ONLY the JSON array, no markdown, no explanation.\n\n"
            f"Entries:\n{json.dumps(chunk_for_api, ensure_ascii=False)}"
        )

        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 8192,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=120,
            )
        except Exception as e:
            self.send_log(job_id, f"  [ERROR] Claude API request failed: {e}")
            return None

        if r.status_code != 200:
            self.send_log(job_id, f"  [ERROR] Claude API {r.status_code}: {r.text[:300]}")
            return None

        try:
            data = r.json()
            content_blocks = data.get("content", [])
            text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
            response_text = "".join(text_parts).strip()
        except Exception as e:
            self.send_log(job_id, f"  [ERROR] Could not parse Claude response: {e}")
            return None

        # Strip code fences if Claude added them despite instructions
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)

        try:
            translated = json.loads(response_text)
        except json.JSONDecodeError as e:
            self.send_log(job_id, f"  [ERROR] Claude returned non-JSON: {e}")
            self.send_log(job_id, f"  Response: {response_text[:300]}")
            return None

        # Build dict by index for fast lookup
        by_idx = {item.get("i"): item.get("t", "") for item in translated if isinstance(item, dict)}

        # Apply translations preserving timestamps
        result = []
        for i, src in enumerate(chunk):
            translated_text = by_idx.get(i, src["text"])  # fallback to original if missing
            new_entry = dict(src)
            new_entry["text"] = translated_text
            result.append(new_entry)
        return result

    def _translate_srt_to_japanese(self, entries, api_key, model, chunk_size, title, job_id):
        """Translate full SRT entries list to Japanese in chunks. Returns translated entries."""
        if not entries:
            return entries
        if not api_key:
            self.send_log(job_id, "  [ERROR] Claude API key not set in server settings")
            return None

        translated_all = []
        prev_context = []  # list of recently-translated texts
        total_chunks = (len(entries) + chunk_size - 1) // chunk_size
        self.send_log(job_id,
            f"  Translating {len(entries)} subtitle entries in {total_chunks} chunks of {chunk_size}")

        for ci in range(total_chunks):
            if self.cancelled:
                self.send_log(job_id, "  Cancel requested — stopping translation")
                return None
            chunk = entries[ci * chunk_size : (ci + 1) * chunk_size]
            self.send_log(job_id, f"  Chunk {ci+1}/{total_chunks} ({len(chunk)} entries)...")
            translated = self._claude_translate_chunk(
                api_key, model, chunk, prev_context, title, job_id)
            if translated is None:
                self.send_log(job_id, f"  [ERROR] Translation failed at chunk {ci+1}")
                return None
            translated_all.extend(translated)
            # Add the most recent translations to context for next chunk
            for e in translated[-10:]:
                prev_context.append(e["text"])

            # Update progress within the translation phase (60% → 90%)
            phase_pct = 60 + ((ci + 1) / total_chunks) * 30
            self.update_progress(job_id, phase_pct,
                f"Translating subtitles ({ci+1}/{total_chunks})")
        return translated_all

    def _whisper_transcribe(self, audio_path, model_name, device, compute_type,
                            language, task, job_id):
        """
        Run faster-whisper on audio. Returns list of SRT entries.
        language: 'ja', 'en', or None (auto-detect)
        task: 'transcribe' or 'translate' (translate = to English only)
        """
        model = self._ensure_whisper(model_name, device, compute_type, job_id)
        if model is None:
            return None

        self.send_log(job_id,
            f"  Whisper transcribing: lang={language or 'auto'}, task={task}")

        try:
            segments, info = model.transcribe(
                audio_path,
                language=language,
                task=task,
                beam_size=5,
                vad_filter=True,  # voice-activity detection — better timestamps
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            self.send_log(job_id,
                f"  Detected language: {info.language} (prob {info.language_probability:.2f}), "
                f"duration {info.duration:.0f}s")
        except Exception as e:
            self.send_log(job_id, f"  [ERROR] Whisper transcribe failed: {e}")
            return None

        entries = []
        last_log_time = time.time()
        total_duration = info.duration if info.duration else 1
        for seg in segments:
            if self.cancelled:
                self.send_log(job_id, "  Cancel requested — stopping transcription")
                return None
            text = (seg.text or "").strip()
            if not text:
                continue
            entries.append({
                "idx": len(entries) + 1,
                "start": seg.start,
                "end": seg.end,
                "text": text,
            })
            # Periodic progress updates within the Whisper phase
            if time.time() - last_log_time > 5:
                pct_audio = (seg.end / total_duration) if total_duration else 0
                # Whisper occupies 20%-60% of the bar
                phase_pct = 20 + pct_audio * 40
                self.update_progress(job_id, phase_pct,
                    f"Whisper transcribing ({seg.end:.0f}s / {total_duration:.0f}s)")
                last_log_time = time.time()

        self.send_log(job_id, f"  Transcribed {len(entries)} subtitle entries")
        return entries

    def _extract_audio_for_whisper(self, filepath, audio_stream_idx, output_wav, job_id):
        """Extract a specific audio stream as 16kHz mono WAV for Whisper."""
        cmd = [
            self.ffmpeg, "-y", "-i", filepath,
            "-map", f"0:{audio_stream_idx}",
            "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            output_wav,
        ]
        ok, err = self.run_cmd(cmd, "Extract audio for Whisper", job_id)
        if not ok:
            return False
        if not os.path.exists(output_wav) or os.path.getsize(output_wav) < 1024:
            self.send_log(job_id, "[ERROR] Extracted audio is empty")
            return False
        return True

    def _extract_text_subtitle(self, filepath, sub_stream_idx, output_srt, job_id):
        """Extract a text subtitle track as SRT via ffmpeg."""
        cmd = [
            self.ffmpeg, "-y", "-i", filepath,
            "-map", f"0:{sub_stream_idx}",
            "-c:s", "srt",
            output_srt,
        ]
        ok, err = self.run_cmd(cmd, "Extract subtitle to SRT", job_id)
        if not ok:
            return False
        if not os.path.exists(output_srt) or os.path.getsize(output_srt) == 0:
            self.send_log(job_id, "[ERROR] Extracted SRT is empty")
            return False
        return True

    def _select_subgen_source(self, probe_data, job_id):
        """
        Decide which source to use for subtitle generation. Returns dict:
          {'mode': 'translate_text' | 'whisper_jpn' | 'whisper_en_translate' | 'whisper_translate_to_en',
           'sub_stream_idx': int (for translate_text mode),
           'audio_stream_idx': int (for whisper modes),
           'description': str}
        """
        streams = (probe_data or {}).get("streams", [])
        text_codecs = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

        # Path 1: English text subtitles exist → use those, translate via Claude
        for s in streams:
            if s.get("codec_type") != "subtitle":
                continue
            codec = (s.get("codec_name") or "").lower()
            if codec not in text_codecs:
                continue
            tags = s.get("tags") or {}
            lang = (tags.get("language") or "").lower()
            if lang in ("eng", "en"):
                return {
                    "mode": "translate_text",
                    "sub_stream_idx": s.get("index"),
                    "description": f"English text subs (stream {s.get('index')}) → Claude translate",
                }

        # Path 2: Japanese audio exists → Whisper transcribe in Japanese
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        for a in audio_streams:
            tags = a.get("tags") or {}
            lang = (tags.get("language") or "").lower()
            if lang in ("jpn", "ja"):
                return {
                    "mode": "whisper_jpn",
                    "audio_stream_idx": a.get("index"),
                    "description": f"Japanese audio (stream {a.get('index')}) → Whisper",
                }

        # Path 3: English audio → Whisper transcribe English, then Claude translates
        for a in audio_streams:
            tags = a.get("tags") or {}
            lang = (tags.get("language") or "").lower()
            if lang in ("eng", "en"):
                return {
                    "mode": "whisper_en_translate",
                    "audio_stream_idx": a.get("index"),
                    "description": f"English audio (stream {a.get('index')}) → Whisper + Claude translate",
                }

        # Path 4: First audio track, language unknown → Whisper auto-detect, then translate to Japanese
        if audio_streams:
            return {
                "mode": "whisper_en_translate",
                "audio_stream_idx": audio_streams[0].get("index"),
                "description": f"Audio stream {audio_streams[0].get('index')} (auto-detect) → Whisper + Claude translate",
            }

        return None

    def subgen(self, job, settings):
        """
        Subtitle generation pipeline. Produces a Japanese SRT and muxes it into the file.
        """
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]

        api_key = settings.get("claude_api_key", "")
        claude_model = settings.get("claude_model", "claude-sonnet-4-6")
        whisper_model = settings.get("whisper_model", "large-v3")
        whisper_device = settings.get("whisper_device", "auto")
        whisper_compute = settings.get("whisper_compute", "auto")
        try:
            chunk_size = int(settings.get("subgen_translate_chunk", "40"))
        except (TypeError, ValueError):
            chunk_size = 40

        basename = os.path.splitext(filename)[0]
        work_dir = self._work_dir(job_id, settings)

        try:
            # Step 1: Probe to determine pipeline path
            step = "[Step 1/5] Probing source for subtitle/audio streams"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)
            probe = self.probe_file(filepath, job_id)
            if not probe:
                return False, "Failed to probe source file", None, work_dir

            choice = self._select_subgen_source(probe, job_id)
            if not choice:
                return False, "No usable audio or subtitle source for SubGen", None, work_dir
            self.send_log(job_id, f"  Source: {choice['description']}")

            # Step 2: Get English/source-language entries
            step = "[Step 2/5] Acquiring source subtitle entries"
            self.update_progress(job_id, 15, step)
            self.send_log(job_id, step)
            source_entries = None
            translate_to_japanese = False  # if False, output IS Japanese (whisper_jpn)

            if choice["mode"] == "translate_text":
                # Extract English SRT, parse it
                src_srt_path = os.path.join(work_dir, f"{basename}.eng.srt")
                if not self._extract_text_subtitle(
                        filepath, choice["sub_stream_idx"], src_srt_path, job_id):
                    return False, "Failed to extract English subtitle stream", None, work_dir
                with open(src_srt_path, "r", encoding="utf-8", errors="replace") as f:
                    srt_text = f.read()
                source_entries = self._parse_srt(srt_text)
                if not source_entries:
                    return False, "Source SRT was empty after parsing", None, work_dir
                translate_to_japanese = True

            elif choice["mode"] == "whisper_jpn":
                # Whisper transcribe Japanese audio directly — output IS Japanese
                wav_path = os.path.join(work_dir, f"{basename}.audio.wav")
                self.update_progress(job_id, 18, "Extracting audio for Whisper")
                if not self._extract_audio_for_whisper(
                        filepath, choice["audio_stream_idx"], wav_path, job_id):
                    return False, "Failed to extract audio", None, work_dir
                source_entries = self._whisper_transcribe(
                    wav_path, whisper_model, whisper_device, whisper_compute,
                    language="ja", task="transcribe", job_id=job_id)
                if source_entries is None:
                    return False, "Whisper transcription failed", None, work_dir
                translate_to_japanese = False

            elif choice["mode"] == "whisper_en_translate":
                # Whisper transcribe (auto-detect or English), then Claude translates to Japanese
                wav_path = os.path.join(work_dir, f"{basename}.audio.wav")
                self.update_progress(job_id, 18, "Extracting audio for Whisper")
                if not self._extract_audio_for_whisper(
                        filepath, choice["audio_stream_idx"], wav_path, job_id):
                    return False, "Failed to extract audio", None, work_dir
                # Auto-detect language for the source — keep transcribe (not Whisper-translate)
                # to preserve fidelity, then Claude does the JP translation
                source_entries = self._whisper_transcribe(
                    wav_path, whisper_model, whisper_device, whisper_compute,
                    language=None, task="transcribe", job_id=job_id)
                if source_entries is None:
                    return False, "Whisper transcription failed", None, work_dir
                translate_to_japanese = True
            else:
                return False, f"Unknown SubGen mode: {choice['mode']}", None, work_dir

            self.send_log(job_id, f"  Acquired {len(source_entries)} subtitle entries")

            # Step 3: Translate to Japanese (if needed)
            if translate_to_japanese:
                step = "[Step 3/5] Translating to Japanese via Claude API"
                self.update_progress(job_id, 60, step)
                self.send_log(job_id, step)
                if not api_key:
                    return False, "Claude API key not set — open Settings → AI to add it", None, work_dir
                jpn_entries = self._translate_srt_to_japanese(
                    source_entries, api_key, claude_model, chunk_size, basename, job_id)
                if jpn_entries is None:
                    return False, "Claude translation failed (see log)", None, work_dir
            else:
                # Whisper-Japanese: source IS already Japanese
                jpn_entries = source_entries
                self.send_log(job_id, "[Step 3/5] Source is Japanese — skipping translation")
                self.update_progress(job_id, 90, "Japanese transcription ready")

            # Step 4: Write Japanese SRT
            step = "[Step 4/5] Writing Japanese SRT"
            self.update_progress(job_id, 92, step)
            self.send_log(job_id, step)
            jpn_srt_path = os.path.join(work_dir, f"{basename}.jpn.srt")
            with open(jpn_srt_path, "w", encoding="utf-8") as f:
                f.write(self._serialize_srt(jpn_entries))
            self.send_log(job_id, f"  Wrote {os.path.getsize(jpn_srt_path)} bytes to {jpn_srt_path}")

            # Step 5: mkvmerge to add Japanese SRT track to the file
            step = "[Step 5/5] mkvmerge: adding Japanese SRT track"
            self.update_progress(job_id, 95, step)
            self.send_log(job_id, step)
            output_mkv = os.path.join(work_dir, f"{basename}_jpn.mkv")
            cmd = [
                self.mkvmerge, "-o", output_mkv,
                filepath,
                "--language", "0:jpn",
                "--track-name", "0:Japanese",
                jpn_srt_path,
            ]
            ok, err = self.run_cmd_with_watchdog(
                cmd, "mkvmerge add Japanese SRT", job_id, stale_timeout=300)
            if not ok:
                return False, f"mkvmerge failed: {err[:200]}", None, work_dir
            if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                return False, "mkvmerge produced empty output", None, work_dir

            output_gb = os.path.getsize(output_mkv) / (1024**3)
            size_change_pct = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0

            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id,
                f"  COMPLETE: Added Japanese SRT ({len(jpn_entries)} entries) "
                f"to {basename}.mkv")

            return True, None, {
                "output_path": output_mkv,
                "output_size_gb": output_gb,
                "reduction_pct": size_change_pct,
                "saved_gb": file_size_gb - output_gb,
            }, work_dir

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.log(f"SubGen exception: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] SubGen exception: {e}")
            self.send_log(job_id, f"[ERROR] {tb[-500:]}")
            return False, str(e), None, work_dir

    def _translate_path(self, server_path, settings):
        """
        Translate server-relative path → node-local path.
        Server: /media/data/media/movies/foo.mkv
        Node (Windows): Z:\\data\\media\\movies\\foo.mkv

        Configured by server settings:
          node_path_remote_prefix (default '/media/')
          node_path_local_prefix  (default '' = no translation)
        """
        if not server_path:
            return server_path
        remote = settings.get("node_path_remote_prefix") or "/media/"
        local = settings.get("node_path_local_prefix") or ""
        if local and server_path.startswith(remote):
            translated = local + server_path[len(remote):]
            # Normalize separators on Windows
            if os.sep == "\\":
                translated = translated.replace("/", "\\")
            return translated
        return server_path

    def process_job(self, job_data):
        """Process a single job. Dispatches based on job_type."""
        job = job_data["job"]
        settings = dict(job.get("settings", {}))
        job_id = job["id"]
        filename = job["file_name"]

        # v2.7 — apply per-node overrides (GUI/CLI + server worker config)
        # over the global settings snapshot the server sent with the job.
        overrides = self._node_overrides()
        if overrides:
            settings.update(overrides)
            job["settings"] = settings

        # v2.5 — translate server path → node-local before any handler runs
        server_path = job["file_path"]
        local_path = self._translate_path(server_path, settings)
        if local_path != server_path:
            job["file_path"] = local_path  # downstream handlers all read job["file_path"]
            # Stash the original for any code that needs to talk back to the server
            job["file_path_server"] = server_path
        filepath = job["file_path"]

        job_type = (job.get("job_type") or "transcode").lower()
        has_dovi = job.get("has_dovi", 0)
        video_codec = job.get("video_codec", "")
        self.current_job_id = job_id
        self.cancelled = False
        st = self._job_entry(job_id)
        if st is not None:
            st["file"] = filename

        self.log(f"Processing job #{job_id} [{job_type}]: {filename}")
        self.send_log(job_id, f"Job #{job_id} started: {filename}")
        self.send_log(job_id, f"  Type: {job_type}")
        if local_path != server_path:
            self.send_log(job_id, f"  Server path: {server_path}")
            self.send_log(job_id, f"  Local path: {local_path}")
        else:
            self.send_log(job_id, f"  Path: {filepath}")
        self.send_log(job_id, f"  Size: {job.get('file_size_gb', 0):.2f} GB")

        # Verify file exists
        if not os.path.exists(filepath):
            self.send_log(job_id, f"[ERROR] File not found at local path: {filepath}")
            if local_path != server_path:
                self.send_log(job_id, f"[ERROR] Path was translated from: {server_path}")
                self.send_log(job_id, f"[HINT] Check Settings → Path Mapping (node_path_local_prefix)")
            self.api("POST", f"/api/jobs/{job_id}/error", {
                "worker_id": self.worker_id, "error": f"File not found: {filepath}"
            })
            self.current_job_id = None
            return

        start_time = time.time()
        success, error, result, work_dir = False, None, None, None

        # ── Dispatch ────────────────────────────────────────────────────────
        if job_type == "remuxclean":
            self.send_log(job_id, f"  Pipeline: RemuxClean (track filter + name cleanup)")
            success, error, result, work_dir = self.remux_clean(job, settings)
        elif job_type == "dv78only":
            self.send_log(job_id, f"  Pipeline: DV Profile → 8 (no re-encode)")
            success, error, result, work_dir = self.dv78only_convert(job, settings)
        elif job_type == "compatfix":
            self.send_log(job_id, f"  Pipeline: Compatibility fix (device-safe convert)")
            success, error, result, work_dir = self.compat_fix(job, settings)
        elif job_type == "subgen":
            self.send_log(job_id, f"  Pipeline: SubGen (Whisper + Claude → Japanese SRT)")
            success, error, result, work_dir = self.subgen(job, settings)
        else:
            # Default: 'transcode' (full DoVi/standard NVENC pipeline)
            self.send_log(job_id, f"  HDR: {job.get('hdr_type', 'SDR')}")
            self.send_log(job_id, f"  Codec: {video_codec}")
            self.send_log(job_id, f"  DoVi: {bool(has_dovi)} (Profile {job.get('dovi_profile', 'N/A')})")
            self.send_log(job_id, f"  CQ: {settings.get('cq', '18')}, Preset: {settings.get('preset', 'slow')}")

            # Pre-flight probe: verify codec before choosing transcode pipeline
            self.send_log(job_id, f"  Pre-flight: Probing source file...")
            probe = self.probe_file(filepath, job_id)
            if probe:
                streams = probe.get("streams", [])
                vid = next((s for s in streams if s.get("codec_type") == "video"), None)
                if vid:
                    actual_codec = vid.get("codec_name", "")
                    self.send_log(job_id, f"  Detected codec: {actual_codec}")
                    if has_dovi and actual_codec != "hevc":
                        self.send_log(job_id,
                            f"  [WARN] DoVi flagged but codec is {actual_codec}, not HEVC — "
                            f"using standard pipeline")
                        has_dovi = 0
                else:
                    self.send_log(job_id, f"  [WARN] No video stream found in probe — proceeding anyway")

            if has_dovi:
                success, error, result, work_dir = self.transcode_dovi(job, settings)
            else:
                success, error, result, work_dir = self.transcode_standard(job, settings)

        elapsed = time.time() - start_time
        elapsed_str = f"{int(elapsed/60)}m {int(elapsed%60)}s"

        if success and result:
            # No-op result (RemuxClean: file was already clean) — skip verification/replacement
            if result.get("no_op"):
                self.send_log(job_id, f"  No changes applied (no-op)")
                self.cleanup_workdir(work_dir)
                self.api("POST", f"/api/jobs/{job_id}/complete", {
                    "worker_id": self.worker_id,
                    "output_path": result["output_path"],
                    "output_size_gb": result["output_size_gb"],
                    "reduction_pct": 0,
                    "saved_gb": 0,
                })
                self.log(f"Job #{job_id} no-op complete in {elapsed_str}")
                self.current_job_id = None
                return

            # Verify output with ffprobe before accepting
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

        self.current_job_id = None

    def run(self):
        """Main loop: register, heartbeat, poll for jobs."""
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
        self.log(f"Polling for jobs every {self.poll_interval}s...")

        while self.running:
            try:
                r = self.api("POST", "/api/jobs/next", {"worker_id": self.worker_id})
                if r and r.get("job"):
                    self.process_job(r)
                elif r:
                    reason = r.get("reason", "")
                    if "paused" not in reason.lower() and "no jobs" not in reason.lower():
                        self.log(f"No job: {reason}")
            except Exception as e:
                self.log(f"Poll error: {e}", "ERROR")

            time.sleep(self.poll_interval)

    # ─── Multi-worker launcher ────────────────────────────────────────────────
    def start_workers(self):
        """
        v2.7 — register and spawn all worker threads, then RETURN (non-
        blocking). Used by byte_node_gui.py, which runs its own supervision
        loop; the old blocking start_all_workers() deadlocked the GUI's
        engine thread mid-setup (the "stuck on Connecting" bug).
        Returns True on success, False if registration failed.
        """
        if not self.register():
            self.log("Cannot start without server connection", "ERROR")
            self.connected = False
            self.is_connected = False
            self.registered = False
            return False

        # v2.4 — flip status flags so GUI updates "Connecting" → "Connected"
        self.connected = True
        self.is_connected = True
        self.registered = True
        self.is_running = True
        self.running = True

        # Heartbeat thread
        def hb_loop():
            while self.running:
                try:
                    self.heartbeat()
                except Exception as e:
                    self.log(f"Heartbeat error: {e}", "WARN")
                time.sleep(15)
        threading.Thread(target=hb_loop, daemon=True, name="heartbeat").start()
        self.log(f"Heartbeat thread started (every 15s)")

        # Read worker counts: global server settings + per-node overrides
        settings = self.api("GET", "/api/settings") or {}
        settings.update(self._node_overrides(force=True))
        try:
            n_tw = int(settings.get("transcode_gpu_count", "1") or "1")
        except (TypeError, ValueError):
            n_tw = 1
        if n_tw < 1:
            n_tw = 1
        try:
            n_hc = int(settings.get("healthcheck_gpu_count", "0") or "0")
        except (TypeError, ValueError):
            n_hc = 0

        self.log(f"Worker counts: {n_tw} transcode GPU, {n_hc} health check GPU")
        self.log(f"Temp path: {settings.get('node_temp_path') or settings.get('temp_path') or '(auto)'}")
        self.log(f"Path mapping: {settings.get('node_path_remote_prefix', '/media/')} -> "
                 f"{settings.get('node_path_local_prefix', '(none)')}")

        # Health check workers — server runs the actual HC loop, so these
        # just idle-poll. They're here for log-format compatibility with the
        # earlier multi-worker architecture.
        if n_hc > 0:
            self.log(f"Starting {n_hc} health check workers...")
            for i in range(n_hc):
                threading.Thread(target=self._hc_worker_loop, args=(i,),
                                 daemon=True, name=f"hc-{i}").start()
                self.log(f"Health check worker #{i} started")

        # Transcode workers — each polls /api/jobs/next independently
        self.log(f"Starting {n_tw} transcode worker(s)...")
        for i in range(n_tw):
            threading.Thread(target=self._tw_worker_loop, args=(i,),
                             daemon=True, name=f"tw-{i}").start()
            self.log(f"Transcode worker #{i} started")
        return True

    def start_all_workers(self):
        """
        Blocking wrapper around start_workers() for CLI use — keeps the
        process alive while daemon worker threads run. (Kept under the old
        name for backwards compatibility with older GUI versions.)
        """
        if not self.start_workers():
            return
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.running = False

    def _tw_worker_loop(self, worker_idx):
        """Single transcode worker poll loop."""
        # Stagger initial poll so 4 workers don't hit the API at the exact same instant
        time.sleep(worker_idx * 0.5)
        while self.running:
            try:
                r = self.api("POST", "/api/jobs/next", {"worker_id": self.worker_id})
                if r and r.get("job"):
                    job = r["job"]
                    fname = job.get("file_name", "")
                    self.log(f"TW#{worker_idx}: starting {fname}")
                    self.process_job(r)
                elif r:
                    reason = r.get("reason", "")
                    rl = reason.lower()
                    # Suppress noisy/expected reasons:
                    #  - "no jobs ready" — idle, expected
                    #  - "paused" — pipeline disabled, expected
                    #  - "race" — another worker won the claim, expected with multi-worker
                    if (reason and "no jobs" not in rl and "paused" not in rl
                            and "race" not in rl):
                        self.log(f"TW#{worker_idx}: waiting — {reason}")
            except Exception as e:
                self.log(f"TW#{worker_idx} poll error: {e}", "ERROR")
            # Random small jitter on the poll interval so concurrent workers stay desynchronized
            time.sleep(self.poll_interval + random.uniform(0, 1.0))

    def _hc_worker_loop(self, worker_idx):
        """
        Health check worker idle loop. The server has its own HC loop
        (start_health_check_loop) so node-side HC is a no-op for now.
        Logs once every 5 minutes (v2.7: was every 30s — pure log spam).
        """
        while self.running:
            try:
                self.log(f"HC#{worker_idx}: idle — server handles health checks")
            except Exception:
                pass
            # Sleep in 1-second slices so shutdown is responsive
            for _ in range(300):
                if not self.running:
                    return
                time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Byte Transcode Node v2")
    parser.add_argument("--server", required=True, help="Server URL (e.g., http://192.168.3.13:5800)")
    parser.add_argument("--name", default="ByteNode", help="Node name")
    parser.add_argument("--gpu", default="GPU", help="GPU name")
    parser.add_argument("--poll", type=int, default=10, help="Poll interval in seconds")
    # v2.7 — per-node overrides (run_node.bat was already passing these;
    # argparse previously rejected them with "unrecognized arguments")
    parser.add_argument("--nas-prefix", default="", help="Server path prefix to translate (e.g. /media)")
    parser.add_argument("--nas-drive", default="", help="Local path the prefix maps to (e.g. Z:)")
    parser.add_argument("--temp-dir", default="", help="Local temp/work directory for jobs")
    parser.add_argument("--workers", type=int, default=0, help="Transcode worker threads (overrides server setting)")
    args = parser.parse_args()

    overrides = {}
    if args.nas_prefix:
        overrides["node_path_remote_prefix"] = args.nas_prefix
    if args.nas_drive:
        overrides["node_path_local_prefix"] = args.nas_drive
    if args.temp_dir:
        overrides["node_temp_path"] = args.temp_dir
    if args.workers > 0:
        overrides["transcode_gpu_count"] = str(args.workers)

    node = ByteNode(args.server, args.name, args.gpu, args.poll, local_overrides=overrides)

    def shutdown(sig, frame):
        print("\nShutting down...")
        node.running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    node.start_all_workers()


if __name__ == "__main__":
    main()
