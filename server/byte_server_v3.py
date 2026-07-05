#!/usr/bin/env python3
"""
Byte Transcode Server v3.8
==========================
v3.5 — path mapping (server→node) settings + SubGen 'show all'
v3.6 — adds node_temp_path setting (Windows nodes need F:\\Byte_Engine_temp,
       not the Linux /temp/byte_work). Defaults remain empty so node auto-detects.
v3.7 — dual-node support: per-node overrides (worker_config_<id>) are now
       editable from the web UI worker cards and honored by node v2.7+
       (temp path, path mapping, worker counts per node). /api/server/overview
       reports the real server version instead of a hardcoded "3.0.0".
       The API key from Settings → API is now honored (X-API-Key header).
       All text-mode subprocess captures use encoding="utf-8" so scans no
       longer silently skip files with non-Latin track titles when the
       server runs on a non-UTF-8 locale (e.g. Windows).
v3.8 — Universal DV → P8 scan (flags every DV profile != 8, incl. P5) and
       the new Compatibility pipeline: scan-compat flags files with likely
       playback issues (bad codecs, Hi10P, 10-bit HEVC SDR, interlaced,
       non-MKV containers, subtitle overload), records clean files as
       skipped so any file can be force-converted via Requeue, notifies
       via ntfy, and queues 'compatfix' jobs (remux or NVENC re-encode to
       compat_target).

Run: python3 byte_server.py --port 5800
"""

import os, sys, json, time, sqlite3, hashlib, subprocess, threading, logging, secrets, functools
from datetime import datetime, timedelta
from contextlib import contextmanager
from flask import Flask, request, jsonify, send_from_directory, Response, session, redirect

SERVER_VERSION = "3.29"
NODE_VERSION = "2.22"   # fallback only; the update bell uses each connected node's reported version
# Where the update checker looks for the newest published versions.
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main/version.json"
DEFAULT_PORT = 5800
DB_PATH = os.environ.get("BYTE_DB_PATH", "/config/byte_transcode.db")
LOG_DIR = os.environ.get("BYTE_LOG_DIR", "/config/logs")
STATIC_DIR = os.environ.get("BYTE_STATIC_DIR", "/app/static")
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m2ts', '.mpg', '.mpeg'}

app = Flask(__name__, static_folder=STATIC_DIR)

def _session_secret():
    """
    v3.8 — persist the Flask session secret next to the DB. It used to be
    random on every boot (unless BYTE_SECRET was set), which invalidated
    all sessions and logged everyone out of the UI on each server restart.
    """
    env = os.environ.get("BYTE_SECRET")
    if env:
        return env
    secret_path = os.path.join(os.path.dirname(DB_PATH) or ".", ".byte_secret")
    try:
        if os.path.exists(secret_path):
            with open(secret_path, "r") as f:
                val = f.read().strip()
                if val:
                    return val
        val = secrets.token_hex(32)
        os.makedirs(os.path.dirname(secret_path) or ".", exist_ok=True)
        with open(secret_path, "w") as f:
            f.write(val)
        return val
    except Exception:
        return secrets.token_hex(32)

app.config["SECRET_KEY"] = _session_secret()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# v3.11 — do NOT sort JSON keys. The in-memory scan_progress dict is keyed by
# BOTH ints (transcode scans use library_id) and strings (other scans use
# "remuxclean_<id>" etc). Flask's default sort_keys=True tried to order those
# mixed keys during serialization and crashed the whole /api/dashboard endpoint
# with "'<' not supported between instances of 'str' and 'int'" — which made the
# dashboard show no stats/history at all once more than one scan type had run.
try:
    app.json.sort_keys = False
except Exception:
    pass

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
    # busy_timeout is set both via connect(timeout=) and the pragma —
    # some sqlite3 builds only honor one of them for lock waits (v3.7)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
            "auth_user": "admin", "auth_hash": "",
            # v3.2 — Subtitle Generation + AI Translation
            "claude_api_key": "",
            "claude_model": "claude-sonnet-4-6",
            "whisper_model": "large-v3",
            "whisper_device": "auto",
            "whisper_compute": "auto",
            "subgen_target_lang": "jpn",
            "subgen_translate_chunk": "40",
            # v3.9 — SubGen ensures BOTH English + target-lang text subs exist, creating
            # whichever is missing (English via Whisper; target via AI translation).
            # Embed new tracks via mkvmerge when quick; write external SRTs for big files.
            "subgen_embed": "auto",              # auto | always | never
            "subgen_embed_max_gb": "25",         # auto: embed if file <= this, else external SRT
            # v3.9 — provider-agnostic translation. Users pick any AI service and
            # supply their own key. 'openai_compatible' + translate_base_url covers
            # local Ollama/LM Studio/vLLM and OpenRouter/Together/DeepSeek/etc.
            # Empty translate_* falls back to the legacy claude_api_key/claude_model.
            # Default = Gemini 2.5 Flash (best quality-per-cost; free tier available).
            "translate_provider": "gemini",      # anthropic | openai | gemini | openai_compatible
            "translate_api_key": "",
            "translate_model": "gemini-2.5-flash",  # blank = provider default / legacy claude_model
            "translate_base_url": "",            # required for openai_compatible; optional override otherwise
            "translate_glossary": "",            # optional recurring names/terms/context for consistency
            # v3.4 — Per-job-type processing toggles. Each defaults to "true";
            # the master `processing_enabled` is the gatekeeper (must be "true"
            # for ANY job to process). When master is on, only job types with
            # their own flag set to "true" are claimed by workers.
            "processing_enabled_transcode": "true",
            "processing_enabled_subgen": "true",
            "processing_enabled_remuxclean": "true",
            "processing_enabled_dv78only": "true",
            # v3.8 — Compatibility pipeline (flag + fix playback-risk files)
            "processing_enabled_compatfix": "true",
            # v3.10 — languages kept by Audio/Track Cleanup and Compatibility
            # subtitle filtering (comma-separated ISO codes; und always kept)
            "keep_langs": "eng,jpn",
            # v3.9 — DV Profile 5 conversion mode: 'reencode' re-encodes the
            # IPTPQc2 base layer to real PQ HDR10 (correct on all devices);
            # 'relabel' is the old metadata-only pass (fast but leaves
            # purple/green output on non-DV playback paths — and in
            # practice on DV TVs too)
            "dv5_mode": "reencode",
            # Target codec for compat re-encodes: h264 = plays on everything,
            # hevc = smaller files, plays on most modern devices
            "compat_target": "h264",
            # Worker / concurrency counts (v3.0+; defaults surfaced v3.11).
            # transcode_gpu_count = concurrent transcode workers per node.
            # healthcheck_(gpu|cpu)_count = how many file health checks the
            # SERVER runs in parallel (CPU/ffprobe validation, not GPU work).
            "transcode_gpu_count": "1",
            "transcode_cpu_count": "0",
            "healthcheck_gpu_count": "3",
            "healthcheck_cpu_count": "0",
            # v3.5 — Server→Node path translation. The server stores library
            # paths from its own viewpoint (e.g. /media/data/media/movies because
            # /mnt/media is bind-mounted to /media inside Docker). The node runs
            # on Windows where it accesses the same files via SMB at Z:\data\media\.
            # Empty `node_path_local_prefix` disables translation (pass-through).
            "node_path_remote_prefix": "/media/",
            "node_path_local_prefix": "Z:\\",
            # v3.6 — Where the node writes temporary work files (HEVC dumps,
            # SRT extracts, audio chunks, etc.). Empty = node auto-detects:
            #   Windows → C:\\Byte_Engine_temp
            #   Linux   → /tmp/byte_work
            "node_temp_path": "F:\\Byte_Engine_temp",
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
                'skipped_reason': 'TEXT', 'probe_data': 'TEXT', 'progress': 'REAL DEFAULT 0', 'duration_str': 'TEXT DEFAULT ""',
                'job_type': "TEXT DEFAULT 'transcode'",
                # v3.17 — external submissions (Byte Media Manager etc.)
                'requested_by': 'TEXT', 'ext_issues': 'TEXT', 'ext_note': 'TEXT', 'file_size_bytes': 'INTEGER',
                # v3.19 — poison-job protection: auto-requeue attempt counter
                'attempts': 'INTEGER DEFAULT 0'}
            for col, typedef in queue_needed.items():
                if col not in queue_cols:
                    db.execute(f'ALTER TABLE queue ADD COLUMN {col} {typedef}')
                    log.info(f"Migration: added queue.{col}")
                    if col == 'job_type':
                        db.execute("UPDATE queue SET job_type='transcode' WHERE job_type IS NULL OR job_type=''")

            # v3.25 — workers.version so the update bell reflects each node's real
            # running version instead of a stale hardcoded server constant.
            try:
                worker_cols = [r[1] for r in db.execute('PRAGMA table_info(workers)').fetchall()]
                if 'version' not in worker_cols:
                    db.execute("ALTER TABLE workers ADD COLUMN version TEXT DEFAULT ''")
                    log.info("Migration: added workers.version")
            except Exception as _e:
                log.warning(f"workers.version migration skipped: {_e}")

            # v3.1 migration: file_path UNIQUE → (file_path, job_type) UNIQUE
            # so the same file can be queued for both transcode and remuxclean.
            try:
                table_sql_row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='queue'").fetchone()
                table_sql = (table_sql_row['sql'] if table_sql_row else '') or ''
                old_unique = 'file_path TEXT NOT NULL UNIQUE' in table_sql
                new_unique_present = 'UNIQUE (file_path, job_type)' in table_sql or 'UNIQUE(file_path, job_type)' in table_sql
                if old_unique and not new_unique_present:
                    log.info("Migration: rebuilding queue table with UNIQUE (file_path, job_type)")
                    cur_cols = [r[1] for r in db.execute('PRAGMA table_info(queue)').fetchall()]
                    db.execute("ALTER TABLE queue RENAME TO _queue_pre_v31")
                    db.executescript("""
                        CREATE TABLE queue (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            file_path TEXT NOT NULL,
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
                            job_type TEXT DEFAULT 'transcode',
                            requested_by TEXT,
                            ext_issues TEXT,
                            ext_note TEXT,
                            file_size_bytes INTEGER,
                            attempts INTEGER DEFAULT 0,
                            FOREIGN KEY (library_id) REFERENCES libraries(id),
                            UNIQUE (file_path, job_type)
                        );
                    """)
                    # Copy ALL old columns (job_type already added above; if somehow missing, default applies)
                    common = [c for c in cur_cols if c in (
                        'id','file_path','file_name','file_size_gb','duration_min','duration_str',
                        'video_codec','resolution','fps','hdr_type','has_dovi','dovi_profile',
                        'audio_summary','audio_track_count','subtitle_track_count','library_id','library_name',
                        'priority','status','health_status','progress','current_step','eta','worker_id',
                        'output_path','output_size_gb','reduction_pct','error_message','started_at',
                        'completed_at','created_at','accepted','skipped_reason','probe_data','job_type',
                        'requested_by','ext_issues','ext_note','file_size_bytes','attempts'
                    )]
                    cols_csv = ", ".join(common)
                    db.execute(f"INSERT INTO queue ({cols_csv}) SELECT {cols_csv} FROM _queue_pre_v31")
                    db.execute("DROP TABLE _queue_pre_v31")
                    log.info("Migration: queue UNIQUE constraint updated to (file_path, job_type)")
            except Exception as me:
                log.error(f"UNIQUE-migration error (continuing): {me}")

            worker_cols = [r[1] for r in db.execute('PRAGMA table_info(workers)').fetchall()]
            for col in ['cpu_usage','ram_usage','gpu_usage','vram_usage']:
                if col not in worker_cols:
                    db.execute(f'ALTER TABLE workers ADD COLUMN {col} REAL DEFAULT 0')
                    log.info(f"Migration: added workers.{col}")
            # Fix any NULL health statuses
            db.execute('UPDATE queue SET health_status="pending" WHERE health_status IS NULL')
            db.execute('UPDATE queue SET progress=0 WHERE progress IS NULL')
            db.execute("UPDATE queue SET job_type='transcode' WHERE job_type IS NULL OR job_type=''")
            # Reset stuck scanning libraries on startup
            stuck = db.execute("UPDATE libraries SET status='idle' WHERE status='scanning'")
            if stuck.rowcount > 0:
                log.warning(f"Reset {stuck.rowcount} stuck library scans on startup")
            # Reset stuck processing jobs on startup (worker probably died)
            stuck_jobs = db.execute("UPDATE queue SET status='error', error_message='Server restarted — job was interrupted' WHERE status='processing'")
            if stuck_jobs.rowcount > 0:
                log.warning(f"Reset {stuck_jobs.rowcount} stuck processing jobs on startup")
            db.execute("UPDATE workers SET status='idle', current_job_id=NULL")
    except Exception as e:
        log.error(f"Migration error: {e}")

def add_log(msg, level="INFO", source="server", job_id=None):
    try:
        with get_db() as db:
            db.execute("INSERT INTO logs (level, source, message, job_id) VALUES (?, ?, ?, ?)",
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
    # v3.8: retry on transient "database is locked" — short write bursts
    # (pipeline toggles, worker config saves) could 500 under scan load,
    # notably on Windows where AV can briefly hold the WAL file.
    for attempt in range(3):
        try:
            with get_db() as db:
                db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            return
        except sqlite3.OperationalError:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))


def active_lib_conditions():
    """
    v3.15 — per-tool library scoping. Setting active_lib_<jobtype> = a
    library id restricts that tool to only health-check / assign / process
    files from that one library; empty (default) = all libraries. Returns
    (sql, params): an AND-condition usable in the health-check and
    jobs/next queries. Types with no filter set are left unrestricted.
    """
    conds, params = [], []
    for jt in ("transcode", "subgen", "remuxclean", "dv78only", "compatfix"):
        lid = get_setting(f"active_lib_{jt}")
        if lid and str(lid).strip():
            try:
                lid_i = int(lid)
            except (TypeError, ValueError):
                continue
            # keep rows that are NOT (this type in a different library)
            conds.append("NOT (COALESCE(NULLIF(job_type,''),'transcode')=? AND library_id<>?)")
            params += [jt, lid_i]
    if not conds:
        return "", []
    return " AND " + " AND ".join(conds), params


