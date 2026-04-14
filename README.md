<p align="center">
  <img src="https://img.shields.io/badge/Byte-Transcode-E040FB?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAzMiAzMic+PHJlY3Qgd2lkdGg9JzMyJyBoZWlnaHQ9JzMyJyByeD0nNicgZmlsbD0nIzEwMTAxMCcvPjx0ZXh0IHg9JzE2JyB5PScyMicgdGV4dC1hbmNob3I9J21pZGRsZScgZmlsbD0nI0UwNDBGQicgZm9udC1zaXplPScxNicgZm9udC13ZWlnaHQ9J2JvbGQnIGZvbnQtZmFtaWx5PSdtb25vc3BhY2UnPkI8L3RleHQ+PC9zdmc+" alt="Byte Transcode"/>
  <br/>
  <strong>A self-hosted media transcoding tool with Dolby Vision support — a Tdarr alternative</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue?style=flat-square" alt="Python"/>
  <img src="https://img.shields.io/badge/GPU-NVENC-76B900?style=flat-square" alt="NVENC"/>
  <img src="https://img.shields.io/badge/Dolby_Vision-P7→P8-E040FB?style=flat-square" alt="DoVi"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"/>
  <img src="https://img.shields.io/badge/docker-ready-2496ED?style=flat-square" alt="Docker"/>
</p>

---

## Screenshots

> _Coming soon — Dashboard, Settings, Node GUI, Active Transcode, Staging Section_

---

## Features

- **Full Dolby Vision P7→P8 pipeline** — Extract HEVC → Extract RPU → NVENC transcode → Inject RPU → Convert P7→P8 → mkvmerge remux
- **HDR10, HDR10+, HLG, SDR support** — All HDR formats handled automatically
- **Preserves everything** — All audio tracks (TrueHD Atmos, DTS-HD MA, etc.), subtitles, chapters
- **NVENC GPU hardware encoding** with CUDA hardware decoding (near-zero CPU usage)
- **DoVi concurrency control** — Limit heavy DoVi jobs while filling remaining slots with HDR10/SDR
- **Remux mode** — Convert container formats (MKV↔MP4) without re-encoding
- **Real-time dashboard** — Progress, FPS, ETA, compression stats, live log streaming
- **Library scanning** with parallel health checks
- **Queue management** — Bump, skip, requeue, force-start, cancel with process kill
- **Three themes** — Dark, Carbon, Cobalt
- **ntfy.sh push notifications** — Job complete, errors, scan complete
- **Native Windows node** with Tdarr-style GUI
- **Path translator** for NAS → Windows drive mapping
- **Local SSD temp processing** for maximum transcode speed
- **Crash recovery** — Resumes after node/server restart, cleans up temp files
- **Auto-accept** — Optionally replace originals automatically

---

## Architecture

```
┌─────────────────────────────┐         ┌──────────────────────────────────┐
│  SERVER (NAS / Docker)      │  HTTP   │  NODE (Windows / Native)         │
│                             │◄───────►│                                  │
│  Flask + SQLite + React UI  │  :5800  │  Python + ffmpeg + dovi_tool     │
│  Libraries, Queue, Settings │         │  + mkvmerge + NVIDIA GPU         │
│                             │         │                                  │
│  /media/data/media/...      │         │  Z:\data\media\...  (mapped)     │
│  (NAS storage)              │         │  F:\Byte_Engine_temp (local SSD) │
└─────────────────────────────┘         └──────────────────────────────────┘
```

**File flow:** NAS media → read over network → GPU transcode to local SSD → copy result back to NAS → replace original (if accepted)

For DoVi files, the pipeline extracts the raw HEVC bitstream and Dolby Vision RPU metadata to the local SSD, transcodes with NVENC, re-injects the RPU, converts Profile 7 to Profile 8 for maximum compatibility, then remuxes with all original audio/subtitle tracks.

---

## Requirements

### Server (NAS / Docker host)
- Docker and Docker Compose
- Linux, Unraid, or any Docker-capable NAS
- Network-accessible media storage

