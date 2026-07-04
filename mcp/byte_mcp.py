#!/usr/bin/env python3
"""
Byte Transcode MCP Server v1.0
==============================
Exposes the Byte Transcode REST API as MCP tools so AI assistants
(Claude Code, Claude Desktop, or any MCP client) can monitor and control
the transcoding system: scan libraries, manage the queue, start/pause
pipelines, edit settings, and read logs.

Transport: stdio.

Usage:
    py byte_mcp.py --server http://YOUR_NAS_IP:5800 [--api-key KEY]

Or via environment:
    BYTE_SERVER_URL=http://YOUR_NAS_IP:5800  BYTE_API_KEY=...  py byte_mcp.py

Register with Claude Code:
    claude mcp add byte-transcode -- py path/to/byte_mcp.py --server http://YOUR_NAS_IP:5800

The API key is the one from Settings → API in the web UI (server v3.7+
honors it via the X-API-Key header). Without it, only endpoints that don't
require a login session will work.
"""
import os, sys, json, argparse, subprocess

# Auto-install dependencies on first run
for pkg, mod in (("mcp", "mcp"), ("requests", "requests")):
    try:
        __import__(mod)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q", "--break-system-packages"])

import requests
from mcp.server.fastmcp import FastMCP

parser = argparse.ArgumentParser(description="Byte Transcode MCP Server")
parser.add_argument("--server", default=os.environ.get("BYTE_SERVER_URL", "http://localhost:5800"))
parser.add_argument("--api-key", default=os.environ.get("BYTE_API_KEY", ""))
args, _ = parser.parse_known_args()

BASE = args.server.rstrip("/")
HEADERS = {"X-API-Key": args.api_key} if args.api_key else {}

mcp = FastMCP("byte-transcode")

SCAN_ENDPOINTS = {
    "transcode": "/api/libraries/{lid}/scan",
    "remuxclean": "/api/libraries/{lid}/scan-remuxclean",
    "dv78only": "/api/libraries/{lid}/scan-dv78only",
    "subgen": "/api/libraries/{lid}/scan-subgen",
    "compat": "/api/libraries/{lid}/scan-compat",
}
JOB_TYPES = ("transcode", "subgen", "remuxclean", "dv78only", "compatfix")


