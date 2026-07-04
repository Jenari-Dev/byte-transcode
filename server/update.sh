#!/usr/bin/env bash
# Byte Transcode — Server updater
# Run this from your server build dir (the folder with byte_server.py +
# docker-compose.yml). Downloads the latest server code from GitHub and
# rebuilds the container. Your database, config, and docker-compose.yml
# (your media paths / TZ) are left untouched.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
RAW="https://raw.githubusercontent.com/Jenari-Dev/byte-transcode/main"
STAMP="$(date +%Y%m%d-%H%M%S)"

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
VER="$(curl -s http://localhost:5800/api/status | grep -o '\"version\":\"[^\"]*\"' || true)"
echo "Done. Live $VER"
echo "(backups saved as *.bak-$STAMP — delete once you've confirmed it works)"