def db_write_retry(fn, attempts=8, base_delay=0.4):
    """
    v3.11 — run a write callable, retrying on SQLite "database is locked".
    Multiple scan threads + health checks + the node all write to one SQLite
    file; under heavy scan load a lone insert could exceed busy_timeout and
    kill the whole scan thread mid-library. Retrying keeps scans alive so
    every file gets recorded. Returns fn()'s result, or raises after attempts.
    """
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == attempts - 1:
                raise
            time.sleep(base_delay * (i + 1))


# ── v3.13 — async progress/log buffering ─────────────────────────────────────
# The node POSTs progress ~1×/sec/job and logs many lines per job. Each of
# those was a synchronous UPDATE/INSERT competing for the single SQLite write
# lock against the health-check loop + scans. Under a big queue that made
# /api/jobs/<id>/progress take 7+ SECONDS, so the node blocked on every update,
# never made real progress, and the ghost-requeue loop reassigned its jobs.
# Now those endpoints write to an in-memory buffer and return instantly; a
# single background thread flushes to the DB every ~2s (progress is latest-
# wins, logs are appended in batch). This decouples node responsiveness from
# DB write contention entirely.
_progress_buffer = {}       # jid -> dict(progress, step, eta, fps, compression)
_progress_lock = threading.Lock()
_log_buffer = []            # list of (level, source, message, job_id)
_log_lock = threading.Lock()

def buffer_progress(jid, d):
    with _progress_lock:
        _progress_buffer[jid] = {
            "progress": d.get("progress", 0), "step": d.get("step", ""),
            "eta": d.get("eta", ""), "fps": d.get("fps", ""),
            "compression": d.get("compression", 0),
        }

def buffer_log(level, source, message, job_id):
    with _log_lock:
        _log_buffer.append((level, source, message, job_id))
        if len(_log_buffer) > 5000:      # hard cap so a runaway can't eat RAM
            del _log_buffer[:1000]

def _flush_buffers():
    # snapshot + clear under lock, then write outside the lock
    with _progress_lock:
        prog = list(_progress_buffer.items()); _progress_buffer.clear()
    with _log_lock:
        logs = _log_buffer[:]; _log_buffer.clear()
    if prog:
        def _wp():
            with get_db() as db:
                db.executemany(
                    "UPDATE queue SET progress=?, current_step=?, eta=?, fps=?, reduction_pct=? WHERE id=?",
                    [(v["progress"], v["step"], v["eta"], v["fps"], v["compression"], jid)
                     for jid, v in prog])
        try: db_write_retry(_wp)
        except Exception as e: log.error(f"progress flush failed: {e}")
    if logs:
        def _wl():
            with get_db() as db:
                db.executemany(
                    "INSERT INTO logs (level, source, message, job_id) VALUES (?, ?, ?, ?)", logs)
        try: db_write_retry(_wl)
        except Exception as e: log.error(f"log flush failed: {e}")

def start_buffer_flusher():
    def loop():
        while True:
            time.sleep(2)
            try:
                _flush_buffers()
            except Exception as e:
                log.error(f"buffer flush error: {e}")
    threading.Thread(target=loop, daemon=True, name="buffer-flusher").start()


# ─── Auth ────────────────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth_hash = get_setting("auth_hash")
        if not auth_hash:  # No password set = no auth required
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            # v3.7: the API key generated in Settings → API is now actually
            # honored (it was generated but never checked before)
            api_key = get_setting("api_key")
            if api_key and request.headers.get("X-API-Key") == api_key:
                return f(*args, **kwargs)
            if request.path.startswith("/api/workers") or request.path.startswith("/api/jobs"):
                # Node API calls use worker auth, not session
                return f(*args, **kwargs)
            # v3.4: nodes need to read settings (worker counts, claude_api_key, etc.)
            # GET-only — PUT still requires auth so users can't change settings without logging in
            if request.path == "/api/settings" and request.method == "GET":
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
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
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


# ─── Cleanup Analysis (RemuxClean) ───────────────────────────────────────────
KEEP_LANGS = {"eng", "en", "jpn", "ja", "und", ""}  # empty string = no lang tag = treat as und

# v3.10 — ISO 639-1/639-2(B/T) equivalence groups so the keep_langs setting
# accepts either form (e.g. "de" keeps ger+deu-tagged tracks too)
LANG_GROUPS = [
    {"en", "eng"}, {"ja", "jpn"}, {"de", "ger", "deu"}, {"fr", "fre", "fra"},
    {"es", "spa"}, {"it", "ita"}, {"pt", "por"}, {"ru", "rus"},
    {"zh", "chi", "zho"}, {"ko", "kor"}, {"nl", "dut", "nld"}, {"pl", "pol"},
    {"sv", "swe"}, {"no", "nor"}, {"da", "dan"}, {"fi", "fin"},
    {"hi", "hin"}, {"ar", "ara"}, {"tr", "tur"}, {"th", "tha"},
    {"vi", "vie"}, {"cs", "cze", "ces"}, {"el", "gre", "ell"},
    {"hu", "hun"}, {"ro", "rum", "ron"}, {"sk", "slo", "slk"},
]

def get_keep_langs():
    """
    v3.10 — languages to KEEP for cleanup filtering, from the keep_langs
    setting (comma-separated ISO codes, default "eng,jpn"). Undetermined
    and untagged tracks are always kept.
    """
    raw = (get_setting("keep_langs") or "eng,jpn").lower()
    langs = {"und", ""}
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        langs.add(tok)
        for group in LANG_GROUPS:
            if tok in group:
                langs.update(group)
                break
    return langs

DIRTY_NAME_KEYWORDS = [
    "blu-ray", "bluray", "blu ray", "uhd", "web-dl", "web dl", "webrip",
    "bdrip", "hdrip", "brrip", "remux", "1080p", "2160p", "720p", "4k",
    "x264", "x265", "hevc-", "ddp", "rarbg", "psa", "tigole", "cee",
    "subrip", "pgssub", "subtitleedit",
]

def is_dirty_track_name(name):
    """Detect if a track title is 'dirty' (contains source/release info or non-ASCII)."""
    if not name or not isinstance(name, str):
        return False
    # Non-ASCII characters (Cyrillic, CJK, etc. — these are foreign-language groups)
    if any(ord(c) > 127 for c in name):
        return True
    name_lower = name.lower()
    for kw in DIRTY_NAME_KEYWORDS:
        if kw in name_lower:
            return True
    # Excessive separators suggest junk like "Forced / Release Group / SUBRIP"
    if name.count("/") >= 2:
        return True
    if name.count(" - ") >= 4:
        return True
    return False

def analyze_for_cleanup(probe_json, keep_langs=None):
    """
    Given parsed ffprobe JSON, return cleanup analysis.
    Returns dict with: unwanted_audio, unwanted_subs, dirty_names,
    total_audio, total_subs, needs_cleanup (bool).
    v3.10: keep_langs is configurable via the keep_langs setting.
    """
    if not probe_json:
        return None
    keep = keep_langs if keep_langs is not None else KEEP_LANGS
    streams = probe_json.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    unwanted_audio = 0
    unwanted_subs = 0
    dirty_names = 0

    for s in audio:
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "und").lower()
        if lang not in keep:
            unwanted_audio += 1
        title = tags.get("title") or ""
        if is_dirty_track_name(title):
            dirty_names += 1

    # Safety net: if EVERY audio track is non-keep-lang, scanner shouldn't
    # mark this as needing cleanup unless there's exactly one — node will keep
    # the first audio track regardless. We still queue it because the user
    # presumably wants OTHER cleanup (e.g., subtitle filtering or name fixes).

    for s in subs:
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "und").lower()
        if lang not in keep:
            unwanted_subs += 1
        title = tags.get("title") or ""
        if is_dirty_track_name(title):
            dirty_names += 1

    return {
        "unwanted_audio": unwanted_audio,
        "unwanted_subs": unwanted_subs,
        "dirty_names": dirty_names,
        "total_audio": len(audio),
        "total_subs": len(subs),
        "needs_cleanup": (unwanted_audio + unwanted_subs + dirty_names) > 0,
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

    path = lib["path"]
    add_log(f"Scanning library: {lib['name']} ({path})")

    # Count total files first for progress
    total_files = count_video_files(path)
    scan_progress[library_id] = {"total": total_files, "scanned": 0, "current_file": "", "eta": "calculating...", "status": "scanning"}
    start_time = time.time()

    file_count, total_size, added, skipped = 0, 0, 0, 0
    ext_breakdown = {}
    walked_files = 0
    walked_skipped_ext = 0

    for root, dirs, files in os.walk(path):
        for filename in sorted(files):
            walked_files += 1
            ext = os.path.splitext(filename)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                walked_skipped_ext += 1
                continue
            ext_breakdown[ext] = ext_breakdown.get(ext, 0) + 1

            filepath = os.path.join(root, filename)
            try: size_gb = os.path.getsize(filepath) / (1024**3)
            except OSError: continue

            file_count += 1
            total_size += size_gb

            # Update progress
            elapsed = time.time() - start_time
            rate = file_count / elapsed if elapsed > 0 else 1
            remaining = (total_files - file_count) / rate if rate > 0 else 0
            eta_min = remaining / 60
            eta_str = f"{int(eta_min)}m {int(remaining % 60)}s" if eta_min >= 1 else f"{int(remaining)}s"
            scan_progress[library_id] = {
                "total": total_files, "scanned": file_count,
                "current_file": filename, "eta": eta_str, "status": "scanning"
            }

            if size_gb < min_size: skipped += 1; continue

            with get_db() as db:
                exists = db.execute("SELECT id FROM queue WHERE file_path = ?", (filepath,)).fetchone()
                if exists: skipped += 1; continue

            info = None
            try:
                info = probe_file(filepath)
            except Exception as e:
                log.warning(f"Probe failed for {filename}: {e}")
            if not info: skipped += 1; continue
            if skip_transcoded and info["already_transcoded"]: skipped += 1; continue

            with get_db() as db:
                # Set priority based on queue position
                max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
                db.execute("""INSERT INTO queue (
                    file_path, file_name, file_size_gb, duration_min, duration_str,
                    video_codec, resolution, fps, hdr_type, has_dovi,
                    dovi_profile, audio_summary, audio_track_count,
                    subtitle_track_count, library_id, library_name,
                    status, health_status, priority, probe_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', ?, ?)""",
                (filepath, filename, info["file_size_gb"], info["duration_min"], info["duration_str"],
                 info["video_codec"], info["resolution"], info["fps"],
                 info["hdr_type"], int(info["has_dovi"]), info["dovi_profile"],
                 info["audio_summary"], info["audio_track_count"],
                 info["subtitle_track_count"], library_id, lib["name"],
                 max_pri + 1, info["probe_data"]))
                added += 1

    with get_db() as db:
        db.execute("UPDATE libraries SET file_count=?, total_size_gb=?, last_scanned=datetime('now','localtime'), status='scanned' WHERE id=?",
                   (file_count, total_size, library_id))

    scan_progress[library_id] = {"total": total_files, "scanned": total_files, "current_file": "", "eta": "done", "status": "complete"}
    ext_summary = ", ".join(f"{e}:{n}" for e, n in sorted(ext_breakdown.items())) if ext_breakdown else "none"
    add_log(f"Scan complete: {lib['name']} — {added} queued, {skipped} skipped, {file_count} video files matched. "
            f"Walked {walked_files} files total ({walked_skipped_ext} non-video). Extensions: {ext_summary}")
    if get_setting("ntfy_on_scan") == "true":
        send_notification("Scan Complete", f"{lib['name']}: {added} new files queued, {file_count} total")


def scan_remuxclean_task(library_id):
    """
    Scan a library for files that need RemuxClean (track removal + name cleanup).
    Inserts queue entries with job_type='remuxclean'.
    A separate scan_progress key 'remuxclean_<lid>' is used so it doesn't
    collide with the regular transcode scan.
    """
    global scan_progress
    pkey = f"remuxclean_{library_id}"
    with get_db() as db:
        lib = db.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
        if not lib:
            return
    path = lib["path"]
    add_log(f"[RemuxClean Scan] {lib['name']} ({path})")

    # Cleanup is MKV-only (mkvmerge can only filter MKV containers cleanly)
    cleanup_exts = {'.mkv'}
    total_files = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            if os.path.splitext(f)[1].lower() in cleanup_exts:
                total_files += 1

    scan_progress[pkey] = {"total": total_files, "scanned": 0, "current_file": "",
                           "eta": "calculating...", "status": "scanning"}
    start_time = time.time()
    file_count, added, skipped, skipped_clean, skipped_dup = 0, 0, 0, 0, 0

    for root, dirs, files in os.walk(path):
        for filename in sorted(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in cleanup_exts:
                continue
            filepath = os.path.join(root, filename)
            try:
                size_gb = os.path.getsize(filepath) / (1024**3)
            except OSError:
                continue

            file_count += 1
            elapsed = time.time() - start_time
            rate = file_count / elapsed if elapsed > 0 else 1
            remaining = (total_files - file_count) / rate if rate > 0 else 0
            eta_str = (f"{int(remaining/60)}m {int(remaining%60)}s" if remaining >= 60 else f"{int(remaining)}s")
            scan_progress[pkey] = {
                "total": total_files, "scanned": file_count,
                "current_file": filename, "eta": eta_str, "status": "scanning"
            }

            # Skip if a remuxclean job already exists for this file (any status)
            with get_db() as db:
                exists = db.execute(
                    "SELECT id, status FROM queue WHERE file_path = ? AND job_type = 'remuxclean'",
                    (filepath,)).fetchone()
                if exists:
                    skipped_dup += 1
                    continue

            # Probe to determine if cleanup is needed
            probe_json = None
            try:
                cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
                       "-show_format", "-show_streams", filepath]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                if r.returncode == 0:
                    probe_json = json.loads(r.stdout)
            except Exception as e:
                log.warning(f"[RemuxClean Scan] FFprobe failed on {filename}: {e}")
                continue

            if not probe_json:
                skipped += 1
                continue

            cleanup = analyze_for_cleanup(probe_json, keep_langs=get_keep_langs())
            audio_count = (cleanup or {}).get("total_audio", 0)
            sub_count = (cleanup or {}).get("total_subs", 0)

            # Get duration + video info from probe for display
            fmt = probe_json.get("format", {})
            try:
                dur_min = float(fmt.get("duration", 0)) / 60
            except Exception:
                dur_min = 0
            dur_str = f"{int(dur_min//60)}h {int(dur_min%60)}m" if dur_min >= 60 else f"{int(dur_min)}m"
            v = next((s for s in probe_json.get("streams", []) if s.get("codec_type") == "video"), {})
            vcodec = v.get("codec_name", "")
            res = f"{v.get('width', 0)}x{v.get('height', 0)}"

            # v3.11 — record EVERY file, like the SubGen/Compatibility scans:
            # files needing cleanup queue as pending → health check → queued;
            # already-clean files are recorded as 'skipped' with a reason, so
            # the tab clearly shows every file was examined (not just missing).
            if not cleanup or not cleanup["needs_cleanup"]:
                status, health, current_step, reason = ("skipped", "healthy",
                    "No cleanup needed — already clean", "Already clean — requeue to force")
            else:
                note = []
                if cleanup["unwanted_audio"]:
                    note.append(f"{cleanup['unwanted_audio']} unwanted audio")
                if cleanup["unwanted_subs"]:
                    note.append(f"{cleanup['unwanted_subs']} unwanted subs")
                if cleanup["dirty_names"]:
                    note.append(f"{cleanup['dirty_names']} dirty names")
                status, health, current_step, reason = ("pending", "pending",
                    "Pending: " + ", ".join(note), None)

            def _do_insert(status=status, health=health, current_step=current_step, reason=reason,
                           filepath=filepath, filename=filename, size_gb=size_gb, dur_min=dur_min,
                           dur_str=dur_str, vcodec=vcodec, res=res, audio_count=audio_count,
                           sub_count=sub_count, probe_json=probe_json):
                with get_db() as db:
                    max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
                    db.execute("""INSERT INTO queue (
                        file_path, file_name, file_size_gb, duration_min, duration_str,
                        video_codec, resolution, hdr_type, audio_track_count, subtitle_track_count,
                        library_id, library_name, status, health_status, priority, probe_data,
                        job_type, current_step, skipped_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SDR', ?, ?, ?, ?, ?, ?, ?, ?, 'remuxclean', ?, ?)""",
                    (filepath, filename, size_gb, dur_min, dur_str,
                     vcodec, res, audio_count, sub_count,
                     library_id, lib["name"], status, health, max_pri + 1, json.dumps(probe_json),
                     current_step, reason))
            try:
                db_write_retry(_do_insert)
                if status == "skipped":
                    skipped_clean += 1
                else:
                    added += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1

    scan_progress[pkey] = {"total": total_files, "scanned": total_files,
                           "current_file": "", "eta": "done", "status": "complete"}
    add_log(f"[RemuxClean Scan] {lib['name']} — {added} queued, "
            f"{skipped_clean} recorded clean (no work needed), {skipped_dup} already in queue, "
            f"{skipped} probe-failed, {file_count} total MKVs")
    if get_setting("ntfy_on_scan") == "true":
        send_notification("Cleanup Scan Complete",
            f"{lib['name']}: {added} files queued for cleanup ({skipped_clean} already clean)")


# ─── DV7→8 Only Scan ─────────────────────────────────────────────────────────
def analyze_for_dv78(probe_json):
    """Detect DoVi Profile 7 files. Returns dict with profile + has_dovi info."""
    if not probe_json:
        return None
    streams = probe_json.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("codec_name") in ("hevc", "h264")), None)
    if not video:
        return None

    has_dovi = False
    dovi_profile = None
    for sd in video.get("side_data_list", []):
        sdt = sd.get("side_data_type", "")
        if "DOVI" in sdt:
            has_dovi = True
            dovi_profile = sd.get("dv_profile")
            break

    # v3.8 — any DV profile other than 8 needs conversion (P7 dual-layer,
    # P5 IPTPQc2 "purple and green" on unsupported devices, P4, ...).
    try:
        prof_num = int(dovi_profile) if dovi_profile is not None else None
    except (TypeError, ValueError):
        prof_num = None
    return {
        "has_dovi": has_dovi,
        "dovi_profile": dovi_profile,
        "needs_conversion": has_dovi and prof_num is not None and prof_num != 8,
    }

