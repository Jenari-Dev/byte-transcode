#!/usr/bin/env python3
"""
Byte Transcode Server v2
========================
Flask API server with authentication, health checks, scan progress,
theme support, and comprehensive queue management.

Run: python3 byte_server.py --port 5800
Reset password: python3 byte_server.py --reset-password
"""

import os, sys, json, time, sqlite3, hashlib, subprocess, threading, logging, secrets, functools
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from contextlib import contextmanager
from flask import Flask, request, jsonify, send_from_directory, Response, session, redirect

DEFAULT_PORT = 5800
DB_PATH = os.environ.get("BYTE_DB_PATH", "/config/byte_transcode.db")
LOG_DIR = os.environ.get("BYTE_LOG_DIR", "/config/logs")
STATIC_DIR = os.environ.get("BYTE_STATIC_DIR", "/app/static")
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.flv', '.webm'}

app = Flask(__name__, static_folder=STATIC_DIR)
app.config["SECRET_KEY"] = os.environ.get("BYTE_SECRET", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, "server.log")), logging.StreamHandler()])
log = logging.getLogger("byte-server")

# Track scan progress in memory
scan_progress = {}  # library_id -> {total, scanned, current_file, eta, status}


# ─── Database ────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                file_count INTEGER DEFAULT 0,
                total_size_gb REAL DEFAULT 0,
                last_scanned TEXT,
                status TEXT DEFAULT 'idle',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                file_size_gb REAL NOT NULL,
                duration_min REAL DEFAULT 0,
                duration_str TEXT DEFAULT '',
                video_codec TEXT DEFAULT '',
                resolution TEXT DEFAULT '',
                fps TEXT DEFAULT '',
                hdr_type TEXT DEFAULT 'SDR',
                has_dovi INTEGER DEFAULT 0,
                dovi_profile INTEGER,
                audio_summary TEXT DEFAULT '',
                audio_track_count INTEGER DEFAULT 0,
                subtitle_track_count INTEGER DEFAULT 0,
                library_id INTEGER,
                library_name TEXT DEFAULT '',
                priority INTEGER DEFAULT 999,
                status TEXT DEFAULT 'pending',
                health_status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0,
                current_step TEXT DEFAULT '',
                eta TEXT DEFAULT '',
                worker_id TEXT,
                output_path TEXT,
                output_size_gb REAL,
                reduction_pct REAL,
                error_message TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                accepted INTEGER DEFAULT 0,
                skipped_reason TEXT,
                probe_data TEXT,
                FOREIGN KEY (library_id) REFERENCES libraries(id)
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                host TEXT DEFAULT '',
                gpu TEXT DEFAULT '',
                status TEXT DEFAULT 'idle',
                current_job_id INTEGER,
                last_heartbeat TEXT,
                registered_at TEXT DEFAULT (datetime('now','localtime')),
                jobs_completed INTEGER DEFAULT 0,
                total_saved_gb REAL DEFAULT 0,
                cpu_usage REAL DEFAULT 0,
                ram_usage REAL DEFAULT 0,
                gpu_usage REAL DEFAULT 0,
                vram_usage REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now','localtime')),
                level TEXT DEFAULT 'INFO',
                source TEXT DEFAULT 'server',
                message TEXT NOT NULL,
                job_id INTEGER
            );
        """)
        defaults = {
            "cq": "18", "preset": "slow", "max_workers": "4", "min_size_gb": "10",
            "container": "mkv", "dovi_convert_p8": "true", "replace_original": "true",
            "temp_path": "/temp/byte_work", "gpu": "RTX 5080", "processing_enabled": "false",
            "auto_accept": "false", "skip_transcoded": "true", "theme": "dark",
            "max_dovi_concurrent": "2", "processing_mode": "transcode",
            "staged_limit": "10", "transcode_gpu_count": "4", "healthcheck_gpu_count": "4",
            "auth_user": "admin", "auth_hash": "",
        }
        for k, v in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    log.info("Database initialized")
    # Auto-migrate: add any missing columns
    try:
        with get_db() as db:
            queue_cols = [r[1] for r in db.execute('PRAGMA table_info(queue)').fetchall()]
            queue_needed = {'health_status': 'TEXT DEFAULT "pending"', 'current_step': 'TEXT DEFAULT ""',
                'eta': 'TEXT DEFAULT ""', 'worker_id': 'TEXT', 'output_path': 'TEXT',
                'output_size_gb': 'REAL', 'reduction_pct': 'REAL', 'error_message': 'TEXT',
                'started_at': 'TEXT', 'completed_at': 'TEXT', 'accepted': 'INTEGER DEFAULT 0',
                'skipped_reason': 'TEXT', 'probe_data': 'TEXT', 'progress': 'REAL DEFAULT 0', 'duration_str': 'TEXT DEFAULT ""'}
            for col, typedef in queue_needed.items():
                if col not in queue_cols:
                    db.execute(f'ALTER TABLE queue ADD COLUMN {col} {typedef}')
                    log.info(f"Migration: added queue.{col}")
            worker_cols = [r[1] for r in db.execute('PRAGMA table_info(workers)').fetchall()]
            for col in ['cpu_usage','ram_usage','gpu_usage','vram_usage']:
                if col not in worker_cols:
                    db.execute(f'ALTER TABLE workers ADD COLUMN {col} REAL DEFAULT 0')
                    log.info(f"Migration: added workers.{col}")
            # Fix any NULL health statuses
            db.execute('UPDATE queue SET health_status="pending" WHERE health_status IS NULL')
            db.execute('UPDATE queue SET progress=0 WHERE progress IS NULL')
            # Reset stuck scanning libraries on startup
            stuck = db.execute("UPDATE libraries SET status='idle' WHERE status='scanning'")
            if stuck.rowcount > 0:
                log.warning(f"Reset {stuck.rowcount} stuck library scans on startup")
            # Reset stuck processing jobs on startup — put them back in queue for automatic retry
            stuck_jobs = db.execute("""UPDATE queue SET status='queued', health_status='healthy',
                error_message=NULL, progress=0, current_step='', worker_id=NULL,
                started_at=NULL WHERE status='processing'""")
            if stuck_jobs.rowcount > 0:
                log.warning(f"Reset {stuck_jobs.rowcount} interrupted jobs back to queued on startup")
                add_log(f"Server restart: reset {stuck_jobs.rowcount} interrupted jobs back to queue", db=db)
            # Reset health checks that were in progress
            stuck_hc = db.execute("UPDATE queue SET health_status='pending' WHERE health_status='checking'")
            if stuck_hc.rowcount > 0:
                log.warning(f"Reset {stuck_hc.rowcount} stuck health checks on startup")
            db.execute("UPDATE workers SET status='idle', current_job_id=NULL")
    except Exception as e:
        log.error(f"Migration error: {e}")

def add_log(msg, level="INFO", source="server", job_id=None, db=None):
    try:
        if db:
            db.execute("INSERT INTO logs (level, source, message, job_id) VALUES (?, ?, ?, ?)",
                       (level, source, msg, job_id))
        else:
            with get_db() as conn:
                conn.execute("INSERT INTO logs (level, source, message, job_id) VALUES (?, ?, ?, ?)",
                           (level, source, msg, job_id))
    except Exception:
        pass

def send_notification(title, message, priority="default"):
    """Send push notification via ntfy.sh if enabled."""
    try:
        url = get_setting("ntfy_url")
        enabled = get_setting("ntfy_enabled")
        if enabled != "true" or not url:
            return
        import urllib.request, urllib.error
        req = urllib.request.Request(url, data=message.encode('utf-8'))
        req.add_header("Title", title)
        req.add_header("Priority", priority)
        req.add_header("Tags", "movie_camera")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning(f"Notification failed: {e}")

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_setting(key):
    with get_db() as db:
        r = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

def set_setting(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


# ─── Auth ────────────────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth_hash = get_setting("auth_hash")
        if not auth_hash:  # No password set = no auth required
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            if request.path.startswith("/api/workers") or request.path.startswith("/api/jobs") or request.path.startswith("/api/settings") or request.path.startswith("/api/queue/"):
                # Node API calls use worker auth, not session
                return f(*args, **kwargs)
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route("/api/auth/status")
def auth_status():
    auth_hash = get_setting("auth_hash")
    return jsonify({
        "needs_setup": not auth_hash,
        "authenticated": session.get("authenticated", False) or not auth_hash,
        "username": get_setting("auth_user") or "admin",
    })

@app.route("/api/auth/setup", methods=["POST"])
def auth_setup():
    data = request.json
    username = data.get("username", "admin")
    password = data.get("password", "")
    if not password:
        return jsonify({"error": "Password required"}), 400
    set_setting("auth_user", username)
    set_setting("auth_hash", hash_password(password))
    session["authenticated"] = True
    session.permanent = True
    add_log(f"Auth configured for user: {username}")
    return jsonify({"ok": True})

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json
    username = data.get("username", "")
    password = data.get("password", "")
    stored_user = get_setting("auth_user")
    stored_hash = get_setting("auth_hash")
    if not stored_hash:
        session["authenticated"] = True
        return jsonify({"ok": True})
    if username == stored_user and hash_password(password) == stored_hash:
        session["authenticated"] = True
        session.permanent = True
        add_log(f"User {username} logged in")
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/change-password", methods=["POST"])
def auth_change_password():
    """Change password — requires current password verification."""
    data = request.json
    current = data.get("current", "")
    new_pw = data.get("password", "")
    if not current or not new_pw:
        return jsonify({"error": "Current and new password required"}), 400
    if len(new_pw) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400
    stored_hash = get_setting("auth_hash")
    if stored_hash and hash_password(current) != stored_hash:
        return jsonify({"error": "Current password is incorrect"}), 401
    set_setting("auth_hash", hash_password(new_pw))
    add_log("Password changed")
    return jsonify({"ok": True})


# ─── Probing ─────────────────────────────────────────────────────────────────
def find_ffprobe():
    for c in ["ffprobe", "tdarr-ffprobe", "/usr/bin/ffprobe", "/usr/local/bin/ffprobe"]:
        try:
            r = subprocess.run([c, "-version"], capture_output=True, timeout=5)
            if r.returncode == 0: return c
        except: continue
    return "ffprobe"

FFPROBE = find_ffprobe()

def probe_file(path):
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0: return None
        data = json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        log.warning(f"FFprobe timed out (30s): {os.path.basename(path)}")
        return None
    except: return None

    video = None
    audio_streams, sub_streams = [], []
    for s in data.get("streams", []):
        ct = s.get("codec_type", "")
        if ct == "video" and s.get("codec_name") in ("hevc","h264","av1","vp9") and not video:
            video = s
        elif ct == "audio": audio_streams.append(s)
        elif ct == "subtitle": sub_streams.append(s)
    if not video: return None

    ct = video.get("color_transfer", "")
    has_hdr10 = ct == "smpte2084"
    has_hlg = ct == "arib-std-b67"
    has_dovi, has_hdr10p, dovi_profile = False, False, None
    for sd in video.get("side_data_list", []):
        sdt = sd.get("side_data_type", "")
        if "DOVI" in sdt: has_dovi = True; dovi_profile = sd.get("dv_profile")
        if "HDR10+" in sdt: has_hdr10p = True

    hdr_type = "DoVi" if has_dovi else "HDR10+" if has_hdr10p else "HDR10" if has_hdr10 else "HLG" if has_hlg else "SDR"

    tags = video.get("tags", {})
    encoder = tags.get("ENCODER", tags.get("encoder", ""))
    already_transcoded = "nvenc" in encoder.lower()

    audio_summary = ""
    if audio_streams:
        a = audio_streams[0]
        codec, ch, prof = a.get("codec_name",""), a.get("channels",0), a.get("profile","")
        if "truehd" in codec: audio_summary = f"TrueHD Atmos {ch}ch" if ch >= 8 else f"TrueHD {ch}ch"
        elif "dts" in codec: audio_summary = f"DTS-HD MA {ch}ch" if "ma" in prof.lower() else f"DTS {ch}ch"
        elif codec == "flac": audio_summary = f"FLAC {ch}ch"
        elif codec == "ac3": audio_summary = f"DD {ch}ch"
        elif codec == "eac3": audio_summary = f"DDP {ch}ch"
        else: audio_summary = f"{codec} {ch}ch"

    fmt = data.get("format", {})
    dur = float(fmt.get("duration", 0))
    dur_min = dur / 60
    hours = int(dur_min // 60)
    mins = int(dur_min % 60)
    dur_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

    return {
        "file_size_gb": int(fmt.get("size", 0)) / (1024**3),
        "duration_min": dur_min, "duration_str": dur_str,
        "video_codec": video.get("codec_name", ""),
        "resolution": f"{video.get('width',0)}x{video.get('height',0)}",
        "fps": video.get("r_frame_rate", ""),
        "hdr_type": hdr_type, "has_dovi": has_dovi, "dovi_profile": dovi_profile,
        "audio_summary": audio_summary,
        "audio_track_count": len(audio_streams), "subtitle_track_count": len(sub_streams),
        "already_transcoded": already_transcoded, "probe_data": json.dumps(data),
    }


# ─── Library Scanner ─────────────────────────────────────────────────────────
def count_video_files(path):
    count = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                count += 1
    return count

def scan_library_task(library_id):
    global scan_progress
    with get_db() as db:
        lib = db.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
        if not lib: return
        db.execute("UPDATE libraries SET status = 'scanning' WHERE id = ?", (library_id,))
        settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
        min_size = float(settings.get("min_size_gb", "10"))
        skip_transcoded = settings.get("skip_transcoded", "true") == "true"
        # Build set of already-queued file paths to skip re-probing
        existing_paths = set(r[0] for r in db.execute("SELECT file_path FROM queue").fetchall())

    path = lib["path"]
    add_log(f"Scanning library: {lib['name']} ({path})")

    # Phase 1: Walk filesystem — collect files needing probe (fast, no I/O per file beyond stat)
    total_files = count_video_files(path)
    scan_progress[library_id] = {"total": total_files, "scanned": 0, "current_file": "Collecting files...", "eta": "calculating...", "status": "scanning"}
    start_time = time.time()

    files_to_probe = []
    files_undersized = []  # Files below min_size — will be added as skipped
    file_count, total_size, skipped = 0, 0, 0

    for root, dirs, files in os.walk(path):
        for filename in sorted(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in VIDEO_EXTENSIONS: continue
            filepath = os.path.join(root, filename)
            try: size_gb = os.path.getsize(filepath) / (1024**3)
            except OSError: continue

            file_count += 1
            total_size += size_gb

            # Update progress (fast phase)
            scan_progress[library_id]["scanned"] = file_count
            scan_progress[library_id]["current_file"] = filename

            if filepath in existing_paths: skipped += 1; continue
            if size_gb < min_size:
                files_undersized.append((filepath, filename, size_gb))
                continue

            files_to_probe.append((filepath, filename, size_gb))

    # Phase 2: Parallel probe — 6 workers for NAS I/O concurrency
    added = 0
    probe_total = len(files_to_probe)
    probe_done = 0
    scan_progress[library_id]["current_file"] = f"Probing {probe_total} new files..."
    scan_progress[library_id]["total"] = probe_total
    scan_progress[library_id]["scanned"] = 0

    def probe_one(args):
        fpath, fname, sgb = args
        try:
            info = probe_file(fpath)
            return (fpath, fname, sgb, info)
        except Exception as e:
            log.warning(f"Probe failed for {fname}: {e}")
            return (fpath, fname, sgb, None)

    probe_workers = min(6, max(1, probe_total))
    with ThreadPoolExecutor(max_workers=probe_workers) as executor:
        futures = {executor.submit(probe_one, f): f for f in files_to_probe}
        for future in as_completed(futures):
            fpath, fname, sgb, info = future.result()
            probe_done += 1

            elapsed = time.time() - start_time
            rate = probe_done / elapsed if elapsed > 0 else 1
            remaining = (probe_total - probe_done) / rate if rate > 0 else 0
            eta_min = remaining / 60
            eta_str = f"{int(eta_min)}m {int(remaining % 60)}s" if eta_min >= 1 else f"{int(remaining)}s"
            scan_progress[library_id] = {
                "total": probe_total, "scanned": probe_done,
                "current_file": fname, "eta": eta_str, "status": "scanning"
            }

            if not info: skipped += 1; continue
            if skip_transcoded and info["already_transcoded"]: skipped += 1; continue

            with get_db() as db:
                # Double-check not added by another scan
                exists = db.execute("SELECT id FROM queue WHERE file_path = ?", (fpath,)).fetchone()
                if exists: skipped += 1; continue
                max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
                db.execute("""INSERT INTO queue (
                    file_path, file_name, file_size_gb, duration_min, duration_str,
                    video_codec, resolution, fps, hdr_type, has_dovi,
                    dovi_profile, audio_summary, audio_track_count,
                    subtitle_track_count, library_id, library_name,
                    status, health_status, priority, probe_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', ?, ?)""",
                (fpath, fname, info["file_size_gb"], info["duration_min"], info["duration_str"],
                 info["video_codec"], info["resolution"], info["fps"],
                 info["hdr_type"], int(info["has_dovi"]), info["dovi_profile"],
                 info["audio_summary"], info["audio_track_count"],
                 info["subtitle_track_count"], library_id, lib["name"],
                 max_pri + 1, info["probe_data"]))
                added += 1

    # Phase 3: Insert undersized files as 'skipped' so they appear in the queue
    skipped_added = 0
    for fpath, fname, sgb in files_undersized:
        with get_db() as db:
            exists = db.execute("SELECT id FROM queue WHERE file_path = ?", (fpath,)).fetchone()
            if exists: continue
            max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
            db.execute("""INSERT INTO queue (
                file_path, file_name, file_size_gb, library_id, library_name,
                status, health_status, priority, skipped_reason
            ) VALUES (?, ?, ?, ?, ?, 'skipped', 'skipped', ?, ?)""",
            (fpath, fname, sgb, library_id, lib["name"],
             max_pri + 1, f"Below minimum file size ({min_size} GB)"))
            skipped_added += 1

    with get_db() as db:
        db.execute("UPDATE libraries SET file_count=?, total_size_gb=?, last_scanned=datetime('now','localtime'), status='scanned' WHERE id=?",
                   (file_count, total_size, library_id))

    scan_progress[library_id] = {"total": total_files, "scanned": total_files, "current_file": "", "eta": "done", "status": "complete"}
    add_log(f"Scan complete: {lib['name']} — {added} queued, {skipped_added} undersized, {skipped} skipped, {file_count} total")
    if get_setting("ntfy_on_scan") == "true":
        send_notification("Scan Complete", f"{lib['name']}: {added} new files queued, {file_count} total")


