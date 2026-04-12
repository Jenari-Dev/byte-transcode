# Byte Transcode

**Automated media transcoding with full Dolby Vision, HDR10, HDR10+, and SDR support.**

Preserves ALL audio tracks (TrueHD Atmos 7.1, DTS-HD MA, etc.), subtitles, and chapters. Converts DoVi Profile 7 → Profile 8 for maximum client compatibility.

## Architecture

```
┌─────────────────────────────────────┐     ┌──────────────────────────────────────┐
│         BYTE SERVER (NAS)           │     │        BYTE NODE (Windows PC)         │
│                                     │     │                                      │
│  • Web Dashboard (:5800)            │◄───►│  • Pulls jobs from server             │
│  • Queue Management (SQLite)        │ API │  • GPU transcode (NVENC)              │
│  • Library Scanning                 │     │  • DoVi pipeline (dovi_tool)          │
│  • Worker Coordination              │     │  • Reports progress in real-time      │
│                                     │     │  • Supports multiple concurrent jobs  │
│  Docker on Linux/NAS                │     │  Docker on Windows + NVIDIA GPU       │
└─────────────────────────────────────┘     └──────────────────────────────────────┘
```

## Transcode Pipelines

### Dolby Vision (6-step pipeline)
1. Extract raw HEVC bitstream
2. Extract DoVi RPU metadata
3. NVENC GPU transcode (CQ 18)
4. Inject RPU back into transcoded HEVC
5. Convert Profile 7 → Profile 8 (for Jellyfin/Plex compatibility)
6. Remux with mkvmerge (all audio/subs/chapters preserved)

### HDR10 / HDR10+ / HLG / SDR
1. NVENC GPU transcode with all streams copied

## Quick Start

### 1. Deploy the Server (on your NAS / Linux machine)

```bash
mkdir -p ~/byte-transcode/server && cd ~/byte-transcode/server

# Download files
wget https://raw.githubusercontent.com/YOUR_USERNAME/byte-transcode/main/server/docker-compose.yml
wget https://raw.githubusercontent.com/YOUR_USERNAME/byte-transcode/main/server/Dockerfile
wget https://raw.githubusercontent.com/YOUR_USERNAME/byte-transcode/main/server/byte_server.py
mkdir -p static && wget -O static/index.html https://raw.githubusercontent.com/YOUR_USERNAME/byte-transcode/main/server/static/index.html

# Edit docker-compose.yml to set your media path
nano docker-compose.yml

# Build and start
docker compose up -d

# Check logs
docker logs byte-server
```

The dashboard is now at `http://<NAS_IP>:5800`

### 2. Deploy the Node (on your Windows PC with GPU)

```powershell
mkdir D:\byte-transcode\node
cd D:\byte-transcode\node

# Download files
curl -o docker-compose.yml https://raw.githubusercontent.com/YOUR_USERNAME/byte-transcode/main/node/docker-compose.yml
curl -o Dockerfile https://raw.githubusercontent.com/YOUR_USERNAME/byte-transcode/main/node/Dockerfile
curl -o byte_node.py https://raw.githubusercontent.com/YOUR_USERNAME/byte-transcode/main/node/byte_node.py

# Edit docker-compose.yml to set your server IP, GPU name, and paths
notepad docker-compose.yml

# Build and start
docker compose up -d

# Check logs
docker logs byte-node
```

### 3. Start Transcoding

1. Open `http://<NAS_IP>:5800` in your browser
2. Go to **Libraries** → Add your media paths
3. Click **Scan** on each library
4. Go to **Queue** → Click **Start**
5. Watch your files transcode in real-time!

## Configuration

### Server Settings (via API)

| Setting | Default | Description |
|---------|---------|-------------|
| `cq` | `18` | NVENC Constant Quality (10-30, lower = better) |
| `preset` | `slow` | NVENC preset (fast/medium/slow) |
| `max_workers` | `4` | Max concurrent transcode jobs |
| `min_size_gb` | `10` | Skip files smaller than this |
| `container` | `mkv` | Output container (mkv/mp4) |
| `dovi_convert_p8` | `true` | Convert DoVi P7 → P8 |
| `replace_original` | `true` | Replace source file after transcode |

### GPU Concurrent Job Recommendations

| GPU | Recommended Workers | Notes |
|-----|-------------------|-------|
| RTX 5090 | 5 | Flagship — handles 5 concurrent 4K NVENC |
| RTX 5080 | 4 | Sweet spot for 4 concurrent jobs |
| RTX 5070 Ti | 3 | 3 concurrent recommended |
| RTX 4090 | 5 | Same NVENC chip as 5090 |
| RTX 4080 | 4 | 4 concurrent recommended |
| RTX 4070 Ti | 3 | 3 concurrent recommended |
| RTX 4060 | 2 | 2 concurrent recommended |
| RTX 3090 | 4 | Strong encoder |
| RTX 3080 | 3 | 3 concurrent recommended |
| RTX 3060 | 2 | 2 concurrent recommended |

## File Structure

```
byte-transcode/
├── README.md
├── server/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── byte_server.py          # Flask API server
│   └── static/
│       └── index.html          # Web dashboard
└── node/
    ├── Dockerfile
    ├── docker-compose.yml
    └── byte_node.py            # Worker node client
```

## Requirements

### Server
- Docker
- Access to media library (read-only is fine)

### Node
- Docker with NVIDIA Container Toolkit
- NVIDIA GPU with NVENC support (GTX 1650+ / RTX series)
- Network access to the NAS media share

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Server status |
| `/api/dashboard` | GET | Full dashboard data |
| `/api/libraries` | GET/POST | List/add libraries |
| `/api/libraries/<id>/scan` | POST | Scan a library |
| `/api/queue` | GET | List queue (supports filters) |
| `/api/queue/<id>/bump` | POST | Bump job priority |
| `/api/queue/<id>/cancel` | POST | Cancel a job |
| `/api/queue/start` | POST | Enable processing |
| `/api/queue/pause` | POST | Pause processing |
| `/api/settings` | GET/PUT | Get/update settings |
| `/api/workers` | GET | List connected workers |
| `/api/jobs/next` | POST | Node pulls next job |
| `/api/jobs/<id>/progress` | POST | Node reports progress |
| `/api/jobs/<id>/complete` | POST | Node reports completion |

## License

MIT License — use it, modify it, share it.
