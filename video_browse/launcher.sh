#!/bin/bash
# Video Browser launcher: starts the local server if needed, opens the UI.
# Resolves the repo from this script's own location, so the .app wrapper and
# the checkout can live anywhere.
set -e
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=$(python3 -c "import json;print(json.load(open('$REPO/config/video_browse.json')).get('port',8765))" 2>/dev/null || echo 8765)
if ! curl -s -o /dev/null --max-time 1 "http://localhost:$PORT/api/progress"; then
  cd "$REPO"
  mkdir -p local_videos
  nohup python3 video_browse/serve.py >> local_videos/serve.log 2>&1 &
  for i in $(seq 1 20); do
    curl -s -o /dev/null --max-time 1 "http://localhost:$PORT/api/progress" && break
    sleep 0.3
  done
fi
open "http://localhost:$PORT"
