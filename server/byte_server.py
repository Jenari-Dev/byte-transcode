#!/usr/bin/env python3
"""
Byte Transcode Server
=====================
Flask API server that manages the transcode queue, library scanning,
and communicates with Byte Node workers.

Runs on the NAS as a Docker container.
Access the dashboard at http://<NAS_IP>:5800

Architecture:
  Server (NAS)  →  Scans libraries, manages queue, serves web UI
  Node (Windows) →  Pulls jobs, transcodes with GPU, reports progress
"""

import os
import sys
import json
import time
import sqlite3
import hashlib
import subprocess
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
from flask import Flask, request, jsonify, send_from_directory, Response

# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_PORT = 5800
DB_PATH = os.environ.get("BYTE_DB_PATH", "/config/byte_transcode.db")
LOG_DIR = os.environ.get("BYTE_LOG_DIR", "/config/logs")
STATIC_DIR = os.environ.get("BYTE_STATIC_DIR", "/app/static")

# ─── Flask App ───────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=STATIC_DIR)
app.config["SECRET_KEY"] = os.environ.get("BYTE_SECRET", "byte-transcode-secret")

# ─── Logging ─────────────────────────────────────────────────────────────────

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "server.log")),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("byte-server")


# ─── Database ────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Thread-safe database connection."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
    """Initialize database schema."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                file_size_gb REAL NOT NULL,
                duration_min REAL DEFAULT 0,
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
                status TEXT DEFAULT 'queued',
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
                created_at TEXT DEFAULT (datetime('now')),
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
                registered_at TEXT DEFAULT (datetime('now')),
                jobs_completed INTEGER DEFAULT 0,
                total_saved_gb REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                level TEXT DEFAULT 'INFO',
                source TEXT DEFAULT 'server',
                message TEXT NOT NULL,
                job_id INTEGER
            );
        """)

        # Default settings
        defaults = {
            "cq": "18",
            "preset": "slow",
            "max_workers": "4",
            "min_size_gb": "10",
            "container": "mkv",
            "dovi_convert_p8": "true",
            "replace_original": "true",
            "temp_path": "/temp/byte_work",
            "gpu": "RTX 5080",
            "processing_enabled": "false",
        }
        for key, value in defaults.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )

    log.info("Database initialized")


def add_log(message, level="INFO", source="server", job_id=None):
    """Add a log entry to the database."""
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO logs (level, source, message, job_id) VALUES (?, ?, ?, ?)",
                (level, source, message, job_id)
            )
    except Exception:
        pass  # Don't let logging failures crash anything


# ─── Library Scanner ─────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.flv', '.webm'}