def _get(path, params=None):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path, body=None):
    r = requests.post(f"{BASE}{path}", headers=HEADERS, json=body or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _put(path, body=None):
    r = requests.put(f"{BASE}{path}", headers=HEADERS, json=body or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _delete(path):
    r = requests.delete(f"{BASE}{path}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_status() -> str:
    """Overall server status: queue counts by status, active workers, version."""
    return json.dumps(_get("/api/status"))


@mcp.tool()
def get_dashboard() -> str:
    """Dashboard snapshot: per-status/per-type stats, currently processing jobs, recent completions, scan progress."""
    d = _get("/api/dashboard")
    # Trim the heaviest fields for LLM consumption
    for j in d.get("processing", []) + d.get("recent_complete", []):
        j.pop("probe_data", None)
    return json.dumps(d)


@mcp.tool()
def list_queue(status: str = "", job_type: str = "", search: str = "", limit: int = 50) -> str:
    """List queue items. status: queued/pending/processing/complete/error/skipped/cancelled (empty = all). job_type: transcode/subgen/remuxclean/dv78only/compatfix. search filters by file name."""
    params = {}
    if status:
        params["status"] = status
    if job_type:
        params["job_type"] = job_type
    if search:
        params["search"] = search
    items = _get("/api/queue", params)
    out = [{k: v for k, v in i.items() if k != "probe_data"} for i in items[:max(1, min(limit, 500))]]
    return json.dumps({"total": len(items), "shown": len(out), "items": out})


@mcp.tool()
def get_job_log(job_id: int, limit: int = 100) -> str:
    """Log lines for a specific job (most recent first)."""
    return json.dumps(_get("/api/logs", {"job_id": job_id, "limit": limit}))


@mcp.tool()
def get_logs(limit: int = 50) -> str:
    """Recent server log lines (most recent first)."""
    return json.dumps(_get("/api/logs", {"limit": limit}))


@mcp.tool()
def list_libraries() -> str:
    """All media libraries with paths, file counts and scan status."""
    return json.dumps(_get("/api/libraries"))


@mcp.tool()
def add_library(name: str, path: str) -> str:
    """Add a media library. path is the SERVER's view (e.g. /media/data/media/movies)."""
    return json.dumps(_post("/api/libraries", {"name": name, "path": path}))


@mcp.tool()
def scan_library(library_id: int, scan_type: str = "transcode") -> str:
    """Start a library scan. scan_type: transcode, remuxclean (track cleanup), dv78only (DV → P8), subgen (AI subtitles), compat (playback compatibility)."""
    ep = SCAN_ENDPOINTS.get(scan_type)
    if not ep:
        return json.dumps({"error": f"scan_type must be one of {sorted(SCAN_ENDPOINTS)}"})
    return json.dumps(_post(ep.format(lid=library_id)))


@mcp.tool()
def get_scan_progress() -> str:
    """Progress of currently running library scans."""
    return json.dumps(_get("/api/scan-progress"))


@mcp.tool()
def start_pipeline(job_type: str = "") -> str:
    """Start processing. job_type starts one pipeline (transcode/subgen/remuxclean/dv78only/compatfix); empty starts the master switch."""
    if job_type:
        if job_type not in JOB_TYPES:
            return json.dumps({"error": f"job_type must be one of {JOB_TYPES}"})
        return json.dumps(_post(f"/api/queue/start/{job_type}"))
    return json.dumps(_post("/api/queue/start"))


@mcp.tool()
def pause_pipeline(job_type: str = "") -> str:
    """Pause processing. job_type pauses one pipeline; empty pauses the master switch (all types)."""
    if job_type:
        if job_type not in JOB_TYPES:
            return json.dumps({"error": f"job_type must be one of {JOB_TYPES}"})
        return json.dumps(_post(f"/api/queue/pause/{job_type}"))
    return json.dumps(_post("/api/queue/pause"))


@mcp.tool()
def job_action(job_id: int, action: str) -> str:
    """Act on a queue item. action: cancel, requeue, skip, accept, force-start, bump-top, delete."""
    actions = {"cancel", "requeue", "skip", "accept", "force-start", "bump-top", "delete"}
    if action not in actions:
        return json.dumps({"error": f"action must be one of {sorted(actions)}"})
    if action == "delete":
        return json.dumps(_delete(f"/api/queue/{job_id}"))
    body = {"reason": "Skipped via MCP"} if action == "skip" else {}
    return json.dumps(_post(f"/api/queue/{job_id}/{action}", body))


@mcp.tool()
def requeue_by_status(status: str, job_type: str = "") -> str:
    """Bulk-requeue jobs by status (error/skipped/cancelled), optionally limited to one job_type."""
    body = {"job_type": job_type} if job_type else {}
    return json.dumps(_post(f"/api/queue/requeue-status/{status}", body))


@mcp.tool()
def get_settings() -> str:
    """All server settings (secrets redacted)."""
    s = _get("/api/settings")
    for k in ("claude_api_key", "auth_hash", "api_key"):
        if s.get(k):
            s[k] = "***redacted***"
    return json.dumps(s)


@mcp.tool()
def update_settings(settings_json: str) -> str:
    """Update server settings. Pass a JSON object of key/value pairs, e.g. {"cq": "18", "staged_limit": "100"}. Requires the API key."""
    try:
        body = json.loads(settings_json)
        assert isinstance(body, dict)
    except Exception:
        return json.dumps({"error": "settings_json must be a JSON object"})
    body.pop("auth_hash", None)
    return json.dumps(_put("/api/settings", body))


@mcp.tool()
def list_workers() -> str:
    """Registered worker nodes with live metrics and current jobs."""
    return json.dumps(_get("/api/workers"))


@mcp.tool()
def get_worker_config(worker_id: str) -> str:
    """Per-node setting overrides for a worker (temp path, path mapping, worker counts)."""
    return json.dumps(_get(f"/api/workers/{worker_id}/config"))


@mcp.tool()
def set_worker_config(worker_id: str, config_json: str) -> str:
    """Set per-node overrides for a worker. JSON object; useful keys: node_temp_path, node_path_remote_prefix, node_path_local_prefix, transcode_gpu_count, healthcheck_gpu_count. The node applies changes within ~60s."""
    try:
        body = json.loads(config_json)
        assert isinstance(body, dict)
    except Exception:
        return json.dumps({"error": "config_json must be a JSON object"})
    return json.dumps(_post(f"/api/workers/{worker_id}/config", body))


if __name__ == "__main__":
    mcp.run()