def scan_dv78only_task(library_id):
    """
    Scan a library for Dolby Vision files needing P8 conversion. Queues
    'dv78only' jobs. These are fast jobs — HEVC extraction + profile
    conversion + remux, no re-encoding.
    """
    global scan_progress
    pkey = f"dv78only_{library_id}"
    with get_db() as db:
        lib = db.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
        if not lib:
            return
    path = lib["path"]
    add_log(f"[DV→P8 Scan] {lib['name']} ({path})")

    # v3.8: DV P5 WEB-DLs commonly ship as .mp4 — scanning only .mkv missed
    # them entirely. mkvmerge reads MP4 fine and the output is always MKV.
    cleanup_exts = {'.mkv', '.mp4', '.m4v'}
    total_files = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            if os.path.splitext(f)[1].lower() in cleanup_exts:
                total_files += 1

    scan_progress[pkey] = {"total": total_files, "scanned": 0, "current_file": "",
                           "eta": "calculating...", "status": "scanning"}
    start_time = time.time()
    file_count, added, skipped_dup, skipped_no_p7, skipped_probe = 0, 0, 0, 0, 0

    for root, dirs, files in os.walk(path):
        for filename in sorted(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in cleanup_exts:
                continue
            filepath = os.path.join(root, filename)
            try:
                size_gb = os.path.getsize(filepath) / (1024**3)
            except OSError:
                continue

            file_count += 1
            elapsed = time.time() - start_time
            rate = file_count / elapsed if elapsed > 0 else 1
            remaining = (total_files - file_count) / rate if rate > 0 else 0
            eta_str = (f"{int(remaining/60)}m {int(remaining%60)}s" if remaining >= 60 else f"{int(remaining)}s")
            scan_progress[pkey] = {
                "total": total_files, "scanned": file_count,
                "current_file": filename, "eta": eta_str, "status": "scanning"
            }

            with get_db() as db:
                exists = db.execute(
                    "SELECT id FROM queue WHERE file_path = ? AND job_type = 'dv78only'",
                    (filepath,)).fetchone()
                if exists:
                    skipped_dup += 1
                    continue

            probe_json = None
            try:
                cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
                       "-show_format", "-show_streams", filepath]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                if r.returncode == 0:
                    probe_json = json.loads(r.stdout)
            except Exception as e:
                log.warning(f"[DV7→8 Scan] FFprobe failed on {filename}: {e}")
                skipped_probe += 1
                continue

            if not probe_json:
                skipped_probe += 1
                continue

            dv = analyze_for_dv78(probe_json) or {}

            fmt = probe_json.get("format", {})
            try:
                dur_min = float(fmt.get("duration", 0)) / 60
            except Exception:
                dur_min = 0
            dur_str = f"{int(dur_min//60)}h {int(dur_min%60)}m" if dur_min >= 60 else f"{int(dur_min)}m"
            v = next((s for s in probe_json.get("streams", []) if s.get("codec_type") == "video"), {})
            vcodec = v.get("codec_name", "")
            res = f"{v.get('width', 0)}x{v.get('height', 0)}"
            src_profile = dv.get("dovi_profile")
            has_dovi = 1 if dv.get("has_dovi") else 0

            # v3.11 — record EVERY file: files needing conversion queue as
            # pending → health check → queued; files already at Profile 8 or
            # with no Dolby Vision are recorded as 'skipped' with a reason so
            # the tab shows the whole library was examined.
            if not dv.get("needs_conversion"):
                if has_dovi:
                    reason = f"Already DV Profile {src_profile} — no conversion needed"
                else:
                    reason = "No Dolby Vision"
                status, health, current_step, skipped_reason, hdr = (
                    "skipped", "healthy", reason, reason + " (requeue to force)",
                    ("DoVi" if has_dovi else "SDR"))
            else:
                status, health, current_step, skipped_reason, hdr = (
                    "pending", "pending", f"Pending: DV Profile {src_profile} → 8 conversion",
                    None, "DoVi")

            def _do_insert(status=status, health=health, current_step=current_step,
                           skipped_reason=skipped_reason, hdr=hdr, has_dovi=has_dovi,
                           src_profile=src_profile, filepath=filepath, filename=filename,
                           size_gb=size_gb, dur_min=dur_min, dur_str=dur_str, vcodec=vcodec,
                           res=res, probe_json=probe_json):
                with get_db() as db:
                    max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
                    db.execute("""INSERT INTO queue (
                        file_path, file_name, file_size_gb, duration_min, duration_str,
                        video_codec, resolution, hdr_type, has_dovi, dovi_profile,
                        library_id, library_name, status, health_status, priority, probe_data,
                        job_type, current_step, skipped_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dv78only', ?, ?)""",
                    (filepath, filename, size_gb, dur_min, dur_str,
                     vcodec, res, hdr, has_dovi, src_profile, library_id, lib["name"],
                     status, health, max_pri + 1, json.dumps(probe_json),
                     current_step, skipped_reason))
            try:
                db_write_retry(_do_insert)
                if status == "skipped":
                    skipped_no_p7 += 1
                else:
                    added += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1

    scan_progress[pkey] = {"total": total_files, "scanned": total_files,
                           "current_file": "", "eta": "done", "status": "complete"}
    add_log(f"[DV→P8 Scan] {lib['name']} — {added} queued, "
            f"{skipped_no_p7} recorded already-P8/no-DV, {skipped_dup} already in queue, "
            f"{skipped_probe} probe-failed, {file_count} total MKVs")


# ─── Compatibility Scan (v3.8) ───────────────────────────────────────────────
# Flags files likely to have playback problems on TVs / streaming clients
# and queues 'compatfix' jobs. Every scanned file is recorded (like SubGen):
# flagged files go in as pending, clean files as skipped — so any file can
# be force-converted later via Requeue even if the heuristics passed it.

COMPAT_OK_CONTAINERS = {'.mkv', '.mp4', '.m4v'}
# v3.21 — AV1 and VP9 removed: modern TVs/clients direct-play them and they're
# already efficient, so flagging them just wasted worker time re-encoding fine
# files. Only genuinely legacy/problematic codecs remain.
COMPAT_BAD_VCODECS = {'vc1', 'mpeg2video', 'mpeg4', 'msmpeg4v3', 'msmpeg4v2',
                      'wmv1', 'wmv2', 'wmv3', 'vp8', 'h263', 'mjpeg'}
COMPAT_MAX_SUB_TRACKS = 12

def analyze_for_compat(probe_json, ext):
    """
    Decide whether a file risks playback issues and how to fix it.
    Returns {"flag": bool, "reasons": [str], "strategy": 'remux'|'reencode',
             "deinterlace": bool, "filter_subs": bool}.
    """
    reasons = []
    strategy = None
    deinterlace = False
    filter_subs = False

    streams = probe_json.get("streams", []) if probe_json else []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    if ext not in COMPAT_OK_CONTAINERS:
        reasons.append(f"container {ext} may not direct-play")
        strategy = strategy or "remux"

    if video:
        vcodec = (video.get("codec_name") or "").lower()
        pix = (video.get("pix_fmt") or "").lower()
        transfer = (video.get("color_transfer") or "").lower()
        field = (video.get("field_order") or "").lower()
        is_10bit = "10le" in pix or "10be" in pix or "p010" in pix
        # Dolby Vision (esp. P5/IPTPQc2) may not report a PQ transfer —
        # treat any DOVI side data as HDR so DV files aren't misflagged
        # as "10-bit HEVC SDR" (the DV → P8 pipeline owns those).
        has_dovi_sd = any("DOVI" in (sd.get("side_data_type") or "")
                          for sd in video.get("side_data_list", []))
        is_hdr = transfer in ("smpte2084", "arib-std-b67") or has_dovi_sd

        if vcodec in COMPAT_BAD_VCODECS:
            reasons.append(f"video codec {vcodec} not widely supported")
            strategy = "reencode"
        if vcodec == "h264" and is_10bit:
            reasons.append("10-bit H.264 (Hi10P) — unplayable on most devices")
            strategy = "reencode"
        if vcodec == "hevc" and is_10bit and not is_hdr:
            reasons.append("10-bit HEVC SDR — known playback issues on some TV clients")
            strategy = "reencode"
        if field in ("tt", "bb", "tb", "bt"):
            reasons.append("interlaced video")
            strategy = "reencode"
            deinterlace = True

    if len(subs) > COMPAT_MAX_SUB_TRACKS:
        reasons.append(f"{len(subs)} subtitle tracks — can stall some players")
        strategy = strategy or "remux"
        filter_subs = True

    return {
        "flag": bool(reasons),
        "reasons": reasons,
        # Requeued clean files get a full re-encode — the safest "make it
        # play anywhere" treatment when heuristics found nothing specific.
        "strategy": strategy or "reencode",
        "deinterlace": deinterlace,
        "filter_subs": filter_subs,
    }

