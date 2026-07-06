<p align="center">
  <img src="https://img.shields.io/badge/Byte-Transcode-E040FB?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAzMiAzMic+PHJlY3Qgd2lkdGg9JzMyJyBoZWlnaHQ9JzMyJyByeD0nNicgZmlsbD0nIzEwMTAxMCcvPjx0ZXh0IHg9JzE2JyB5PScyMicgdGV4dC1hbmNob3I9J21pZGRsZScgZmlsbD0nI0UwNDBGQicgZm9udC1zaXplPScxNicgZm9udC13ZWlnaHQ9J2JvbGQnIGZvbnQtZmFtaWx5PSdtb25vc3BhY2UnPkI8L3RleHQ+PC9zdmc+" alt="Byte Transcode"/>
  <br/>
  <strong>Self-hosted, multi-GPU media transcoding with real Dolby Vision support — a simpler, free Tdarr alternative.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue?style=flat-square" alt="Python"/>
  <img src="https://img.shields.io/badge/GPU-NVENC-76B900?style=flat-square" alt="NVENC"/>
  <img src="https://img.shields.io/badge/Dolby_Vision-P5_&_P7→P8-E040FB?style=flat-square" alt="DoVi"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"/>
  <img src="https://img.shields.io/badge/docker-ready-2496ED?style=flat-square" alt="Docker"/>
</p>

One server on your NAS, one or more Windows GPU machines. The server holds the library, queue, and web UI; the nodes do the GPU work. Point it at your media, pick what you want done, and it works through the queue — with a live dashboard the whole time.

---

## Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Worker counts & multiple nodes](#worker-counts--multiple-nodes)
- [Everyday use](#everyday-use)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [MCP (AI assistant control)](#mcp-ai-assistant-control)
- [Reference](#reference)

---

## What it does

**Five pipelines**, each with its own scan, queue, and start/pause switch:

| Pipeline | What it does | Re-encodes video? |
|---|---|---|
| **Transcode** | NVENC compression with full DoVi / HDR10 / HDR10+ / HLG / SDR handling | Yes |
| **DV → P8** | Converts any Dolby Vision profile to Profile 8. **P7** (dual-layer): metadata-only, seconds per file. **P5** (the profile that plays purple/green on non-DV devices): rebuilds a genuine HDR10 base layer so it plays correctly on DV **and** non-DV displays | P7: No · P5: Yes |
| **Audio/Track Cleanup** | Strips audio/subtitle tracks not in your keep-list (default eng + jpn) and tidies messy track names | No |
| **Compatibility** | Flags files likely to misbehave (bad codecs, Hi10P, 10-bit HEVC SDR, interlaced, non-MKV, subtitle overload) and fixes them. Each job shows a badge: **container-only rewrap** (lossless) or **video re-encode** | Depends |
| **AI Subtitles** | Ensures English + a target language exist on every file: extracts or Whisper-transcribes, translates via your AI provider (Gemini / Claude / OpenAI / local), embeds or writes sidecar `.srt` | No |

**Built for real setups:**

- **Multi-node** — several GPU machines share one server; each node has its own temp drive, path mapping, and worker counts, all editable from the web UI and applied **live** (no restart).
- **Live everything** — dashboard with per-job progress, FPS, ETA, compression, and streaming logs.
- **Staged queue** — the next N files are health-checked and on-deck so a freed worker starts instantly (Tdarr-style).
- **Review or auto-accept** — completed jobs wait for you to **Accept** (replace the original) or **Decline** (keep the original, discard the new file); flip on **Auto-accept** once you trust the results.
- **Resilient** — resumes through node/server restarts, cleans up its own temp and orphaned `ffmpeg` processes, auto-remaps a dropped network drive, and requeues jobs that stall instead of black-holing.
- **Preserves everything** — all audio (TrueHD Atmos, DTS-HD MA…), subtitles, and chapters.
- **NVENC hardware encode + CUDA decode**, with a native Windows node GUI and ntfy.sh push notifications.

---

## How it works

```
┌─────────────────────────────┐         ┌──────────────────────────────────┐
│  SERVER (NAS / Docker)      │  HTTP   │  NODE(S) (Windows / Native)      │
│                             │◄───────►│                                  │
│  Flask + SQLite + React UI  │  :5800  │  Python + ffmpeg + dovi_tool     │
│  Libraries · Queue · Config │         │  + mkvmerge + NVIDIA GPU         │
│                             │         │                                  │
│  /media/... (NAS storage)   │         │  Z:\...  (mapped network drive)  │
│                             │         │  C:\Byte_temp (local SSD temp)   │
└─────────────────────────────┘         └──────────────────────────────────┘
```

**File flow:** NAS media → read over the network → GPU work into a **local** temp drive → result copied back to the NAS → original replaced (when accepted).

For Dolby Vision, the node extracts the raw HEVC bitstream and the DV RPU, does the GPU work, re-injects the RPU, converts to Profile 8, and remuxes with all original tracks. The P5 path uses ffmpeg's `libplacebo` (Vulkan) filter to rebuild a true HDR10 base layer — see [worker counts & multiple nodes](#worker-counts--multiple-nodes) for the one thing that matters when you run more than one machine.

---

## Quick start

### Requirements

**Server (NAS / Docker host):** Docker + Docker Compose, network-accessible media storage.

**Node (Windows GPU machine):** Windows 10/11 · Python 3.10+ · an NVIDIA GPU with NVENC (GTX 1650+ / RTX) · current NVIDIA drivers (Vulkan is included and is required for DV P5) · the NAS media mapped to a drive letter (e.g. `Z:\`) · a local SSD for temp (DoVi jobs write tens of GB per file).

### 1 · Server (NAS)

Create the folders and drop in the server files:

```bash
mkdir -p /home/YOU/byte-transcode/server/app/static /home/YOU/byte-transcode/server/config
cd /home/YOU/byte-transcode/server/app
wget https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main/server/byte_server_v3.py
wget -O static/index.html https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main/server/static/index.html
```

Create `docker-compose.yml` next to `app/`:

```yaml
services:
  byte-server:
    image: python:3.12-slim
    container_name: byte-server
    restart: unless-stopped
    ports:
      - "5800:5800"
    environment:
      - TZ=Asia/Tokyo          # ← set to YOUR timezone, or logs/queue times will be off
    volumes:
      - ./app/byte_server_v3.py:/app/byte_server.py    # mounted AS byte_server.py
      - ./app/static:/app/static
      - ./config:/config
      - /mnt/media:/media:ro                            # ← your media root (read-only)
    working_dir: /app
    command: >
      bash -c "pip install flask --quiet --break-system-packages &&
               python3 /app/byte_server.py --port 5800"
```

```bash
docker compose up -d
```

Open `http://YOUR_NAS_IP:5800`, create your admin account. Done.

> **Two things people miss:** the bind mount maps `byte_server_v3.py` → `/app/byte_server.py` (the container runs `python3 /app/byte_server.py`), and **`TZ` must match your locale** — a plain restart won't re-read it, so after changing it run `docker compose down && docker compose up -d`.

### 2 · Node (Windows)

```powershell
git clone https://github.com/Jenari-Dev/byte-transcode.git
cd byte-transcode\node
pip install requests
py setup_tools.py          # downloads ffmpeg, ffprobe, dovi_tool, mkvmerge into tools\
```

Copy `byte_node_config.example.json` → `byte_node_config.json` and edit:

```json
{
  "node_name": "MyNode",
  "server_url": "http://YOUR_NAS_IP:5800",
  "gpu": "RTX 5080",
  "poll_interval": 10,
  "path_from": "/media",
  "path_to": "Z:\\",
  "temp_dir": "C:\\Byte_temp",
  "start_paused": false
}
```

`path_from`/`path_to` translate the server's Docker path to your Windows drive (`/media/...` ↔ `Z:\...`). **`temp_dir` must be a real local drive that exists on _this_ machine** — see the troubleshooting note about copying configs between PCs.

Start it — double-click `run_node.bat`, or:

```
py byte_node_gui.py
```

It registers with the server automatically.

### 3 · First run

1. **Libraries** → add your media folder using the **server's** path (e.g. `/media/data/media/movies`) → **Scan**.
2. Start the node (it appears on the Dashboard).
3. **Dashboard** → **Start Processing**.
4. On the node's worker card, set its **GPU / CPU worker counts** (see [below](#worker-counts--multiple-nodes)) — changes apply within ~60s, no restart.
5. Watch it go.

---

## Configuration

### Node config (`byte_node_config.json`)

| Field | Default | Description |
|-------|---------|-------------|
| `node_name` | `"MyNode"` | Display name in the dashboard |
| `server_url` | | Server URL, `http://IP:5800` |
| `gpu` | | GPU name (cosmetic label) |
| `poll_interval` | `10` | Seconds between job polls (5–30) |
| `path_from` | `"/media"` | Server-side media prefix (what Docker sees) |
| `path_to` | `"Z:\\"` | The same media as your Windows mapped drive |
| `temp_dir` | | **Local** temp path on this machine — use your fastest SSD |
| `ffmpeg_path` / `ffprobe_path` / `dovi_tool_path` / `mkvmerge_path` | `""` | Override tool paths (blank = auto-detect from `tools\`) |
| `start_paused` | `false` | Start the node without processing until you click Start |

> Anything set here is a **local override** and wins over the same setting pushed from the server's worker card. Leave a field blank to let the server/global value apply.

### Server settings (Web UI → Settings)

| Setting | Default | Description |
|---------|---------|-------------|
| Constant Quality (CQ) | 16 | Lower = better quality, bigger files. 16–20 for 4K |
| NVENC Preset | p7 | p1 (fastest) → p7 (best compression) |
| Output Codec | HEVC | HEVC · H.264 · AV1 |
| Hardware Decoding | On | GPU decode (keeps CPU near 0%) |
| Max Workers | 4 | Floor for the fleet cap (actual cap = sum of nodes' counts, see below) |
| Min File Size | 10 GB | Skip files smaller than this |
| DV5 Mode | reencode | `reencode` rebuilds a true HDR10 base (correct everywhere); `relabel` is metadata-only (fast, but non-DV playback stays wrong) |
| Keep Languages | eng,jpn | Languages kept by Cleanup/Compatibility (ISO codes; untagged tracks always kept) |
| Replace Original | On | Replace the source after a job is accepted |
| Auto-Accept | Off | Skip manual review and replace automatically |
| Staged File Limit | 100 | How many files are staged/on-deck. **0 pauses hand-out** |
| AI / Subtitles | Gemini 2.5 Flash | Provider + API key, Whisper model, embed behavior — set your key before running AI Subtitles |

---

## Worker counts & multiple nodes

**A node's concurrency = its Transcode `GPU` count + its `CPU` count**, summed. So `4 GPU + 4 CPU = 8` jobs running on that node at once. Set both on the node's worker card with the +/- steppers (a **blue** number means that node has its own override; white = the global default). Changes apply **live within ~60 seconds — no node restart needed.**

The whole fleet's cap is the **sum of every online node's counts** (floored at *Max Workers*), so adding a node automatically raises total throughput.

Pick counts to match the card. A 16 GB card handles many concurrent 4K DoVi re-encodes; a 12 GB card should stay modest — DoVi P5 re-encodes are VRAM-heavy, so start at 2–3 and raise it while watching for failures.

### The one rule for multiple nodes: same ffmpeg build everywhere

The DV → P8 Profile-5 pipeline runs each frame through ffmpeg's `libplacebo` (Vulkan) filter for Dolby Vision tone-mapping. **Some bleeding-edge nightly ffmpeg builds ship a broken libplacebo that won't initialize** — so P5 jobs fail on that node (`Error initializing filters`) while running perfectly on another node whose ffmpeg is an older, working build. Same node code, same command, same GPU — the only difference is the ffmpeg binary.

- If one node fails P5 and another doesn't, run `ffmpeg -version` on both. Different builds = your cause.
- Copy the working node's `tools\ffmpeg.exe` + `ffprobe.exe` onto the failing node, restart it, fixed.
- **Don't update ffmpeg to the latest nightly on one machine only.** Update all nodes from the same build and test one P5 job first.

Each node still keeps its **own** temp drive and path mapping — those *should* differ per machine. Only the tool binaries must match.

---

## Everyday use

- **Dashboard & staging** — active transcodes show live progress, FPS, ETA, and compression. The staging panel shows health checks and the on-deck files. Click any job for detailed, live-streaming logs.
- **Accept / Decline** — with Auto-Accept off, finished jobs land in **Completed** and wait: **Accept** replaces the original with the new file; **Decline** keeps the original and discards the new one (its temp is cleaned up automatically). Turn on **Auto-Accept** to replace originals without review.
- **Node ON/OFF** — every worker card has a switch. A disabled node keeps heartbeating but is never handed jobs — flip it back on anytime without touching the machine. If a node's network drive drops, it shows a red **MEDIA DRIVE OFFLINE** badge and re-maps the drive itself.
- **Requeue** — the **Errored** tab has *Requeue All*, or requeue individual jobs. Requeued jobs are health-checked and picked up automatically.
- **Priorities** — drag tools in the sidebar to set which pipeline runs first; use a job's up-arrow to push it into the staged area.

---

## Updating

A **bell** appears in the sidebar when a new server or node version is on GitHub. Your database, settings, paths, and tools are left untouched.

**Server (NAS)** — from your server build dir:
```bash
bash update.sh
```
Backs up the current code, pulls the latest, rebuilds the container.

**Node (Windows)** — close the node, then from your `ByteNode` folder:
```bat
update_node.bat
```
Backs up and pulls the latest node code; run `py setup_tools.py` afterward only if a release added new tools, then restart the node. **Don't re-download ffmpeg from a newer nightly just to update** — see the multi-node rule above.

> Rare releases that change `docker-compose.yml` or the `Dockerfile` say so in the notes; apply those by re-copying the file.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| **DV → P8 (Profile-5) jobs fail instantly** at the re-encode step | Almost always a **broken ffmpeg build on that node**. Run the re-encode command manually — if even `-vf libplacebo` (no options) prints `Error initializing filters` / `Invalid argument`, replace that node's `tools\ffmpeg.exe` + `ffprobe.exe` with a known-good build (ideally the exact build your other nodes use). See [the multi-node rule](#the-one-rule-for-multiple-nodes-same-ffmpeg-build-everywhere). |
| A node errors **every** job with `The device is not ready: 'X:\'` | Its temp directory points at a drive that doesn't exist on that machine (often from copying another PC's config). Set the node's temp dir to a real local drive here (e.g. `C:\Byte_temp`) — in the node GUI or its worker-card override. |
| Log / queue timestamps are hours off | Set `TZ` in `docker-compose.yml` to your locale, then `docker compose down && docker compose up -d` (a plain restart won't re-read it). |
| One node hogs slots / claims too many | Concurrency = that node's **GPU + CPU** counts summed. Lower them on its worker card; keep weaker/low-VRAM cards modest for 4K DoVi. |
| "File not found" during transcode | Path translator mismatch: `path_from` must match the Docker media mount and `path_to` your Windows mapped drive. Verify with `dir Z:\data\media\movies`. |
| Node shows "server unreachable" | Check `server_url`. Test `curl http://YOUR_NAS_IP:5800/api/worker-counts`. |
| Stray `ffmpeg` piling up / PC lags after a node restart | Handled automatically — the node reaps its own orphaned tool processes on startup and every 60s. Just be on a current node version. |
| Editing one node's worker count changed every node | Older bug — update the server; the steppers are per-node now. |
| Scans appear stuck | `docker restart byte-server` clears any SQLite lock contention. |
| High CPU during transcode | Ensure Hardware Decoding is on in Settings. |
| Staged file limit shows 0 | Set it to 10+ in Dashboard Options — 0 pauses hand-out. |

---

## MCP (AI assistant control)

`mcp/byte_mcp.py` is a Model Context Protocol server exposing the whole system as tools — queue, scans, pipeline start/pause, settings, per-node config, logs. Works with any MCP client.

```bash
# Generate an API key in Settings → API first, then:
claude mcp add byte-transcode -- py path/to/mcp/byte_mcp.py --server http://YOUR_NAS_IP:5800 --api-key YOUR_KEY
```

Requires Python 3.10+ with `mcp` and `requests` (auto-installed on first run).

---

## Reference

**Tech stack** — Python 3 / Flask (server) · requests + tkinter (node) · SQLite (WAL) · React 18 (single-file, CDN + Babel, no build step) · ffmpeg (NVENC/NVDEC + libplacebo/Vulkan) · dovi_tool · MKVToolNix · Docker (server).

**Project structure**

```
byte-transcode/
├── README.md · LICENSE · version.json
├── server/
│   ├── byte_server_v3.py       # Flask server + API + SQLite
│   ├── static/index.html       # React frontend (single file)
│   ├── docker-compose.yml
│   └── update.sh
├── node/
│   ├── byte_node_v2.py         # transcode engine
│   ├── byte_node_gui.py        # Windows GUI (tkinter)
│   ├── byte_node_config.example.json
│   ├── setup_tools.py          # downloads ffmpeg, dovi_tool, mkvmerge
│   ├── run_node.bat · update_node.bat
│   └── tools/                  # populated by setup_tools.py
└── mcp/byte_mcp.py             # MCP server
```

**License** — MIT, see [LICENSE](LICENSE).

**Credits** — [ffmpeg](https://ffmpeg.org/) · [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) · [quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool) · [MKVToolNix](https://mkvtoolnix.download/) · inspired by [Tdarr](https://tdarr.io/).