# ─── Health Check ────────────────────────────────────────────────────────────
def health_check_task():
    """Server-side cleanup only — actual health checks run on nodes (Tdarr-style).
    This only resets items stuck in 'checking' state if a node died mid-check."""
    with get_db() as db:
        # Reset items stuck in 'checking' for >5 minutes (node probably died)
        stuck = db.execute("""UPDATE queue SET health_status='pending', current_step=''
            WHERE health_status='checking'
            AND created_at < datetime('now','localtime', '-120 seconds')""")
        if stuck.rowcount > 0:
            add_log(f"Reset {stuck.rowcount} stuck health checks back to pending", level="WARN", db=db)

def start_health_check_loop():
    def loop():
        while True:
            try:
                health_check_task()
            except Exception as e:
                log.error(f"Health check error: {e}")

            # ── Auto-recovery: fix stuck states ──
            try:
                with get_db() as db:
                    # Reset libraries stuck in 'scanning' for > 30 minutes
                    stuck_libs = db.execute("""UPDATE libraries SET status='idle'
                        WHERE status='scanning' AND last_scanned IS NOT NULL
                        AND last_scanned < datetime('now','localtime', '-30 minutes')""")
                    if stuck_libs.rowcount > 0:
                        log.warning(f"Auto-reset {stuck_libs.rowcount} stuck library scans")
                        add_log(f"Auto-reset {stuck_libs.rowcount} stuck library scans (>30min)", level="WARN", db=db)

                    # Also reset libraries stuck with no last_scanned timestamp
                    stuck_libs2 = db.execute("""UPDATE libraries SET status='idle'
                        WHERE status='scanning' AND last_scanned IS NULL
                        AND created_at < datetime('now','localtime', '-30 minutes')""")
                    if stuck_libs2.rowcount > 0:
                        log.warning(f"Auto-reset {stuck_libs2.rowcount} stuck new library scans")

                    # Reset processing jobs with no progress update for > 2 hours — put back in queue
                    stuck_jobs = db.execute("""SELECT id, worker_id, file_name FROM queue
                        WHERE status='processing' AND started_at < datetime('now','localtime', '-7200 seconds')
                        AND (progress = 0 OR progress IS NULL)""").fetchall()
                    for sj in stuck_jobs:
                        db.execute("""UPDATE queue SET status='queued', health_status='healthy',
                            progress=0, current_step='', worker_id=NULL, started_at=NULL
                            WHERE id=?""", (sj["id"],))
                        if sj["worker_id"]:
                            db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=?", (sj["worker_id"],))
                        add_log(f"Auto-reset stuck job #{sj['id']}: {sj['file_name']} — back to queue", level="WARN", job_id=sj["id"], db=db)

                    # Clean up workers with stale heartbeats (> 5 minutes) that show as active
                    db.execute("""UPDATE workers SET status='idle', current_job_id=NULL
                        WHERE status='active' AND last_heartbeat < datetime('now','localtime', '-60 seconds')""")

                    # Auto-delete workers with no heartbeat for 30+ minutes (dead nodes)
                    deleted = db.execute("""DELETE FROM workers
                        WHERE last_heartbeat < datetime('now','localtime', '-1800 seconds')""")
                    if deleted.rowcount > 0:
                        log.info(f"Auto-deleted {deleted.rowcount} dead worker(s) (no heartbeat for 30+ min)")

            except Exception as e:
                log.error(f"Auto-recovery error: {e}")

            time.sleep(30)  # Server HC is fallback — nodes do primary health checking
    t = threading.Thread(target=loop, daemon=True)
    t.start()


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with get_db() as db:
        qs = db.execute("SELECT status, COUNT(*) as c, COALESCE(SUM(file_size_gb),0) as gb FROM queue GROUP BY status").fetchall()
        wc = db.execute("SELECT COUNT(*) as c FROM workers WHERE last_heartbeat > datetime('now','localtime','-60 seconds')").fetchone()["c"]
    return jsonify({"status": "running", "queue": {s["status"]: {"count": s["c"], "total_gb": s["gb"]} for s in qs}, "active_workers": wc, "version": "3.0.0"})