def scan_compat_task(library_id):
    """Scan a library for playback-compatibility risks. Queues 'compatfix' jobs."""
    global scan_progress
    pkey = f"compatfix_{library_id}"
    with get_db() as db:
        lib = db.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
        if not lib:
            return
    path = lib["path"]
    add_log(f"[Compat Scan] {lib['name']} ({path})")

    total_files = count_video_files(path)
    scan_progress[pkey] = {"total": total_files, "scanned": 0, "current_file": "",
                           "eta": "calculating...", "status": "scanning"}
    start_time = time.time()
    file_count, flagged, recorded_ok, skipped_dup, skipped_probe = 0, 0, 0, 0, 0

    for root, dirs, files in os.walk(path):
        for filename in sorted(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            filepath = os.path.join(root, filename)
            try:
                size_gb = os.path.getsize(filepath) / (1024**3)
            except OSError:
                continue

            file_count += 1
            elapsed = time.time() - start_time
            rate = file_count / elapsed if elapsed > 0 else 1
            remaining = (total_files - file_count) / rate if rate > 0 else 0
            eta_str = (f"{int(remaining/60)}m {int(remaining%60)}s" if remaining >= 60 else f"{int(remaining)}s")
            scan_progress[pkey] = {"total": total_files, "scanned": file_count,
                                   "current_file": filename, "eta": eta_str, "status": "scanning"}

            with get_db() as db:
                exists = db.execute(
                    "SELECT id FROM queue WHERE file_path = ? AND job_type = 'compatfix'",
                    (filepath,)).fetchone()
                if exists:
                    skipped_dup += 1
                    continue

            probe_json = None
            try:
                cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
                       "-show_format", "-show_streams", filepath]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                if r.returncode == 0:
                    probe_json = json.loads(r.stdout)
            except Exception as e:
                log.warning(f"[Compat Scan] FFprobe failed on {filename}: {e}")
            if not probe_json:
                skipped_probe += 1
                continue

            compat = analyze_for_compat(probe_json, ext)
            # Stash the decision inside probe_data so the node knows the
            # strategy without any schema change.
            probe_json["_compat"] = compat

            fmt = probe_json.get("format", {})
            try:
                dur_min = float(fmt.get("duration", 0)) / 60
            except Exception:
                dur_min = 0
            dur_str = f"{int(dur_min//60)}h {int(dur_min%60)}m" if dur_min >= 60 else f"{int(dur_min)}m"
            v = next((s for s in probe_json.get("streams", []) if s.get("codec_type") == "video"), {})
            vcodec = v.get("codec_name", "")
            res = f"{v.get('width', 0)}x{v.get('height', 0)}"

            # v3.8: record the REAL HDR type — the node's compat handler
            # refuses to SDR-re-encode anything that isn't SDR, and that
            # guard reads this column. Hardcoding 'SDR' here previously let
            # a forced compat job flatten a DV file to SDR H.264.
            v_transfer = (v.get("color_transfer") or "").lower()
            v_dovi = any("DOVI" in (sd.get("side_data_type") or "") for sd in v.get("side_data_list", []))
            hdr_type = ("DoVi" if v_dovi else
                        "HDR10" if v_transfer == "smpte2084" else
                        "HLG" if v_transfer == "arib-std-b67" else "SDR")

            if compat["flag"]:
                # v3.10 — strategy label lives in skipped_reason so it survives
                # the health check overwriting current_step; the UI renders it
                # as a persistent badge (container rewrap vs re-encode).
                strategy_label = ("CONTAINER-ONLY REWRAP — media untouched"
                                  if compat["strategy"] == "remux" else "VIDEO RE-ENCODE")
                status, current_step, skipped_reason = "pending", "Pending [" + compat["strategy"] + "]: " + "; ".join(compat["reasons"]), strategy_label
            else:
                status, current_step, skipped_reason = "skipped", "No compatibility issues detected", "No issues — requeue to force-convert"

            def _do_insert(status=status, current_step=current_step, skipped_reason=skipped_reason,
                           hdr_type=hdr_type, filepath=filepath, filename=filename, size_gb=size_gb,
                           dur_min=dur_min, dur_str=dur_str, vcodec=vcodec, res=res, probe_json=probe_json):
                with get_db() as db:
                    max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
                    db.execute("""INSERT INTO queue (
                        file_path, file_name, file_size_gb, duration_min, duration_str,
                        video_codec, resolution, hdr_type, library_id, library_name,
                        status, health_status, priority, probe_data, job_type,
                        current_step, skipped_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'compatfix', ?, ?)""",
                    (filepath, filename, size_gb, dur_min, dur_str, vcodec, res, hdr_type,
                     library_id, lib["name"], status,
                     "pending" if status == "pending" else "healthy",
                     max_pri + 1, json.dumps(probe_json), current_step, skipped_reason))
            try:
                db_write_retry(_do_insert)
                if status == "pending":
                    flagged += 1
                else:
                    recorded_ok += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1

    scan_progress[pkey] = {"total": total_files, "scanned": total_files,
                           "current_file": "", "eta": "done", "status": "complete"}
    add_log(f"[Compat Scan] {lib['name']} — {flagged} flagged with possible playback issues, "
            f"{recorded_ok} clean (requeue to force), {skipped_dup} already recorded, "
            f"{skipped_probe} probe-failed, {file_count} files")
    if flagged > 0:
        send_notification("Playback issues found",
            f"{lib['name']}: {flagged} file(s) flagged with possible playback issues. "
            f"Review the Compatibility tab and start the pipeline to convert them.")
    elif get_setting("ntfy_on_scan") == "true":
        send_notification("Compatibility Scan Complete", f"{lib['name']}: no issues found")


# ─── Subtitle Generation Scan ────────────────────────────────────────────────
def has_subtitle_lang(probe_json, target_langs):
    """
    Returns True if any subtitle stream has language in target_langs set.
    Distinguishes text-based subtitles (subrip, ass, etc.) from PGS/image subs.
    """
    if not probe_json:
        return False
    target_langs = set(l.lower() for l in target_langs)
    for s in probe_json.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        if lang in target_langs:
            return True
    return False

def has_text_subtitle_lang(probe_json, target_langs):
    """Returns True if a TEXT-based subtitle exists in target_langs (not PGS)."""
    if not probe_json:
        return False
    target_langs = set(l.lower() for l in target_langs)
    text_codecs = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
    for s in probe_json.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        codec = (s.get("codec_name") or "").lower()
        if codec not in text_codecs:
            continue
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        if lang in target_langs:
            return True
    return False

def scan_subgen_task(library_id):
    """
    Scan a library for files needing Japanese subtitle generation.

    v3.5 behavior change: ALL video files are recorded.
      - Files MISSING Japanese subs → queued as 'pending' (real subgen jobs)
      - Files that already have Japanese audio or text subs → recorded as 'skipped'
        with a `skipped_reason` tag like "Has Japanese subs" or "Has Japanese audio".
      This way the AI Subtitles tab reflects the FULL library; the user can see
      which files have JP already (in the Skipped tab) and which still need work.
    """
    global scan_progress
    pkey = f"subgen_{library_id}"
    with get_db() as db:
        lib = db.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
        if not lib:
            return
    path = lib["path"]
    add_log(f"[SubGen Scan] {lib['name']} ({path})")

    target_lang = (get_setting("subgen_target_lang") or "jpn").lower()
    has_target_set = {"jpn", "ja"} if target_lang in ("jpn", "ja") else {target_lang}

    # All video extensions count for subtitle generation. Keep this in sync with VIDEO_EXTENSIONS at top of file.
    video_exts = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m2ts', '.mpg', '.mpeg'}
    total_files = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            if os.path.splitext(f)[1].lower() in video_exts:
                total_files += 1

    scan_progress[pkey] = {"total": total_files, "scanned": 0, "current_file": "",
                           "eta": "calculating...", "status": "scanning"}
    start_time = time.time()
    file_count = 0
    queued = 0           # Real subgen jobs (need translation)
    auto_skipped = 0     # Files with JP already → recorded as skipped
    skipped_dup = 0      # Already in queue
    skipped_probe = 0    # Probe failed
    ext_breakdown = {}

    for root, dirs, files in os.walk(path):
        for filename in sorted(files):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in video_exts:
                continue
            ext_breakdown[ext] = ext_breakdown.get(ext, 0) + 1
            filepath = os.path.join(root, filename)
            try:
                size_gb = os.path.getsize(filepath) / (1024**3)
            except OSError:
                continue

            file_count += 1
            elapsed = time.time() - start_time
            rate = file_count / elapsed if elapsed > 0 else 1
            remaining = (total_files - file_count) / rate if rate > 0 else 0
            eta_str = (f"{int(remaining/60)}m {int(remaining%60)}s" if remaining >= 60 else f"{int(remaining)}s")
            scan_progress[pkey] = {
                "total": total_files, "scanned": file_count,
                "current_file": filename, "eta": eta_str, "status": "scanning"
            }

            with get_db() as db:
                exists = db.execute(
                    "SELECT id FROM queue WHERE file_path = ? AND job_type = 'subgen'",
                    (filepath,)).fetchone()
                if exists:
                    skipped_dup += 1
                    continue

            probe_json = None
            try:
                cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
                       "-show_format", "-show_streams", filepath]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                if r.returncode == 0:
                    probe_json = json.loads(r.stdout)
            except Exception as e:
                log.warning(f"[SubGen Scan] FFprobe failed on {filename}: {e}")
                skipped_probe += 1
                continue

            if not probe_json:
                skipped_probe += 1
                continue

            # Determine what JP content already exists (if any)
            audio_streams = [s for s in probe_json.get("streams", []) if s.get("codec_type") == "audio"]
            audio_langs = set()
            for a in audio_streams:
                tags = a.get("tags") or {}
                lng = (tags.get("language") or "und").lower()
                audio_langs.add(lng)
            has_jp_audio = bool(has_target_set & audio_langs)
            has_any_jp_sub = has_subtitle_lang(probe_json, has_target_set)
            has_jp_text_sub = has_text_subtitle_lang(probe_json, has_target_set)
            has_eng_text_sub = has_text_subtitle_lang(probe_json, {"eng", "en"})

            # Build common metadata
            fmt = probe_json.get("format", {})
            try:
                dur_min = float(fmt.get("duration", 0)) / 60
            except Exception:
                dur_min = 0
            dur_str = f"{int(dur_min//60)}h {int(dur_min%60)}m" if dur_min >= 60 else f"{int(dur_min)}m"
            v = next((s for s in probe_json.get("streams", []) if s.get("codec_type") == "video"), {})
            vcodec = v.get("codec_name", "")
            res = f"{v.get('width', 0)}x{v.get('height', 0)}"
            sub_count = sum(1 for s in probe_json.get("streams", []) if s.get("codec_type") == "subtitle")

            # ── Decision ────────────────────────────────────────────────────
            # Goal: every file has BOTH an English and a target-language TEXT subtitle.
            # Queue if either is missing; skip only when both are already present.
            tgt_up = target_lang.upper()
            if has_jp_text_sub and has_eng_text_sub:
                status = "skipped"
                reason = f"Has English + {tgt_up} text subs"
                step = "Skipped: English and target-language subtitles already present"
                health = "skipped"
            else:
                need = []
                if not has_eng_text_sub:
                    need.append("English")
                if not has_jp_text_sub:
                    need.append(tgt_up)
                # Where the English pivot comes from (node decides definitively at run time)
                if has_eng_text_sub:
                    src = "existing English subs"
                elif "eng" in audio_langs or "en" in audio_langs:
                    src = "Whisper English audio"
                elif has_jp_audio:
                    src = "Whisper target audio"
                else:
                    src = "Whisper audio"
                status = "pending"
                reason = None
                step = f"Pending: create {' + '.join(need)} ({src} → AI translate)"
                health = "pending"

            with get_db() as db:
                max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
                try:
                    db.execute("""INSERT INTO queue (
                        file_path, file_name, file_size_gb, duration_min, duration_str,
                        video_codec, resolution, hdr_type, audio_track_count, subtitle_track_count,
                        library_id, library_name, status, health_status, priority, probe_data,
                        job_type, current_step, skipped_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SDR', ?, ?, ?, ?, ?, ?, ?, ?, 'subgen', ?, ?)""",
                    (filepath, filename, size_gb, dur_min, dur_str,
                     vcodec, res, len(audio_streams), sub_count,
                     library_id, lib["name"], status, health, max_pri + 1, json.dumps(probe_json),
                     step, reason))
                    if status == "skipped":
                        auto_skipped += 1
                    else:
                        queued += 1
                except sqlite3.IntegrityError:
                    skipped_dup += 1

    scan_progress[pkey] = {"total": total_files, "scanned": total_files,
                           "current_file": "", "eta": "done", "status": "complete"}

    ext_summary = ", ".join(f"{e}:{n}" for e, n in sorted(ext_breakdown.items())) if ext_breakdown else "none"
    add_log(f"[SubGen Scan] {lib['name']} — {queued} queued (need JP), "
            f"{auto_skipped} skipped (already have JP), "
            f"{skipped_dup} already in queue, {skipped_probe} probe-failed, "
            f"{file_count} total video files. Extensions: {ext_summary}")
    if get_setting("ntfy_on_scan") == "true":
        send_notification("SubGen Scan Complete",
            f"{lib['name']}: {queued} need Japanese subs, {auto_skipped} already have them ({file_count} total)")



# ─── Health Check ────────────────────────────────────────────────────────────
def _health_check_concurrency():
    """
    v3.11 — how many health checks to run at once. Wired to the health-check
    worker counts the user sets on the worker card (healthcheck_gpu_count +
    healthcheck_cpu_count). Health checks are CPU/disk validation on the
    server (ffprobe), NOT GPU work — the "GPU" label is historical. Falls
    back to 3, capped at 16 so a big scan can't swamp the NAS CPU.
    """
    try:
        conc = int(get_setting("healthcheck_gpu_count") or "0") + int(get_setting("healthcheck_cpu_count") or "0")
    except (TypeError, ValueError):
        conc = 0
    if conc < 1:
        conc = 3
    return min(conc, 16)