### Node (Windows GPU machine)
- Windows 10 or 11
- Python 3.10+ ([python.org](https://www.python.org/downloads/) or Microsoft Store)
- NVIDIA GPU with NVENC support (GTX 1650+ / RTX series)
- Latest [NVIDIA drivers](https://www.nvidia.com/Download/index.aspx)
- NAS media accessible via mapped network drive (e.g. `Z:\`)
- Local SSD recommended for temp directory (dramatically faster than NAS for temp files)

### Network
- NAS and GPU machine on the same network
- NAS media path mapped as a Windows drive letter (e.g. `Z:\` → `\\NAS\media`)

---

## Installation

### Step 1: Deploy the Server (NAS)

**Option A — Docker Compose (recommended)**

Create a directory on your NAS for the server files:

```bash
mkdir -p /home/YOUR_USER/byte-transcode/server/app/static
mkdir -p /home/YOUR_USER/byte-transcode/server/config
```

Download the server files from this repo into the `app/` folder:

```bash
cd /home/YOUR_USER/byte-transcode/server/app
wget https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main/server/byte_server_v3.py
wget -O static/index.html https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main/server/static/index.html
```

Create `docker-compose.yml` in the server directory:

```yaml
services:
  byte-server:
    image: python:3.12-slim
    container_name: byte-server
    restart: unless-stopped
    ports:
      - "5800:5800"
    volumes:
      - ./app/byte_server_v3.py:/app/byte_server.py    # Note: mounted AS byte_server.py
      - ./app/static:/app/static
      - ./config:/config
      - /mnt/media:/media:ro                            # Your media path (read-only)
    working_dir: /app
    command: >
      bash -c "pip install flask --quiet --break-system-packages &&
               python3 /app/byte_server.py --port 5800"
```

> **Important:** The bind mount maps `byte_server_v3.py` → `/app/byte_server.py` because the container runs `python3 /app/byte_server.py`. Adjust the `/mnt/media` path to match your NAS media root.

Start the server:

```bash
docker compose up -d
```

**Option B — Docker Run (manual)**

```bash
docker run -d \
  --name byte-server \
  --restart unless-stopped \
  -p 5800:5800 \
  -v /path/to/app/byte_server_v3.py:/app/byte_server.py \
  -v /path/to/app/static:/app/static \
  -v /path/to/config:/config \
  -v /mnt/media:/media:ro \
  python:3.12-slim \
  bash -c "pip install flask --quiet --break-system-packages && python3 /app/byte_server.py --port 5800"
```

**Verify** — Open `http://YOUR_NAS_IP:5800` in your browser. You should see the Byte Transcode login screen. Create your admin account on first visit.

---

### Step 2: Set Up the Node (Windows)

**Download the node files:**

```powershell
git clone https://github.com/Jenari-Dev/byte-transcode.git
cd byte-transcode\node
```

Or download the ZIP from GitHub and extract the `node/` folder.

**Install Python dependencies:**

```
pip install requests
```

**Download required tools** (ffmpeg, dovi_tool, mkvmerge):

```
py setup_tools.py
```

This downloads all three tools into the `tools/` subfolder automatically. Alternatively, download them manually:

| Tool | Download | Place in |
|------|----------|----------|
| ffmpeg | [BtbN GPL build](https://github.com/BtbN/FFmpeg-Builds/releases) (ffmpeg-master-latest-win64-gpl.zip) | `node/tools/ffmpeg.exe` + `ffprobe.exe` |
| dovi_tool | [quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool/releases) | `node/tools/dovi_tool.exe` |
| mkvmerge | [MKVToolNix](https://mkvtoolnix.download/downloads.html#windows) (portable) | `node/tools/mkvmerge.exe` |

**Configure the node** — Edit `byte_node_config.json` (or copy from `byte_node_config.example.json`):

```json
{
  "node_name": "MyNode",
  "server_url": "http://YOUR_NAS_IP:5800",
  "gpu": "RTX 5080",
  "poll_interval": 10,
  "path_from": "/media",
  "path_to": "Z:\\",
  "temp_dir": "F:\\Byte_Engine_temp",
  "ffmpeg_path": "",
  "ffprobe_path": "",
  "dovi_tool_path": "",
  "mkvmerge_path": "",
  "start_paused": false
}
```

**Key settings to configure:**

| Setting | What it does | Example |
|---------|-------------|---------|
| `server_url` | Your NAS IP + port where the server runs | `http://192.168.1.100:5800` |
| `path_from` | The media path as the server sees it (inside Docker) | `/media` |
| `path_to` | The same media as your Windows mapped drive | `Z:\\` |
| `temp_dir` | **Local SSD path** for temp files during transcoding. Use your fastest drive — DoVi jobs write 50-80 GB of temp data per file. NAS temp is 5-10x slower. | `F:\\Byte_Engine_temp` |
| `gpu` | Your GPU name (cosmetic, shown in the dashboard) | `RTX 4090` |

> **Path translator explained:** The server sees media at `/media/data/media/movies/...` (inside Docker). Your Windows machine sees the same files at `Z:\data\media\movies\...` via a mapped network drive. The `path_from`/`path_to` settings tell the node how to convert between the two.

**Start the node:**

Double-click `run_node.bat` or run manually:

```
py byte_node_gui.py
```

---

### Step 3: First-Time Setup

1. Open `http://YOUR_NAS_IP:5800` and create your admin account
2. Go to **Libraries** → Add your media folder (use the server's Docker path, e.g. `/media/data/media/movies`)
3. Click **Scan** — the server probes each file with ffprobe and queues them
4. Start the Windows node — it registers with the server automatically
5. Go to **Dashboard** → Click **Start Processing**
6. Set the **Staged file limit** to 10 (controls how many files are queued at once)
7. Adjust **GPU workers** via the +/- buttons on the worker card (4 recommended for RTX 5080)
8. Watch transcodes progress in real-time

---

## Configuration Reference

### Node Config (`byte_node_config.json`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `node_name` | string | `"MyNode"` | Display name shown in the dashboard |
| `server_url` | string | | Byte Transcode server URL (http://IP:5800) |
| `gpu` | string | | GPU name (cosmetic label for dashboard) |
| `poll_interval` | int | `10` | Seconds between checking server for new jobs (5-30 recommended) |
| `path_from` | string | `"/media"` | Server-side media path prefix (what Docker sees) |
| `path_to` | string | `"Z:\\"` | Windows-side mapped drive equivalent |
| `temp_dir` | string | | Local path for temp files — use fastest SSD |
| `ffmpeg_path` | string | `""` | Override ffmpeg path (blank = auto-detect from tools/) |
| `ffprobe_path` | string | `""` | Override ffprobe path (blank = auto-detect) |
| `dovi_tool_path` | string | `""` | Override dovi_tool path (blank = auto-detect) |
| `mkvmerge_path` | string | `""` | Override mkvmerge path (blank = auto-detect) |
| `start_paused` | bool | `false` | If true, node starts but doesn't process until you click Start |

### Server Settings (Web UI → Settings)

| Setting | Default | Description |
|---------|---------|-------------|
| Constant Quality (CQ) | 18 | Lower = better quality, larger files. 16-20 for 4K. |
| NVENC Preset | p7 | P1 (fastest) to P7 (slowest/best compression) |
| Output Codec | HEVC | HEVC (H.265), H.264, or AV1 |
| Hardware Encoder | NVENC | NVENC (NVIDIA), QSV (Intel), VAAPI (Linux), Software |
| Hardware Decoding | Enabled | Use GPU for decoding (reduces CPU to near 0%) |
| Max Workers | 4 | Server-side cap on concurrent processing jobs |
| Min File Size | 10 GB | Skip files smaller than this |
| Container | MKV | Output container format (MKV or MP4) |
| DoVi P7→P8 | Enabled | Convert Dolby Vision Profile 7 to Profile 8 |
| Replace Original | Enabled | Replace source file after transcode |
| Auto-Accept | Disabled | Automatically accept completed transcodes |
| Skip Transcoded | Enabled | Skip files already encoded with NVENC |
| Max DoVi Concurrent | 2 | Limit heavy DoVi pipeline jobs (remaining workers get HDR10/SDR) |
| Processing Mode | Transcode | "Transcode" (compress) or "Remux only" (container conversion) |
| Staged File Limit | 10 | How many files to stage for processing at once |

---

## Usage

### Adding a Library
Go to **Libraries** → enter a name and the server-side path (e.g. `/media/data/media/movies`) → click **+ Add** → click **Scan**.

### Starting Processing
**Dashboard** → **Start Processing**. The node picks up jobs automatically. Adjust worker counts with the +/- buttons on the worker card.

### Monitoring
The dashboard shows active transcodes with real-time progress bars, FPS, ETA, and compression ratios. Click any file to see detailed logs. The **Staging** section shows health checks in progress and queued files.

### Accepting Results
Completed transcodes appear in the **Completed** panel. Click **Accept** to replace the original, or enable **Auto-accept** in Options to do this automatically.

### Requeuing Failed Jobs
Click the **Errored** tab → **Requeue All Errors**. Or click individual files to requeue them.

### Changing Worker Counts
Use the +/- buttons on the worker card in the Dashboard. **Restart the node GUI** after changing for the new counts to take effect.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "File not found" during transcode | Check your path translator: `path_from` must match the Docker media mount, `path_to` must match your Windows mapped drive. Verify with `dir Z:\data\media\movies` |
| Node shows "server unreachable" | Verify the server URL in config. Test: `curl http://YOUR_NAS_IP:5800/api/worker-counts` |
| NVENC errors | Update NVIDIA drivers. Verify GPU supports NVENC: `nvidia-smi` |
| High CPU usage during transcode | Ensure hardware decoding is enabled in Settings. The node should use `-hwaccel cuda` |
| Scans appear stuck | Restart the server: `docker restart byte-server`. This clears any SQLite lock contention |
| DoVi jobs fail with "Access denied" | Too many DoVi jobs competing for disk I/O. Lower "Max DoVi Concurrent" to 2 |
| Health check timeouts | Normal during library scans on large libraries. Jobs will retry automatically |
| Only 1 worker active despite setting 4 | Restart the node GUI — worker counts are read at startup only |
| Staged file limit shows 0 | Set it to 10+ in the Dashboard Options section. 0 pauses all processing |

---

## Tech Stack

- **Python 3** — Flask (server), requests (node), tkinter (GUI)
- **SQLite** — WAL mode for concurrent access
- **React 18** — CDN + Babel standalone (single-file frontend, no build step)
- **ffmpeg** — BtbN GPL build with NVENC/NVDEC support
- **dovi_tool** — Dolby Vision metadata extraction and conversion
- **mkvmerge** — MKVToolNix for final container remuxing
- **Docker** — Server deployment (node runs natively on Windows)

---

## Project Structure

```
byte-transcode/
├── README.md
├── LICENSE
├── server/
│   ├── byte_server_v3.py          # Flask server + API + SQLite
│   ├── static/
│   │   └── index.html             # React frontend (single file)
│   └── docker-compose.yml
├── node/
│   ├── byte_node_v2.py            # Transcode engine
│   ├── byte_node_gui.py           # Windows GUI wrapper (tkinter)
│   ├── byte_node_config.json      # Your local config (gitignored)
│   ├── byte_node_config.example.json
│   ├── setup_tools.py             # Downloads ffmpeg, dovi_tool, mkvmerge
│   ├── run_node.bat               # Windows launcher with Python auto-detection
│   └── tools/                     # Auto-populated by setup_tools.py
│       ├── ffmpeg.exe
│       ├── ffprobe.exe
│       ├── dovi_tool.exe
│       └── mkvmerge.exe
└── docker/
    └── Dockerfile                 # Optional: Docker-based node
```

---

## License

MIT License — see [LICENSE](LICENSE)

---

## Credits

- [ffmpeg](https://ffmpeg.org/) — multimedia framework
- [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) — Windows GPL builds with NVENC
- [quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool) — Dolby Vision metadata tools
- [MKVToolNix](https://mkvtoolnix.download/) — Matroska container tools
- [Tdarr](https://tdarr.io/) — inspiration for the distributed transcoding architecture