# Libraries
@app.route("/api/libraries", methods=["GET"])
@login_required
def api_list_libraries():
    with get_db() as db:
        libs = db.execute("SELECT * FROM libraries ORDER BY name").fetchall()
    result = []
    for l in libs:
        d = dict(l)
        d["scan_progress"] = scan_progress.get(l["id"])
        result.append(d)
    return jsonify(result)

@app.route("/api/libraries", methods=["POST"])
@login_required
def api_add_library():
    data = request.json
    name, path = data.get("name","").strip(), data.get("path","").strip()
    if not name or not path: return jsonify({"error": "Name and path required"}), 400
    if not os.path.isdir(path): return jsonify({"error": f"Path not found: {path}"}), 400
    with get_db() as db:
        try:
            db.execute("INSERT INTO libraries (name, path) VALUES (?, ?)", (name, path))
            lid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except sqlite3.IntegrityError:
            return jsonify({"error": "Path already exists"}), 409
    add_log(f"Library added: {name} ({path})")
    return jsonify({"id": lid}), 201

@app.route("/api/libraries/<int:lid>", methods=["DELETE"])
@login_required
def api_del_library(lid):
    with get_db() as db:
        # Only delete pending/queued items — preserve completed/error history
        db.execute("DELETE FROM queue WHERE library_id=? AND status IN ('pending','queued','skipped')", (lid,))
        db.execute("DELETE FROM libraries WHERE id=?", (lid,))
    return jsonify({"ok": True})

