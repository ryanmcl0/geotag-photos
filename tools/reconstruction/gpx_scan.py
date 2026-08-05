#!/usr/bin/env python3
"""
Scan a folder of recorded GPX tracks and report what they actually cover.

Written for trips recorded day-by-day on Strava, where the recording is not
continuous: some days were never started, some were started late, and some
tracks contain multi-hour lapses that show up as one long straight line.

For each file it reports the time window, point count and distance, and finds
the holes inside it (a jump between consecutive points that is too long in time
or too far in space to be real driving). Across files it reports the windows
where nothing was recorded at all.

Points are cached to a pickle so later passes (gap fill, merge) don't re-read
the source files, which may live on a slow network mount.

Usage:
  python tools/reconstruction/gpx_scan.py "<gpx_dir>" [--cache /tmp/x.pkl]
                                          [--gap-min 5] [--jump-km 1.0]
"""

import argparse
import math
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

GPX_NS = '{http://www.topografix.com/GPX/1/1}'


def haversine_km(a, b):
    (lat1, lon1), (lat2, lon2) = a, b
    p = math.pi / 180
    dlat, dlon = (lat2 - lat1) * p, (lon2 - lon1) * p
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def parse_time(s):
    # Strava writes 2026-07-11T01:36:28Z (UTC)
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)


def parse_gpx(path):
    """[(datetime_utc|None, lat, lon, ele|None)] in file order, plus the track name."""
    pts, name = [], None
    for event, el in ET.iterparse(str(path), events=('end',)):
        if el.tag == GPX_NS + 'name' and name is None:
            name = (el.text or '').strip()
        elif el.tag == GPX_NS + 'trkpt':
            t = el.find(GPX_NS + 'time')
            e = el.find(GPX_NS + 'ele')
            pts.append((
                parse_time(t.text) if t is not None and t.text else None,
                float(el.get('lat')), float(el.get('lon')),
                float(e.text) if e is not None and e.text else None,
            ))
            el.clear()
    return pts, name


def track_gaps(pts, gap_min, jump_km):
    """Holes inside one track: consecutive points separated by too much time or
    distance. A long straight line in Strava is exactly this."""
    out = []
    for i in range(1, len(pts)):
        t0, lat0, lon0, _ = pts[i - 1]
        t1, lat1, lon1, _ = pts[i]
        if not (t0 and t1):
            continue
        dt_min = (t1 - t0).total_seconds() / 60
        dkm = haversine_km((lat0, lon0), (lat1, lon1))
        if dt_min >= gap_min or dkm >= jump_km:
            out.append({'i': i, 'start': t0, 'end': t1, 'minutes': dt_min, 'km': dkm,
                        'from': (lat0, lon0), 'to': (lat1, lon1)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('gpx_dir')
    ap.add_argument('--cache', default='/tmp/gpx_scan.pkl')
    ap.add_argument('--gap-min', type=float, default=5.0,
                    help='report an in-track hole at or above this many minutes')
    ap.add_argument('--jump-km', type=float, default=1.0,
                    help='report an in-track hole at or above this straight-line km')
    args = ap.parse_args()

    files = sorted(Path(args.gpx_dir).glob('*.gpx'))
    if not files:
        print(f'No .gpx in {args.gpx_dir}', file=sys.stderr)
        return 1

    tracks, all_windows = {}, []
    for f in files:
        try:
            pts, name = parse_gpx(f)
        except ET.ParseError as e:
            print(f'{f.name}: PARSE ERROR {e}')
            continue
        timed = [p for p in pts if p[0]]
        if not timed:
            print(f'{f.name}: {len(pts)} points, NO timestamps')
            tracks[f.name] = {'name': name, 'points': pts}
            continue
        dist = sum(haversine_km((pts[i - 1][1], pts[i - 1][2]), (pts[i][1], pts[i][2]))
                   for i in range(1, len(pts)))
        gaps = track_gaps(timed, args.gap_min, args.jump_km)
        t0, t1 = timed[0][0], timed[-1][0]
        tracks[f.name] = {'name': name, 'points': pts, 'start': t0, 'end': t1,
                          'km': dist, 'gaps': gaps}
        all_windows.append((t0, t1, f.name))
        print(f'\n{f.name}')
        print(f'  name: {name!r}  points: {len(pts)}  km: {dist:,.1f}')
        print(f'  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} UTC '
              f'({(t1 - t0).total_seconds() / 3600:.1f} h)')
        if gaps:
            print(f'  {len(gaps)} internal hole(s):')
            for g in sorted(gaps, key=lambda g: -g['km'])[:12]:
                print(f"    {g['start']:%m-%d %H:%M} → {g['end']:%H:%M}  "
                      f"{g['minutes']:6.1f} min  {g['km']:7.2f} km straight")

    all_windows.sort()
    print('\n=== coverage across files (UTC) ===')
    prev_end, prev_name = None, None
    for t0, t1, fname in all_windows:
        if prev_end and t0 > prev_end:
            hrs = (t0 - prev_end).total_seconds() / 3600
            print(f'  !! NOT RECORDED {prev_end:%m-%d %H:%M} → {t0:%m-%d %H:%M} '
                  f'({hrs:.1f} h)  between {prev_name} and {fname}')
        print(f'  {t0:%m-%d %H:%M} → {t1:%m-%d %H:%M}  {fname}')
        if prev_end is None or t1 > prev_end:
            prev_end, prev_name = t1, fname

    Path(args.cache).write_bytes(pickle.dumps(tracks))
    print(f'\ncached {len(tracks)} tracks → {args.cache}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
