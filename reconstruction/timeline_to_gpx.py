#!/usr/bin/env python3
"""
Extract GPS points from Google Maps Timeline JSON export for a date range
and write a GPX track file.

Usage:
    python reconstruction/timeline_to_gpx.py \
        --start 2024-01-01 --end 2024-01-07 \
        --out /path/to/output/trip.gpx

    --end is inclusive (adds 1 day internally).
    Timeline.json is expected at ~/Downloads/Timeline.json (override with --timeline).
"""
import argparse, json, re, sys, os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

TIMELINE_DEFAULT = Path.home() / 'Downloads' / 'Timeline.json'


def parse_latlng(s):
    nums = re.findall(r'[-\d.]+', s)
    return float(nums[0]), float(nums[1])


def extract_points(timeline_path, start_dt, end_dt):
    with open(timeline_path) as f:
        d = json.load(f)

    def in_range(ts_str):
        try:
            return start_dt <= datetime.fromisoformat(ts_str).astimezone(timezone.utc) < end_dt
        except Exception:
            return False

    pts = []
    for seg in d.get('semanticSegments', []):
        if not (in_range(seg.get('startTime', '')) or in_range(seg.get('endTime', ''))):
            continue
        for pt in seg.get('timelinePath', []):
            ts = pt.get('time', '')
            latlng = pt.get('point', '')
            if ts and latlng and in_range(ts):
                dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
                lat, lon = parse_latlng(latlng)
                pts.append((dt, lat, lon))

    pts.sort()
    return pts


def build_gpx(pts, name):
    gpx = ET.Element('gpx', {
        'version': '1.1',
        'creator': 'geotag-photos/reconstruction/timeline_to_gpx.py',
        'xmlns': 'http://www.topografix.com/GPX/1/1',
    })
    trk = ET.SubElement(gpx, 'trk')
    ET.SubElement(trk, 'name').text = name
    trkseg = ET.SubElement(trk, 'trkseg')
    for dt, lat, lon in pts:
        trkpt = ET.SubElement(trkseg, 'trkpt', {'lat': str(lat), 'lon': str(lon)})
        ET.SubElement(trkpt, 'time').text = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    tree = ET.ElementTree(gpx)
    ET.indent(tree, space='  ')
    return tree


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True, help='Start date YYYY-MM-DD (inclusive)')
    ap.add_argument('--end',   required=True, help='End date YYYY-MM-DD (inclusive)')
    ap.add_argument('--out',   required=True, help='Output .gpx file path')
    ap.add_argument('--name',  default='',    help='Track name (default: output filename stem)')
    ap.add_argument('--timeline', default=str(TIMELINE_DEFAULT), help='Path to Timeline.json')
    args = ap.parse_args()

    start_dt = datetime.strptime(args.start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    end_dt   = datetime.strptime(args.end,   '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1)

    print(f"Loading Timeline.json from: {args.timeline}")
    pts = extract_points(args.timeline, start_dt, end_dt)
    print(f"Timeline points found: {len(pts)}")
    if not pts:
        print("No points found — check date range or fall back to manual reconstruction.")
        sys.exit(1)

    # Warn about train/flight-speed segments
    from math import radians, sin, cos, sqrt, atan2
    def hav(la1, lo1, la2, lo2):
        R = 6371
        la1,lo1,la2,lo2 = map(radians,[la1,lo1,la2,lo2])
        a = sin((la2-la1)/2)**2 + cos(la1)*cos(la2)*sin((lo2-lo1)/2)**2
        return R * 2 * atan2(sqrt(a), sqrt(1-a))

    train_gaps = []
    for i in range(1, len(pts)):
        dt_secs = (pts[i][0] - pts[i-1][0]).total_seconds()
        if dt_secs <= 0:
            continue
        dist_km = hav(pts[i-1][1], pts[i-1][2], pts[i][1], pts[i][2])
        speed = dist_km / (dt_secs / 3600)
        if speed > 120 and dist_km > 30:
            train_gaps.append((dist_km, dt_secs/3600, pts[i-1][0], pts[i][0]))

    if train_gaps:
        print(f"\n⚠ Train/flight segments detected ({len(train_gaps)}) — route lines split, but consider")
        print(f"  adding \"max_interp_gap_hours\": {min(g[1] for g in train_gaps):.1f} to trips.json to prevent photo misplacement:")
        for dist, hrs, t1, t2 in sorted(train_gaps, reverse=True):
            print(f"  {dist:.0f}km in {hrs:.1f}h  ({t1.strftime('%m-%d %H:%M')} → {t2.strftime('%m-%d %H:%M')} UTC)")

    # Coverage summary by day
    days = {}
    for dt, lat, lon in pts:
        days.setdefault(dt.strftime('%Y-%m-%d'), []).append((lat, lon))
    for day, dpts in sorted(days.items()):
        print(f"  {day}: {len(dpts):3d} pts  {dpts[0][0]:.3f},{dpts[0][1]:.3f} → {dpts[-1][0]:.3f},{dpts[-1][1]:.3f}")

    name = args.name or Path(args.out).stem
    tree = build_gpx(pts, name)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(str(out), 'w', encoding='utf-8') as f:
        tree.write(f, encoding='unicode', xml_declaration=True)
    print(f"\nWritten: {out}  ({len(pts)} trackpoints)")


if __name__ == '__main__':
    main()