@app.route("/api/libraries/<int:lid>/scan", methods=["POST"])
@login_required
def api_scan_library(lid):
    threading.Thread(target=scan_library_task, args=(lid,), daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})

@app.route("/api/libraries/scan-all", methods=["POST"])
@login_required
def api_scan_all():
    with get_db() as db:
        libs = db.execute("SELECT id FROM libraries").fetchall()
    for lib in libs:
        threading.Thread(target=scan_library_task, args=(lib["id"],), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/libraries/<int:lid>/refresh", methods=["POST"])
@login_required
def api_refresh_library(lid):
    """Re-scan and update changed files — preserves job history."""
    with get_db() as db:
        # Remove ONLY pending/queued items whose files no longer exist
        # Completed/error items are history and should be preserved even if file was replaced
        items = db.execute("SELECT id, file_path FROM queue WHERE library_id=? AND status IN ('pending','queued','skipped')", (lid,)).fetchall()
        removed = 0
        for item in items:
            if not os.path.exists(item["file_path"]):
                db.execute("DELETE FROM queue WHERE id=?", (item["id"],))
                removed += 1
        if removed:
            add_log(f"Refresh: removed {removed} missing files from library #{lid}", db=db)
    # Then do a fresh scan to pick up new files
    threading.Thread(target=scan_library_task, args=(lid,), daemon=True).start()
    return jsonify({"ok": True, "removed": removed})

@app.route("/api/scan-progress")
@login_required
def api_scan_progress():
    return jsonify(scan_progress)

# Queue
@app.route("/api/queue", methods=["GET"])
@login_required
def api_list_queue():
    status = request.args.get("status")
    hdr = request.args.get("hdr_type")
    lib = request.args.get("library")
    sort = request.args.get("sort", "priority")
    search = request.args.get("search", "")

    q = "SELECT * FROM queue WHERE 1=1"
    p = []
    if status: q += " AND status=?"; p.append(status)
    if hdr: q += " AND hdr_type=?"; p.append(hdr)
    if lib: q += " AND library_name=?"; p.append(lib)
    if search: q += " AND file_name LIKE ?"; p.append(f"%{search}%")

    sorts = {"priority": "priority ASC,id ASC", "size-desc": "file_size_gb DESC", "size-asc": "file_size_gb ASC", "name": "file_name ASC", "hdr": "hdr_type ASC", "status": "status ASC,priority ASC", "queue-number": "id ASC"}
    q += f" ORDER BY {sorts.get(sort, 'priority ASC,id ASC')}"

    with get_db() as db:
        items = db.execute(q, p).fetchall()
    return jsonify([{k: v for k, v in dict(i).items() if k != "probe_data"} for i in items])

@app.route("/api/queue/<int:jid>/bump", methods=["POST"])
@login_required
def api_bump(jid):
    d = request.json.get("direction", "up")
    with get_db() as db:
        job = db.execute("SELECT * FROM queue WHERE id=?", (jid,)).fetchone()
        if not job or job["status"] not in ("queued","pending"): return jsonify({"error": "Cannot bump"}), 400
        if d == "up":
            swap = db.execute("SELECT * FROM queue WHERE status IN ('queued','pending') AND priority < ? ORDER BY priority DESC LIMIT 1", (job["priority"],)).fetchone()
        else:
            swap = db.execute("SELECT * FROM queue WHERE status IN ('queued','pending') AND priority > ? ORDER BY priority ASC LIMIT 1", (job["priority"],)).fetchone()
        if swap:
            db.execute("UPDATE queue SET priority=? WHERE id=?", (swap["priority"], jid))
            db.execute("UPDATE queue SET priority=? WHERE id=?", (job["priority"], swap["id"]))
    return jsonify({"ok": True})

@app.route("/api/queue/<int:jid>/cancel", methods=["POST"])
@login_required
def api_cancel(jid):
    with get_db() as db:
        job = db.execute("SELECT * FROM queue WHERE id=?", (jid,)).fetchone()
        if not job:
            return jsonify({"error": "Job not found"}), 404
        db.execute("UPDATE queue SET status='cancelled', error_message='Cancelled by user', progress=0, current_step='Cancelled' WHERE id=? AND status IN ('queued','processing','pending')", (jid,))
        # Free up the worker if it was processing this job
        if job["status"] == "processing" and job["worker_id"]:
            db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=?", (job["worker_id"],))
        # Set cancel flag for the node to pick up
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, 'true')", (f"cancel_{jid}",))
    add_log(f"Job #{jid} cancelled by user", job_id=jid)
    return jsonify({"ok": True})

@app.route("/api/queue/<int:jid>/skip", methods=["POST"])
@login_required
def api_skip(jid):
    reason = request.json.get("reason", "Marked as not needed")
    with get_db() as db:
        db.execute("UPDATE queue SET status='skipped', skipped_reason=? WHERE id=?", (reason, jid))
    return jsonify({"ok": True})

