#!/bin/bash
# Re-resolve GPS placement + clusters for the 6 trips still on old-code centroid
# fallback. Reuses existing images (--skip-existing-images), so this is fast.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=./venv/bin/python
LOG=/tmp/geotag-photos-runs
mkdir -p "$LOG"

run() {
    local slug=$1 name=$2 gpx=$3 photos=$4
    echo ""
    echo "=== [$(date +%H:%M:%S)] re-placing $slug ==="
    if $PY process_trip.py --name "$name" --gpx "$gpx" --photos "$photos" \
        --skip-existing-images >"$LOG/$slug-replace.log" 2>&1; then
        grep -E "Processed|GPS sources|clusters|Done" "$LOG/$slug-replace.log" | sed 's/^/  /'
    else
        echo "  FAILED — see $LOG/$slug-replace.log"; tail -5 "$LOG/$slug-replace.log" | sed 's/^/  /'
    fi
}

run 2020-via-alpina  "2020 Via Alpina" "/Volumes/RYAN/2020/Alps/2020 Via Alpina GPX/2020 Via Alpina GPX.gpx" "/Volumes/RYAN/Edits/2020 Via Alpina"
run 2024-japan       "2024 Japan"      "/Volumes/RYAN/2024/Asia 24 pt2/07.2024 Japan/2024 Japan GPX/2024 Japan.gpx" "/Volumes/RYAN/Edits/2024 Japan"
run 2024-kyrgyzstan  "2024 Kyrgyzstan" "/Volumes/RYAN/2024/Kyrgyzstan/2024 Kyrgyzstan GPX/2024 Kyrgyzstan.gpx" "/Volumes/RYAN/Edits/2024 Kyrgyzstan"
run 2024-mongolia    "2024 Mongolia"   "/tmp/2024-mongolia-combined.gpx" "/Volumes/RYAN/Edits/2024 Mongolia"
run 2025-china-cny   "2025 China CNY"  "/Volumes/RYAN/2025/01:25 China/2025 China CNY GPX/2025 China CNY North.gpx" "/Volumes/RYAN/Edits/2025 China CNY"
run 2026-china-cny   "2026 China CNY"  "/Volumes/RYAN/2026/02-03.26 China CNY/Bridges/China CNY 2026 Bridges GPX/China CNY 2026 Bridges GPX.gpx" "/Volumes/RYAN/Edits/2026:02 China CNY"

echo ""; echo "All re-placed."