def find_ffprobe():
    """Find ffprobe binary."""
    candidates = ["ffprobe", "tdarr-ffprobe",
                   "/usr/bin/ffprobe", "/usr/local/bin/ffprobe"]
    for c in candidates:
        try:
            result = subprocess.run([c, "-version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return c
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "ffprobe"


FFPROBE = find_ffprobe()


def probe_file(path):
    """Probe a media file and return structured info dict."""
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except Exception:
        return None

    video = None
    audio_streams = []
    sub_streams = []

    for s in data.get("streams", []):
        ct = s.get("codec_type", "")
        if ct == "video" and s.get("codec_name") in ("hevc", "h264", "av1", "vp9") and video is None:
            video = s
        elif ct == "audio":
            audio_streams.append(s)
        elif ct == "subtitle":
            sub_streams.append(s)

    if not video:
        return None

    # HDR detection
    color_transfer = video.get("color_transfer", "")
    has_hdr10 = color_transfer == "smpte2084"
    has_hlg = color_transfer == "arib-std-b67"
    has_dovi = False
    has_hdr10plus = False
    dovi_profile = None

    for sd in video.get("side_data_list", []):
        sdt = sd.get("side_data_type", "")
        if "DOVI" in sdt:
            has_dovi = True
            dovi_profile = sd.get("dv_profile")
        if "HDR10+" in sdt or "hdr10plus" in sdt.lower():
            has_hdr10plus = True

    if has_dovi:
        hdr_type = "DoVi"
    elif has_hdr10plus:
        hdr_type = "HDR10+"
    elif has_hdr10:
        hdr_type = "HDR10"
    elif has_hlg:
        hdr_type = "HLG"
    else:
        hdr_type = "SDR"

    # Already transcoded check
    tags = video.get("tags", {})
    encoder = tags.get("ENCODER", tags.get("encoder", ""))
    already_transcoded = "nvenc" in encoder.lower()

    # Audio summary
    audio_summary = ""
    if audio_streams:
        first = audio_streams[0]
        codec = first.get("codec_name", "")
        channels = first.get("channels", 0)
        profile = first.get("profile", "")
        if "truehd" in codec.lower():
            audio_summary = f"TrueHD Atmos {channels}ch" if "atmos" in profile.lower() or channels >= 8 else f"TrueHD {channels}ch"
        elif "dts" in codec.lower():
            audio_summary = f"DTS-HD MA {channels}ch" if "ma" in profile.lower() else f"DTS {channels}ch"
        elif codec == "flac":
            audio_summary = f"FLAC {channels}ch"
        elif codec == "ac3":
            audio_summary = f"DD {channels}ch"
        elif codec == "eac3":
            audio_summary = f"DDP {channels}ch"
        else:
            audio_summary = f"{codec} {channels}ch"

    fmt = data.get("format", {})
    w = video.get("width", 0)
    h = video.get("height", 0)

    return {
        "file_size_gb": int(fmt.get("size", 0)) / (1024**3),
        "duration_min": float(fmt.get("duration", 0)) / 60,
        "video_codec": video.get("codec_name", ""),
        "resolution": f"{w}x{h}",
        "fps": video.get("r_frame_rate", ""),
        "hdr_type": hdr_type,
        "has_dovi": has_dovi,
        "dovi_profile": dovi_profile,
        "audio_summary": audio_summary,
        "audio_track_count": len(audio_streams),
        "subtitle_track_count": len(sub_streams),
        "already_transcoded": already_transcoded,
        "probe_data": json.dumps(data),
    }


def scan_library_task(library_id):
    """Background task to scan a library and populate the queue."""
    with get_db() as db:
        lib = db.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
        if not lib:
            return

        db.execute("UPDATE libraries SET status = 'scanning' WHERE id = ?", (library_id,))
        settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
        min_size = float(settings.get("min_size_gb", "10"))

    path = lib["path"]
    add_log(f"Scanning library: {lib['name']} ({path})")

    file_count = 0
    total_size = 0
    added = 0
    skipped = 0

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
            total_size += size_gb

            if size_gb < min_size:
                skipped += 1
                continue

            # Check if already in queue
            with get_db() as db:
                exists = db.execute(
                    "SELECT id FROM queue WHERE file_path = ?", (filepath,)
                ).fetchone()
                if exists:
                    skipped += 1
                    continue

            # Probe file
            info = probe_file(filepath)
            if not info:
                skipped += 1
                continue

            if info["already_transcoded"]:
                skipped += 1
                continue

            # Add to queue
            with get_db() as db:
                db.execute("""
                    INSERT INTO queue (
                        file_path, file_name, file_size_gb, duration_min,
                        video_codec, resolution, fps, hdr_type, has_dovi,
                        dovi_profile, audio_summary, audio_track_count,
                        subtitle_track_count, library_id, library_name,
                        status, probe_data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """, (
                    filepath, filename, info["file_size_gb"], info["duration_min"],
                    info["video_codec"], info["resolution"], info["fps"],
                    info["hdr_type"], int(info["has_dovi"]), info["dovi_profile"],
                    info["audio_summary"], info["audio_track_count"],
                    info["subtitle_track_count"], library_id, lib["name"],
                    info["probe_data"],
                ))
                added += 1

            add_log(f"  Queued: {filename} ({size_gb:.1f} GB, {info['hdr_type']})")

    # Update library stats
    with get_db() as db:
        db.execute("""
            UPDATE libraries SET
                file_count = ?, total_size_gb = ?,
                last_scanned = datetime('now'), status = 'scanned'
            WHERE id = ?
        """, (file_count, total_size, library_id))

    add_log(f"Scan complete: {lib['name']} — {added} added, {skipped} skipped, {file_count} total files")


# ─── API Routes ──────────────────────────────────────────────────────────────

# --- Status ---
@app.route("/api/status")
def api_status():
    with get_db() as db:
        queue_stats = db.execute("""
            SELECT status, COUNT(*) as count, SUM(file_size_gb) as total_gb
            FROM queue GROUP BY status
        """).fetchall()
        worker_count = db.execute(
            "SELECT COUNT(*) as c FROM workers WHERE last_heartbeat > datetime('now', '-60 seconds')"
        ).fetchone()["c"]

    stats = {s["status"]: {"count": s["count"], "total_gb": s["total_gb"] or 0} for s in queue_stats}
    return jsonify({
        "status": "running",
        "queue": stats,
        "active_workers": worker_count,
        "version": "1.0.0",
    })


# --- Libraries ---
@app.route("/api/libraries", methods=["GET"])
def api_list_libraries():
    with get_db() as db:
        libs = db.execute("SELECT * FROM libraries ORDER BY name").fetchall()
    return jsonify([dict(l) for l in libs])


@app.route("/api/libraries", methods=["POST"])
def api_add_library():
    data = request.json
    name = data.get("name", "").strip()
    path = data.get("path", "").strip()

    if not name or not path:
        return jsonify({"error": "Name and path required"}), 400

    if not os.path.isdir(path):
        return jsonify({"error": f"Path not found: {path}"}), 400

    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO libraries (name, path) VALUES (?, ?)",
                (name, path)
            )
            lib_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        except sqlite3.IntegrityError:
            return jsonify({"error": "Library path already exists"}), 409

    add_log(f"Library added: {name} ({path})")
    return jsonify({"id": lib_id, "name": name, "path": path}), 201


@app.route("/api/libraries/<int:lib_id>", methods=["DELETE"])
def api_remove_library(lib_id):
    with get_db() as db:
        db.execute("DELETE FROM queue WHERE library_id = ? AND status = 'queued'", (lib_id,))
        db.execute("DELETE FROM libraries WHERE id = ?", (lib_id,))
    add_log(f"Library removed: #{lib_id}")
    return jsonify({"ok": True})


@app.route("/api/libraries/<int:lib_id>/scan", methods=["POST"])
def api_scan_library(lib_id):
    thread = threading.Thread(target=scan_library_task, args=(lib_id,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Scan started"})


# --- Queue ---
@app.route("/api/queue", methods=["GET"])
def api_list_queue():
    status = request.args.get("status")
    hdr_type = request.args.get("hdr_type")
    library = request.args.get("library")
    sort = request.args.get("sort", "priority")
    search = request.args.get("search", "")

    query = "SELECT * FROM queue WHERE 1=1"
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if hdr_type:
        query += " AND hdr_type = ?"
        params.append(hdr_type)
    if library:
        query += " AND library_name = ?"
        params.append(library)
    if search:
        query += " AND file_name LIKE ?"
        params.append(f"%{search}%")

    sort_map = {
        "priority": "priority ASC, id ASC",
        "size-desc": "file_size_gb DESC",
        "size-asc": "file_size_gb ASC",
        "name": "file_name ASC",
        "hdr": "hdr_type ASC, file_name ASC",
        "status": "status ASC, priority ASC",
    }
    query += f" ORDER BY {sort_map.get(sort, 'priority ASC, id ASC')}"

    with get_db() as db:
        items = db.execute(query, params).fetchall()

    result = []
    for item in items:
        d = dict(item)
        d.pop("probe_data", None)  # Don't send raw probe data to frontend
        result.append(d)

    return jsonify(result)


@app.route("/api/queue/<int:job_id>/bump", methods=["POST"])
def api_bump_job(job_id):
    direction = request.json.get("direction", "up")
    with get_db() as db:
        job = db.execute("SELECT * FROM queue WHERE id = ?", (job_id,)).fetchone()
        if not job or job["status"] != "queued":
            return jsonify({"error": "Can only bump queued items"}), 400

        if direction == "up":
            swap = db.execute(
                "SELECT * FROM queue WHERE status = 'queued' AND priority < ? ORDER BY priority DESC LIMIT 1",
                (job["priority"],)
            ).fetchone()
        else:
            swap = db.execute(
                "SELECT * FROM queue WHERE status = 'queued' AND priority > ? ORDER BY priority ASC LIMIT 1",
                (job["priority"],)
            ).fetchone()

        if swap:
            db.execute("UPDATE queue SET priority = ? WHERE id = ?", (swap["priority"], job_id))
            db.execute("UPDATE queue SET priority = ? WHERE id = ?", (job["priority"], swap["id"]))

    return jsonify({"ok": True})


@app.route("/api/queue/<int:job_id>/cancel", methods=["POST"])
def api_cancel_job(job_id):
    with get_db() as db:
        db.execute(
            "UPDATE queue SET status = 'cancelled', error_message = 'Cancelled by user' WHERE id = ? AND status IN ('queued', 'processing', 'scanning')",
            (job_id,)
        )
    add_log(f"Job #{job_id} cancelled", job_id=job_id)
    return jsonify({"ok": True})


@app.route("/api/queue/<int:job_id>", methods=["DELETE"])
def api_remove_job(job_id):
    with get_db() as db:
        db.execute("DELETE FROM queue WHERE id = ?", (job_id,))
    return jsonify({"ok": True})


@app.route("/api/queue/start", methods=["POST"])
def api_start_processing():
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('processing_enabled', 'true')")
    add_log("Processing started")
    return jsonify({"ok": True})


@app.route("/api/queue/pause", methods=["POST"])
def api_pause_processing():
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('processing_enabled', 'false')")
    add_log("Processing paused")
    return jsonify({"ok": True})


@app.route("/api/queue/clear/<string:status>", methods=["POST"])
def api_clear_queue(status):
    """Clear completed, errored, or cancelled items from queue."""
    if status not in ("complete", "error", "cancelled", "skipped"):
        return jsonify({"error": "Can only clear complete/error/cancelled/skipped"}), 400
    with get_db() as db:
        db.execute("DELETE FROM queue WHERE status = ?", (status,))
    add_log(f"Cleared {status} items from queue")
    return jsonify({"ok": True})


# --- Workers / Nodes ---
@app.route("/api/workers", methods=["GET"])
def api_list_workers():
    with get_db() as db:
        workers = db.execute("SELECT * FROM workers ORDER BY registered_at").fetchall()
    return jsonify([dict(w) for w in workers])


@app.route("/api/workers/register", methods=["POST"])
def api_register_worker():
    data = request.json
    worker_id = data.get("id", "")
    name = data.get("name", "unknown")
    host = data.get("host", "")
    gpu = data.get("gpu", "")

    if not worker_id:
        return jsonify({"error": "Worker ID required"}), 400

    with get_db() as db:
        db.execute("""
            INSERT OR REPLACE INTO workers (id, name, host, gpu, status, last_heartbeat)
            VALUES (?, ?, ?, ?, 'idle', datetime('now'))
        """, (worker_id, name, host, gpu))

    add_log(f"Worker registered: {name} ({gpu})")
    return jsonify({"ok": True})


@app.route("/api/workers/heartbeat", methods=["POST"])
def api_worker_heartbeat():
    data = request.json
    worker_id = data.get("id", "")
    with get_db() as db:
        db.execute(
            "UPDATE workers SET last_heartbeat = datetime('now') WHERE id = ?",
            (worker_id,)
        )
    return jsonify({"ok": True})


# --- Job Assignment (Node pulls work) ---
@app.route("/api/jobs/next", methods=["POST"])
def api_next_job():
    """Node requests the next available job."""
    data = request.json
    worker_id = data.get("worker_id", "")

    with get_db() as db:
        # Check if processing is enabled
        enabled = db.execute(
            "SELECT value FROM settings WHERE key = 'processing_enabled'"
        ).fetchone()
        if not enabled or enabled["value"] != "true":
            return jsonify({"job": None, "reason": "Processing paused"})

        # Check worker limit
        settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
        max_workers = int(settings.get("max_workers", "4"))

        active = db.execute(
            "SELECT COUNT(*) as c FROM queue WHERE status = 'processing'"
        ).fetchone()["c"]

        if active >= max_workers:
            return jsonify({"job": None, "reason": "Max workers reached"})

        # Get next queued job
        job = db.execute(
            "SELECT * FROM queue WHERE status = 'queued' ORDER BY priority ASC, id ASC LIMIT 1"
        ).fetchone()

        if not job:
            return jsonify({"job": None, "reason": "No jobs in queue"})

        # Assign to worker
        db.execute("""
            UPDATE queue SET
                status = 'processing', worker_id = ?,
                started_at = datetime('now'), current_step = 'Starting...'
            WHERE id = ?
        """, (worker_id, job["id"]))

        db.execute(
            "UPDATE workers SET status = 'active', current_job_id = ? WHERE id = ?",
            (job["id"], worker_id)
        )

        # Return job with settings
        job_data = dict(job)
        job_data["settings"] = settings

    add_log(f"Job #{job['id']} assigned to worker {worker_id}: {job['file_name']}", job_id=job["id"])
    return jsonify({"job": job_data})


@app.route("/api/jobs/<int:job_id>/progress", methods=["POST"])
def api_update_progress(job_id):
    """Node reports progress on a job."""
    data = request.json
    with get_db() as db:
        db.execute("""
            UPDATE queue SET
                progress = ?, current_step = ?, eta = ?
            WHERE id = ?
        """, (
            data.get("progress", 0),
            data.get("step", ""),
            data.get("eta", ""),
            job_id
        ))
    return jsonify({"ok": True})


@app.route("/api/jobs/<int:job_id>/complete", methods=["POST"])
def api_complete_job(job_id):
    """Node reports job completion."""
    data = request.json
    worker_id = data.get("worker_id", "")

    with get_db() as db:
        db.execute("""
            UPDATE queue SET
                status = 'complete', progress = 100,
                current_step = 'Done',
                output_path = ?, output_size_gb = ?,
                reduction_pct = ?, completed_at = datetime('now')
            WHERE id = ?
        """, (
            data.get("output_path", ""),
            data.get("output_size_gb", 0),
            data.get("reduction_pct", 0),
            job_id
        ))

        db.execute("""
            UPDATE workers SET
                status = 'idle', current_job_id = NULL,
                jobs_completed = jobs_completed + 1,
                total_saved_gb = total_saved_gb + ?
            WHERE id = ?
        """, (data.get("saved_gb", 0), worker_id))

    add_log(f"Job #{job_id} complete: {data.get('reduction_pct', 0):.0f}% reduction", job_id=job_id)
    return jsonify({"ok": True})


@app.route("/api/jobs/<int:job_id>/error", methods=["POST"])
def api_error_job(job_id):
    """Node reports job failure."""
    data = request.json
    worker_id = data.get("worker_id", "")

    with get_db() as db:
        db.execute("""
            UPDATE queue SET
                status = 'error', progress = 0,
                current_step = 'Failed',
                error_message = ?, completed_at = datetime('now')
            WHERE id = ?
        """, (data.get("error", "Unknown error"), job_id))

        db.execute(
            "UPDATE workers SET status = 'idle', current_job_id = NULL WHERE id = ?",
            (worker_id,)
        )

    add_log(f"Job #{job_id} failed: {data.get('error', 'Unknown')}", level="ERROR", job_id=job_id)
    return jsonify({"ok": True})


# --- Settings ---
@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    with get_db() as db:
        rows = db.execute("SELECT * FROM settings").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/settings", methods=["PUT"])
def api_update_settings():
    data = request.json
    with get_db() as db:
        for key, value in data.items():
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value))
            )
    add_log(f"Settings updated: {list(data.keys())}")
    return jsonify({"ok": True})