@app.route("/api/queue/<int:jid>/accept", methods=["POST"])
@login_required
def api_accept(jid):
    """Accept a completed transcode — replaces original with transcoded file if keep-both mode."""
    with get_db() as db:
        job = db.execute("SELECT * FROM queue WHERE id=?", (jid,)).fetchone()
        if not job:
            return jsonify({"error": "Job not found"}), 404
        db.execute("UPDATE queue SET accepted=1 WHERE id=?", (jid,))

        # If keep-both mode and output exists on NAS, replace original
        output_path = job["output_path"] if job["output_path"] else ""
        original_path = job["file_path"]
        if output_path and output_path != original_path and os.path.exists(output_path):
            try:
                # Safety: verify output is reasonable size
                out_size = os.path.getsize(output_path)
                if out_size > 1024:  # At least 1KB
                    base, _ = os.path.splitext(original_path)
                    final_path = base + os.path.splitext(output_path)[1]  # Keep output extension
                    if os.path.exists(original_path):
                        os.remove(original_path)
                        add_log(f"Accept: deleted original {os.path.basename(original_path)}", job_id=jid, db=db)
                    os.rename(output_path, final_path)
                    db.execute("UPDATE queue SET output_path=?, file_path=? WHERE id=?", (final_path, final_path, jid))
                    add_log(f"Accept: replaced with transcode {os.path.basename(final_path)} ({out_size/(1024**3):.2f} GB)", job_id=jid, db=db)
            except Exception as e:
                add_log(f"Accept: file replacement failed for #{jid}: {e}", level="ERROR", job_id=jid, db=db)
        elif output_path == original_path:
            # Replace mode — original already replaced by node, just mark accepted
            add_log(f"Accept: #{jid} already replaced", job_id=jid, db=db)
    return jsonify({"ok": True})

@app.route("/api/queue/<int:jid>", methods=["GET"])
def api_get_job(jid):
    with get_db() as db:
        job = db.execute("SELECT * FROM queue WHERE id=?", (jid,)).fetchone()
    if not job: return jsonify({"error": "Not found"}), 404
    return jsonify({k:v for k,v in dict(job).items() if k != "probe_data"})

@app.route("/api/queue/<int:jid>", methods=["DELETE"])
@login_required
def api_del_job(jid):
    with get_db() as db:
        db.execute("DELETE FROM queue WHERE id=?", (jid,))
    return jsonify({"ok": True})

@app.route("/api/queue/start", methods=["POST"])
@login_required
def api_start():
    set_setting("processing_enabled", "true")
    add_log("Processing started")
    return jsonify({"ok": True})

@app.route("/api/queue/pause", methods=["POST"])
@login_required
def api_pause():
    set_setting("processing_enabled", "false")
    add_log("Processing paused — state preserved")
    return jsonify({"ok": True})

@app.route("/api/queue/clear/<string:status>", methods=["POST"])
@login_required
def api_clear(status):
    if status not in ("complete","error","cancelled","skipped"): return jsonify({"error": "Invalid"}), 400
    with get_db() as db:
        db.execute("DELETE FROM queue WHERE status=?", (status,))
    return jsonify({"ok": True})

@app.route("/api/queue/skip-below", methods=["POST"])
@login_required
def api_skip_below_size():
    """Skip all queued files below a certain size."""
    size_gb = request.json.get("size_gb", 10)
    with get_db() as db:
        db.execute("UPDATE queue SET status='skipped', skipped_reason=? WHERE status IN ('queued','pending') AND file_size_gb < ?",
                   (f"Below {size_gb} GB threshold", size_gb))
        count = db.execute("SELECT changes()").fetchone()[0]
    add_log(f"Skipped {count} files below {size_gb} GB")
    return jsonify({"ok": True, "count": count})

@app.route("/api/queue/requeue-library", methods=["POST"])
@login_required
def api_requeue_library():
    """Requeue all errored/skipped/cancelled items for a library."""
    lid = request.json.get("library_id")
    with get_db() as db:
        count = 0
        if lid:
            db.execute("""UPDATE queue SET status='pending', health_status='pending',
                progress=0, current_step='', eta='', error_message=NULL,
                worker_id=NULL, started_at=NULL, completed_at=NULL
                WHERE library_id=? AND status IN ('error','skipped','cancelled')""", (lid,))
            count = db.execute("SELECT changes()").fetchone()[0]
        else:
            db.execute("""UPDATE queue SET status='pending', health_status='pending',
                progress=0, current_step='', eta='', error_message=NULL,
                worker_id=NULL, started_at=NULL, completed_at=NULL
                WHERE status IN ('error','skipped','cancelled')""")
            count = db.execute("SELECT changes()").fetchone()[0]
    add_log(f"Requeued {count} items" + (f" for library #{lid}" if lid else ""))
    return jsonify({"ok": True, "count": count})

@app.route("/api/queue/clear-library", methods=["POST"])
@login_required
def api_clear_library():
    """Clear pending/queued/skipped items for a library — preserves complete/error history."""
    lid = request.json.get("library_id")
    if not lid: return jsonify({"error": "library_id required"}), 400
    with get_db() as db:
        db.execute("DELETE FROM queue WHERE library_id=? AND status IN ('pending','queued','skipped')", (lid,))
        count = db.execute("SELECT changes()").fetchone()[0]
    add_log(f"Cleared {count} pending/queued items from library #{lid} (history preserved)")
    return jsonify({"ok": True, "count": count})

@app.route("/api/queue/<int:jid>/requeue", methods=["POST"])
@login_required
def api_requeue(jid):
    """Move errored/skipped/cancelled jobs back to pending for re-processing."""
    with get_db() as db:
        job = db.execute("SELECT * FROM queue WHERE id=?", (jid,)).fetchone()
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] not in ("error", "skipped", "cancelled", "complete"):
            return jsonify({"error": f"Cannot requeue job with status '{job['status']}'"}), 400
        db.execute("""UPDATE queue SET status='pending', health_status='pending',
            progress=0, current_step='', eta='', error_message=NULL,
            worker_id=NULL, started_at=NULL, completed_at=NULL,
            accepted=0, skipped_reason=NULL WHERE id=?""", (jid,))
    add_log(f"Job #{jid} requeued for re-processing ({job['file_name']})", job_id=jid)
    return jsonify({"ok": True})

@app.route("/api/queue/<int:jid>/bump-top", methods=["POST"])
@login_required
def api_bump_top(jid):
    """Move a job to the top of the queue (highest priority)."""
    with get_db() as db:
        min_pri = db.execute("SELECT MIN(priority) FROM queue WHERE status IN ('queued','pending')").fetchone()[0]
        if min_pri is not None:
            db.execute("UPDATE queue SET priority=? WHERE id=?", (min_pri - 1, jid))
    add_log(f"Job #{jid} bumped to top of queue", job_id=jid)
    return jsonify({"ok": True})

@app.route("/api/workers/pause-all", methods=["POST"])
@login_required
def api_pause_all():
    """Pause all processing — stops new jobs being assigned."""
    set_setting("processing_enabled", "false")
    add_log("All processing paused")
    return jsonify({"ok": True})

@app.route("/api/workers/resume-all", methods=["POST"])
@login_required
def api_resume_all():
    """Resume all processing."""
    set_setting("processing_enabled", "true")
    add_log("All processing resumed")
    return jsonify({"ok": True})

@app.route("/api/jobs/<int:jid>/check-cancel")
def api_check_cancel(jid):
    """Called by byte_node.py during transcode to check if job was cancelled."""
    val = get_setting(f"cancel_{jid}")
    if val == "true":
        with get_db() as db:
            db.execute("DELETE FROM settings WHERE key=?", (f"cancel_{jid}",))
        return jsonify({"cancel": True})
    return jsonify({"cancel": False})

# Feature 19: Job log streaming — node sends log lines in real-time
@app.route("/api/jobs/<int:jid>/log", methods=["POST"])
def api_job_log(jid):
    """Receive log lines from the node during transcode."""
    d = request.json
    lines = d.get("lines", [])
    if isinstance(lines, str):
        lines = [lines]
    for line in lines:
        level = "INFO"
        if "[ERROR]" in line or "FAILED" in line:
            level = "ERROR"
        elif "[WARN]" in line:
            level = "WARN"
        add_log(line, level=level, source="node", job_id=jid)
    return jsonify({"ok": True})