def _health_check_one(item):
    """Validate one queue item: exists, readable, ffprobe-valid. Writes the
    result (healthy→queued, else error) with lock-retry. v3.11: extracted so
    a batch can run in parallel; logging trimmed to one line per file."""
    path = item["file_path"]
    job_id = item["id"]
    filename = item["file_name"]
    status, error = "healthy", None
    try:
        if not os.path.exists(path):
            status, error = "missing", "File not found on disk"
        elif os.path.getsize(path) == 0:
            status, error = "corrupt", "File is empty (0 bytes)"
        else:
            try:
                with open(path, "rb") as f:
                    f.read(65536)
            except Exception as e:
                status, error = "unreadable", f"Cannot read file: {e}"
            if status == "healthy":
                try:
                    cmd = [FFPROBE, "-v", "error", "-show_entries",
                           "format=duration,size,nb_streams", "-of", "json", path]
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=120)
                    if r.returncode != 0:
                        status, error = "corrupt", f"FFprobe failed: {(r.stderr or 'Unknown')[:200]}"
                    else:
                        probe = json.loads(r.stdout)
                        streams = int(probe.get("format", {}).get("nb_streams", 0))
                        duration = float(probe.get("format", {}).get("duration", 0))
                        if streams == 0:
                            status, error = "corrupt", "No media streams found"
                        elif duration < 1:
                            status, error = "corrupt", "Duration is 0 — file may be corrupt"
                except subprocess.TimeoutExpired:
                    status, error = "timeout", "FFprobe timed out after 120s"
                except Exception:
                    pass  # ffprobe unavailable — best-effort pass
    except Exception as e:
        status, error = "error", str(e)[:200]

    def _write():
        with get_db() as db:
            if status == "healthy":
                db.execute("UPDATE queue SET health_status='healthy', status='queued', current_step='Health check passed' WHERE id=?", (job_id,))
            else:
                db.execute("UPDATE queue SET health_status=?, status='error', error_message=?, current_step='Health check failed' WHERE id=?",
                           (status, error, job_id))
    try:
        db_write_retry(_write)
    except Exception as e:
        log.error(f"[Health Check] result write failed for #{job_id}: {e}")
    hc_checking_since.pop(job_id, None)
    if status == "healthy":
        add_log(f"[Health Check] PASSED: {filename}", source="healthcheck", job_id=job_id)
    else:
        add_log(f"[Health Check] FAILED: {filename} — {error}", level="ERROR", source="healthcheck", job_id=job_id)


def health_check_task():
    """v3.11 — run health checks in PARALLEL, up to the configured concurrency
    (was: sequential, hard-capped at 3). Maintains a steady pool: each 5s tick
    tops up to `conc` in-flight checks. The 'checking' count gates spawning so
    we never exceed it."""
    conc = _health_check_concurrency()
    lib_sql, lib_params = active_lib_conditions()   # v3.15 per-tool library scope
    with get_db() as db:
        active_hc = db.execute("SELECT COUNT(*) as c FROM queue WHERE health_status='checking'").fetchone()["c"]
        limit = max(0, conc - active_hc)
        if limit == 0:
            return
        # v3.27 — only health-check jobs that are actually waiting. Without the
        # status guard, a poison-parked (status='error') row whose health_status
        # was left 'pending' got re-checked and silently un-parked back to 'queued'.
        pending = db.execute(
            f"SELECT * FROM queue WHERE health_status = 'pending' "
            f"AND status IN ('pending','queued'){lib_sql} ORDER BY priority LIMIT ?",
            (*lib_params, limit)).fetchall()
    if not pending:
        return

    # Mark the whole batch 'checking' up front so the next 5s tick sees an
    # accurate in-flight count (prevents over-spawning).
    now = time.time()
    for item in pending:
        hc_checking_since[item["id"]] = now
    def _mark():
        with get_db() as db:
            db.executemany("UPDATE queue SET health_status='checking', current_step='Health check…' WHERE id=?",
                           [(item["id"],) for item in pending])
    try:
        db_write_retry(_mark)
    except Exception:
        pass

    # Spawn one thread per item and return (don't join): the active_hc gate on
    # the next tick maintains ~conc concurrent checks without blocking the loop.
    for item in pending:
        threading.Thread(target=_health_check_one, args=(item,), daemon=True).start()

# v3.7 — when the HC thread marked each job 'checking' (monotonic time).
# Auto-recovery uses this to re-queue checks orphaned by a server restart
# or a hung probe: unknown or >10-minute-old 'checking' states go back to
# 'pending'.
hc_checking_since = {}

def start_health_check_loop():
    def loop():
        while True:
            try:
                health_check_task()
            except Exception as e:
                log.error(f"Health check error: {e}")
            time.sleep(5)

    # v3.7 — auto-recovery runs in its OWN thread. It used to share the HC
    # loop, so a single hung health check (e.g. a stuck probe subprocess)
    # silently disabled all stuck-state recovery as collateral damage.
    def recovery_loop():
        while True:
            # ── Auto-recovery: fix stuck states ──
            # v3.8: add_log() must NOT be called inside the `with get_db()`
            # block — it opens a second connection while this one holds the
            # write lock, stalling every other writer for the busy-timeout.
            # Messages are buffered and logged after the transaction.
            deferred_logs = []
            try:
                with get_db() as db:
                    # v3.7: reset health checks stuck in 'checking' — either
                    # older than 10 minutes or orphaned by a restart
                    now = time.time()
                    for row in db.execute("SELECT id FROM queue WHERE health_status='checking'").fetchall():
                        t0 = hc_checking_since.get(row["id"])
                        if t0 is None or now - t0 > 600:
                            db.execute("""UPDATE queue SET health_status='pending',
                                current_step='Health check retry (previous attempt stalled)' WHERE id=?""", (row["id"],))
                            hc_checking_since.pop(row["id"], None)
                            deferred_logs.append((f"Auto-reset stalled health check for job #{row['id']}", "WARN", row["id"]))
                    # Reset libraries stuck in 'scanning' for > 30 minutes
                    stuck_libs = db.execute("""UPDATE libraries SET status='idle'
                        WHERE status='scanning' AND last_scanned IS NOT NULL
                        AND last_scanned < datetime('now','localtime', '-30 minutes')""")
                    if stuck_libs.rowcount > 0:
                        log.warning(f"Auto-reset {stuck_libs.rowcount} stuck library scans")
                        deferred_logs.append((f"Auto-reset {stuck_libs.rowcount} stuck library scans (>30min)", "WARN", None))

                    # Also reset libraries stuck with no last_scanned timestamp
                    stuck_libs2 = db.execute("""UPDATE libraries SET status='idle'
                        WHERE status='scanning' AND last_scanned IS NULL
                        AND created_at < datetime('now','localtime', '-30 minutes')""")
                    if stuck_libs2.rowcount > 0:
                        log.warning(f"Auto-reset {stuck_libs2.rowcount} stuck new library scans")

                    # v3.13: a job is only a "ghost" if the worker that claimed
                    # it is actually GONE (no heartbeat for 3+ min) or has moved
                    # on to a different job. The old rule (0% progress for 30
                    # min) also caught jobs that were genuinely being worked on
                    # but slow to report — which, combined with the 7s progress
                    # endpoint, created an endless requeue loop. Now: if the
                    # worker is alive and still on this job, leave it alone
                    # regardless of reported progress.
                    # v3.26 — a job is a ghost ONLY when its worker's heartbeat is
                    # stale (node genuinely gone). The old rule also required
                    # w.current_job_id = q.id, but a node runs several worker
                    # THREADS that share one worker_id, and workers.current_job_id
                    # only holds the last-claimed job — so every OTHER concurrent job
                    # (e.g. a 20-min DV extract) looked like a ghost and got requeued
                    # every 5 min while actively processing, until poison-parked.
                    # Now: if the node is heartbeating (<10 min), all its in-flight
                    # jobs are considered alive. (Genuinely hung jobs are still caught
                    # by the separate 2-hour no-progress check below.)
                    ghost_jobs = db.execute("""SELECT q.id, q.worker_id, q.file_name,
                            COALESCE(q.attempts,0) AS attempts FROM queue q
                        WHERE q.status='processing'
                          AND q.started_at < datetime('now','localtime', '-300 seconds')
                          AND NOT EXISTS (
                              SELECT 1 FROM workers w
                              WHERE w.id = q.worker_id
                                AND w.last_heartbeat > datetime('now','localtime', '-600 seconds'))""").fetchall()
                    for sj in ghost_jobs:
                        att = (sj["attempts"] or 0) + 1
                        if att >= MAX_REQUEUE_ATTEMPTS:
                            # v3.19 — poison job: stop re-feeding it to workers.
                            db.execute("""UPDATE queue SET status='error', worker_id=NULL, started_at=NULL,
                                attempts=?, current_step='Failed', completed_at=datetime('now','localtime'),
                                error_message=? WHERE id=?""",
                                (att, f"Auto-failed after {att} worker-offline requeues (likely a bad file or node issue)", sj["id"]))
                            deferred_logs.append((f"Parked poison job #{sj['id']} after {att} requeues: {sj['file_name']}", "WARN", sj["id"]))
                        else:
                            db.execute("""UPDATE queue SET status='queued', worker_id=NULL, started_at=NULL,
                                attempts=?, current_step='Requeued (worker went offline)' WHERE id=?""",
                                (att, sj["id"]))
                            deferred_logs.append((f"Auto-requeued job #{sj['id']} (worker offline, attempt {att}/{MAX_REQUEUE_ATTEMPTS}): {sj['file_name']}", "WARN", sj["id"]))
                        if sj["worker_id"]:
                            db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=? AND current_job_id=?",
                                       (sj["worker_id"], sj["id"]))

                    # Jobs stuck mid-work for 2+ hours (worker alive but no
                    # progress advance) still error out for manual attention.
                    stuck_jobs = db.execute("""SELECT id, worker_id, file_name FROM queue
                        WHERE status='processing' AND started_at < datetime('now','localtime', '-7200 seconds')
                        AND progress > 0""").fetchall()
                    for sj in stuck_jobs:
                        db.execute("UPDATE queue SET status='error', error_message='Stuck — no progress for 2+ hours', completed_at=datetime('now','localtime') WHERE id=?", (sj["id"],))
                        if sj["worker_id"]:
                            db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=?", (sj["worker_id"],))
                        deferred_logs.append((f"Auto-cancelled stuck job #{sj['id']}: {sj['file_name']}", "WARN", sj["id"]))

                    # Clean up workers with stale heartbeats (> 5 minutes) that show as active
                    db.execute("""UPDATE workers SET status='idle', current_job_id=NULL
                        WHERE status='active' AND last_heartbeat < datetime('now','localtime', '-300 seconds')""")

                    # Auto-delete workers with no heartbeat for 10+ minutes (dead
                    # nodes). v3.22 — was 30 min, which left stale rows inflating
                    # the "nodes online" count long after a node stopped.
                    deleted = db.execute("""DELETE FROM workers
                        WHERE last_heartbeat < datetime('now','localtime', '-600 seconds')""")
                    if deleted.rowcount > 0:
                        log.info(f"Auto-deleted {deleted.rowcount} dead worker(s) (no heartbeat for 10+ min)")

            except Exception as e:
                log.error(f"Auto-recovery error: {e}")

            for msg, lvl, jid in deferred_logs:
                add_log(msg, level=lvl, job_id=jid)

            time.sleep(5)

    threading.Thread(target=loop, daemon=True, name="health-check").start()
    threading.Thread(target=recovery_loop, daemon=True, name="auto-recovery").start()


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with get_db() as db:
        qs = db.execute("SELECT status, COUNT(*) as c, COALESCE(SUM(file_size_gb),0) as gb FROM queue GROUP BY status").fetchall()
        wc = db.execute("SELECT COUNT(*) as c FROM workers WHERE last_heartbeat > datetime('now','localtime','-60 seconds')").fetchone()["c"]
    return jsonify({"status": "running", "queue": {s["status"]: {"count": s["c"], "total_gb": s["gb"]} for s in qs}, "active_workers": wc, "version": SERVER_VERSION})

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
        db.execute("DELETE FROM queue WHERE library_id=? AND status IN ('pending','queued')", (lid,))
        db.execute("DELETE FROM libraries WHERE id=?", (lid,))
    return jsonify({"ok": True})

@app.route("/api/libraries/<int:lid>/scan", methods=["POST"])
@login_required
def api_scan_library(lid):
    threading.Thread(target=scan_library_task, args=(lid,), daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})

@app.route("/api/libraries/<int:lid>/scan-remuxclean", methods=["POST"])
@login_required
def api_scan_remuxclean(lid):
    """Scan a library for files needing track cleanup. Queues remuxclean jobs."""
    threading.Thread(target=scan_remuxclean_task, args=(lid,), daemon=True).start()
    return jsonify({"ok": True, "message": "RemuxClean scan started"})

@app.route("/api/libraries/scan-remuxclean-all", methods=["POST"])
@login_required
def api_scan_remuxclean_all():
    """Run remuxclean scan across all libraries."""
    with get_db() as db:
        libs = db.execute("SELECT id FROM libraries").fetchall()
    for lib in libs:
        threading.Thread(target=scan_remuxclean_task, args=(lib["id"],), daemon=True).start()
    return jsonify({"ok": True, "message": f"RemuxClean scan started for {len(libs)} libraries"})

@app.route("/api/libraries/<int:lid>/scan-dv78only", methods=["POST"])
@login_required
def api_scan_dv78only(lid):
    """Scan a library for DoVi Profile 7 files. Queues dv78only jobs."""
    threading.Thread(target=scan_dv78only_task, args=(lid,), daemon=True).start()
    return jsonify({"ok": True, "message": "DV7→8 scan started"})

