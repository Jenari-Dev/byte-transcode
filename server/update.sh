#!/usr/bin/env bash
# Byte Transcode — Server updater
# Downloads the latest server code from GitHub and rebuilds the container.
# Your database, config, and docker-compose.yml (media paths / TZ) are left
# untouched. You can run this from ANYWHERE — it locates your build dir (the
# folder holding byte_server.py + your compose file) automatically.
set -e
RAW="https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main"
STAMP="$(date +%Y%m%d-%H%M%S)"
SELFDIR="$(cd "$(dirname "$0")" && pwd)"

has_compose() { ls "$1"/docker-compose.y*ml "$1"/compose.y*ml >/dev/null 2>&1; }

find_build_dir() {
  # 1) explicit override:  BYTE_DIR=/path/to/server bash update.sh
  if [ -n "$BYTE_DIR" ] && [ -f "$BYTE_DIR/byte_server.py" ]; then echo "$BYTE_DIR"; return; fi
  # 2) common locations (incl. the default install path on this NAS)
  for d in "$SELFDIR" "$PWD" \
           "$HOME/configs/byte-transcode/server" \
           /home/*/configs/byte-transcode/server \
           /opt/byte-transcode/server /srv/byte-transcode/server; do
    if [ -f "$d/byte_server.py" ] && has_compose "$d"; then echo "$d"; return; fi
  done
  # 3) last resort: search under $HOME
  local hit
  hit="$(find "$HOME" -maxdepth 6 -name byte_server.py -path '*byte-transcode*' 2>/dev/null | head -1)"
  [ -n "$hit" ] && dirname "$hit"
}

DIR="$(find_build_dir)"
if [ -z "$DIR" ] || [ ! -f "$DIR/byte_server.py" ]; then
  echo "ERROR: couldn't find your Byte Transcode build dir (the folder with"
  echo "byte_server.py + docker-compose.yml). Run it from that folder, or set:"
  echo "  BYTE_DIR=/path/to/byte-transcode/server bash update.sh"
  echo "On this NAS it's usually: /home/jenariskywalker/configs/byte-transcode/server"
  exit 1
fi

echo "== Byte Transcode server update =="
echo "Build dir: $DIR"

echo "Backing up current code..."
[ -f "$DIR/byte_server.py" ] && cp "$DIR/byte_server.py" "$DIR/byte_server.py.bak-$STAMP"
[ -f "$DIR/static/index.html" ] && cp "$DIR/static/index.html" "$DIR/static/index.html.bak-$STAMP"

echo "Downloading latest..."
curl -fsSL "$RAW/server/byte_server_v3.py" -o "$DIR/byte_server.py"
mkdir -p "$DIR/static"
curl -fsSL "$RAW/server/static/index.html" -o "$DIR/static/index.html"

echo "Rebuilding container..."
cd "$DIR"
docker compose up -d --build

echo "Waiting for server to come back..."
sleep 6
VER="$(curl -s http://localhost:5800/api/status | grep -o '"version":"[^"]*"' || true)"
echo "Done. Live $VER"
echo "(backups saved as *.bak-$STAMP — delete once you've confirmed it works)"