# Feature 20: Worker counts stored in settings (transcode_gpu_count etc.)
# Already handled by PUT /api/settings — no new endpoint needed

# Feature 21: Node schedule storage
@app.route("/api/workers/<string:wid>/schedule", methods=["GET"])
def api_get_schedule(wid):
    val = get_setting(f"worker_schedule_{wid}")
    if val:
        try: return jsonify(json.loads(val))
        except: pass
    # Default: all hours enabled
    return jsonify({f"{h:02d}-{(h+1)%24:02d}": True for h in range(24)})

@app.route("/api/workers/<string:wid>/schedule", methods=["POST"])
@login_required
def api_set_schedule(wid):
    d = request.json
    set_setting(f"worker_schedule_{wid}", json.dumps(d))
    add_log(f"Worker {wid} schedule updated")
    return jsonify({"ok": True})

# Feature 22: Server overview with OS stats
@app.route("/api/server/overview")
def api_server_overview():
    info = {"version": "3.0.0", "uptime_seconds": 0}
    try:
        import psutil
        mem = psutil.virtual_memory()
        proc = psutil.Process()
        info.update({
            "os_mem_used_gb": round(mem.used / (1024**3), 2),
            "os_mem_total_gb": round(mem.total / (1024**3), 2),
            "os_cpu_pct": psutil.cpu_percent(interval=0.1),
            "process_mem_mb": round(proc.memory_info().rss / (1024**2), 1),
        })
    except ImportError:
        pass  # psutil not installed
    except Exception as e:
        info["error"] = str(e)
    with get_db() as db:
        wc = db.execute("SELECT COUNT(*) as c FROM workers WHERE last_heartbeat > datetime('now','localtime','-300 seconds')").fetchone()["c"]
        qc = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='queued'").fetchone()["c"]
        pc = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='processing'").fetchone()["c"]
    info.update({"active_workers": wc, "queued": qc, "processing": pc,
                 "processing_enabled": get_setting("processing_enabled") == "true"})
    return jsonify(info)

# Enhanced progress endpoint with FPS, compression ratio, ETA
@app.route("/api/jobs/<int:jid>/progress-ext", methods=["POST"])
def api_progress_ext(jid):
    """Extended progress update with FPS, compression ratio, ETA."""
    d = request.json
    with get_db() as db:
        db.execute("""UPDATE queue SET progress=?, current_step=?, eta=? WHERE id=?""",
                   (d.get("progress", 0), d.get("step", ""), d.get("eta", ""), jid))
    return jsonify({"ok": True})

@app.route("/api/workers/<string:wid>/config", methods=["GET"])
def api_get_worker_config(wid):
    val = get_setting(f"worker_config_{wid}")
    if val:
        try: return jsonify(json.loads(val))
        except: pass
    return jsonify({})

@app.route("/api/workers/<string:wid>/config", methods=["POST"])
@login_required
def api_set_worker_config(wid):
    d = request.json
    set_setting(f"worker_config_{wid}", json.dumps(d))
    add_log(f"Worker {wid} config updated")
    return jsonify({"ok": True})

# Workers
@app.route("/api/workers", methods=["GET"])
def api_workers():
    with get_db() as db:
        ws = db.execute("""SELECT w.*, q.file_name as current_file, q.progress as job_progress,
            q.current_step, q.eta as job_eta, q.hdr_type, q.dovi_profile, q.file_size_gb
            FROM workers w LEFT JOIN queue q ON w.current_job_id=q.id ORDER BY w.registered_at""").fetchall()
    return jsonify([dict(w) for w in ws])

@app.route("/api/worker-counts")
def api_worker_counts():
    """Public endpoint — returns worker count settings for node startup. No auth required."""
    return jsonify({
        "transcode_gpu_count": get_setting("transcode_gpu_count") or "1",
        "transcode_cpu_count": get_setting("transcode_cpu_count") or "0",
        "healthcheck_gpu_count": get_setting("healthcheck_gpu_count") or "3",
        "healthcheck_cpu_count": get_setting("healthcheck_cpu_count") or "0",
        "max_dovi_concurrent": get_setting("max_dovi_concurrent") or "2",
    })

@app.route("/api/workers/register", methods=["POST"])
def api_register_worker():
    d = request.json
    wid = d.get("id", "")
    if not wid: return jsonify({"error": "ID required"}), 400
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO workers (id,name,host,gpu,status,last_heartbeat) VALUES (?,?,?,?,'idle',datetime('now','localtime'))",
                   (wid, d.get("name",""), d.get("host",""), d.get("gpu","")))
    add_log(f"Worker registered: {d.get('name','')} ({d.get('gpu','')})")
    return jsonify({"ok": True})

@app.route("/api/workers/heartbeat", methods=["POST"])
def api_heartbeat():
    d = request.json
    with get_db() as db:
        db.execute("UPDATE workers SET last_heartbeat=datetime('now','localtime'), cpu_usage=?, ram_usage=?, gpu_usage=?, vram_usage=? WHERE id=?",
                   (d.get("cpu", 0), d.get("ram", 0), d.get("gpu_usage", 0), d.get("vram", 0), d.get("id", "")))
    return jsonify({"ok": True})

@app.route("/api/workers/<string:wid>/reset-jobs", methods=["POST"])
def api_reset_worker_jobs(wid):
    """Reset any jobs stuck as 'processing' for this worker back to queued. Called by node on startup."""
    with get_db() as db:
        stuck = db.execute("""UPDATE queue SET status='queued', health_status='healthy',
            progress=0, current_step='', worker_id=NULL, started_at=NULL
            WHERE status='processing' AND worker_id=?""", (wid,))
        count = stuck.rowcount
        if count > 0:
            add_log(f"Node {wid} reconnected: reset {count} stuck jobs to queued", db=db)
        db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=?", (wid,))
    return jsonify({"ok": True, "reset": count})

# Jobs
@app.route("/api/jobs/next", methods=["POST"])
def api_next_job():
    d = request.json
    wid = d.get("worker_id", "")
    prefer_non_dovi = d.get("prefer_non_dovi", False)
    with get_db() as db:
        if get_setting("processing_enabled") != "true":
            return jsonify({"job": None, "reason": "Processing paused"})
        # Staged file limit — 0 means stop assigning new jobs
        staged_limit = int(get_setting("staged_limit") or "100")
        if staged_limit == 0:
            return jsonify({"job": None, "reason": "Staged limit is 0 — paused"})
        max_w = int(get_setting("max_workers") or "4")
        active = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='processing'").fetchone()["c"]
        if active >= max_w: return jsonify({"job": None, "reason": "Max workers reached"})
        if active >= staged_limit: return jsonify({"job": None, "reason": "Staged limit reached"})

        # DoVi concurrency limit — check server-side too
        max_dovi = int(get_setting("max_dovi_concurrent") or "2")
        active_dovi = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='processing' AND has_dovi=1").fetchone()["c"]
        dovi_full = active_dovi >= max_dovi

        # Atomic claim — try non-DoVi first if DoVi slots are full or node requests it
        claimed = False
        if dovi_full or prefer_non_dovi:
            # Try non-DoVi job first
            db.execute("""UPDATE queue SET status='processing', worker_id=?, started_at=datetime('now','localtime'),
                current_step='Starting...' WHERE id = (SELECT id FROM queue WHERE status='queued'
                AND health_status='healthy' AND has_dovi=0 ORDER BY priority ASC, id ASC LIMIT 1)""", (wid,))
            claimed = db.execute("SELECT changes()").fetchone()[0] > 0
            if not claimed and not dovi_full:
                # Node preferred non-DoVi but none available, and DoVi slots open — take a DoVi job
                db.execute("""UPDATE queue SET status='processing', worker_id=?, started_at=datetime('now','localtime'),
                    current_step='Starting...' WHERE id = (SELECT id FROM queue WHERE status='queued'
                    AND health_status='healthy' ORDER BY priority ASC, id ASC LIMIT 1)""", (wid,))
                claimed = db.execute("SELECT changes()").fetchone()[0] > 0

        if not claimed:
            # Normal claim — any job (DoVi slots available)
            db.execute("""UPDATE queue SET status='processing', worker_id=?, started_at=datetime('now','localtime'),
                current_step='Starting...' WHERE id = (SELECT id FROM queue WHERE status='queued'
                AND health_status='healthy' ORDER BY priority ASC, id ASC LIMIT 1)""", (wid,))
            if db.execute("SELECT changes()").fetchone()[0] == 0:
                if dovi_full:
                    return jsonify({"job": None, "reason": f"DoVi limit reached ({max_dovi}), no non-DoVi jobs ready"})
                return jsonify({"job": None, "reason": "No jobs ready"})

        job = db.execute("SELECT * FROM queue WHERE status='processing' AND worker_id=? ORDER BY id DESC LIMIT 1", (wid,)).fetchone()
        if not job: return jsonify({"job": None, "reason": "Claim failed"})
        db.execute("UPDATE workers SET status='active', current_job_id=? WHERE id=?", (job["id"], wid))
        settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
        jd = dict(job)
        jd["settings"] = settings
    add_log(f"Job #{job['id']} → {wid}: {job['file_name']}", job_id=job["id"])
    return jsonify({"job": jd})

