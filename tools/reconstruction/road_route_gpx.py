#!/usr/bin/env python3
"""
Build a road-following GPX from a list of named waypoints.

Uses OSRM public routing API to get actual road paths between consecutive
waypoints, then distributes timestamps linearly along each segment.

No API key needed. Falls back to straight-line if OSRM fails for a segment.

Usage:
    1. Edit WAYPOINTS below (lat, lon, utc_iso_string, label)
    2. Set OUT_PATH
    3. python tools/reconstruction/road_route_gpx.py

Best for:
    - Trips with no Timeline.js coverage, reconstructed purely from named locations
    - Filling driving segments between known waypoints with road-accurate paths
    - Europe / North America / Japan (OSRM road data is excellent)

For China:
    - OSRM may have sparse data in rural areas — check output visually
    - If a segment looks wrong, set "straight_line": True in the waypoint to skip routing

Timestamp note:
    - All times must be UTC ISO strings (e.g. "2024-03-07T15:00:00Z")
    - If camera was on UK BST (Mar-Oct), subtract 1h from EXIF times before entering
    - If camera was on UK GMT (Nov-Mar), EXIF times are already UTC — use as-is
"""

import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib.request
import urllib.parse

# ── CONFIG ───────────────────────────────────────────────────────────────────

OUT_PATH = "/tmp/road-route-output.gpx"
TRIP_NAME = "Trip Name"

# OSRM profile: "driving", "cycling", or "foot"
OSRM_PROFILE = "driving"

# Public OSRM server — no rate limit documented, but be polite (script adds 0.3s delay)
OSRM_BASE = "http://router.project-osrm.org"

# ── WAYPOINTS ────────────────────────────────────────────────────────────────
# (lat, lon, utc_iso_string, label)
# Consecutive pairs are connected by road route.
# Times mark when you ARRIVED at each waypoint.
WAYPOINTS = [
    # Example (camera on GMT = UTC):
    (51.5007, -0.1246, "2024-01-01T10:00:00Z", "Start Location"),
    (51.5074, -0.1278, "2024-01-01T11:00:00Z", "End Location"),
]

# ── OSRM ─────────────────────────────────────────────────────────────────────

def get_road_segment(lat1, lon1, lat2, lon2, profile=OSRM_PROFILE):
    """Return list of (lat, lon) along the road between two points.
    Falls back to [start, end] straight-line if OSRM fails."""
    url = (f"{OSRM_BASE}/route/v1/{profile}"
           f"/{lon1},{lat1};{lon2},{lat2}"
           f"?overview=full&geometries=geojson")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        if data.get('code') != 'Ok':
            raise ValueError(f"OSRM code: {data.get('code')}")
        coords = data['routes'][0]['geometry']['coordinates']  # [[lon,lat],...]
        return [(lat, lon) for lon, lat in coords]
    except Exception as e:
        print(f"  ⚠ OSRM failed ({e}) — using straight line")
        return [(lat1, lon1), (lat2, lon2)]


def interpolate_times(n_points, t_start, t_end):
    total_s = (t_end - t_start).total_seconds()
    return [t_start + timedelta(seconds=total_s * i / max(n_points - 1, 1))
            for i in range(n_points)]


def parse_utc(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))

# ── BUILD ─────────────────────────────────────────────────────────────────────

def main():
    wpts = [(lat, lon, parse_utc(ts), label)
            for lat, lon, ts, label in WAYPOINTS]

    all_pts = []  # (datetime, lat, lon)

    for i, (lat1, lon1, t1, name1) in enumerate(wpts):
        # Add the waypoint itself
        all_pts.append((t1, lat1, lon1))

        if i + 1 >= len(wpts):
            break

        lat2, lon2, t2, name2 = wpts[i + 1]
        print(f"Routing {name1} → {name2} ...", end=' ', flush=True)

        segment = get_road_segment(lat1, lon1, lat2, lon2)
        # Skip the first point (already added above)
        segment = segment[1:]
        if not segment:
            continue

        times = interpolate_times(len(segment), t1, t2)[1:]
        for t, (slat, slon) in zip(times, segment):
            all_pts.append((t, slat, slon))

        print(f"{len(segment)} road pts")
        time.sleep(0.3)  # be polite to public OSRM

    # Write GPX
    NS_URI = "http://www.topografix.com/GPX/1/1"
    ET.register_namespace('', NS_URI)
    gpx = ET.Element(f'{{{NS_URI}}}gpx', {'version': '1.1', 'creator': 'road_route_gpx.py'})
    trk = ET.SubElement(gpx, f'{{{NS_URI}}}trk')
    ET.SubElement(trk, f'{{{NS_URI}}}name').text = TRIP_NAME
    trkseg = ET.SubElement(trk, f'{{{NS_URI}}}trkseg')

    for t, lat, lon in all_pts:
        pt = ET.SubElement(trkseg, f'{{{NS_URI}}}trkpt',
                           {'lat': str(round(lat, 6)), 'lon': str(round(lon, 6))})
        ET.SubElement(pt, f'{{{NS_URI}}}time').text = t.strftime('%Y-%m-%dT%H:%M:%SZ')

    tree = ET.ElementTree(gpx)
    ET.indent(tree, space='  ')
    Path(OUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    tree.write(OUT_PATH, encoding='unicode', xml_declaration=True)
    print(f"\nWritten: {OUT_PATH}  ({len(all_pts)} trackpoints)")


if __name__ == '__main__':
    main()