@app.route("/api/libraries/scan-dv78only-all", methods=["POST"])
@login_required
def api_scan_dv78only_all():
    """Run DV7→8 scan across all libraries."""
    with get_db() as db:
        libs = db.execute("SELECT id FROM libraries").fetchall()
    for lib in libs:
        threading.Thread(target=scan_dv78only_task, args=(lib["id"],), daemon=True).start()
    return jsonify({"ok": True, "message": f"DV7→8 scan started for {len(libs)} libraries"})

@app.route("/api/libraries/<int:lid>/scan-compat", methods=["POST"])
@login_required
def api_scan_compat(lid):
    """Scan a library for playback-compatibility risks. Queues compatfix jobs."""
    threading.Thread(target=scan_compat_task, args=(lid,), daemon=True).start()
    return jsonify({"ok": True, "message": "Compatibility scan started"})

@app.route("/api/libraries/scan-compat-all", methods=["POST"])
@login_required
def api_scan_compat_all():
    """Run compatibility scan across all libraries."""
    with get_db() as db:
        libs = db.execute("SELECT id FROM libraries").fetchall()
    for lib in libs:
        threading.Thread(target=scan_compat_task, args=(lib["id"],), daemon=True).start()
    return jsonify({"ok": True, "message": f"Compatibility scan started for {len(libs)} libraries"})

@app.route("/api/libraries/<int:lid>/scan-subgen", methods=["POST"])
@login_required
def api_scan_subgen(lid):
    """Scan a library for files missing Japanese subtitles. Queues subgen jobs."""
    threading.Thread(target=scan_subgen_task, args=(lid,), daemon=True).start()
    return jsonify({"ok": True, "message": "Subtitle generation scan started"})

@app.route("/api/libraries/scan-subgen-all", methods=["POST"])
@login_required
def api_scan_subgen_all():
    """Run subtitle generation scan across all libraries."""
    with get_db() as db:
        libs = db.execute("SELECT id FROM libraries").fetchall()
    for lib in libs:
        threading.Thread(target=scan_subgen_task, args=(lib["id"],), daemon=True).start()
    return jsonify({"ok": True, "message": f"SubGen scan started for {len(libs)} libraries"})

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
    """Re-scan and update changed files."""
    with get_db() as db:
        # Remove queue items whose files no longer exist
        items = db.execute("SELECT id, file_path FROM queue WHERE library_id=?", (lid,)).fetchall()
        removed = 0
        for item in items:
            if not os.path.exists(item["file_path"]):
                db.execute("DELETE FROM queue WHERE id=?", (item["id"],))
                removed += 1
        if removed:
            add_log(f"Refresh: removed {removed} missing files from library #{lid}")
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
    job_type = request.args.get("job_type")  # NEW: filter by job type

    q = "SELECT * FROM queue WHERE 1=1"
    p = []
    if status: q += " AND status=?"; p.append(status)
    if hdr: q += " AND hdr_type=?"; p.append(hdr)
    if lib: q += " AND library_name=?"; p.append(lib)
    if search: q += " AND file_name LIKE ?"; p.append(f"%{search}%")
    if job_type and job_type != "all": q += " AND job_type=?"; p.append(job_type)

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
        # (v3.7: job is a sqlite3.Row — .get() doesn't exist on Row, so this
        # crashed with AttributeError and the rollback made cancelling a
        # processing job a silent no-op: the cancel flag never got set)
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
    with get_db() as db:
        db.execute("UPDATE queue SET accepted=1 WHERE id=?", (jid,))
    return jsonify({"ok": True})

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

# v3.4 — per-job-type pipeline controls
VALID_JOB_TYPES = ("transcode", "subgen", "remuxclean", "dv78only", "compatfix")
JOB_TYPE_LABELS = {
    "transcode": "Transcode",
    "subgen": "AI Subtitles",
    "remuxclean": "Audio/Track Cleanup",
    "dv78only": "DV → P8",
    "compatfix": "Compatibility",
}

@app.route("/api/queue/start/<string:jobtype>", methods=["POST"])
@login_required
def api_start_jobtype(jobtype):
    if jobtype not in VALID_JOB_TYPES:
        return jsonify({"error": f"Invalid job type. Must be one of {VALID_JOB_TYPES}"}), 400
    set_setting(f"processing_enabled_{jobtype}", "true")
    # Auto-enable the master switch when a specific pipeline is started
    if get_setting("processing_enabled") != "true":
        set_setting("processing_enabled", "true")
        add_log(f"Master processing enabled (auto, due to {jobtype} start)")
    add_log(f"{JOB_TYPE_LABELS.get(jobtype, jobtype)} pipeline: STARTED")
    return jsonify({"ok": True})

@app.route("/api/queue/pause/<string:jobtype>", methods=["POST"])
@login_required
def api_pause_jobtype(jobtype):
    if jobtype not in VALID_JOB_TYPES:
        return jsonify({"error": f"Invalid job type. Must be one of {VALID_JOB_TYPES}"}), 400
    set_setting(f"processing_enabled_{jobtype}", "false")
    add_log(f"{JOB_TYPE_LABELS.get(jobtype, jobtype)} pipeline: PAUSED")
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
                worker_id=NULL, started_at=NULL, completed_at=NULL, attempts=0
                WHERE library_id=? AND status IN ('error','skipped','cancelled')""", (lid,))
            count = db.execute("SELECT changes()").fetchone()[0]
        else:
            db.execute("""UPDATE queue SET status='pending', health_status='pending',
                progress=0, current_step='', eta='', error_message=NULL,
                worker_id=NULL, started_at=NULL, completed_at=NULL, attempts=0
                WHERE status IN ('error','skipped','cancelled')""")
            count = db.execute("SELECT changes()").fetchone()[0]
    add_log(f"Requeued {count} items" + (f" for library #{lid}" if lid else ""))
    return jsonify({"ok": True, "count": count})

@app.route("/api/queue/requeue-status/<string:status>", methods=["POST"])
@login_required
def api_requeue_by_status(status):
    """Bulk-requeue all jobs in a given status, optionally filtered by job_type. Single SQL UPDATE — much faster than per-row."""
    if status not in ("error", "skipped", "cancelled", "complete"):
        return jsonify({"error": f"Cannot requeue items with status '{status}'"}), 400
    job_type = request.json.get("job_type") if request.json else None
    with get_db() as db:
        if job_type:
            db.execute("""UPDATE queue SET status='pending', health_status='pending',
                progress=0, current_step='', eta='', error_message=NULL,
                worker_id=NULL, started_at=NULL, completed_at=NULL,
                accepted=0, skipped_reason=NULL, attempts=0
                WHERE status=? AND COALESCE(NULLIF(job_type,''),'transcode')=?""",
                (status, job_type))
        else:
            db.execute("""UPDATE queue SET status='pending', health_status='pending',
                progress=0, current_step='', eta='', error_message=NULL,
                worker_id=NULL, started_at=NULL, completed_at=NULL,
                accepted=0, skipped_reason=NULL, attempts=0
                WHERE status=?""", (status,))
        count = db.execute("SELECT changes()").fetchone()[0]
    add_log(f"Bulk requeue: {count} jobs from status '{status}'" + (f" (type {job_type})" if job_type else ""))
    return jsonify({"ok": True, "count": count})

@app.route("/api/queue/clear-library", methods=["POST"])
@login_required
def api_clear_library():
    """Clear all queue items for a specific library."""
    lid = request.json.get("library_id")
    if not lid: return jsonify({"error": "library_id required"}), 400
    with get_db() as db:
        db.execute("DELETE FROM queue WHERE library_id=? AND status NOT IN ('processing')", (lid,))
        count = db.execute("SELECT changes()").fetchone()[0]
    add_log(f"Cleared {count} items from library #{lid}")
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
            accepted=0, skipped_reason=NULL, attempts=0 WHERE id=?""", (jid,))
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
    """Receive log lines from the node during transcode.
    v3.13 — buffered (see async buffering above) so the node's many per-job
    log posts don't block on the DB write lock."""
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
        buffer_log(level, "node", line, jid)
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
# ── v3.14 — update checker ───────────────────────────────────────────────────
_update_cache = {"at": 0, "data": None}
_update_lock = threading.Lock()

def _vtuple(v):
    """'3.13' -> (3, 13) for comparison; non-numeric parts sort as 0."""
    out = []
    for p in str(v or "0").split("."):
        try: out.append(int(p))
        except ValueError: out.append(0)
    return tuple(out)

@app.route("/api/updates/check")
def api_updates_check():
    """Compare running server/node versions against the published manifest on
    GitHub. Cached 30 min (or force with ?force=1) so the bell can poll freely.
    Never fails the UI — returns update_available:false if GitHub is
    unreachable."""
    force = request.args.get("force") == "1"
    now = time.time()
    with _update_lock:
        if not force and _update_cache["data"] and now - _update_cache["at"] < 1800:
            return jsonify(_update_cache["data"])
    latest_server, latest_node, notes, err = SERVER_VERSION, NODE_VERSION, "", None
    try:
        import urllib.request
        req = urllib.request.Request(UPDATE_MANIFEST_URL, headers={"User-Agent": "ByteTranscode"})
        with urllib.request.urlopen(req, timeout=8) as r:
            m = json.loads(r.read().decode("utf-8"))
        latest_server = str(m.get("server", SERVER_VERSION))
        latest_node = str(m.get("node", NODE_VERSION))
        notes = m.get("notes", "")
    except Exception as e:
        err = str(e)[:120]

    # v3.25 — base the node-update flag on the REAL versions the connected nodes
    # report, not a stale hardcoded constant. A node is out of date only if it's
    # online and behind the manifest; nodes that haven't reported a version yet
    # (pre-3.25) are ignored so we don't nag about phantom updates.
    with get_db() as db:
        online = db.execute("""SELECT name, COALESCE(version,'') AS version FROM workers
            WHERE last_heartbeat > datetime('now','localtime','-300 seconds')""").fetchall()
    node_versions = [{"name": w["name"], "version": w["version"]} for w in online]
    reported = [w["version"] for w in online if w["version"]]
    outdated = [w for w in node_versions if w["version"] and _vtuple(w["version"]) < _vtuple(latest_node)]
    node_update = len(outdated) > 0

    data = {
        "current_server": SERVER_VERSION, "latest_server": latest_server,
        "latest_node": latest_node,
        "node_versions": node_versions,          # per-node running versions
        "nodes_outdated": [w["name"] for w in outdated],
        "server_update": _vtuple(latest_server) > _vtuple(SERVER_VERSION),
        "node_update": node_update,
        "notes": notes,
        "update_docs": "https://github.com/Jenari-Dev/byte-transcode#updating",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": err,
    }
    data["update_available"] = data["server_update"] or data["node_update"]
    with _update_lock:
        _update_cache["at"] = now; _update_cache["data"] = data
    return jsonify(data)

# ── v3.17 — External submission API (Byte Media Manager & other clients) ──────
# A client (e.g. the WPF app) POSTs a file + detected issues; Byte maps the
# issues to the right pipeline, dedupes on (path, job_type), and runs it
# through the normal health-check → queue → process flow.

# Issue vocabulary → Byte pipeline. First match wins (priority order).
EXT_ISSUE_JOBTYPE = [
    (("dv_profile_7", "dv_profile_5", "dv_profile_4", "dv_profile_8", "dv_profile"), "dv78only"),
    (("non_eng_jpn_tracks", "missing_language_tags", "pgs_subtitles"), "remuxclean"),
    (("container_unsupported", "corrupt_container", "legacy_codec"), "compatfix"),
]

def _jobtype_for_issues(issues):
    s = set((i or "").lower() for i in (issues or []))
    for keys, jt in EXT_ISSUE_JOBTYPE:
        if s & set(keys):
            return jt
    return "compatfix"   # default: general "make it play" fix

def _resolve_source(p):
    """
    v3.17 — a client may send a path in the HOST namespace (e.g. the SMB
    share's real path /mnt/storage/...) but the server runs in Docker where
    that tree is mounted at /media. Try the path as sent, then translate any
    known host prefix to /media. Returns the first path that exists (the one
    the server + node both understand), or None.
    """
    cands = [p]
    for hp in ("/mnt/storage/", "/mnt/media/"):
        if p.startswith(hp):
            cands.append("/media/" + p[len(hp):])
    # let ops override via a setting if their host prefix differs
    extra = (get_setting("ext_source_prefix") or "").strip()
    if extra and p.startswith(extra.rstrip("/") + "/"):
        cands.append("/media/" + p[len(extra.rstrip("/") + "/"):])
    for c in cands:
        try:
            if os.path.exists(c):
                return c
        except Exception:
            pass
    return None

def _require_api_key():
    """External endpoints require the Settings→API key via X-API-Key.
    Returns an error response tuple if invalid, else None."""
    key = get_setting("api_key")
    if key and request.headers.get("X-API-Key") != key:
        return jsonify({"error": "invalid or missing X-API-Key"}), 401
    return None

_EXT_STATUS_MAP = {"pending": "queued", "queued": "queued", "processing": "processing",
                   "complete": "complete", "error": "error", "skipped": "skipped",
                   "cancelled": "cancelled"}

def _ext_job_view(row):
    return {
        "job_id": row["id"],
        "status": _EXT_STATUS_MAP.get(row["status"], row["status"]),
        "progress": round(row["progress"] or 0, 1),
        "job_type": row["job_type"] or "transcode",
        "message": row["current_step"] or row["error_message"] or "",
        "output_path": row["output_path"] or "",
        "source_path": row["file_path"],
        "file_name": row["file_name"],
        "requested_by": (row["requested_by"] if "requested_by" in row.keys() else "") or "",
    }