@app.route("/api/jobs/next-healthcheck", methods=["POST"])
def api_next_healthcheck():
    """Claim a pending health check job atomically (prevents race condition with concurrent workers)."""
    d = request.json
    wid = d.get("worker_id", "")
    with get_db() as db:
        active_hc = db.execute("SELECT COUNT(*) as c FROM queue WHERE health_status='checking'").fetchone()["c"]
        if active_hc >= 6:
            return jsonify({"job": None, "reason": "Max health checks reached"})
        # Atomic claim — UPDATE with subquery prevents two workers claiming the same job
        db.execute("""UPDATE queue SET health_status='checking', current_step='Health check starting...'
            WHERE id = (SELECT id FROM queue WHERE status='pending' AND health_status='pending'
            ORDER BY priority ASC, id ASC LIMIT 1)""")
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            return jsonify({"job": None, "reason": "No pending health checks"})
        job = db.execute("SELECT * FROM queue WHERE health_status='checking' AND current_step='Health check starting...' ORDER BY id DESC LIMIT 1").fetchone()
        if not job:
            return jsonify({"job": None, "reason": "Claim failed"})
        jd = dict(job)
    return jsonify({"job": jd})

@app.route("/api/jobs/<int:jid>/health-result", methods=["POST"])
def api_health_result(jid):
    """Receive health check result from node."""
    d = request.json
    status = d.get("status", "healthy")
    error = d.get("error")
    with get_db() as db:
        if status == "healthy":
            db.execute("UPDATE queue SET health_status='healthy', status='queued', current_step='Health check passed' WHERE id=?", (jid,))
            add_log(f"[Health Check] PASSED: job #{jid}", source="healthcheck", job_id=jid, db=db)
        else:
            db.execute("UPDATE queue SET health_status=?, status='error', error_message=?, current_step='Health check failed' WHERE id=?",
                       (status, error, jid))
            add_log(f"[Health Check] FAILED: job #{jid} — {error}", level="ERROR", source="healthcheck", job_id=jid, db=db)
    return jsonify({"ok": True})

@app.route("/api/queue/<int:jid>/force-start", methods=["POST"])
@login_required
def api_force_start(jid):
    """Force-start a specific file, bypassing queue order."""
    with get_db() as db:
        job = db.execute("SELECT * FROM queue WHERE id=?", (jid,)).fetchone()
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] not in ("queued", "pending"):
            return jsonify({"error": f"Cannot start job with status '{job['status']}'"}), 400
        # Set priority to absolute top and mark as queued/healthy so node picks it up next
        min_pri = db.execute("SELECT MIN(priority) FROM queue").fetchone()[0] or 0
        db.execute("UPDATE queue SET priority=?, status='queued', health_status='healthy' WHERE id=?",
                   (min_pri - 100, jid))
    add_log(f"Force-started job #{jid}: {job['file_name']}", job_id=jid)
    return jsonify({"ok": True})

@app.route("/api/jobs/<int:jid>/progress", methods=["POST"])
def api_progress(jid):
    d = request.json
    with get_db() as db:
        # Always update progress and step
        db.execute("UPDATE queue SET progress=?, current_step=? WHERE id=?",
                   (d.get("progress",0), d.get("step",""), jid))
        # Only update ETA if non-empty (don't wipe last known good value)
        if d.get("eta"):
            db.execute("UPDATE queue SET eta=? WHERE id=?", (d["eta"], jid))
        # Only update FPS if non-empty
        if d.get("fps"):
            db.execute("UPDATE queue SET fps=? WHERE id=?", (d["fps"], jid))
        # Only update compression if non-zero
        if d.get("compression"):
            db.execute("UPDATE queue SET reduction_pct=? WHERE id=?", (d["compression"], jid))
    return jsonify({"ok": True})

@app.route("/api/jobs/<int:jid>/complete", methods=["POST"])
def api_complete(jid):
    d = request.json
    wid = d.get("worker_id", "")
    auto = get_setting("auto_accept") == "true"
    with get_db() as db:
        db.execute("UPDATE queue SET status='complete', progress=100, current_step='Done', output_path=?, output_size_gb=?, reduction_pct=?, completed_at=datetime('now','localtime'), accepted=? WHERE id=?",
                   (d.get("output_path",""), d.get("output_size_gb",0), d.get("reduction_pct",0), int(auto), jid))
    # Auto-accept: trigger file replacement immediately
    if auto:
        try:
            with app.test_request_context():
                api_accept(jid)
        except Exception as e:
            log.warning(f"Auto-accept file replacement failed for #{jid}: {e}")
        db.execute("UPDATE workers SET status='idle', current_job_id=NULL, jobs_completed=jobs_completed+1, total_saved_gb=total_saved_gb+? WHERE id=?",
                   (d.get("saved_gb",0), wid))
    add_log(f"Job #{jid} complete: {d.get('reduction_pct',0):.0f}% reduction", job_id=jid)
    if get_setting("ntfy_on_complete") == "true":
        with get_db() as db:
            fn = db.execute("SELECT file_name FROM queue WHERE id=?", (jid,)).fetchone()
        send_notification("Transcode Complete",
            f"{fn['file_name'] if fn else 'Job #'+str(jid)} — {d.get('reduction_pct',0):.0f}% reduction, saved {d.get('saved_gb',0):.1f} GB")
    return jsonify({"ok": True})

@app.route("/api/jobs/<int:jid>/error", methods=["POST"])
def api_error(jid):
    d = request.json
    wid = d.get("worker_id", "")
    with get_db() as db:
        db.execute("UPDATE queue SET status='error', progress=0, current_step='Failed', error_message=?, completed_at=datetime('now','localtime') WHERE id=?",
                   (d.get("error","Unknown"), jid))
        db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=?", (wid,))
    add_log(f"Job #{jid} failed: {d.get('error','')}", level="ERROR", job_id=jid)
    if get_setting("ntfy_on_error") == "true":
        with get_db() as db:
            fn = db.execute("SELECT file_name FROM queue WHERE id=?", (jid,)).fetchone()
        send_notification("Transcode Failed",
            f"{fn['file_name'] if fn else 'Job #'+str(jid)} — {d.get('error','')[:100]}", priority="high")
    return jsonify({"ok": True})

