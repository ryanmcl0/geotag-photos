#!/usr/bin/env python3
"""
Template for building a GPX from manually identified waypoints
(used as fallback when Timeline.json has no coverage).

Edit the waypoints list below, then run:
    python reconstruction/manual_gpx.py

Timestamps: write them in the camera's clock timezone (usually UK BST/GMT).
The script converts to UTC. Set CAMERA_UTC_OFFSET to match the camera.
If camera was on UK time in China: CAMERA_UTC_OFFSET = +1 (BST) or 0 (GMT).
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── CONFIG ──────────────────────────────────────────────────────────────────
TRIP_NAME = "YYYY Trip Name"
OUT_PATH  = "/path/to/output/trip.gpx"
CAMERA_UTC_OFFSET = 0   # GMT; use +1 for BST, +8 for CST

# ── WAYPOINTS ───────────────────────────────────────────────────────────────
# (lat, lon, year, month, day, hour, minute, description)
# Times are in the camera's clock timezone (CAMERA_UTC_OFFSET above)
WAYPOINTS = [
    # Example:
    (51.5007, -0.1246, 2024, 1, 1, 12, 0, "Location Name"),
    # ... add your route points
]

# ── BUILD ────────────────────────────────────────────────────────────────────
def to_utc(y, mo, d, h, mi):
    tz = timezone(timedelta(hours=CAMERA_UTC_OFFSET))
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

gpx = ET.Element('gpx', {'version': '1.1', 'creator': 'geotag-photos/reconstruction/manual_gpx.py',
                          'xmlns': 'http://www.topografix.com/GPX/1/1'})
trk = ET.SubElement(gpx, 'trk')
ET.SubElement(trk, 'name').text = TRIP_NAME
trkseg = ET.SubElement(trk, 'trkseg')

for lat, lon, y, mo, d, h, mi, name in WAYPOINTS:
    pt = ET.SubElement(trkseg, 'trkpt', {'lat': str(lat), 'lon': str(lon)})
    ET.SubElement(pt, 'time').text = to_utc(y, mo, d, h, mi)
    ET.SubElement(pt, 'name').text = name

tree = ET.ElementTree(gpx)
ET.indent(tree, space='  ')
out = Path(OUT_PATH)
out.parent.mkdir(parents=True, exist_ok=True)
tree.write(str(out), encoding='unicode', xml_declaration=True)
print(f"Written: {out}  ({len(WAYPOINTS)} trackpoints)")