# --- Logs ---
@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    limit = request.args.get("limit", 100, type=int)
    job_id = request.args.get("job_id", type=int)
    level = request.args.get("level")

    query = "SELECT * FROM logs WHERE 1=1"
    params = []

    if job_id:
        query += " AND job_id = ?"
        params.append(job_id)
    if level:
        query += " AND level = ?"
        params.append(level)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with get_db() as db:
        rows = db.execute(query, params).fetchall()

    return jsonify([dict(r) for r in rows])


# --- Dashboard Stats ---
@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    with get_db() as db:
        # Queue stats by status
        status_stats = db.execute("""
            SELECT status, COUNT(*) as count, COALESCE(SUM(file_size_gb), 0) as total_gb
            FROM queue GROUP BY status
        """).fetchall()

        # HDR breakdown (non-complete only)
        hdr_stats = db.execute("""
            SELECT hdr_type, COUNT(*) as count
            FROM queue WHERE status NOT IN ('complete', 'cancelled')
            GROUP BY hdr_type
        """).fetchall()

        # Active workers
        workers = db.execute("""
            SELECT w.*, q.file_name as current_file, q.progress as job_progress,
                   q.current_step, q.eta as job_eta, q.hdr_type, q.dovi_profile,
                   q.file_size_gb
            FROM workers w
            LEFT JOIN queue q ON w.current_job_id = q.id
            WHERE w.last_heartbeat > datetime('now', '-60 seconds')
        """).fetchall()

        # Total saved
        saved = db.execute("""
            SELECT COALESCE(SUM(file_size_gb - COALESCE(output_size_gb, file_size_gb)), 0) as saved_gb
            FROM queue WHERE status = 'complete'
        """).fetchone()

        # Settings
        settings = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}

    return jsonify({
        "status_stats": {s["status"]: {"count": s["count"], "total_gb": s["total_gb"]} for s in status_stats},
        "hdr_stats": {h["hdr_type"]: h["count"] for h in hdr_stats},
        "workers": [dict(w) for w in workers],
        "saved_gb": saved["saved_gb"],
        "settings": settings,
        "processing_enabled": settings.get("processing_enabled", "false") == "true",
    })


# --- Serve Frontend ---
@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory(STATIC_DIR, path)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Byte Transcode Server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    init_db()
    log.info(f"Byte Transcode Server starting on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
