#!/usr/bin/env python3
"""
Byte Transcode Node v2.10
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

# v2.20 — when the node runs under pythonw (the GUI has no console), Windows pops
# a brief console window for EVERY child process — nvidia-smi every 15s (the
# heartbeat), plus ffmpeg/ffprobe/mkvmerge/dovi_tool/pip — which flashes on screen
# and steals focus. subprocess.run() and check_call() both funnel through
# subprocess.Popen, so wrapping Popen once hides them all.
if os.name == "nt":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen = subprocess.Popen
    class _HiddenPopen(_orig_popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    subprocess.Popen = _HiddenPopen


def _pip(*args):
    return subprocess.call([sys.executable, "-m", "pip", *args],
                           stdout=subprocess.DEVNULL if "-q" in args else None)


def _refresh_site_dirs():
    """Make freshly pip-installed packages importable in THIS running process.
    On a misconfigured Python whose prefix resolves to the node folder, pip
    installs into <node>/Lib/site-packages; add every plausible site dir and
    clear import caches so the new packages are found without a restart."""
    try:
        import site, importlib
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(here, "Lib", "site-packages"),
                     os.path.join(sys.prefix, "Lib", "site-packages"),
                     os.path.join(sys.base_prefix, "Lib", "site-packages")):
            if os.path.isdir(cand):
                site.addsitedir(cand)
        importlib.invalidate_caches()
    except Exception:
        pass


def _ensure_requests():
    """
    Import requests, self-healing on a broken/missing/partial install or a
    Python without pip — all of which produced cryptic startup crashes on some
    nodes. Key: after installing, re-scan site dirs so the just-installed
    packages import in this same process (v2.14 — v2.13 wrongly dropped the
    node-folder site dir, which is the ONLY site dir on a broken Python, so the
    freshly-installed requests couldn't be found).
    """
    try:
        import requests  # noqa
        return requests
    except Exception:
        pass
    # Ensure pip exists (ensurepip), then install/complete the full stack. A
    # partial install missing e.g. 'idna' is completed here.
    if _pip("--version") != 0:
        try:
            subprocess.call([sys.executable, "-m", "ensurepip", "--default-pip"])
        except Exception:
            pass
    if _pip("--version") == 0:
        _pip("install", "-q", "--upgrade", "requests", "idna",
             "charset-normalizer", "urllib3", "certifi")
    _refresh_site_dirs()
    try:
        import requests  # noqa
        return requests
    except Exception as e:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.stderr.write(
            "\n[Byte Node] Could not load 'requests' even after installing it.\n"
            f"  Python : {sys.executable}\n"
            f"  Error  : {e}\n\n"
            "Your Python looks broken ('Could not find platform independent libraries'\n"
            "means it can't locate its own standard library). Cleanest fix:\n"
            "  1) Install Python 3.12 or 3.13 from python.org (tick 'Add to PATH' + pip).\n"
            f"  2) Delete these folders:  {os.path.join(here,'Lib')}   and   {os.path.join(here,'Scripts')}\n"
            "  3) Open a NEW terminal, confirm 'python -m pip --version' works, then\n"
            "     run run_node.bat again.\n")
        sys.exit(1)


requests = _ensure_requests()

# psutil is optional (heartbeat CPU/RAM metrics) — try to install once, but
# never block startup on it; heartbeat degrades to zeros without it.
try:
    import psutil  # noqa: F401
except Exception:
    try:
        if _pip("--version") == 0:
            _pip("install", "-q", "psutil")
    except Exception:
        pass


# ─── Self-update (v2.11) ─────────────────────────────────────────────────────
# The node checks the same published manifest the web UI uses and can pull its
# own new files from GitHub, then relaunch — matching the website's update flow.
NODE_VERSION = "2.33"
GITHUB_RAW = "https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main"
VERSION_MANIFEST_URL = GITHUB_RAW + "/version.json"
NODE_FILES = ["byte_node_v2.py", "byte_node_gui.py", "setup_tools.py",
              "run_node.bat", "run_node_console.bat", "update_node.bat"]


def _vparts(v):
    """'2.11' -> (2, 11) for numeric comparison; tolerant of junk."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def check_for_update(current=NODE_VERSION, timeout=8):
    """
    Fetch the manifest and compare its 'node' version to ours.
    Returns {available, latest, current, notes} or None if the check failed
    (offline, GitHub down) — callers treat None as 'no info, carry on'.
    """
    try:
        r = requests.get(VERSION_MANIFEST_URL, timeout=timeout)
        if r.status_code != 200:
            return None
        m = r.json()
        latest = str(m.get("node", "")).strip()
        if not latest:
            return None
        return {"available": _vparts(latest) > _vparts(current),
                "latest": latest, "current": str(current),
                "notes": m.get("notes", "")}
    except Exception:
        return None


def download_update(dest_dir=None, timeout=30, log_fn=print):
    """
    Download the latest node files from GitHub into dest_dir (default: this
    script's folder), backing up each existing file to .bak first. Config
    (byte_node_config.json) and tools/ are never touched. Returns
    (ok, [messages]).
    """
    dest_dir = dest_dir or os.path.dirname(os.path.abspath(__file__))
    msgs, ok = [], True
    for fn in NODE_FILES:
        try:
            r = requests.get(f"{GITHUB_RAW}/node/{fn}", timeout=timeout)
            if r.status_code != 200:
                msgs.append(f"skip {fn} (HTTP {r.status_code})")
                continue
            path = os.path.join(dest_dir, fn)
            if os.path.exists(path):
                shutil.copy2(path, path + ".bak")
            with open(path, "wb") as f:
                f.write(r.content)
            msgs.append(f"updated {fn}")
        except Exception as e:
            ok = False
            msgs.append(f"FAIL {fn}: {e}")
    for m in msgs:
        try: log_fn(f"  [update] {m}")
        except Exception: pass
    return ok, msgs


def restart_process():
    """Relaunch the current process with the same args, picking up new code."""
    os.execv(sys.executable, [sys.executable] + sys.argv)


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
        # v2.9 — guards model load so two concurrent SubGen jobs don't double-load
        self._whisper_lock = threading.Lock()

        # v2.4 — GUI status flags. byte_node_gui.py polls these to
        # update its top-bar indicator from "Connecting" → "Connected".
        # Multiple aliases set so different GUI versions all see truth.
        self.connected = False
        self.registered = False
        self.is_running = False
        self.is_connected = False  # alias
        self.rate_limited_until = 0  # v2.17 — set when a translation provider 429s hard

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

        self.log(f"Byte Node v{NODE_VERSION} initialized")
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

    def _free(self, path):
        # v2.29 — timeout-guarded. shutil.disk_usage on a hung/flaky drive can
        # block indefinitely, which froze temp pre-flight before Step 1. Cap it
        # at 6s; a non-responsive drive reads as 0 free (→ spill to another drive
        # or a clean 'no space' error — never a hang).
        box = {}
        def probe():
            try: box["v"] = shutil.disk_usage(path).free
            except Exception: box["v"] = 0
        t = threading.Thread(target=probe, daemon=True)
        t.start(); t.join(6.0)
        return box.get("v", 0)

    def _list_temp_drives(self):
        """Fixed local drives usable for temp, as base mount paths (Windows 'D:\\').
        v2.28 — Windows: enumerate with GetDriveTypeW and keep ONLY DRIVE_FIXED.
        The old psutil.disk_partitions() path (and os.path.exists fallback) could
        HANG INDEFINITELY on a dead/zombie NETWORK drive — which wedged a spill
        (triggered when the temp drive is low on space) right before Step 1 with
        no timeout (the 3060 black-hole after 2.27). GetDriveTypeW reads the local
        mount table only; it never touches the network, so it cannot hang."""
        if os.name == "nt":
            try:
                import ctypes
                get_type = ctypes.windll.kernel32.GetDriveTypeW
                DRIVE_FIXED = 3
                drives = []
                for l in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    root = f"{l}:\\"
                    try:
                        if get_type(root) == DRIVE_FIXED:
                            drives.append(root)
                    except Exception:
                        pass
                return drives
            except Exception:
                pass
        drives = []
        try:
            import psutil
            for p in psutil.disk_partitions(all=False):
                opts = (p.opts or "").lower()
                if "cdrom" in opts or "removable" in opts or not p.fstype:
                    continue
                drives.append(p.mountpoint)
        except Exception:
            drives.append("/")
        return drives

    def _need_bytes(self, filepath, file_size_gb, mult):
        try:
            sz = os.path.getsize(filepath)
        except Exception:
            sz = int((file_size_gb or 0) * (1024 ** 3))
        return int(sz * mult)

    def _best_temp_base(self, configured, need_bytes, job_id):
        """
        v2.22 — pick a temp base with room. Prefer the configured drive; if it
        can't fit this job (need_bytes), spill to the LOCAL DRIVE WITH THE MOST
        FREE SPACE that can (e.g. F: full -> D:). Returns a base dir; if nothing
        fits, returns the configured base and the pre-flight check reports it.
        """
        if os.name != "nt":
            return configured
        cfg_letter = os.path.splitdrive(os.path.abspath(configured))[0]  # 'F:'
        cfg_root = cfg_letter + os.sep if cfg_letter else configured
        if not need_bytes or self._free(cfg_root) >= need_bytes:
            return configured
        cands = []
        for d in self._list_temp_drives():
            if os.path.splitdrive(d)[0].lower() == cfg_letter.lower():
                continue
            f = self._free(d)
            if f >= need_bytes:
                cands.append((f, d))
        if cands:
            cands.sort(reverse=True)   # roomiest first
            drive = cands[0][1]
            newbase = os.path.join(drive, "Byte_Engine_temp")
            self.send_log(job_id, f"  [temp] {cfg_root} low on space — spilling this job to "
                                  f"{drive} ({cands[0][0]/(1024**3):.0f} GB free)")
            return newbase
        return configured

    def _work_dir(self, job_id, settings, need_bytes=0):
        """
        Temp work dir for a job. Honors node_temp_path (else temp_path, else
        auto by OS). v2.22 — when need_bytes is given and the configured drive
        can't fit it, auto-spill to the roomiest local drive (e.g. F: -> D:).
        """
        base = (settings.get("node_temp_path") or "").strip()
        if not base:
            base = (settings.get("temp_path") or "").strip()
        if not base:
            base = "C:\\Byte_Engine_temp" if os.name == "nt" else "/tmp/byte_work"
        base = self._best_temp_base(base, need_bytes, job_id)
        work_dir = os.path.join(base, f"job_{job_id}")
        # v2.29 — timeout-guarded makedirs. On a flaky disk or a bad temp path
        # this call could hang forever, freezing the worker at 'Starting...'
        # before Step 1 (the 3060's intermittent wedge). Cap it at 15s; on
        # timeout raise so the job errors cleanly and the thread frees instead
        # of black-holing. process_job catches this and reports it.
        box = {}
        def mk():
            try: os.makedirs(work_dir, exist_ok=True); box["ok"] = True
            except Exception as e: box["err"] = e
        t = threading.Thread(target=mk, daemon=True)
        t.start(); t.join(15.0)
        if box.get("ok"):
            return work_dir
        if "err" in box:
            raise box["err"]
        raise RuntimeError(f"Temp folder creation timed out (>15s) at {work_dir} — "
                           f"this node's temp drive is unresponsive; point node_temp_path "
                           f"at a healthy local drive with free space")

    def _sweep_stale_temp(self, settings):
        """v2.22 — on startup, delete leftover job_* temp dirs from previous runs
        (crashes/kills orphan them and they eat disk).
        v2.24 — SKIP dirs whose job is complete-but-not-yet-accepted: those hold
        the output awaiting the user's review/Accept."""
        base = (settings.get("node_temp_path") or settings.get("temp_path") or "").strip()
        if not base or not os.path.isdir(base):
            return
        keep = set()
        try:
            r = self.api("GET", f"/api/jobs/awaiting-finalize?worker_id={self.worker_id}&all=1")
            for j in (r or {}).get("jobs") or []:
                keep.add(f"job_{j.get('job_id')}")
        except Exception:
            pass
        removed = 0
        try:
            for name in os.listdir(base):
                if name.startswith("job_") and name not in keep:
                    try:
                        shutil.rmtree(os.path.join(base, name), ignore_errors=True)
                        removed += 1
                    except Exception:
                        pass
        except Exception:
            return
        if removed:
            self.log(f"Swept {removed} stale temp job dir(s) from {base}"
                     + (f" (kept {len(keep)} awaiting review)" if keep else ""))

    # ── v2.30 orphaned-process reaper ────────────────────────────────────────
    def _byte_tool_paths(self):
        """Normalized full paths of the tool binaries THIS node runs. Used to
        recognize our own ffmpeg/dovi_tool/mkv* processes (and nothing else) so
        the reaper never touches unrelated ffmpeg the user is running."""
        paths = set()
        for t in (self.ffmpeg, self.ffprobe, self.dovi_tool, self.mkvmerge,
                  self.mkvpropedit, self.mkvextract):
            if not t:
                continue
            try:
                paths.add(os.path.normcase(os.path.abspath(t)))
            except Exception:
                paths.add(os.path.normcase(t))
        return paths

    def _tracked_pids(self):
        """PIDs of subprocesses currently owned by an active job."""
        pids = set()
        try:
            with self._jobs_lock:
                procs = [e.get("process") for e in self._jobs.values()]
        except Exception:
            procs = []
        for p in procs:
            if p is not None:
                try:
                    pids.add(p.pid)
                except Exception:
                    pass
        return pids

    def _reap_orphan_tools(self, kill_all=False):
        """Kill leftover tool processes this node spawned that no active job
        owns — the orphaned ffmpeg that pile up (and lag the PC) when the node
        is restarted/killed mid-job (Windows doesn't kill child processes with
        the parent). kill_all=True (startup) kills every Byte tool process: a
        freshly started node owns none, so all are orphans from the dead prior
        instance. Otherwise only UNTRACKED processes older than 45s die, so a
        live job's encoder is never touched and a just-spawned step (not yet
        registered) is spared by the age grace."""
        try:
            import psutil
        except Exception:
            return 0
        tool_paths = self._byte_tool_paths()
        if not tool_paths:
            return 0
        tracked = set() if kill_all else self._tracked_pids()
        now = time.time()
        killed = 0
        for p in psutil.process_iter(["pid", "exe", "create_time"]):
            try:
                exe = p.info.get("exe")
                if not exe:
                    continue
                if os.path.normcase(os.path.abspath(exe)) not in tool_paths:
                    continue
                if p.info["pid"] in tracked:
                    continue
                if not kill_all and (now - (p.info.get("create_time") or 0)) < 45:
                    continue
                p.kill()
                killed += 1
            except Exception:
                pass
        if killed:
            self.log(f"Reaped {killed} orphaned tool process(es)"
                     + (" on startup" if kill_all else ""), "WARN")
        return killed

    def _orphan_reaper_loop(self):
        """Every 60s, reap untracked (orphaned) tool processes — leaves the
        active jobs' encoders alone."""
        while self.running:
            for _ in range(60):
                if not self.running:
                    return
                time.sleep(1)
            try:
                self._reap_orphan_tools(kill_all=False)
            except Exception as e:
                self.log(f"Orphan reaper error: {e}", "WARN")

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
            "version": NODE_VERSION,   # v2.18 — so the web update bell knows this node's real version
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
            "version": NODE_VERSION,
            # v2.25 — media-drive health so the dashboard can show a red
            # 'MEDIA DRIVE OFFLINE' badge instead of a silently idle node
            "media_ok": 1 if getattr(self, "_media_ok_val", True) else 0,
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

    @staticmethod
    def _fmt_eta(secs):
        """Format seconds as '1h 3m' / '4m 12s' / '38s'."""
        secs = int(max(0, secs))
        if secs > 3600:
            return f"{secs // 3600}h {(secs % 3600) // 60}m"
        if secs > 60:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs}s"

    def _tw_count(self, settings):
        """v2.27 — total transcode workers = GPU count + CPU count, summed
        (the same way health-check workers already sum CPU+GPU). The Transcode
        CPU box used to be read nowhere — a dead control. Now each unit, CPU or
        GPU, adds one concurrent job slot on this node, so 4 GPU + 4 CPU = 8
        concurrent jobs. (All slots currently encode via the GPU/NVENC path, so
        treat CPU as extra concurrency and keep a weaker card's total modest.)"""
        def _i(k):
            try:
                return max(0, int(settings.get(k, "0") or "0"))
            except (TypeError, ValueError):
                return 0
        return max(1, _i("transcode_gpu_count") + _i("transcode_cpu_count"))

    def update_progress(self, job_id, progress, step, eta="", fps=0, compression=0):
        """Send progress update to server."""
        # v2.27 — steps without frame-based progress (RPU extract, dovi_tool
        # convert/inject, mkvmerge remux, and the tail of every job) used to
        # send eta="" so the UI showed a blank ETA that "turned off early".
        # Fall back to a wall-clock estimate from overall progress so the ETA
        # counts down smoothly to 100%.
        if not eta and 0 < progress < 100:
            try:
                st = self._jobs.get(job_id)
                t0 = st.get("started") if st else None
                if t0:
                    elapsed = time.time() - t0
                    if elapsed > 2:
                        eta = self._fmt_eta(elapsed * (100 - progress) / progress)
            except Exception:
                pass
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
            # v2.33 — mkvmerge (the only user of this watchdog) returns exit code
            # 1 for WARNINGS but still writes a complete, valid output file
            # (unsupported-track-type notices, timestamp/CUE warnings, etc.).
            # Treating that as a failure errored out perfectly good remuxes at
            # the final step. Accept exit 1 when the -o output exists and is
            # non-trivial; only 2+ (or 1 with no real output) is a true failure.
            out_file = cmd[cmd.index("-o") + 1] if "-o" in cmd else None
            out_ok = bool(out_file and os.path.exists(out_file) and os.path.getsize(out_file) > 1024)
            if rc == 0 or (rc == 1 and out_ok):
                if rc == 1:
                    self.send_log(job_id, f"  [note] {description}: mkvmerge finished with warnings (exit 1) — output is valid")
                self.send_log(job_id, f"[OK] {description} completed")
                return True, stdout or ""
            self.log(f"[FAILED] {description} (exit {rc})", "ERROR")
            self.send_log(job_id, f"[ERROR] {description} failed (exit {rc})")
            if stderr:
                self.send_log(job_id, f"[ERROR] {stderr[:500]}")
            return False, stderr or "Failed"

        except Exception as e:
            self.log(f"[EXCEPTION] {description}: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] Exception: {e}")
            self.current_process = None
            return False, str(e)

    def _normalize_default_audio(self, mkv_path, job_id):
        """
        v2.9 — ensure exactly ONE audio track carries the default flag (the
        first). mkvmerge remuxes of MP4 sources can leave default=1 on every
        audio track, and duplicate defaults break track selection on some
        players (observed: ExoPlayer direct-play with no audio at all).
        Best-effort: logs a warning on failure, never fails the job.
        """
        try:
            r = subprocess.run([self.mkvmerge, "-J", mkv_path],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            if r.returncode != 0:
                return
            tracks = json.loads(r.stdout).get("tracks", [])
            n_audio = sum(1 for t in tracks if t.get("type") == "audio")
            if n_audio == 0:
                return
            # Always assert default on the first audio track — a lone track
            # with default=0 can still confuse strict track selectors.
            cmd = [self.mkvpropedit, mkv_path, "--edit", "track:a1", "--set", "flag-default=1"]
            for i in range(2, n_audio + 1):
                cmd += ["--edit", f"track:a{i}", "--set", "flag-default=0"]
            r2 = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120)
            if r2.returncode == 0:
                self.send_log(job_id, f"  Normalized default audio flag (1 of {n_audio} tracks default)")
            else:
                self.send_log(job_id, f"  [WARN] Could not normalize default audio flags: {(r2.stderr or r2.stdout)[:150]}")
        except Exception as e:
            self.send_log(job_id, f"  [WARN] Default-audio normalize skipped: {e}")

    def cleanup_workdir(self, work_dir):
        """Clean up temp files on failure."""
        if work_dir and os.path.isdir(work_dir):
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
                self.log(f"Cleaned up work dir: {work_dir}")
            except Exception as e:
                self.log(f"Cleanup failed: {e}", "WARN")

    def _ensure_temp_space(self, work_dir, filepath, job_id, settings, multiplier=2.2):
        """
        v2.22 — make sure the temp dir can hold this job's working files (source
        copied in + output written out ≈ multiplier x source). If the current
        drive is too full, SPILL to the roomiest OTHER local drive (e.g. F: full
        -> D:) by moving the empty job dir there, so big files still process.
        Only if NO drive fits does it fail — with a clear message instead of the
        cryptic 'No space left on device' 20 minutes into an extract.
        Returns (ok, err, work_dir) — work_dir may change if it spilled.
        """
        # v2.26 — size the source with a TIMEOUT. os.path.getsize on a zombie
        # SMB mount (root/exists cached-OK but fresh file stat hangs) used to
        # block this worker thread forever, before Step 1 even reports — the
        # node sat at 'Starting…' 0% GPU indefinitely while heartbeats kept the
        # server trusting it. Now: if the stat can't complete in 10s, treat the
        # drive as down — error the job fast, stop claiming, and trigger a
        # re-map — instead of black-holing.
        sz = self._getsize_timeout(filepath, timeout=10.0)
        if sz is None:
            self._media_ok_val = False
            self._media_ok_at = time.time()  # stop claiming until Z: is back
            drv = os.path.splitdrive(filepath)[0] or "media drive"
            msg = (f"Media drive not responding when sizing source ({drv}) — "
                   f"mount looks hung; will re-map and retry")
            self.send_log(job_id, f"  [ERROR] {msg}")
            return False, msg, work_dir
        if sz <= 0:
            return True, None, work_dir  # can't size it — let it try
        need = int(sz * multiplier)
        if self._free(work_dir) >= need:
            return True, None, work_dir
        # current drive too full — try to spill to a roomier drive
        base = os.path.dirname(work_dir)
        newbase = self._best_temp_base(base, need, job_id)
        if os.path.normcase(os.path.abspath(newbase)) != os.path.normcase(os.path.abspath(base)):
            try:
                newdir = os.path.join(newbase, os.path.basename(work_dir))
                os.makedirs(newdir, exist_ok=True)
                try:
                    os.rmdir(work_dir)   # drop the now-unused (empty) original job dir
                except Exception:
                    pass
                return True, None, newdir
            except Exception as e:
                self.send_log(job_id, f"  [temp] spill failed: {e}")
        gb = 1024 ** 3
        drive = os.path.splitdrive(os.path.abspath(work_dir))[0] or work_dir
        msg = (f"Not enough temp space on any drive: need ~{need/gb:.0f} GB "
               f"(~{multiplier:g}x the source), {drive} has {self._free(work_dir)/gb:.0f} GB free. "
               f"Free space or add a bigger drive.")
        self.send_log(job_id, f"  [ERROR] {msg}")
        return False, msg, work_dir

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
        ok, err, work_dir = self._ensure_temp_space(work_dir, filepath, job_id, settings)
        if not ok:
            return False, err, None, work_dir

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
        ok, err, work_dir = self._ensure_temp_space(work_dir, filepath, job_id, settings, multiplier=1.6)
        if not ok:
            return False, err, None, work_dir
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

    # v2.10 — ISO 639-1/639-2(B/T) equivalence groups: entering any member
    # of a group in the keep_langs setting keeps them all.
    LANG_GROUPS = [
        {"en", "eng"}, {"ja", "jpn"}, {"de", "ger", "deu"}, {"fr", "fre", "fra"},
        {"es", "spa"}, {"it", "ita"}, {"pt", "por"}, {"ru", "rus"},
        {"zh", "chi", "zho"}, {"ko", "kor"}, {"nl", "dut", "nld"}, {"pl", "pol"},
        {"sv", "swe"}, {"no", "nor"}, {"da", "dan"}, {"fi", "fin"},
        {"hi", "hin"}, {"ar", "ara"}, {"tr", "tur"}, {"th", "tha"},
        {"vi", "vie"}, {"cs", "cze", "ces"}, {"el", "gre", "ell"},
        {"hu", "hun"}, {"ro", "rum", "ron"}, {"sk", "slo", "slk"},
    ]

    def _keep_langs(self, settings):
        """
        v2.10 — languages to KEEP for cleanup-style track filtering, from
        the keep_langs setting (comma-separated ISO codes, default
        "eng,jpn"). Undetermined/untagged tracks are always kept. Both
        2- and 3-letter forms of each entered language are honored.
        """
        raw = (settings.get("keep_langs") or "eng,jpn").lower()
        langs = {"und", ""}
        for tok in raw.replace(";", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            langs.add(tok)
            for group in self.LANG_GROUPS:
                if tok in group:
                    langs.update(group)
                    break
        return langs

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
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)  # v2.33 — 120s was too short for big files over slow SMB
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

    def _plan_remux_clean(self, analysis, job_id, keep_langs=None):
        keep = keep_langs if keep_langs is not None else self.KEEP_LANGS
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
            if a["lang"] in keep:
                plan["audio_keep_ids"].append(a["id"])

        # Safety: NEVER strip all audio. If filter removed everything, keep first audio track.
        if not plan["audio_keep_ids"] and all_audio:
            plan["audio_keep_ids"].append(all_audio[0]["id"])
            self.send_log(job_id,
                "  [SAFETY] No kept-language audio found — keeping first audio track")

        plan["removed_audio"] = len(all_audio) - len(plan["audio_keep_ids"])
        plan["kept_audio"] = len(plan["audio_keep_ids"])

        # Filter subs (no safety net — fine to remove all subs)
        for s in all_subs:
            if s["lang"] in keep:
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
            plan = self._plan_remux_clean(analysis, job_id, keep_langs=self._keep_langs(settings))

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
        ok, err, work_dir = self._ensure_temp_space(work_dir, filepath, job_id, settings)
        if not ok:
            return False, err, None, work_dir
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
            reenc_cmd = [
                self.ffmpeg, "-y", "-init_hw_device", "vulkan",
                "-i", filepath, "-map", "0:v:0", "-vf", vf,
                "-c:v", "hevc_nvenc", "-preset", preset, "-cq", str(cq),
                "-profile:v", "main10",
                "-color_primaries", "bt2020", "-color_trc", "smpte2084",
                "-colorspace", "bt2020nc",
                "-f", "hevc", pq_hevc
            ]
            # v2.31 — retry the NVENC re-encode up to 3x. On some GPUs (notably
            # the RTX 3060) opening NVENC alongside the Vulkan tone-map device
            # fails INTERMITTENTLY ("Error while opening encoder — maybe
            # incorrect parameters"), even at 1 concurrent job. It almost always
            # succeeds on a retry, so an occasional open-failure no longer errors
            # the whole job. A genuinely bad file still fails after 3 tries.
            ok, err = False, ""
            for attempt in range(1, 4):
                if self.cancelled:
                    return False, "Cancelled by user", None, work_dir
                ok, err = self.run_cmd(reenc_cmd, "DV5→HDR10 Re-encode", job_id,
                    parse_progress=True, input_size_gb=file_size_gb,
                    total_duration_sec=duration_sec)
                if ok:
                    break
                if attempt < 3:
                    self.send_log(job_id, f"  [retry] NVENC re-encode attempt {attempt}/3 failed "
                                          f"(likely transient encoder-open) — retrying in 5s")
                    time.sleep(5)
            if not ok:
                return False, f"Base layer re-encode failed after 3 attempts: {err[:200]}", None, work_dir
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
        ok, err, work_dir = self._ensure_temp_space(work_dir, filepath, job_id, settings)
        if not ok:
            return False, err, None, work_dir

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
        ok, err, work_dir = self._ensure_temp_space(work_dir, filepath, job_id, settings, multiplier=1.5)
        if not ok:
            return False, err, None, work_dir
        output_mkv = os.path.join(work_dir, f"{basename}_compat.mkv")

        if reasons:
            self.send_log(job_id, "  Flagged: " + "; ".join(reasons))
        self.send_log(job_id, f"  Strategy: {strategy}")

        # v2.10 — subtitle filtering honors the configurable keep_langs setting.
        # v2.19 — mkvmerge --subtitle-tracks takes NUMERIC track IDs or ISO 639-2
        # (3-letter) language codes; 2-letter codes like 'en'/'ja' make it error
        # ("not a valid ... language code"). Normalize to 3-letter, and if nothing
        # valid remains, keep all subs rather than failing the whole rewrap.
        _ISO1_3 = {"en": "eng", "ja": "jpn", "es": "spa", "fr": "fre", "de": "ger",
                   "it": "ita", "pt": "por", "ru": "rus", "zh": "chi", "ko": "kor",
                   "nl": "dut", "ar": "ara", "hi": "hin", "sv": "swe", "pl": "pol"}
        keep = sorted({(_ISO1_3.get(l, l)) for l in self._keep_langs(settings)
                       if l and len(_ISO1_3.get(l, l)) == 3})

        try:
            if strategy == "remux":
                do_filter = filter_subs and bool(keep)
                step = "[Step 1/1] mkvmerge rewrap to MKV" + (f" ({'/'.join(keep)} subs only)" if do_filter else "")
                self.update_progress(job_id, 20, step)
                self.send_log(job_id, step)
                cmd = [self.mkvmerge, "-o", output_mkv]
                if do_filter:
                    cmd += ["--subtitle-tracks", ",".join(keep)]
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
                if filter_subs:
                    sub_maps = []
                    for lang in keep:
                        if lang != "und":
                            sub_maps += ["-map", f"0:s:m:language:{lang}?"]
                else:
                    sub_maps = ["-map", "0:s?"]
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
        """Lazy-load faster-whisper model, thread-safely. The lock prevents two
        concurrent SubGen jobs (transcode_gpu_count>1) from double-loading it."""
        def _loaded():
            return (self._whisper_model is not None
                    and self._whisper_model_name == model_name
                    and self._whisper_device == device
                    and self._whisper_compute == compute_type)
        if _loaded():
            return self._whisper_model
        with self._whisper_lock:
            if _loaded():          # another job may have loaded it while we waited
                return self._whisper_model
            return self._load_whisper(model_name, device, compute_type, job_id)

    def _register_cuda_dll_dirs(self):
        """
        faster-whisper (via CTranslate2) needs cuBLAS + cuDNN DLLs at RUNTIME
        — cublas64_12.dll / cudnn*.dll. When installed via pip they land under
        site-packages/nvidia/<lib>/bin, which is NOT on the Windows DLL search
        path, so the model loads but transcription dies with
        'Library cublas64_12.dll is not found or cannot be loaded'. Register
        those bin dirs explicitly. Returns True if at least one was found.
        v2.12 — fixes SubGen failures on nodes without a system CUDA toolkit.
        """
        if os.name != "nt":
            return True  # Linux uses the wheels' bundled .so via RPATH
        found = False
        try:
            import importlib.util
            for pkg in ("nvidia.cublas", "nvidia.cudnn"):
                try:
                    spec = importlib.util.find_spec(pkg)
                except Exception:
                    spec = None
                locs = list(getattr(spec, "submodule_search_locations", None) or [])
                if not locs:
                    continue
                bindir = os.path.join(locs[0], "bin")
                if os.path.isdir(bindir):
                    try:
                        os.add_dll_directory(bindir)
                        found = True
                    except Exception:
                        pass
        except Exception:
            pass
        return found

    def _ensure_cuda_libs(self, job_id):
        """Install the pip-packaged CUDA runtime libs faster-whisper needs on a
        GPU node, then register their DLL dirs. Idempotent and quiet if present."""
        if not self._register_cuda_dll_dirs():
            self.send_log(job_id, "  Installing CUDA runtime libs for Whisper (cuBLAS + cuDNN, one-time)")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install",
                                       "nvidia-cublas-cu12", "nvidia-cudnn-cu12",
                                       "-q", "--break-system-packages"])
            except Exception as e:
                self.send_log(job_id, f"  [WARN] CUDA lib install failed ({e}) — Whisper will use CPU")
                return False
            return self._register_cuda_dll_dirs()
        return True

    def _load_whisper(self, model_name, device, compute_type, job_id):
        """Import (installing if needed), resolve auto device/compute, and load the
        model. Caller must hold self._whisper_lock."""
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
        # Make cuBLAS/cuDNN loadable before any CUDA attempt (install if absent).
        if resolved_device in ("auto", "cuda"):
            self._ensure_cuda_libs(job_id)
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

    # ─── Translation providers (multi-vendor, user-selectable) ───────────────
    # Any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, OpenRouter,
    # Together, DeepSeek, …) works via provider 'openai_compatible' + base_url.
    _LANG_NAMES = {
        "jpn": "Japanese", "ja": "Japanese", "eng": "English", "en": "English",
        "spa": "Spanish", "fre": "French", "fra": "French", "ger": "German",
        "deu": "German", "ita": "Italian", "por": "Portuguese", "kor": "Korean",
        "chi": "Chinese", "zho": "Chinese", "rus": "Russian", "ara": "Arabic",
    }

    def _lang_name(self, code):
        code = (code or "jpn").lower()
        return self._LANG_NAMES.get(code, code.capitalize())

    def _resolve_translate_cfg(self, settings):
        """Provider-agnostic translation config. Falls back to the legacy
        claude_api_key / claude_model settings so existing installs keep working."""
        provider = (settings.get("translate_provider") or "anthropic").strip().lower()
        raw_key = (settings.get("translate_api_key") or "").strip()
        cfg = {
            "provider": provider,
            "api_key": raw_key,
            "model": (settings.get("translate_model") or "").strip(),
            "base_url": (settings.get("translate_base_url") or "").strip().rstrip("/"),
            "glossary": (settings.get("translate_glossary") or "").strip(),
        }
        if provider == "anthropic":
            raw_key = raw_key or (settings.get("claude_api_key") or "").strip()
            cfg["api_key"] = raw_key
            cfg["model"] = cfg["model"] or (settings.get("claude_model") or "claude-sonnet-4-6").strip()
        # v2.18 — the key field may hold MULTIPLE keys (newline/comma separated)
        # for round-robin rotation when one hits its daily quota.
        cfg["api_keys"] = [k.strip() for k in re.split(r"[\n,]+", raw_key) if k.strip()] or [raw_key]
        cfg["api_key"] = cfg["api_keys"][0]
        # pacing: min seconds between translation calls to stay under free-tier RPM
        try:
            cfg["min_interval"] = float(settings.get("translate_min_interval") or 0)
        except (TypeError, ValueError):
            cfg["min_interval"] = 0.0
        return cfg

    def _build_translate_prompts(self, chunk, prev_context, title, target_lang, glossary):
        """Shared prompt used by every provider. Returns (system, user)."""
        lang = self._lang_name(target_lang)
        chunk_for_api = [{"i": i, "t": e["text"]} for i, e in enumerate(chunk)]
        prev_lines = "\n".join(f"- {t}" for t in prev_context[-10:]) if prev_context else "(start of file)"
        system_prompt = (
            f"You are a professional subtitle translator localizing film/TV subtitles into {lang}. "
            f"Write natural, idiomatic {lang} the way a native subtitler would — never literal, "
            f"word-for-word translation. Preserve each speaker's tone, register and character voice; "
            f"use appropriate politeness/honorific levels; keep slang, profanity and crude language "
            f"intact (do NOT censor or soften it); keep idioms idiomatic and short enough to read as "
            f"subtitles. Preserve speaker labels, musical ♪ markers and (sound) caption cues, "
            f"translating their content. Do not add notes or explanations. Output ONLY a JSON array."
        )
        gloss = f"\nGlossary / context (apply consistently):\n{glossary}\n" if glossary else ""
        user_prompt = (
            f"Title: {title}\n{gloss}\n"
            f"Previous lines already translated (for continuity):\n{prev_lines}\n\n"
            f"Translate each subtitle entry below into {lang}. Each entry has an index 'i' and source "
            f"text 't'. Return a JSON array of objects, each with 'i' (same index) and 't' (the {lang} "
            f"translation). Output ONLY the JSON array — no markdown, no commentary.\n\n"
            f"Entries:\n{json.dumps(chunk_for_api, ensure_ascii=False)}"
        )
        return system_prompt, user_prompt

    def _retry_after_seconds(self, r):
        """Seconds to wait from a 429/503, from the Retry-After header or (Gemini)
        the error.details[].retryDelay field. None if unspecified."""
        ra = r.headers.get("Retry-After")
        if ra:
            try:
                return int(float(ra))
            except Exception:
                pass
        try:
            for d in (r.json().get("error", {}).get("details") or []):
                m = re.match(r"(\d+)", str(d.get("retryDelay") or ""))
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return None

    def _http_post_retry(self, url, headers, body, job_id, label, timeout=180, max_retries=5):
        """
        v2.17 — POST with backoff on rate-limit (429) and transient (500/503)
        errors, honoring Retry-After / Gemini retryDelay. Free tiers (e.g. Gemini
        ~15 req/min) 429 constantly while translating a movie; retrying paces the
        calls so the job finishes instead of dying. Returns a 200 Response, or
        None. On a 429 it couldn't clear, sets self.rate_limited_until so the
        pipeline can pause rather than burn every remaining job.
        """
        delay = 6
        for attempt in range(max_retries + 1):
            if self.cancelled:
                return None
            try:
                r = requests.post(url, headers=headers, json=body, timeout=timeout)
            except Exception as e:
                if attempt < max_retries:
                    self.send_log(job_id, f"  [{label}] request error ({e}); retry in {delay}s")
                    time.sleep(delay); delay = min(delay * 2, 60); continue
                self.send_log(job_id, f"  [{label}] request failed: {e}")
                return None
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 503) and attempt < max_retries:
                wait = self._retry_after_seconds(r) or delay
                wait = min(max(wait, 3), 90)
                self.send_log(job_id, f"  [{label}] {r.status_code} (rate/transient) — waiting {wait}s "
                                      f"(retry {attempt+1}/{max_retries})")
                time.sleep(wait); delay = min(delay * 2, 60); continue
            if r.status_code == 429:
                self.rate_limited_until = time.time() + 120
                self.send_log(job_id, f"  [{label}] 429 quota exhausted — try a higher-limit provider "
                                      f"or a local model (Ollama). Response: {r.text[:200]}")
            else:
                self.send_log(job_id, f"  [{label}] API {r.status_code}: {r.text[:300]}")
            return None
        return None

    def _call_anthropic(self, cfg, system_prompt, user_prompt, job_id):
        base = cfg["base_url"] or "https://api.anthropic.com"
        r = self._http_post_retry(
            f"{base}/v1/messages",
            {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
            {"model": cfg["model"], "max_tokens": 8192, "system": system_prompt,
             "messages": [{"role": "user", "content": user_prompt}]},
            job_id, "Anthropic", timeout=180)
        if r is None:
            return None
        try:
            data = r.json()
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
        except Exception as e:
            self.send_log(job_id, f"  [ERROR] Could not parse Anthropic response: {e}")
            return None

    def _call_openai(self, cfg, system_prompt, user_prompt, job_id):
        """OpenAI Chat Completions shape — also serves any OpenAI-compatible
        endpoint (local Ollama/LM Studio/vLLM, OpenRouter, etc.) via base_url."""
        base = cfg["base_url"] or "https://api.openai.com/v1"
        headers = {"content-type": "application/json"}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        r = self._http_post_retry(
            f"{base}/chat/completions", headers,
            {"model": cfg["model"], "temperature": 0.3, "max_tokens": 8192,
             "messages": [{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user_prompt}]},
            job_id, "OpenAI-compatible", timeout=300)
        if r is None:
            return None
        try:
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            self.send_log(job_id, f"  [ERROR] Could not parse OpenAI-compatible response: {e}")
            return None

    def _call_gemini(self, cfg, system_prompt, user_prompt, job_id):
        base = cfg["base_url"] or "https://generativelanguage.googleapis.com"
        url = f"{base}/v1beta/models/{cfg['model']}:generateContent?key={cfg['api_key']}"
        r = self._http_post_retry(
            url, {"content-type": "application/json"},
            {"systemInstruction": {"parts": [{"text": system_prompt}]},
             "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
             "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192,
                                  "responseMimeType": "application/json"}},
            job_id, "Gemini", timeout=180)
        if r is None:
            return None
        try:
            cand = (r.json().get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts).strip()
        except Exception as e:
            self.send_log(job_id, f"  [ERROR] Could not parse Gemini response: {e}")
            return None

    def _pace_translate(self, cfg):
        """v2.18 — hold a minimum gap between translation calls to stay under a
        free tier's requests-per-minute limit (e.g. Gemini free ~15/min → set 4s)."""
        iv = cfg.get("min_interval", 0) or 0
        if iv <= 0:
            return
        last = getattr(self, "_last_translate_at", 0)
        wait = iv - (time.time() - last)
        if wait > 0:
            time.sleep(min(wait, 30))
        self._last_translate_at = time.time()

    def _translate_chunk(self, cfg, chunk, prev_context, title, target_lang, job_id):
        """Translate one chunk via the configured provider. Rotates across
        multiple API keys when one hits its quota (429). Returns entries with
        'text' replaced (timestamps preserved)."""
        system_prompt, user_prompt = self._build_translate_prompts(
            chunk, prev_context, title, target_lang, cfg.get("glossary", ""))
        provider = cfg["provider"]
        keys = cfg.get("api_keys") or [cfg.get("api_key")]
        if not hasattr(self, "_key_idx"):
            self._key_idx = 0

        response_text = None
        for _try in range(len(keys)):
            cfg["api_key"] = keys[self._key_idx % len(keys)]
            self._pace_translate(cfg)
            self.rate_limited_until = 0
            if provider == "anthropic":
                response_text = self._call_anthropic(cfg, system_prompt, user_prompt, job_id)
            elif provider == "gemini":
                response_text = self._call_gemini(cfg, system_prompt, user_prompt, job_id)
            elif provider in ("openai", "openai_compatible"):
                response_text = self._call_openai(cfg, system_prompt, user_prompt, job_id)
            else:
                self.send_log(job_id, f"  [ERROR] Unknown translate_provider '{provider}'")
                return None
            if response_text:
                break
            # failed — if it was a rate limit and we have another key, rotate & retry
            if self.rate_limited_until > time.time() and len(keys) > 1 and _try < len(keys) - 1:
                self._key_idx += 1
                self.send_log(job_id, f"  Rotating to API key #{(self._key_idx % len(keys)) + 1}/{len(keys)} (previous hit its quota)")
                continue
            break
        if not response_text:
            return None

        # Strip code fences the model may have added despite instructions
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text).strip()
        try:
            translated = json.loads(response_text)
        except json.JSONDecodeError:
            # Some models wrap the array in prose or an object — pull out the array
            m = re.search(r"\[.*\]", response_text, re.DOTALL)
            if not m:
                self.send_log(job_id, f"  [ERROR] {provider} returned non-JSON: {response_text[:200]}")
                return None
            try:
                translated = json.loads(m.group(0))
            except json.JSONDecodeError as e:
                self.send_log(job_id, f"  [ERROR] {provider} JSON parse failed: {e}")
                return None
        if isinstance(translated, dict):  # e.g. {"translations": [...]}
            for v in translated.values():
                if isinstance(v, list):
                    translated = v
                    break

        by_idx = {item.get("i"): item.get("t", "") for item in translated if isinstance(item, dict)}
        result = []
        for i, src in enumerate(chunk):
            new_entry = dict(src)
            txt = by_idx.get(i)
            new_entry["text"] = txt if txt else src["text"]  # fallback to source if missing
            result.append(new_entry)
        return result

    def _translate_srt(self, entries, cfg, chunk_size, title, target_lang, job_id):
        """Translate full SRT entries to target_lang in chunks via the configured
        provider. Returns translated entries (timestamps preserved) or None."""
        if not entries:
            return entries
        # A key is required for every hosted provider; local OpenAI-compatible
        # servers (Ollama/LM Studio) legitimately need no key.
        if not cfg.get("api_key") and cfg["provider"] != "openai_compatible":
            self.send_log(job_id, "  [ERROR] Translation API key not set in settings")
            return None

        lang = self._lang_name(target_lang)
        translated_all = []
        prev_context = []  # recently-translated lines, for cross-chunk continuity
        total_chunks = (len(entries) + chunk_size - 1) // chunk_size
        self.send_log(job_id,
            f"  Translating {len(entries)} entries → {lang} via {cfg['provider']} "
            f"({cfg.get('model') or 'default'}) in {total_chunks} chunks of {chunk_size}")

        for ci in range(total_chunks):
            if self.cancelled:
                self.send_log(job_id, "  Cancel requested — stopping translation")
                return None
            chunk = entries[ci * chunk_size : (ci + 1) * chunk_size]
            self.send_log(job_id, f"  Chunk {ci+1}/{total_chunks} ({len(chunk)} entries)...")
            translated = self._translate_chunk(cfg, chunk, prev_context, title, target_lang, job_id)
            if translated is None:
                self.send_log(job_id, f"  [ERROR] Translation failed at chunk {ci+1}")
                return None
            translated_all.extend(translated)
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

        def _run(m):
            return m.transcribe(
                audio_path,
                language=language,
                task=task,
                beam_size=5,
                vad_filter=True,  # voice-activity detection — better timestamps
                vad_parameters=dict(min_silence_duration_ms=500),
                # v2.15 — quality: stop the repeated-phrase hallucination drift
                # you saw ("completely wrong / not what was said"), and let
                # Whisper resample low-confidence segments instead of committing.
                condition_on_previous_text=False,
                temperature=[0.0, 0.2, 0.4, 0.6, 0.8],
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.4,
            )

        try:
            segments, info = _run(model)
            self.send_log(job_id,
                f"  Detected language: {info.language} (prob {info.language_probability:.2f}), "
                f"duration {info.duration:.0f}s")
        except Exception as e:
            # v2.12 — a missing/unloadable cuBLAS or cuDNN DLL only surfaces here
            # (model load succeeds, inference doesn't). Reload on CPU and retry
            # once so the subtitle job completes instead of hard-failing.
            msg = str(e).lower()
            cuda_lib_issue = any(k in msg for k in
                ("cublas", "cudnn", "cannot be loaded", "is not found", "cuda", "libcu"))
            if cuda_lib_issue and self._whisper_device == "cuda":
                self.send_log(job_id, f"  [WARN] CUDA transcribe failed ({e}); reloading Whisper on CPU and retrying")
                with self._whisper_lock:
                    self._whisper_model = None  # force a fresh CPU load
                    model = self._load_whisper(model_name, "cpu", "int8", job_id)
                if model is None:
                    return None
                try:
                    segments, info = _run(model)
                    self.send_log(job_id,
                        f"  Detected language: {info.language} (prob {info.language_probability:.2f}), "
                        f"duration {info.duration:.0f}s (CPU)")
                except Exception as e2:
                    self.send_log(job_id, f"  [ERROR] Whisper CPU transcribe also failed: {e2}")
                    return None
            else:
                self.send_log(job_id, f"  [ERROR] Whisper transcribe failed: {e}")
                return None

        raw = []
        last_log_time = time.time()
        total_duration = info.duration if info.duration else 1
        for seg in segments:
            if self.cancelled:
                self.send_log(job_id, "  Cancel requested — stopping transcription")
                return None
            text = (seg.text or "").strip()
            if not text:
                continue
            raw.append({
                "start": seg.start, "end": seg.end, "text": text,
                "nsp": getattr(seg, "no_speech_prob", 0.0) or 0.0,
                "alp": getattr(seg, "avg_logprob", 0.0) or 0.0,
            })
            # Periodic progress updates within the Whisper phase
            if time.time() - last_log_time > 5:
                pct_audio = (seg.end / total_duration) if total_duration else 0
                # Whisper occupies 20%-60% of the bar
                phase_pct = 20 + pct_audio * 40
                self.update_progress(job_id, phase_pct,
                    f"Whisper transcribing ({seg.end:.0f}s / {total_duration:.0f}s)")
                last_log_time = time.time()

        entries = self._clean_whisper_segments(raw)
        self.send_log(job_id, f"  Transcribed {len(entries)} subtitle entries "
                              f"({len(raw) - len(entries)} filtered as hallucination/dupes)")
        return entries

    def _clean_whisper_segments(self, raw):
        """
        v2.15 — clean Whisper output for the issues seen on real shows:
        - drop silence hallucinations (high no-speech prob + low confidence) and
          repeated-phrase dupes ("completely wrong / not what was said"),
        - stop subtitles lingering long after the dialogue by capping each line's
          duration to a reading-time budget and never overlapping the next line.
        """
        cleaned, prev_norm = [], None
        for s in raw:
            # silence hallucination: model itself thinks it's non-speech + unsure
            if s["nsp"] > 0.85 and s["alp"] < -0.8:
                continue
            norm = re.sub(r"\s+", " ", s["text"]).strip().lower()
            # collapsed repeat of the previous short line (classic hallucination loop)
            if prev_norm is not None and norm == prev_norm and (s["end"] - s["start"]) < 2.5:
                continue
            prev_norm = norm
            cleaned.append(s)

        out = []
        for i, s in enumerate(cleaned):
            start = max(0.0, s["start"])
            end = s["end"]
            nxt = cleaned[i + 1]["start"] if i + 1 < len(cleaned) else None
            # reading-time cap: ~0.06s/char, min 1.2s, max 7s
            max_dur = min(7.0, max(1.2, len(s["text"]) * 0.06 + 0.8))
            end = min(end, start + max_dur)
            if nxt is not None:
                end = min(end, nxt - 0.05)   # never overlap / linger into next line
            if end <= start:
                end = start + 0.6
            out.append({"idx": len(out) + 1, "start": start, "end": end, "text": s["text"]})
        return out

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

    # ─── SubGen sync verification + cancellation (v2.9) ───────────────────────
    def _audio_stream_start_pts(self, probe, audio_stream_idx):
        """Container start time (seconds) of the given audio stream — the offset
        Whisper timings are implicitly relative to. 0.0 if unknown."""
        for s in (probe or {}).get("streams", []):
            if s.get("index") == audio_stream_idx:
                try:
                    return float(s.get("start_time") or 0.0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _normalize_srt_timing(self, entries, offset, job_id):
        """Shift all cues by `offset` seconds when it is significant. Whisper emits
        timings relative to decoded audio starting at 0; if the source audio stream
        has a nonzero start PTS, the muxed subs would drift without this."""
        if not entries or abs(offset) < 0.1:
            return entries
        self.send_log(job_id, f"  Adjusting subtitle timing by {offset:+.3f}s (audio start PTS)")
        for e in entries:
            e["start"] = max(0.0, e["start"] + offset)
            e["end"] = max(0.0, e["end"] + offset)
        return entries

    def _verify_srt_sync(self, entries, duration, job_id):
        """Non-fatal sanity check on final cue timings; logs warnings if off."""
        if not entries:
            self.send_log(job_id, "  [WARN] No subtitle entries to verify")
            return False
        ok = True
        if entries[0]["start"] < -0.05:
            self.send_log(job_id, f"  [WARN] First cue starts before zero ({entries[0]['start']:.2f}s)")
            ok = False
        prev = -1.0
        for e in entries:
            if e["end"] < e["start"]:
                self.send_log(job_id, "  [WARN] A cue ends before it starts"); ok = False; break
            if e["start"] < prev - 0.5:
                self.send_log(job_id, "  [WARN] Non-monotonic cue timings detected"); ok = False; break
            prev = e["start"]
        if duration and entries[-1]["end"] > duration + 5:
            self.send_log(job_id,
                f"  [WARN] Last cue ({entries[-1]['end']:.0f}s) exceeds media duration ({duration:.0f}s)")
            ok = False
        if ok:
            self.send_log(job_id,
                f"  Sync OK: {len(entries)} cues over 0–{entries[-1]['end']:.0f}s (media {duration:.0f}s)")
        return ok

    def _subgen_cancel_poller(self, job_id, stop_event):
        """Runs for the whole subgen() lifetime. check_cancel() sets the per-job
        cancelled flag (cross-thread safe via the job registry) so the in-process
        Whisper and translation loops actually observe a user cancel."""
        while not stop_event.wait(3):
            try:
                if self.check_cancel(job_id):
                    return
            except Exception:
                pass

    _TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

    def _find_text_sub_stream(self, probe, langset):
        """Return the index of the first TEXT subtitle stream whose language is in
        `langset`, or None. Image subs (PGS/VobSub) don't count — can't translate them."""
        for s in (probe or {}).get("streams", []):
            if s.get("codec_type") != "subtitle":
                continue
            if (s.get("codec_name") or "").lower() not in self._TEXT_SUB_CODECS:
                continue
            lng = ((s.get("tags") or {}).get("language") or "").lower()
            if lng in langset:
                return s.get("index")
        return None

    def _dur(self, probe):
        try:
            return float((probe.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ── OpenSubtitles: fetch official subs instead of a slow Whisper run (v2.16) ──
    _os_token = None
    _os_token_at = 0.0

    def _movie_hash(self, path):
        """OpenSubtitles' 64-bit hash (filesize + first & last 64KB), hex. Lets
        the search match the exact release for perfectly-synced subs."""
        try:
            import struct
            fmt = "<q"; step = struct.calcsize(fmt); chunk = 65536
            fsize = os.path.getsize(path)
            if fsize < chunk * 2:
                return None
            h = fsize & 0xFFFFFFFFFFFFFFFF
            with open(path, "rb") as f:
                for _ in range(chunk // step):
                    h = (h + struct.unpack(fmt, f.read(step))[0]) & 0xFFFFFFFFFFFFFFFF
                f.seek(fsize - chunk, 0)
                for _ in range(chunk // step):
                    h = (h + struct.unpack(fmt, f.read(step))[0]) & 0xFFFFFFFFFFFFFFFF
            return "%016x" % h
        except Exception:
            return None

    def _os_login(self, key, settings, job_id):
        """Bearer token for downloads (cached ~20 min). Returns None if creds
        are missing (search still works; only downloads need a login)."""
        user = (settings.get("opensubtitles_username") or "").strip()
        pw = (settings.get("opensubtitles_password") or "").strip()
        if not (user and pw):
            return None
        if self._os_token and (time.time() - self._os_token_at) < 1200:
            return self._os_token
        try:
            r = requests.post("https://api.opensubtitles.com/api/v1/login",
                headers={"Api-Key": key, "Content-Type": "application/json",
                         "User-Agent": "ByteTranscode/1.0"},
                json={"username": user, "password": pw}, timeout=20)
            if r.status_code == 200:
                self._os_token = r.json().get("token"); self._os_token_at = time.time()
                return self._os_token
            self.send_log(job_id, f"  [OpenSubtitles] login failed ({r.status_code}) — check username/password")
        except Exception as e:
            self.send_log(job_id, f"  [OpenSubtitles] login error: {e}")
        return None

    def _opensubtitles_fetch(self, filepath, lang, job_id, settings):
        """Search + download a best-match `lang` subtitle. Returns parsed SRT
        entries, or None to fall back to Whisper. Never raises."""
        key = (settings.get("opensubtitles_api_key") or "").strip()
        if not key:
            return None
        ua = {"Api-Key": key, "User-Agent": "ByteTranscode/1.0"}
        title = re.sub(r"[._]", " ", os.path.splitext(os.path.basename(filepath))[0]).strip()
        mhash = self._movie_hash(filepath)
        self.send_log(job_id, f"  [OpenSubtitles] searching {lang} subs for '{title}' (hash={mhash or 'n/a'})")
        try:
            params = {"languages": lang, "query": title}
            if mhash:
                params["moviehash"] = mhash
            r = requests.get("https://api.opensubtitles.com/api/v1/subtitles",
                             headers=ua, params=params, timeout=25)
            if r.status_code == 401:
                self.send_log(job_id, "  [OpenSubtitles] 401 — API key rejected")
                return None
            if r.status_code != 200:
                self.send_log(job_id, f"  [OpenSubtitles] search HTTP {r.status_code}")
                return None
            data = r.json().get("data", [])
            if not data:
                self.send_log(job_id, "  [OpenSubtitles] no matches — will use Whisper")
                return None
            def score(it):
                a = it.get("attributes", {}) or {}
                return (1 if a.get("moviehash_match") else 0,
                        1 if a.get("from_trusted") else 0,
                        a.get("download_count", 0) or 0)
            data.sort(key=score, reverse=True)
            best = data[0].get("attributes", {}) or {}
            files = best.get("files", []) or []
            if not files or not files[0].get("file_id"):
                return None
            token = self._os_login(key, settings, job_id)
            if not token:
                self.send_log(job_id, "  [OpenSubtitles] no login token (set username+password) — using Whisper")
                return None
            dh = dict(ua); dh["Content-Type"] = "application/json"; dh["Authorization"] = f"Bearer {token}"
            r = requests.post("https://api.opensubtitles.com/api/v1/download",
                              headers=dh, json={"file_id": files[0]["file_id"]}, timeout=25)
            if r.status_code != 200:
                self.send_log(job_id, f"  [OpenSubtitles] download HTTP {r.status_code} {r.text[:100]}")
                return None
            j = r.json(); link = j.get("link")
            if not link:
                return None
            srt = requests.get(link, timeout=40)
            srt.encoding = srt.apparent_encoding or "utf-8"
            entries = self._parse_srt(srt.text)
            if not entries:
                self.send_log(job_id, "  [OpenSubtitles] downloaded sub empty/unparseable — using Whisper")
                return None
            self.send_log(job_id,
                f"  [OpenSubtitles] got {len(entries)} cues from '{best.get('release','?')}' "
                f"(hash_match={data[0]['attributes'].get('moviehash_match')}, quota left {j.get('remaining','?')})")
            return entries
        except Exception as e:
            self.send_log(job_id, f"  [OpenSubtitles] error: {e} — using Whisper")
            return None

    def subgen(self, job, settings):
        """
        Subtitle generation. Ensures the file has BOTH an English and a target-language
        (default Japanese) TEXT subtitle track, creating whichever is missing:
          • English  — from an existing eng text sub, else Whisper-transcribed from audio.
          • Japanese — translated from the English pivot via the configured AI provider
                       (or Whisper-transcribed directly when only target-language audio exists).
        New tracks are embedded via mkvmerge when that's quick (small files, or embed=always),
        otherwise written as external SRTs next to the video (embed=auto over the size
        threshold, or embed=never).
        """
        job_id = job["id"]
        filepath = job["file_path"]
        filename = job["file_name"]
        file_size_gb = job["file_size_gb"]

        target_lang = (settings.get("subgen_target_lang") or "jpn").lower()
        target_set = {"jpn", "ja"} if target_lang in ("jpn", "ja") else {target_lang}
        tcfg = self._resolve_translate_cfg(settings)
        whisper_model = settings.get("whisper_model", "large-v3")
        whisper_device = settings.get("whisper_device", "auto")
        whisper_compute = settings.get("whisper_compute", "auto")
        try:
            chunk_size = int(settings.get("subgen_translate_chunk", "40"))
        except (TypeError, ValueError):
            chunk_size = 40
        embed_mode = (settings.get("subgen_embed") or "auto").lower()   # auto | always | never
        try:
            embed_max_gb = float(settings.get("subgen_embed_max_gb") or 25)
        except (TypeError, ValueError):
            embed_max_gb = 25.0

        basename = os.path.splitext(filename)[0]
        work_dir = self._work_dir(job_id, settings)
        lang_name = self._lang_name(target_lang)

        # v2.9 — poll for cancellation for the whole SubGen duration. The Whisper
        # and translation loops check self.cancelled but (unlike run_cmd) spawn no
        # subprocess whose poller would set it — so drive check_cancel() here.
        _cancel_stop = threading.Event()
        _cancel_thr = threading.Thread(
            target=self._subgen_cancel_poller, args=(job_id, _cancel_stop), daemon=True)
        _cancel_thr.start()

        def _need_key():
            return not tcfg["api_key"] and tcfg["provider"] != "openai_compatible"

        try:
            step = "[SubGen] Probing source streams"
            self.update_progress(job_id, 5, step)
            self.send_log(job_id, step)
            probe = self.probe_file(filepath, job_id)
            if not probe:
                return False, "Failed to probe source file", None, work_dir

            eng_sub_idx = self._find_text_sub_stream(probe, {"eng", "en"})
            tgt_sub_idx = self._find_text_sub_stream(probe, target_set)
            audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
            def _alang(a): return ((a.get("tags") or {}).get("language") or "und").lower()
            eng_audio = next((a for a in audio_streams if _alang(a) in ("eng", "en")), None)
            tgt_audio = next((a for a in audio_streams if _alang(a) in target_set), None)

            need_eng = eng_sub_idx is None
            need_tgt = tgt_sub_idx is None
            if not need_eng and not need_tgt:
                self.send_log(job_id, "  Already has English + target subs — nothing to do")
                return True, None, {"no_op": True, "output_path": filepath,
                                    "output_size_gb": file_size_gb, "reduction_pct": 0, "saved_gb": 0}, work_dir
            self.send_log(job_id, "  Missing: " +
                ", ".join(([("English")] if need_eng else []) + ([lang_name] if need_tgt else [])))

            # ── Acquire the English pivot (and, when only target audio exists, a direct
            #    target-language transcription).
            eng_entries = None    # English cues — pivot + English track
            tgt_direct = None     # target cues transcribed directly from target-lang audio
            if eng_sub_idx is not None:
                self.update_progress(job_id, 15, "Extracting existing English subtitles")
                src_srt = os.path.join(work_dir, f"{basename}.eng.extract.srt")
                if not self._extract_text_subtitle(filepath, eng_sub_idx, src_srt, job_id):
                    return False, "Failed to extract English subtitle stream", None, work_dir
                eng_entries = self._parse_srt(open(src_srt, encoding="utf-8", errors="replace").read())
                if not eng_entries:
                    return False, "Extracted English SRT was empty", None, work_dir
            else:
                # v2.16 — official English subs from OpenSubtitles FIRST: seconds
                # instead of a 30-60 min Whisper run, and properly synced. Falls
                # back to Whisper when nothing is found or no key is configured.
                eng_entries = self._opensubtitles_fetch(filepath, "en", job_id, settings)
                if eng_entries:
                    self.update_progress(job_id, 30, f"Fetched official English subtitles ({len(eng_entries)} cues)")
                elif eng_audio is not None or tgt_audio is None:
                    a = eng_audio or (audio_streams[0] if audio_streams else None)
                    if a is None:
                        return False, "No audio or subtitle source for SubGen", None, work_dir
                    a_idx = a.get("index")
                    wav = os.path.join(work_dir, f"{basename}.audio.wav")
                    self.update_progress(job_id, 18, "Extracting audio for Whisper")
                    if not self._extract_audio_for_whisper(filepath, a_idx, wav, job_id):
                        return False, "Failed to extract audio", None, work_dir
                    eng_entries = self._whisper_transcribe(
                        wav, whisper_model, whisper_device, whisper_compute,
                        language=("en" if eng_audio is not None else None), task="transcribe", job_id=job_id)
                    if eng_entries is None:
                        return False, "Whisper transcription failed", None, work_dir
                    eng_entries = self._normalize_srt_timing(
                        eng_entries, self._audio_stream_start_pts(probe, a_idx), job_id)
                else:
                    # No English source; try official target-language subs, else transcribe foreign audio.
                    tgt_direct = self._opensubtitles_fetch(
                        filepath, ("ja" if target_lang in ("jpn", "ja") else target_lang), job_id, settings)
                    if not tgt_direct:
                        a_idx = tgt_audio.get("index")
                        wav = os.path.join(work_dir, f"{basename}.audio.wav")
                        self.update_progress(job_id, 18, "Extracting audio for Whisper")
                        if not self._extract_audio_for_whisper(filepath, a_idx, wav, job_id):
                            return False, "Failed to extract audio", None, work_dir
                        tgt_direct = self._whisper_transcribe(
                            wav, whisper_model, whisper_device, whisper_compute,
                            language=("ja" if target_lang in ("jpn", "ja") else None), task="transcribe", job_id=job_id)
                        if tgt_direct is None:
                            return False, "Whisper transcription failed", None, work_dir
                        tgt_direct = self._normalize_srt_timing(
                            tgt_direct, self._audio_stream_start_pts(probe, a_idx), job_id)

            # ── Build the tracks we need to add: list of (lang_code, track_name, entries)
            tracks = []
            if need_tgt:
                if tgt_direct is not None:
                    tgt_entries = tgt_direct
                else:
                    if _need_key():
                        return False, "Translation API key not set — open Settings → AI / Subtitles", None, work_dir
                    step = f"[SubGen] Translating English → {lang_name} via {tcfg['provider']}"
                    self.update_progress(job_id, 55, step)
                    self.send_log(job_id, step)
                    tgt_entries = self._translate_srt(eng_entries, tcfg, chunk_size, basename, target_lang, job_id)
                    if tgt_entries is None:
                        return False, "Translation failed (see log)", None, work_dir
                self._verify_srt_sync(tgt_entries, self._dur(probe), job_id)
                tracks.append((target_lang, lang_name, tgt_entries))

            if need_eng:
                if eng_entries is None and tgt_direct is not None:
                    if _need_key():
                        return False, "Translation API key not set — open Settings → AI / Subtitles", None, work_dir
                    self.update_progress(job_id, 78, "[SubGen] Translating → English")
                    eng_entries = self._translate_srt(tgt_direct, tcfg, chunk_size, basename, "eng", job_id)
                    if eng_entries is None:
                        return False, "English translation failed (see log)", None, work_dir
                if eng_entries is not None:
                    self._verify_srt_sync(eng_entries, self._dur(probe), job_id)
                    tracks.append(("eng", "English", eng_entries))

            if not tracks:
                return True, None, {"no_op": True, "output_path": filepath,
                                    "output_size_gb": file_size_gb, "reduction_pct": 0, "saved_gb": 0}, work_dir

            # ── Decide embed vs external (embed preferred when quick)
            if embed_mode == "always":
                do_embed = True
            elif embed_mode == "never":
                do_embed = False
            else:  # auto — embed small files, external for big ones (remux would be slow)
                do_embed = file_size_gb <= embed_max_gb
            self.send_log(job_id,
                f"  Output: {'embed (mkvmerge)' if do_embed else 'external SRT'} "
                f"[file {file_size_gb:.1f}GB, threshold {embed_max_gb:.0f}GB, mode={embed_mode}]")

            # ── Write SRT files (work dir for embed; next to the video for external)
            self.update_progress(job_id, 92, "Writing subtitle files")
            written = []
            for lang, name, entries in tracks:
                out_dir = work_dir if do_embed else os.path.dirname(filepath)
                srt_path = os.path.join(out_dir, f"{basename}.{lang}.srt")
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(self._serialize_srt(entries))
                written.append((lang, name, srt_path))
                self.send_log(job_id, f"  Wrote {name} SRT ({len(entries)} cues) → {srt_path}")

            # ── External: done — no remux, don't replace the original (no_op result)
            if not do_embed:
                self.update_progress(job_id, 100, "Complete (external subtitles)")
                self.send_log(job_id, f"  COMPLETE: wrote {len(written)} external subtitle file(s) beside the video")
                return True, None, {"no_op": True, "output_path": filepath,
                                    "output_size_gb": file_size_gb, "reduction_pct": 0, "saved_gb": 0}, work_dir

            # ── Embed: add all new tracks in one mkvmerge pass
            step = f"[SubGen] mkvmerge: embedding {len(written)} subtitle track(s)"
            self.update_progress(job_id, 95, step)
            self.send_log(job_id, step)
            output_mkv = os.path.join(work_dir, f"{basename}_subgen.mkv")
            cmd = [self.mkvmerge, "-o", output_mkv, filepath]
            for lang, name, srt_path in written:
                cmd += ["--language", f"0:{lang}", "--track-name", f"0:{name}", srt_path]
            ok, err = self.run_cmd_with_watchdog(
                cmd, "mkvmerge add subtitle tracks", job_id, stale_timeout=600)
            if not ok:
                return False, f"mkvmerge failed: {err[:200]}", None, work_dir
            if not os.path.exists(output_mkv) or os.path.getsize(output_mkv) < 1024:
                return False, "mkvmerge produced empty output", None, work_dir

            output_gb = os.path.getsize(output_mkv) / (1024**3)
            size_change_pct = (1 - output_gb / file_size_gb) * 100 if file_size_gb > 0 else 0
            self.update_progress(job_id, 100, "Complete")
            self.send_log(job_id,
                f"  COMPLETE: embedded {', '.join(n for _, n, _ in written)} into {basename}.mkv")
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
        finally:
            _cancel_stop.set()

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
            st["started"] = time.time()  # v2.27 — wall-clock start for ETA fallback

        self.log(f"Processing job #{job_id} [{job_type}]: {filename}")
        self.send_log(job_id, f"Job #{job_id} started: {filename}")
        self.send_log(job_id, f"  Type: {job_type}")
        if local_path != server_path:
            self.send_log(job_id, f"  Server path: {server_path}")
            self.send_log(job_id, f"  Local path: {local_path}")
        else:
            self.send_log(job_id, f"  Path: {filepath}")
        self.send_log(job_id, f"  Size: {job.get('file_size_gb', 0):.2f} GB")

        # Verify file exists — with a timeout so a hung network drive errors the
        # job in seconds instead of blocking this worker thread forever (v2.24).
        exists = self._path_exists_timeout(filepath, timeout=10.0)
        if not exists:
            if exists is None:
                self.send_log(job_id, f"[ERROR] Media drive not responding reading: {filepath}")
                err = f"Media drive not responding ({os.path.splitdrive(filepath)[0]}) — check the network mount"
                self._media_ok_val = False; self._media_ok_at = time.time()  # stop claiming until it's back
            else:
                self.send_log(job_id, f"[ERROR] File not found at local path: {filepath}")
                err = f"File not found: {filepath}"
            if local_path != server_path:
                self.send_log(job_id, f"[ERROR] Path was translated from: {server_path}")
                self.send_log(job_id, f"[HINT] Check Settings → Path Mapping (node_path_local_prefix)")
            self.api("POST", f"/api/jobs/{job_id}/error", {
                "worker_id": self.worker_id, "error": err
            })
            self.current_job_id = None
            return

        start_time = time.time()
        success, error, result, work_dir = False, None, None, None

        # ── Dispatch ────────────────────────────────────────────────────────
        # v2.29 — wrapped so ANY unhandled pipeline exception (e.g. temp-folder
        # creation timing out on a flaky disk, v2.29 raises RuntimeError) errors
        # the job cleanly and frees this worker, instead of bubbling up and
        # leaving the job stuck 'processing' at 'Starting...' until requeued.
        try:
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
        except Exception as e:
            self.log(f"Job #{job_id} pipeline error: {e}", "ERROR")
            self.send_log(job_id, f"[ERROR] {str(e)[:300]}")
            success, error, result = False, f"Node pipeline error: {str(e)[:200]}", None

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

            # v2.9 — exactly one default audio track (duplicate default flags
            # from MP4-source remuxes caused silent audio on ExoPlayer
            # direct-play). SubGen outputs excluded (owned elsewhere).
            if job_type != "subgen" and result["output_path"].lower().endswith(".mkv"):
                self._normalize_default_audio(result["output_path"], job_id)

            # v2.24 — tdarr-style accept flow. Replacement happens on ACCEPTANCE:
            #   auto_accept=true  → accept + replace immediately (below)
            #   auto_accept=false → hold the output in temp for review; when the
            #     user clicks Accept, the finalize poller does the replacement.
            replace_on = (settings.get("replace_original", "true") == "true")
            auto_acc = (settings.get("auto_accept", "false") == "true")
            hold_for_review = False
            if replace_on and auto_acc:
                final_path = self.replace_original(job, result, settings, work_dir)
                result["output_path"] = final_path
            elif replace_on:
                hold_for_review = True
                self.send_log(job_id, "  Held for review — will replace the original when you Accept it")
            else:
                self.send_log(job_id, f"  Keep both: original preserved, output at {result['output_path']}")

            self.send_log(job_id, f"  Total time: {elapsed_str}")
            self.api("POST", f"/api/jobs/{job_id}/complete", {
                "worker_id": self.worker_id,
                "output_path": result["output_path"],
                "output_size_gb": result["output_size_gb"],
                "reduction_pct": result["reduction_pct"],
                "saved_gb": result["saved_gb"],
                "hold_for_review": hold_for_review,
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
                r = self.api("POST", "/api/jobs/next", {"worker_id": self.worker_id}, timeout=90)
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
        self._sweep_stale_temp(settings)   # v2.22 — clear leftover temp from prior runs
        self._reap_orphan_tools(kill_all=True)  # v2.30 — kill ffmpeg orphaned by the previous instance
        n_tw = self._tw_count(settings)
        try:
            n_hc = int(settings.get("healthcheck_gpu_count", "0") or "0")
        except (TypeError, ValueError):
            n_hc = 0

        self.log(f"Worker counts: {n_tw} transcode (GPU+CPU summed), {n_hc} health check GPU")
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

        # Transcode workers — each polls /api/jobs/next independently.
        # v2.23 — LIVE SCALING: thread count follows the transcode worker
        # setting while running. A scaler loop re-reads settings every 30s;
        # raising the count spawns threads immediately, lowering it makes the
        # extra threads exit after their current job (never killing work).
        self._tw_target = n_tw
        self._tw_spawned = 0
        self.log(f"Starting {n_tw} transcode worker(s)...")
        for i in range(n_tw):
            threading.Thread(target=self._tw_worker_loop, args=(i,),
                             daemon=True, name=f"tw-{i}").start()
            self._tw_spawned += 1
            self.log(f"Transcode worker #{i} started")
        threading.Thread(target=self._worker_scaler_loop, daemon=True, name="scaler").start()
        threading.Thread(target=self._finalize_poller_loop, daemon=True, name="finalize").start()
        threading.Thread(target=self._orphan_reaper_loop, daemon=True, name="orphan-reaper").start()  # v2.30
        return True

    def _finalize_poller_loop(self):
        """
        v2.24 — completes the manual-accept flow. Jobs finished with auto-accept
        OFF hold their output in this node's temp; when the user clicks Accept,
        the server marks them accepted and this loop performs the replacement
        (original → new file, temp cleaned) and reports the final path.
        """
        while self.running:
            time.sleep(45)
            try:
                r = self.api("GET", f"/api/jobs/awaiting-finalize?worker_id={self.worker_id}")
                jobs = (r or {}).get("jobs") or []
                if not jobs:
                    continue
                settings = self.api("GET", "/api/settings") or {}
                settings.update(self._node_overrides())
                settings["replace_original"] = "true"   # acceptance IS the approval
                for j in jobs:
                    jid = j.get("job_id")
                    out = j.get("output_path") or ""
                    src_server = j.get("source_path") or ""
                    local = self._translate_path(src_server, settings)
                    if not out or not os.path.exists(out):
                        self.log(f"Finalize #{jid}: output missing ({out}) — telling server", "WARN")
                        self.api("POST", f"/api/jobs/{jid}/finalize-done",
                                 {"worker_id": self.worker_id, "output_path": "", "ok": False,
                                  "error": "output no longer on node (temp cleaned?)"})
                        continue
                    self.log(f"Finalize #{jid}: user accepted — replacing original")
                    job_like = {"id": jid, "file_path": local}
                    result_like = {"output_path": out}
                    final = self.replace_original(job_like, result_like, settings, os.path.dirname(out))
                    self.api("POST", f"/api/jobs/{jid}/finalize-done",
                             {"worker_id": self.worker_id, "output_path": final, "ok": True})
            except Exception:
                pass

    def _worker_scaler_loop(self):
        """v2.32 — apply worker-count changes live, ROBUSTLY. The old version
        tracked a single _tw_spawned counter to decide how many threads to add;
        after rapid up/down changes + node restarts that counter drifted (it
        could think 4 workers exist when only 1 does), leaving the node
        permanently under-threaded until a manual restart. Now: each pass we
        count the LIVE transcode worker threads by name and spawn whichever
        indices below the target are actually missing. Threads whose index is
        >= target drain themselves (see _tw_worker_loop). This self-corrects
        from any drift — the node always converges to exactly `target` workers."""
        while self.running:
            time.sleep(20)
            try:
                settings = self.api("GET", "/api/settings") or {}
                settings.update(self._node_overrides(force=True))
                target = self._tw_count(settings)
                self._tw_target = target
                live = set()
                for t in threading.enumerate():
                    if t.name.startswith("tw-") and t.is_alive():
                        idx = t.name[3:]
                        if idx.isdigit():
                            live.add(int(idx))
                spawned = 0
                for i in range(target):
                    if i not in live:
                        threading.Thread(target=self._tw_worker_loop, args=(i,),
                                         daemon=True, name=f"tw-{i}").start()
                        spawned += 1
                if spawned:
                    self._tw_spawned = max(getattr(self, "_tw_spawned", 0), target)
                    self.log(f"Scaler: target {target} workers — spawned {spawned} that were missing "
                             f"(had {len(live)} live)")
            except Exception:
                pass

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

    def _path_exists_timeout(self, path, timeout=8.0):
        """os.path.exists that can't hang forever: a dead SMB mount blocks the
        syscall indefinitely, so run it in a helper thread with a timeout.
        Returns True/False, or None on timeout (drive not responding)."""
        result = {}
        def probe():
            try:
                result["v"] = os.path.exists(path)
            except Exception:
                result["v"] = False
        t = threading.Thread(target=probe, daemon=True)
        t.start(); t.join(timeout)
        return result.get("v") if "v" in result else None

    def _getsize_timeout(self, path, timeout=10.0):
        """os.path.getsize that can't hang forever. A zombie SMB session can
        pass the cached exists()/root check yet block indefinitely on a fresh
        file stat — which used to wedge a worker at 'Starting…' 0% forever
        (the black-hole) because getsize() has no timeout. Run it in a helper
        thread. Returns the size in bytes, or None on timeout (drive hung)."""
        result = {}
        def probe():
            try:
                result["v"] = os.path.getsize(path)
            except Exception:
                result["v"] = -1  # exists-but-unreadable / gone: distinct from hung
        t = threading.Thread(target=probe, daemon=True)
        t.start(); t.join(timeout)
        return result.get("v") if "v" in result else None

    def _remount_media(self, prefix):
        """
        v2.25 — SELF-HEAL a dead Windows drive mapping. Mapped drives are
        per-session and Windows silently drops them (idle autodisconnect,
        expired cached creds) — which left one node idle while the other kept
        working. Re-map the letter to the share and re-check. UNC comes from
        the node_smb_unc setting, else \\\\<server-host>\\storage derived from
        the server URL. Rate-limited to once per 3 minutes.
        """
        if os.name != "nt":
            return False
        now = time.time()
        if now - getattr(self, "_last_remount_at", 0) < 180:
            return False
        self._last_remount_at = now
        letter = os.path.splitdrive(prefix)[0]           # 'Z:'
        if not letter:
            return False
        settings = self.api("GET", "/api/settings") or {}
        settings.update(self._node_overrides())
        unc = (settings.get("node_smb_unc") or "").strip()
        if not unc:
            m = re.match(r"https?://([^:/]+)", self.server)
            if not m:
                return False
            unc = "\\\\" + m.group(1) + "\\storage"
        user = (settings.get("node_smb_user") or "").strip()
        pw = (settings.get("node_smb_pass") or "").strip()
        self.log(f"MEDIA DRIVE {letter} dead — attempting re-map to {unc}", "WARN")
        try:
            def _net(*a):
                return subprocess.run(["net", "use", *a], capture_output=True,
                                      text=True, errors="replace", timeout=60)
            # v2.27 — robust re-map. 'System error 85 (device name already in
            # use)' and '1219 (multiple connections … different credentials)'
            # come from a stale/persistent connection racing the re-map. Clear
            # BOTH the letter and any deviceless session to the share, then
            # authenticate deviceless (so attaching the letter can't fail on
            # credentials), then attach the letter. Retry once on error 85.
            _net(letter, "/delete", "/y")
            _net(unc, "/delete", "/y")
            dev = ["net", "use", unc]
            if pw:
                dev.append(pw)
            if user:
                dev.append(f"/user:{user}")
            subprocess.run(dev, capture_output=True, text=True, errors="replace", timeout=60)
            r = _net(letter, unc, "/persistent:yes")
            if r.returncode != 0 and "85" in ((r.stderr or "") + (r.stdout or "")):
                _net(letter, "/delete", "/y")
                r = _net(letter, unc, "/persistent:yes")
            if r.returncode == 0:
                self.log(f"MEDIA DRIVE {letter} re-mapped to {unc} OK")
                return True
            self.log(f"Re-map failed (rc {r.returncode}): {(r.stderr or r.stdout or '').strip()[:150]}", "ERROR")
        except Exception as e:
            self.log(f"Re-map error: {e}", "ERROR")
        return False

    def _media_ok(self, force=False):
        """
        v2.24 — MEDIA-DRIVE GUARD. Before claiming any job, verify the media
        drive (node_path_local_prefix, e.g. Z:\\) actually responds. A node with
        a hung/unmapped drive used to claim jobs and sit at 'Starting…' forever
        (the black-hole) because exists() blocked on dead SMB while heartbeats
        kept the server trusting it. Result cached 30s.
        v2.25 — on failure it re-maps the drive itself and re-checks.
        """
        now = time.time()
        if not force and now - getattr(self, "_media_ok_at", 0) < 30:
            return getattr(self, "_media_ok_val", True)
        prefix = (self._node_overrides().get("node_path_local_prefix")
                  or self.local_overrides.get("node_path_local_prefix") or "")
        ok = True
        if prefix:
            r = self._path_exists_timeout(prefix, timeout=8.0)
            ok = bool(r)
            if not ok and self._remount_media(prefix):
                ok = bool(self._path_exists_timeout(prefix, timeout=8.0))
            if not ok:
                why = "not responding (hung mount?)" if r is None else "not found (drive not mapped?)"
                self.log(f"MEDIA DRIVE {prefix} {why} — NOT claiming jobs until it's back", "ERROR")
        self._media_ok_at = now
        self._media_ok_val = ok
        return ok

    def _tw_worker_loop(self, worker_idx):
        """Single transcode worker poll loop."""
        # Stagger initial poll so 4 workers don't hit the API at the exact same instant
        time.sleep(worker_idx * 0.5)
        while self.running:
            # v2.23 — live scale-down: if the worker count was lowered below this
            # thread's index, exit before claiming anything new.
            if worker_idx >= getattr(self, "_tw_target", worker_idx + 1):
                self.log(f"TW#{worker_idx}: stopping (worker count lowered)")
                if self._tw_spawned > worker_idx:
                    self._tw_spawned = worker_idx
                return
            # v2.24 — never claim work this node can't do (dead media drive)
            if not self._media_ok():
                time.sleep(15)
                continue
            try:
                r = self.api("POST", "/api/jobs/next", {"worker_id": self.worker_id}, timeout=90)
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
                            and "race" not in rl and "disabled" not in rl):
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
    parser.add_argument("--update", action="store_true", help="Download the latest node code from GitHub and restart")
    parser.add_argument("--check-update", action="store_true", help="Just report whether a newer node version is available, then exit")
    args = parser.parse_args()

    # v2.11 — self-update paths (no server needed).
    if args.check_update:
        u = check_for_update()
        if u is None:
            print("Update check failed (offline or GitHub unreachable).")
        elif u["available"]:
            print(f"Update available: v{u['current']} -> v{u['latest']}")
            print(f"  {u['notes']}")
            print("Run with --update to install.")
        else:
            print(f"Up to date (v{u['current']}).")
        return
    if args.update:
        print(f"Byte Node self-update (current v{NODE_VERSION})...")
        ok, _ = download_update()
        if not ok:
            print("Update incomplete — some files failed. Old files kept as .bak.")
            return
        print("Update downloaded. New tools (if any): py setup_tools.py")
        print("Restarting node with the new code...")
        # Drop --update so the relaunched process runs normally.
        sys.argv = [a for a in sys.argv if a not in ("--update",)]
        restart_process()
        return

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

    # v2.11 — one-shot update notice at startup (non-fatal, best-effort).
    u = check_for_update()
    if u and u.get("available"):
        node.log("=" * 58, "WARN")
        node.log(f"  UPDATE AVAILABLE: v{u['current']} -> v{u['latest']}", "WARN")
        node.log(f"  {u['notes']}", "WARN")
        node.log("  Stop the node and run:  py byte_node_v2.py --update", "WARN")
        node.log("  (or double-click update_node.bat)", "WARN")
        node.log("=" * 58, "WARN")

    node.start_all_workers()


if __name__ == "__main__":
    main()
