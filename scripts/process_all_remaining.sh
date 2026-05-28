#!/bin/bash
# Process the 7 remaining GPX trips sequentially. Logs each to /tmp/.
set -uo pipefail

cd "$(dirname "$0")/.."
PY=./venv/bin/python
LOG_DIR=/tmp/geotag-photos-runs
mkdir -p "$LOG_DIR"

run() {
    local slug=$1 name=$2 gpx=$3 photos=$4
    local log="$LOG_DIR/$slug.log"
    echo ""
    echo "================================================================="
    echo "[$(date +%H:%M:%S)] $slug → $log"
    echo "================================================================="
    if $PY process_trip.py --name "$name" --gpx "$gpx" --photos "$photos" >"$log" 2>&1; then
        tail -10 "$log" | grep -E "Processed|GPS sources|Failed|clusters|Done" | sed "s/^/  /"
    else
        echo "  FAILED — see $log"
        tail -20 "$log" | sed "s/^/  /"
    fi
}

run via-alpina-2020       "2020 Via Alpina"             "/Volumes/RYAN/2020/Alps/2020 Via Alpina GPX/2020 Via Alpina GPX.gpx"                                                       "/Volumes/RYAN/Edits/2020 Via Alpina"
run 2024-japan            "2024 Japan"                  "/Volumes/RYAN/2024/Asia 24 pt2/07.2024 Japan/2024 Japan GPX/2024 Japan.gpx"                                                "/Volumes/RYAN/Edits/2024 Japan"
run 2024-mongolia         "2024 Mongolia"               "/tmp/2024-mongolia-combined.gpx"                                                                                          "/Volumes/RYAN/Edits/2024 Mongolia"
run 2026-china-cny        "2026 China CNY"              "/Volumes/RYAN/2026/02-03.26 China CNY/Bridges/China CNY 2026 Bridges GPX/China CNY 2026 Bridges GPX.gpx"                   "/Volumes/RYAN/Edits/2026:02 China CNY"
run 2024-china-summer     "2024 China (summer)"         "/Volumes/RYAN/2024/Asia 24 pt2/06.2024 China/2024 North Xinjiang + Guizhou GPX/North Xinjiang + Guizhou 2024.gpx"          "/Volumes/RYAN/Edits/2024 China"
run 2024-china-nov        "2024 China Nov"              "/Volumes/RYAN/2024/China - Zhejiang, Shanghai, Jiangsu, Jiangxi, Xinjiang/Xinjiang/2024 South Xinjiang GPX/2024 South Xinjiang.gpx" "/Volumes/RYAN/Edits/2024 China Nov"
run 2025-china-cny        "2025 China CNY"              "/Volumes/RYAN/2025/01:25 China/2025 China CNY GPX/2025 China CNY North.gpx"                                               "/Volumes/RYAN/Edits/2025 China CNY"

echo ""
echo "All 7 trips processed. Logs in $LOG_DIR/"