@app.route("/api/jobs", methods=["POST"])
def api_submit_job():
    """Submit one file to the fix queue. Dedupes on (source_path, job_type)."""
    auth = _require_api_key()
    if auth:
        return auth
    d = request.json or {}
    raw_path = (d.get("source_path") or "").strip()
    if not raw_path:
        return jsonify({"error": "source_path required"}), 400
    path = _resolve_source(raw_path)   # translate host path -> container /media
    if not path:
        return jsonify({"error": f"file not found on server: {raw_path}"}), 404
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return jsonify({"error": "cannot read file on server"}), 400

    issues = d.get("issues") or []
    jobtype = _jobtype_for_issues(issues)
    requested_by = (d.get("requested_by") or "external").strip()
    note = d.get("note") or ""

    # ── Dedupe on (path, job_type) ──
    with get_db() as db:
        existing = db.execute("SELECT * FROM queue WHERE file_path=? AND job_type=?", (path, jobtype)).fetchone()
    if existing:
        st = existing["status"]
        if st in ("pending", "queued"):
            return jsonify({"job_id": existing["id"], "status": "already_queued", "job_type": jobtype}), 200
        if st == "processing":
            return jsonify({"job_id": existing["id"], "status": "processing", "job_type": jobtype}), 200
        if st == "complete":
            prev = existing["file_size_bytes"] if "file_size_bytes" in existing.keys() else None
            if prev and int(prev) == size_bytes:
                return jsonify({"job_id": existing["id"], "status": "already_done", "job_type": jobtype}), 200
            # file changed (re-downloaded) → drop the stale record and re-create
            def _del():
                with get_db() as db:
                    db.execute("DELETE FROM queue WHERE id=?", (existing["id"],))
            db_write_retry(_del)
        else:  # error / skipped / cancelled → requeue this same row
            def _rq():
                with get_db() as db:
                    db.execute("""UPDATE queue SET status='pending', health_status='pending', progress=0,
                        error_message=NULL, skipped_reason=NULL, output_path=NULL, output_size_gb=NULL,
                        reduction_pct=NULL, completed_at=NULL, current_step='Requeued',
                        requested_by=?, ext_issues=?, ext_note=?, file_size_bytes=? WHERE id=?""",
                        (requested_by, json.dumps(issues), note, size_bytes, existing["id"]))
            db_write_retry(_rq)
            return jsonify({"job_id": existing["id"], "status": "queued", "job_type": jobtype}), 202

    # ── Probe for display + type-specific metadata ──
    probe = None
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if r.returncode == 0:
            probe = json.loads(r.stdout)
    except Exception:
        pass
    size_gb = size_bytes / (1024**3)
    v = {}
    dur_min, hdr, vcodec, res, dovi_profile, has_dovi = 0, "SDR", "", "", None, 0
    if probe:
        v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        vcodec = v.get("codec_name", "")
        res = f"{v.get('width',0)}x{v.get('height',0)}"
        try:
            dur_min = float(probe.get("format", {}).get("duration", 0)) / 60
        except Exception:
            dur_min = 0
        tr = (v.get("color_transfer") or "").lower()
        dv_sd = next((sd for sd in v.get("side_data_list", []) if "DOVI" in (sd.get("side_data_type") or "")), None)
        if dv_sd:
            has_dovi = 1; dovi_profile = dv_sd.get("dv_profile")
            hdr = "DoVi"
        elif tr == "smpte2084":
            hdr = "HDR10"
        elif tr == "arib-std-b67":
            hdr = "HLG"
        if jobtype == "compatfix":
            ext = os.path.splitext(path)[1].lower()
            probe["_compat"] = analyze_for_compat(probe, ext)
    dur_str = f"{int(dur_min//60)}h {int(dur_min%60)}m" if dur_min >= 60 else f"{int(dur_min)}m"

    # match library by path prefix (for scoping / display)
    library_id, library_name = None, ""
    with get_db() as db:
        for lib in db.execute("SELECT id, name, path FROM libraries").fetchall():
            if path.startswith(lib["path"].rstrip("/") + "/") or path == lib["path"]:
                library_id, library_name = lib["id"], lib["name"]; break

    step = f"Queued by {requested_by}: " + (", ".join(issues) if issues else "auto-fix")
    new_id = {}
    def _ins():
        with get_db() as db:
            max_pri = db.execute("SELECT COALESCE(MAX(priority), 0) FROM queue").fetchone()[0]
            cur = db.execute("""INSERT INTO queue (
                file_path, file_name, file_size_gb, file_size_bytes, duration_min, duration_str,
                video_codec, resolution, hdr_type, has_dovi, dovi_profile, library_id, library_name,
                status, health_status, priority, probe_data, job_type, current_step,
                requested_by, ext_issues, ext_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (path, os.path.basename(path), size_gb, size_bytes, dur_min, dur_str,
             vcodec, res, hdr, has_dovi, dovi_profile, library_id, library_name,
             max_pri + 1, json.dumps(probe) if probe else None, jobtype, step,
             requested_by, json.dumps(issues), note))
            new_id["id"] = cur.lastrowid
    try:
        db_write_retry(_ins)
    except sqlite3.IntegrityError:
        with get_db() as db:
            ex = db.execute("SELECT id FROM queue WHERE file_path=? AND job_type=?", (path, jobtype)).fetchone()
        return jsonify({"job_id": ex["id"] if ex else None, "status": "already_queued", "job_type": jobtype}), 200
    add_log(f"Job #{new_id['id']} submitted by {requested_by} [{jobtype}]: {os.path.basename(path)}", job_id=new_id["id"])
    return jsonify({"job_id": new_id["id"], "status": "queued", "job_type": jobtype}), 202

@app.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    """List jobs, optionally filtered by requested_by and/or status."""
    auth = _require_api_key()
    if auth:
        return auth
    rb = request.args.get("requested_by")
    st = request.args.get("status")
    q = "SELECT * FROM queue WHERE 1=1"
    params = []
    if rb:
        q += " AND requested_by=?"; params.append(rb)
    if st:
        # accept the app's vocab; map 'queued' to pending+queued
        internal = {"queued": ("pending", "queued")}.get(st, (st,))
        q += " AND status IN (" + ",".join("?" * len(internal)) + ")"; params += list(internal)
    q += " ORDER BY id DESC LIMIT 500"
    with get_db() as db:
        rows = db.execute(q, params).fetchall()
    return jsonify({"jobs": [_ext_job_view(r) for r in rows]})

@app.route("/api/jobs/<int:jid>", methods=["GET"])
def api_get_job(jid):
    """Status of one job in the external client's shape."""
    auth = _require_api_key()
    if auth:
        return auth
    with get_db() as db:
        row = db.execute("SELECT * FROM queue WHERE id=?", (jid,)).fetchone()
    if not row:
        return jsonify({"error": "job not found", "job_id": jid}), 404
    return jsonify(_ext_job_view(row))

@app.route("/api/server/overview")
def api_server_overview():
    info = {"version": SERVER_VERSION, "uptime_seconds": 0}
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

@app.route("/api/workers/register", methods=["POST"])
def api_register_worker():
    d = request.json
    wid = d.get("id", "")
    if not wid: return jsonify({"error": "ID required"}), 400
    name = d.get("name", "")
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO workers (id,name,host,gpu,status,last_heartbeat,version) VALUES (?,?,?,?,'idle',datetime('now','localtime'),?)",
                   (wid, name, d.get("host",""), d.get("gpu",""), d.get("version","")))
        # v3.22 — collapse duplicate rows for the same node name that are no
        # longer heartbeating (e.g. a node relaunched from a different folder,
        # or whose id changed). A genuinely live same-name node keeps a fresh
        # heartbeat and is preserved.
        if name:
            db.execute("""DELETE FROM workers WHERE name=? AND id<>?
                AND (last_heartbeat IS NULL OR last_heartbeat < datetime('now','localtime','-120 seconds'))""",
                (name, wid))
        # v3.28 — a re-registering worker is a FRESH start (it was just
        # (re)launched — e.g. after an update). Any job still marked 'processing'
        # under its id is orphaned from the killed process, so release it back to
        # the queue instead of letting it sit stuck forever (the update black-hole).
        released = db.execute("""UPDATE queue SET status='queued', worker_id=NULL, started_at=NULL,
            current_step='Requeued (node restarted)'
            WHERE status='processing' AND worker_id=?""", (wid,))
        if released.rowcount:
            add_log(f"Released {released.rowcount} orphaned job(s) from restarted node {name}")
    add_log(f"Worker registered: {name} ({d.get('gpu','')})")
    return jsonify({"ok": True})

@app.route("/api/workers/prune", methods=["POST"])
@login_required
def api_prune_workers():
    """v3.22 — immediately drop nodes that haven't heartbeat in >2 min, so a
    stopped node stops inflating the 'nodes online' count without waiting for
    the 10-min auto-delete. Any processing jobs they held are requeued."""
    with get_db() as db:
        stale = db.execute("""SELECT id FROM workers
            WHERE last_heartbeat IS NULL OR last_heartbeat < datetime('now','localtime','-120 seconds')""").fetchall()
        ids = [w["id"] for w in stale]
        for wid in ids:
            db.execute("""UPDATE queue SET status='queued', worker_id=NULL, started_at=NULL,
                current_step='Requeued (node removed)' WHERE worker_id=? AND status='processing'""", (wid,))
        if ids:
            db.execute(f"DELETE FROM workers WHERE id IN ({','.join('?'*len(ids))})", ids)
    add_log(f"Pruned {len(ids)} offline node(s)")
    return jsonify({"ok": True, "removed": len(ids)})

@app.route("/api/workers/heartbeat", methods=["POST"])
def api_heartbeat():
    d = request.json
    with get_db() as db:
        db.execute("""UPDATE workers SET last_heartbeat=datetime('now','localtime'), cpu_usage=?, ram_usage=?,
            gpu_usage=?, vram_usage=?, version=COALESCE(NULLIF(?,''),version) WHERE id=?""",
                   (d.get("cpu", 0), d.get("ram", 0), d.get("gpu_usage", 0), d.get("vram", 0),
                    d.get("version", ""), d.get("id", "")))
    return jsonify({"ok": True})

# Jobs
MAX_REQUEUE_ATTEMPTS = 5   # v3.19 — park a poison job in Errored after this many auto-requeues

ALL_JOB_TYPES = ("transcode", "subgen", "remuxclean", "dv78only", "compatfix")

def _tool_priority_order():
    """v3.23 — the drag-sorted pipeline priority from the sidebar, validated
    against the known job types and completed with any missing ones."""
    order = []
    raw = get_setting("tool_priority_order")
    if raw:
        try:
            order = [t for t in json.loads(raw) if t in ALL_JOB_TYPES]
        except Exception:
            order = []
    return order + [t for t in ALL_JOB_TYPES if t not in order]

def _tool_rank_case():
    """Build a safe SQL CASE mapping job_type -> its priority rank (0 = first).
    Values are from the validated whitelist above, so inlining is injection-safe."""
    order = _tool_priority_order()
    whens = " ".join(f"WHEN '{t}' THEN {i}" for i, t in enumerate(order))
    return f"CASE COALESCE(NULLIF(job_type,''),'transcode') {whens} ELSE 99 END"

def _effective_worker_cap(db):
    """
    v3.19 — total concurrent-job capacity = sum of each ONLINE node's transcode
    worker count (global transcode_gpu_count, overridden per-node), floored at
    the manual max_workers safety value. The old flat max_workers=4 capped the
    whole fleet regardless of how many workers each node ran, so a 2-node x3
    setup only ever ran ~4 jobs instead of 6. This auto-scales as nodes join/leave.
    """
    try:
        manual = int(get_setting("max_workers") or "4")
    except (TypeError, ValueError):
        manual = 4
    try:
        global_tw = int(get_setting("transcode_gpu_count") or "1")
    except (TypeError, ValueError):
        global_tw = 1
    online = db.execute(
        "SELECT id FROM workers WHERE last_heartbeat > datetime('now','localtime','-90 seconds')"
    ).fetchall()
    total = 0
    for w in online:
        n = global_tw
        raw = get_setting(f"worker_config_{w['id']}")
        if raw:
            try:
                v = json.loads(raw).get("transcode_gpu_count")
                if v is not None:
                    n = int(v)
            except Exception:
                pass
        total += max(1, n)
    return max(total, manual)