# Settings
@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    with get_db() as db:
        rows = db.execute("SELECT * FROM settings").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})

@app.route("/api/settings", methods=["PUT"])
@login_required
def api_put_settings():
    data = request.json
    # Track if min_size_gb changed for retroactive filtering
    old_min_size = get_setting("min_size_gb")
    for k, v in data.items():
        if k not in ("auth_hash",):  # Don't allow hash override via settings
            set_setting(k, v)

    # Retroactive min_size filter: move undersized queued/pending files to skipped
    new_min_size = data.get("min_size_gb")
    if new_min_size and new_min_size != old_min_size:
        try:
            threshold = float(new_min_size)
            if threshold > 0:
                with get_db() as db:
                    result = db.execute(
                        "UPDATE queue SET status='skipped', skipped_reason=? WHERE status IN ('queued','pending') AND file_size_gb < ?",
                        (f"Below minimum size threshold ({threshold} GB)", threshold))
                    moved = result.rowcount
                if moved > 0:
                    add_log(f"Min size changed to {threshold} GB — moved {moved} undersized files to Skipped")
        except (ValueError, TypeError):
            pass

    add_log(f"Settings updated")
    return jsonify({"ok": True})

# SSE Log Streaming — real-time log viewer for active jobs
@app.route("/api/jobs/<int:jid>/log-stream")
def api_log_stream(jid):
    """Stream log entries for a job via Server-Sent Events."""
    def generate():
        last_id = 0
        stale_count = 0
        while True:
            try:
                with get_db() as db:
                    # Check if job is still processing
                    job = db.execute("SELECT status FROM queue WHERE id=?", (jid,)).fetchone()
                    if not job or job["status"] not in ("processing", "pending"):
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                    # Fetch new log entries
                    rows = db.execute("SELECT * FROM logs WHERE job_id=? AND id>? ORDER BY id ASC LIMIT 50", (jid, last_id)).fetchall()
                for row in rows:
                    entry = dict(row)
                    last_id = entry["id"]
                    stale_count = 0
                    yield f"data: {json.dumps(entry)}\n\n"
                if not rows:
                    stale_count += 1
                    if stale_count > 300:  # 5 minutes no activity
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
            except Exception as e:
                log.warning(f"SSE stream error for job #{jid}: {e}")
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return
            time.sleep(1)
    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# Logs
@app.route("/api/logs", methods=["GET"])
@login_required
def api_logs():
    limit = request.args.get("limit", 200, type=int)
    jid = request.args.get("job_id", type=int)
    q = "SELECT * FROM logs WHERE 1=1"
    p = []
    if jid: q += " AND job_id=?"; p.append(jid)
    q += " ORDER BY id DESC LIMIT ?"; p.append(limit)
    with get_db() as db:
        rows = db.execute(q, p).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/logs/download")
@login_required
def api_download_logs():
    with get_db() as db:
        rows = db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 5000").fetchall()
    content = "\n".join(f"[{r['timestamp']}] [{r['level']}] [{r['source']}] {r['message']}" for r in rows)
    return Response(content, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=byte_transcode_logs.txt"})

# Dashboard
@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    with get_db() as db:
        ss = db.execute("SELECT status, COUNT(*) as c, COALESCE(SUM(file_size_gb),0) as gb FROM queue GROUP BY status").fetchall()
        hs = db.execute("SELECT hdr_type, COUNT(*) as c FROM queue WHERE status NOT IN ('complete','cancelled','skipped') GROUP BY hdr_type").fetchall()
        cs = db.execute("SELECT video_codec, COUNT(*) as c FROM queue WHERE video_codec != '' GROUP BY video_codec").fetchall()
        # Container format from file extension
        fs = db.execute("""SELECT CASE
            WHEN file_name LIKE '%.mkv' THEN 'MKV' WHEN file_name LIKE '%.mp4' THEN 'MP4'
            WHEN file_name LIKE '%.avi' THEN 'AVI' WHEN file_name LIKE '%.ts' THEN 'TS'
            WHEN file_name LIKE '%.m2ts' THEN 'M2TS' WHEN file_name LIKE '%.wmv' THEN 'WMV'
            WHEN file_name LIKE '%.mov' THEN 'MOV' WHEN file_name LIKE '%.webm' THEN 'WEBM'
            ELSE 'Other' END as container, COUNT(*) as c FROM queue GROUP BY container""").fetchall()
        ws = db.execute("""SELECT w.*, q.file_name as current_file, q.progress as job_progress,
            q.current_step, q.eta as job_eta, q.hdr_type, q.dovi_profile, q.file_size_gb, q.audio_summary
            FROM workers w LEFT JOIN queue q ON w.current_job_id=q.id
            WHERE w.last_heartbeat > datetime('now','localtime','-60 seconds')""").fetchall()
        saved = db.execute("SELECT COALESCE(SUM(file_size_gb-COALESCE(output_size_gb,file_size_gb)),0) as s FROM queue WHERE status='complete'").fetchone()
        processing = db.execute("SELECT * FROM queue WHERE status='processing' ORDER BY priority").fetchall()
        recent_complete = db.execute("SELECT * FROM queue WHERE status='complete' ORDER BY completed_at DESC LIMIT 10").fetchall()
        # Staging: health checks in progress + next queued files (Tdarr-style)
        health_checking = db.execute("SELECT * FROM queue WHERE health_status='checking' ORDER BY priority").fetchall()
        staged_limit = int(get_setting("staged_limit") or "100")
        next_queued = db.execute("SELECT * FROM queue WHERE status='queued' AND health_status='healthy' ORDER BY priority ASC, id ASC LIMIT ?", (staged_limit,)).fetchall()
        settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
    return jsonify({
        "status_stats": {s["status"]: {"count": s["c"], "total_gb": s["gb"]} for s in ss},
        "hdr_stats": {h["hdr_type"]: h["c"] for h in hs},
        "codec_stats": {c["video_codec"]: c["c"] for c in cs if c["video_codec"]},
        "container_stats": {f["container"]: f["c"] for f in fs if f["container"]},
        "workers": [dict(w) for w in ws],
        "processing": [{k:v for k,v in dict(p).items() if k!="probe_data"} for p in processing],
        "recent_complete": [{k:v for k,v in dict(c).items() if k!="probe_data"} for c in recent_complete],
        "health_checking": [{k:v for k,v in dict(h).items() if k!="probe_data"} for h in health_checking],
        "next_queued": [{k:v for k,v in dict(q).items() if k!="probe_data"} for q in next_queued],
        "saved_gb": saved["s"],
        "settings": settings,
        "processing_enabled": settings.get("processing_enabled","false") == "true",
        "scan_progress": dict(scan_progress),
    })

# Serve frontend
@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/<path:path>")
def serve_static(path):
    try: return send_from_directory(STATIC_DIR, path)
    except: return send_from_directory(STATIC_DIR, "index.html")


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Byte Transcode Server v2")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--reset-password", action="store_true", help="Reset admin password")
    args = parser.parse_args()

    init_db()

    if args.reset_password:
        import getpass
        pw = getpass.getpass("New password: ")
        set_setting("auth_hash", hash_password(pw))
        print("Password reset successfully.")
        return

    start_health_check_loop()
    log.info(f"Byte Transcode Server v3 on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)

if __name__ == "__main__":
    main()