@app.route("/api/jobs/next", methods=["POST"])
def api_next_job():
    d = request.json
    wid = d.get("worker_id", "")
    # v3.10 — per-node ON/OFF switch (set from the worker card in the UI).
    # A disabled node keeps polling and heartbeating but is never assigned
    # work, so it can be toggled back on without touching the machine.
    wcfg_raw = get_setting(f"worker_config_{wid}") if wid else None
    if wcfg_raw:
        try:
            if json.loads(wcfg_raw).get("node_enabled") == "false":
                return jsonify({"job": None, "reason": "Node disabled from dashboard"})
        except Exception:
            pass
    with get_db() as db:
        if get_setting("processing_enabled") != "true":
            return jsonify({"job": None, "reason": "Processing paused"})
        # Staged file limit — 0 means stop assigning new jobs
        staged_limit = int(get_setting("staged_limit") or "100")
        if staged_limit == 0:
            return jsonify({"job": None, "reason": "Staged limit is 0 — paused"})
        max_w = _effective_worker_cap(db)
        active = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='processing'").fetchone()["c"]
        if active >= max_w: return jsonify({"job": None, "reason": "Fleet at capacity"})
        if active >= staged_limit: return jsonify({"job": None, "reason": "Staged limit reached"})

        # v3.4: only claim jobs whose type's pipeline is enabled
        all_types = ('transcode', 'subgen', 'remuxclean', 'dv78only', 'compatfix')
        allowed_types = []
        for jt in all_types:
            if get_setting(f"processing_enabled_{jt}") != "false":
                allowed_types.append(jt)
        if not allowed_types:
            return jsonify({"job": None, "reason": "All job types paused"})
        # Build IN clause
        placeholders = ",".join("?" * len(allowed_types))
        lib_sql, lib_params = active_lib_conditions()   # v3.15 per-tool library scope
        # v3.23 — tool priority order (drag-sorted in the sidebar): jobs from a
        # higher-ranked pipeline are claimed first. Ranks come from a validated
        # whitelist, so the CASE is safe to inline.
        rank_case = _tool_rank_case()
        query = (f"SELECT * FROM queue WHERE status='queued' AND health_status='healthy' "
                 f"AND COALESCE(NULLIF(job_type,''),'transcode') IN ({placeholders}){lib_sql} "
                 f"ORDER BY {rank_case} ASC, priority ASC, id ASC LIMIT 1")
        job = db.execute(query, (*allowed_types, *lib_params)).fetchone()
        if not job:
            disabled = [jt for jt in all_types if jt not in allowed_types]
            reason = "No jobs ready"
            if disabled:
                reason += f" (paused: {', '.join(disabled)})"
            return jsonify({"job": None, "reason": reason})

        # Atomic claim — only succeed if status is still 'queued'
        result = db.execute(
            "UPDATE queue SET status='processing', worker_id=?, started_at=datetime('now','localtime'), "
            "current_step='Starting...' WHERE id=? AND status='queued'",
            (wid, job["id"]))
        if result.rowcount == 0:
            return jsonify({"job": None, "reason": "Race — another worker claimed it"})
        db.execute("UPDATE workers SET status='active', current_job_id=? WHERE id=?", (job["id"], wid))
        settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
        jd = dict(job)
        jd["settings"] = settings
    add_log(f"Job #{job['id']} [{job['job_type'] or 'transcode'}] → {wid}: {job['file_name']}", job_id=job["id"])
    return jsonify({"job": jd})

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
    # v3.13 — buffer + return instantly (see async buffering above). No DB
    # write in the request path, so the node never blocks on lock contention.
    buffer_progress(jid, request.json or {})
    return jsonify({"ok": True})

@app.route("/api/jobs/<int:jid>/complete", methods=["POST"])
def api_complete(jid):
    d = request.json
    wid = d.get("worker_id", "")
    auto = get_setting("auto_accept") == "true"
    # v3.13 — drop any buffered progress for this job so a late async flush
    # can't overwrite the final 'Done'/100% state, and use write-retry.
    with _progress_lock:
        _progress_buffer.pop(jid, None)
    def _w():
        with get_db() as db:
            db.execute("UPDATE queue SET status='complete', progress=100, current_step='Done', output_path=?, output_size_gb=?, reduction_pct=?, completed_at=datetime('now','localtime'), accepted=? WHERE id=?",
                       (d.get("output_path",""), d.get("output_size_gb",0), d.get("reduction_pct",0), int(auto), jid))
            # v3.27 — count the completion, but only flip the node to idle / clear
            # the pointer if THIS job was the one displayed. On a multi-threaded
            # node another thread's job may be the current one — don't blank it.
            db.execute("UPDATE workers SET jobs_completed=jobs_completed+1, total_saved_gb=total_saved_gb+? WHERE id=?",
                       (d.get("saved_gb",0), wid))
            db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=? AND current_job_id=?",
                       (wid, jid))
    db_write_retry(_w)
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
    err = d.get("error", "Unknown")
    with _progress_lock:
        _progress_buffer.pop(jid, None)   # v3.13 — no late flush over final state
    with get_db() as db:
        cur = db.execute("SELECT status FROM queue WHERE id=?", (jid,)).fetchone()
    # v3.7: a node reporting "Cancelled by user" is the tail end of a
    # user cancel, not a failure — keep the 'cancelled' status the
    # cancel endpoint already set instead of flipping it to 'error'
    # (which also fired a false "Transcode Failed" notification).
    is_cancel = (cur and cur["status"] == "cancelled") or "cancelled by user" in err.lower()
    def _w():
        with get_db() as db:
            if is_cancel:
                db.execute("UPDATE queue SET status='cancelled', progress=0, current_step='Cancelled', error_message=?, completed_at=datetime('now','localtime') WHERE id=?",
                           (err, jid))
            else:
                db.execute("UPDATE queue SET status='error', progress=0, current_step='Failed', error_message=?, completed_at=datetime('now','localtime') WHERE id=?",
                           (err, jid))
            # v3.27 — only clear the pointer if this job was the displayed one
            # (a multi-threaded node keeps other jobs running).
            db.execute("UPDATE workers SET status='idle', current_job_id=NULL WHERE id=? AND current_job_id=?", (wid, jid))
    db_write_retry(_w)
    if is_cancel:
        add_log(f"Job #{jid} cancelled (node confirmed)", job_id=jid)
        return jsonify({"ok": True})
    add_log(f"Job #{jid} failed: {err}", level="ERROR", job_id=jid)
    if get_setting("ntfy_on_error") == "true":
        with get_db() as db:
            fn = db.execute("SELECT file_name FROM queue WHERE id=?", (jid,)).fetchone()
        send_notification("Transcode Failed",
            f"{fn['file_name'] if fn else 'Job #'+str(jid)} — {err[:100]}", priority="high")
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
    for k, v in request.json.items():
        if k not in ("auth_hash",):  # Don't allow hash override via settings
            set_setting(k, v)
    add_log(f"Settings updated")
    return jsonify({"ok": True})

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
        # v3.3: per-job-type status counts for the dashboard's nested tabs
        ssj = db.execute("SELECT COALESCE(job_type,'transcode') as job_type, status, COUNT(*) as c FROM queue GROUP BY job_type, status").fetchall()
        hs = db.execute("SELECT hdr_type, COUNT(*) as c FROM queue WHERE status NOT IN ('complete','cancelled','skipped') GROUP BY hdr_type").fetchall()
        ws = db.execute("""SELECT w.*, q.file_name as current_file, q.progress as job_progress,
            q.current_step, q.eta as job_eta, q.hdr_type, q.dovi_profile, q.file_size_gb, q.audio_summary
            FROM workers w LEFT JOIN queue q ON w.current_job_id=q.id
            WHERE w.last_heartbeat > datetime('now','localtime','-300 seconds')""").fetchall()
        saved = db.execute("SELECT COALESCE(SUM(file_size_gb-COALESCE(output_size_gb,file_size_gb)),0) as s FROM queue WHERE status='complete'").fetchone()
        processing = db.execute("SELECT * FROM queue WHERE status='processing' ORDER BY priority").fetchall()
        recent_complete = db.execute("SELECT * FROM queue WHERE status='complete' ORDER BY completed_at DESC LIMIT 10").fetchall()
        # v3.28 SECURITY — /api/dashboard is public (no login); never leak secrets
        # here. Mask any key that looks like a credential; the authed /api/settings
        # still returns real values for the settings page.
        _SECRET = ("api_key", "password", "hash", "token", "secret")
        settings = {r["key"]: ("***" if r["value"] and any(s in r["key"].lower() for s in _SECRET) else r["value"])
                    for r in db.execute("SELECT * FROM settings").fetchall()}
        # v3.19 — map worker_id -> node name so every running job shows which
        # machine (and, with job_type, which pipeline) is doing it. All threads
        # on one node share a worker_id, so the workers table alone can't show
        # a node's concurrent jobs — the processing queue can.
        wname = {w["id"]: w["name"] for w in db.execute("SELECT id, name FROM workers").fetchall()}

    # Build {job_type: {status: count}}
    status_stats_by_type = {}
    for row in ssj:
        jt = row["job_type"] or "transcode"
        status_stats_by_type.setdefault(jt, {})[row["status"]] = row["c"]

    # v3.19 — server host resources + fleet capacity for the overview dashboard
    server_res = {}
    try:
        import psutil
        mem = psutil.virtual_memory()
        server_res = {
            "cpu_pct": psutil.cpu_percent(interval=0.05),
            "mem_used_gb": round(mem.used / (1024**3), 1),
            "mem_total_gb": round(mem.total / (1024**3), 1),
            "mem_pct": round(mem.percent),
        }
    except Exception:
        server_res = {}
    with get_db() as db:
        capacity = _effective_worker_cap(db)

    return jsonify({
        "server": server_res,
        "capacity": capacity,
        "status_stats": {s["status"]: {"count": s["c"], "total_gb": s["gb"]} for s in ss},
        "status_stats_by_type": status_stats_by_type,
        "hdr_stats": {h["hdr_type"]: h["c"] for h in hs},
        "workers": [dict(w) for w in ws],
        "processing": [{**{k:v for k,v in dict(p).items() if k!="probe_data"},
                        "worker_name": wname.get(p["worker_id"], "—")} for p in processing],
        "recent_complete": [{k:v for k,v in dict(c).items() if k!="probe_data"} for c in recent_complete],
        "saved_gb": saved["s"],
        "settings": settings,
        "processing_enabled": settings.get("processing_enabled","false") == "true",
        # v3.4 — per-job-type processing flags so the UI can render
        # individual Start/Pause buttons on each job-type tab
        "processing_enabled_by_type": {
            jt: settings.get(f"processing_enabled_{jt}", "true") == "true"
            for jt in ("transcode", "subgen", "remuxclean", "dv78only", "compatfix")
        },
        "scan_progress": dict(scan_progress),
    })

TOOL_LABELS = {
    "transcode": "Video Transcode",
    "dv78only": "Dolby Vision → P8",
    "remuxclean": "Track Cleanup",
    "compatfix": "Compatibility Fix",
    "subgen": "Subtitle Generation",
}

@app.route("/api/stats")
def api_stats():
    """
    v3.18 — richer per-tool statistics. For every pipeline it reports files
    completed, GB in/out, space actually reclaimed (clamped >=0), average
    reduction %, in-flight count/volume, projected savings (pending volume x
    that tool's historical avg reduction), and errors. Track Cleanup's
    space_saved is the space reclaimed by dropping unwanted audio/subtitle
    tracks; Subtitle Generation's completed count is subtitles generated.
    """
    with get_db() as db:
        comp = db.execute("""
            SELECT COALESCE(NULLIF(job_type,''),'transcode') AS jt,
                   COUNT(*) AS n,
                   COALESCE(SUM(file_size_gb),0) AS in_gb,
                   COALESCE(SUM(COALESCE(output_size_gb,file_size_gb)),0) AS out_gb,
                   COALESCE(SUM(MAX(file_size_gb-COALESCE(output_size_gb,file_size_gb),0)),0) AS saved_gb,
                   COALESCE(AVG(CASE WHEN reduction_pct IS NOT NULL AND reduction_pct>0 THEN reduction_pct END),0) AS avg_red
            FROM queue WHERE status='complete' GROUP BY jt
        """).fetchall()
        st = db.execute("""
            SELECT COALESCE(NULLIF(job_type,''),'transcode') AS jt, status,
                   COUNT(*) AS n, COALESCE(SUM(file_size_gb),0) AS gb
            FROM queue GROUP BY jt, status
        """).fetchall()

    comp_by = {r["jt"]: r for r in comp}
    st_by = {}
    for r in st:
        st_by.setdefault(r["jt"], {})[r["status"]] = {"n": r["n"], "gb": r["gb"]}

    tools, tot = [], {"completed": 0, "saved_gb": 0.0, "projected_gb": 0.0,
                      "errors": 0, "pending": 0, "in_gb": 0.0, "out_gb": 0.0}
    for jt in ("transcode", "dv78only", "remuxclean", "compatfix", "subgen"):
        c = comp_by.get(jt)
        sts = st_by.get(jt, {})
        completed = c["n"] if c else 0
        saved = c["saved_gb"] if c else 0.0
        in_gb = c["in_gb"] if c else 0.0
        out_gb = c["out_gb"] if c else 0.0
        avg_red = c["avg_red"] if c else 0.0
        pending_n = sts.get("pending", {}).get("n", 0) + sts.get("queued", {}).get("n", 0)
        pending_gb = sts.get("pending", {}).get("gb", 0.0) + sts.get("queued", {}).get("gb", 0.0)
        errors = sts.get("error", {}).get("n", 0)
        projected = pending_gb * (avg_red / 100.0) if avg_red > 0 else 0.0
        tools.append({
            "job_type": jt, "label": TOOL_LABELS[jt],
            "completed": completed, "space_saved_gb": round(saved, 2),
            "input_gb": round(in_gb, 2), "output_gb": round(out_gb, 2),
            "avg_reduction_pct": round(avg_red, 1),
            "pending": pending_n, "pending_gb": round(pending_gb, 2),
            "projected_saved_gb": round(projected, 2), "errors": errors,
        })
        tot["completed"] += completed; tot["saved_gb"] += saved
        tot["projected_gb"] += projected; tot["errors"] += errors
        tot["pending"] += pending_n; tot["in_gb"] += in_gb; tot["out_gb"] += out_gb

    for k in ("saved_gb", "projected_gb", "in_gb", "out_gb"):
        tot[k] = round(tot[k], 2)
    subs = comp_by.get("subgen")
    return jsonify({
        "tools": tools,
        "totals": tot,
        "subtitles_generated": subs["n"] if subs else 0,
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
    parser.add_argument("--reset-password", action="store_true", help="Reset admin password (interactive prompt)")
    parser.add_argument("--set-password", metavar="PW", help="Set admin password non-interactively (for docker exec)")
    parser.add_argument("--reset-user", metavar="NAME", help="Also set the admin username")
    args = parser.parse_args()

    init_db()

    if args.set_password is not None:
        if args.reset_user:
            set_setting("auth_user", args.reset_user)
        set_setting("auth_hash", hash_password(args.set_password))
        print(f"Password set for user '{get_setting('auth_user') or 'admin'}'. You can log in now.")
        return

    if args.reset_password:
        import getpass
        pw = getpass.getpass("New password: ")
        set_setting("auth_hash", hash_password(pw))
        print("Password reset successfully.")
        return

    start_health_check_loop()
    start_buffer_flusher()   # v3.13 — async progress/log flush
    log.info(f"Byte Transcode Server v3 on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)

if __name__ == "__main__":
    main()
