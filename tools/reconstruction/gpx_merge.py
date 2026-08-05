#!/usr/bin/env python3
"""
Merge day-by-day recorded GPX tracks into one trip route, filling only the holes.

The recorded tracks are the truth and are never modified. Everything else this
script does is confined to the holes found by gpx_scan.py:

  - a window between two tracks where nothing was recorded (a day the recording
    was never started, or an evening/overnight),
  - a hole inside a track where the recording lapsed, which Strava draws as one
    long straight line.

Holes are filled in three passes, best source first:

  1. ANCHORS  — GPS fixes from photos taken during the hole (drone stills, phone
     shots). These are real positions at known times, so they pin the route to
     where it actually went.
  2. ROAD SHAPE — between two consecutive anchors that are far apart, if both sit
     close to one of the planned route polylines (a My Maps "Directions" line),
     the fill follows that polyline's geometry instead of cutting straight
     across. The planned roads are the roads that were driven.
  3. STRAIGHT — anything left over is a straight line, sampled every --step-km.

A hole whose endpoints are close together (parked overnight, lunch stop) is left
alone: interpolating it would only invent movement that never happened.

Every filled point is written with <src>reconstructed</src> so the invented parts
of the route stay identifiable in the output file.

Usage:
  python tools/reconstruction/gpx_merge.py --cache /tmp/gpx_scan.pkl \
      --fixes /tmp/drone.json --fixes /tmp/phone.json \
      --kml <plan.kml> --fix-offset +8 --out <merged.gpx>
"""

import argparse
import json
import math
import pickle
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

KML_NS = '{http://www.opengis.net/kml/2.2}'


def haversine_km(a, b):
    (lat1, lon1), (lat2, lon2) = a, b
    p = math.pi / 180
    dlat, dlon = (lat2 - lat1) * p, (lon2 - lon1) * p
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- inputs

def load_tracks(cache):
    """Recorded points, one flat time-sorted list of (t, lat, lon, ele, src)."""
    tracks = pickle.loads(Path(cache).read_bytes())
    pts = []
    for fname, t in tracks.items():
        for (tm, lat, lon, ele) in t['points']:
            if tm:
                pts.append((tm, lat, lon, ele, 'recorded'))
    pts.sort(key=lambda p: p[0])
    return pts, tracks


def parse_exif_time(s):
    if not s:
        return None
    m = re.match(r'(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})', s)
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, sec, tzinfo=timezone.utc)


def load_fixes(paths, cam_offset_hours):
    """Photo GPS fixes as (utc_time, lat, lon, ele, label).

    GPSDateTime is already UTC and is preferred. A camera clock (DJI = local
    time) is converted with --fix-offset."""
    out = []
    for p in paths:
        for r in json.loads(Path(p).read_text()):
            if r.get('lat') is None or r.get('lon') is None:
                continue
            t = parse_exif_time(r.get('gps_time'))
            if t is None:
                t = parse_exif_time(r.get('cam_time'))
                if t is None:
                    continue
                t -= timedelta(hours=cam_offset_hours)
            out.append((t, r['lat'], r['lon'], r.get('ele'), r['file']))
    out.sort(key=lambda f: f[0])
    return out


def load_kml_lines(path):
    """[(name, [(lat, lon), ...])] for every LineString in a My Maps KML."""
    lines = []
    root = ET.parse(path).getroot()
    for pm in root.iter(KML_NS + 'Placemark'):
        name_el = pm.find(KML_NS + 'name')
        coords = pm.find(f'.//{KML_NS}LineString/{KML_NS}coordinates')
        if coords is None or not coords.text:
            continue
        pts = []
        for tok in coords.text.split():
            parts = tok.split(',')
            if len(parts) >= 2:
                pts.append((float(parts[1]), float(parts[0])))
        if len(pts) > 1:
            lines.append((name_el.text if name_el is not None else '?', pts))
    return lines


# ---------------------------------------------------------------- fills

def nearest_vertex(line, pt):
    best_i, best_d = None, float('inf')
    for i, v in enumerate(line):
        d = haversine_km(v, pt)
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def road_shape(lines, a, b, max_snap_km, min_span_km, max_detour):
    """Geometry between a and b along a planned route line, or None.

    Both ends must sit within max_snap_km of the same line, and the slice
    between them must be a real span (not a couple of vertices).

    The detour guard matters: a planned loop passes near the same place twice,
    so the slice between two vertices can run the wrong way around the entire
    loop. A fill that travels far further than the straight line is that bug,
    not a road, so it is rejected in favour of a straight fill."""
    best = None
    direct = haversine_km(a, b)
    for name, line in lines:
        ia, da = nearest_vertex(line, a)
        ib, db = nearest_vertex(line, b)
        if da > max_snap_km or db > max_snap_km or ia == ib:
            continue
        seg = line[min(ia, ib):max(ia, ib) + 1]
        if ib < ia:
            seg = seg[::-1]
        span = sum(haversine_km(seg[i - 1], seg[i]) for i in range(1, len(seg)))
        if span < min_span_km or span > max(direct * max_detour, direct + 10):
            continue
        # Prefer the line that fits both ends best, then the shorter path.
        score = (da + db, span)
        if best is None or score < best[0]:
            best = (score, seg, name)
    return (best[1], best[2]) if best else None


def densify(a, b, step_km):
    """Straight-line points strictly between a and b, one every step_km."""
    d = haversine_km(a, b)
    n = int(d // step_km)
    return [(a[0] + (b[0] - a[0]) * i / (n + 1), a[1] + (b[1] - a[1]) * i / (n + 1))
            for i in range(1, n + 1)] if n > 0 else []


def densify_path(path, step_km):
    """Same, along a whole polyline. A My Maps directions line can run tens of km
    between vertices on an empty desert highway; the pipeline splits the drawn
    route wherever consecutive points are more than 5 km apart, so a sparse fill
    would show up as a broken line."""
    out = [path[0]]
    for i in range(1, len(path)):
        out.extend(densify(path[i - 1], path[i], step_km))
        out.append(path[i])
    return out


def time_along(path, t_start, t_end):
    """Spread t_start..t_end over path by cumulative distance."""
    if len(path) < 2:
        return [t_start] * len(path)
    cum, total = [0.0], 0.0
    for i in range(1, len(path)):
        total += haversine_km(path[i - 1], path[i])
        cum.append(total)
    if total == 0:
        return [t_start] * len(path)
    span = (t_end - t_start).total_seconds()
    return [t_start + timedelta(seconds=span * c / total) for c in cum]


def fill_hole(start_pt, end_pt, fixes, lines, cfg, log):
    """Points to insert between two known points, exclusive of both ends."""
    t0, lat0, lon0 = start_pt[0], start_pt[1], start_pt[2]
    t1, lat1, lon1 = end_pt[0], end_pt[1], end_pt[2]
    span_km = haversine_km((lat0, lon0), (lat1, lon1))
    inside = [f for f in fixes if t0 < f[0] < t1]

    # Parked: nothing moved and no photo says otherwise. Leave the hole alone.
    if span_km < cfg['still_km'] and not inside:
        return []

    # Anchor chain: known ends + any photo fixes taken during the hole.
    chain = [(t0, lat0, lon0, None, 'edge')]
    last = (lat0, lon0)
    for f in inside:
        # Drop fixes that would imply an impossible speed (bad EXIF clock, or a
        # photo taken at home) and duplicates from a burst at one spot.
        dt_h = max((f[0] - chain[-1][0]).total_seconds() / 3600, 1e-6)
        d = haversine_km(last, (f[1], f[2]))
        if d / dt_h > cfg['max_kmh']:
            continue
        if d < cfg['dedupe_km']:
            continue
        chain.append((f[0], f[1], f[2], f[3], 'photo'))
        last = (f[1], f[2])
    chain.append((t1, lat1, lon1, None, 'edge'))

    out, n_road = [], 0
    for i in range(1, len(chain)):
        pa, pb = chain[i - 1], chain[i]
        a, b = (pa[1], pa[2]), (pb[1], pb[2])
        gap_km = haversine_km(a, b)
        path = None
        if gap_km >= cfg['road_min_km'] and lines:
            shaped = road_shape(lines, a, b, cfg['snap_km'], cfg['road_min_km'] / 2,
                                cfg['max_detour'])
            if shaped:
                shape, _name = shaped
                # Bridge the anchors to the ends of the planned line's slice, and
                # keep the whole thing evenly sampled.
                path = densify_path([a] + shape + [b], cfg['step_km'])
                n_road += 1
        if path is None:
            path = [a] + densify(a, b, cfg['step_km']) + [b]
        times = time_along(path, pa[0], pb[0])
        # skip the first point (already emitted as the previous anchor/edge)
        for (lat, lon), t in list(zip(path, times))[1:]:
            out.append((t, lat, lon, None, 'reconstructed'))
        if pb[4] == 'photo':
            out[-1] = (pb[0], pb[1], pb[2], pb[3], 'photo')
    if out and out[-1][4] != 'photo':
        out.pop()          # the closing edge point is the real recorded point
    log(f"    filled with {len(out)} pts "
        f"({len(chain) - 2} photo anchors, {n_road} road-shaped legs)")
    return out


# ---------------------------------------------------------------- output

def write_gpx(points, out_path, name):
    esc = lambda s: (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="gpx_merge.py" '
             'xmlns="http://www.topografix.com/GPX/1/1">',
             f'  <metadata><name>{esc(name)}</name></metadata>',
             '  <trk>', f'    <name>{esc(name)}</name>', '    <trkseg>']
    for (t, lat, lon, ele, src) in points:
        lines.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">')
        if ele is not None:
            lines.append(f'        <ele>{ele:.1f}</ele>')
        lines.append(f'        <time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>')
        if src != 'recorded':
            lines.append(f'        <src>{src}</src>')
        lines.append('      </trkpt>')
    lines += ['    </trkseg>', '  </trk>', '</gpx>', '']
    Path(out_path).write_text('\n'.join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cache', required=True, help='pickle from gpx_scan.py')
    ap.add_argument('--fixes', action='append', default=[], help='photo_gps.py JSON (repeatable)')
    ap.add_argument('--kml', action='append', default=[], help='plan KML with route lines (repeatable)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--name', default='Trip')
    ap.add_argument('--fix-offset', type=float, default=8.0,
                    help='hours to subtract from a camera clock to get UTC (DJI = local, China = 8)')
    ap.add_argument('--hole-min', type=float, default=8.0,
                    help='minutes: holes shorter than this are ignored')
    ap.add_argument('--hole-km', type=float, default=2.0,
                    help='km: holes with a shorter straight-line jump are ignored')
    ap.add_argument('--still-km', type=float, default=2.0,
                    help='km: below this and with no photos, treat the hole as parked')
    ap.add_argument('--step-km', type=float, default=2.0, help='straight-line sampling')
    ap.add_argument('--road-min-km', type=float, default=5.0,
                    help='km: only try road shaping for legs at least this long')
    ap.add_argument('--snap-km', type=float, default=8.0,
                    help='km: how close an anchor must be to a planned line to use it')
    ap.add_argument('--max-kmh', type=float, default=160.0, help='reject impossible photo anchors')
    ap.add_argument('--dedupe-km', type=float, default=0.3, help='ignore photo anchors this close together')
    ap.add_argument('--max-detour', type=float, default=2.5,
                    help='reject a road-shaped fill longer than this multiple of the straight line')
    ap.add_argument('--simplify-m', type=float, default=15.0,
                    help='drop recorded points closer together than this (0 = keep all)')
    args = ap.parse_args()

    cfg = {'still_km': args.still_km, 'step_km': args.step_km, 'snap_km': args.snap_km,
           'road_min_km': args.road_min_km, 'max_kmh': args.max_kmh,
           'dedupe_km': args.dedupe_km, 'max_detour': args.max_detour}

    recorded, _tracks = load_tracks(args.cache)
    fixes = load_fixes(args.fixes, args.fix_offset) if args.fixes else []
    lines = []
    for k in args.kml:
        lines += load_kml_lines(k)
    print(f'recorded points: {len(recorded):,}')
    print(f'photo fixes:     {len(fixes):,}')
    print(f'planned lines:   {len(lines)} ({sum(len(l[1]) for l in lines):,} vertices)')

    merged, n_holes, n_added = [recorded[0]], 0, 0
    for i in range(1, len(recorded)):
        prev, cur = recorded[i - 1], recorded[i]
        dt_min = (cur[0] - prev[0]).total_seconds() / 60
        dkm = haversine_km((prev[1], prev[2]), (cur[1], cur[2]))
        if dt_min >= args.hole_min or dkm >= args.hole_km:
            n_holes += 1
            print(f'  hole {prev[0]:%m-%d %H:%M} → {cur[0]:%m-%d %H:%M} '
                  f'({dt_min:7.1f} min, {dkm:7.2f} km straight)')
            added = fill_hole(prev, cur, fixes, lines, cfg, print)
            merged.extend(added)
            n_added += len(added)
        merged.append(cur)

    # Thin the recorded 1 Hz data: the map route doesn't need metre spacing.
    if args.simplify_m > 0:
        thinned, last = [merged[0]], (merged[0][1], merged[0][2])
        for p in merged[1:-1]:
            if p[4] != 'recorded' or haversine_km(last, (p[1], p[2])) * 1000 >= args.simplify_m:
                thinned.append(p)
                last = (p[1], p[2])
        thinned.append(merged[-1])
        print(f'\nsimplified {len(merged):,} → {len(thinned):,} points')
        merged = thinned

    total_km = sum(haversine_km((merged[i - 1][1], merged[i - 1][2]), (merged[i][1], merged[i][2]))
                   for i in range(1, len(merged)))
    write_gpx(merged, args.out, args.name)
    print(f'\nholes filled: {n_holes}  points added: {n_added:,}')
    print(f'total route: {total_km:,.0f} km, {len(merged):,} points')
    print(f'{merged[0][0]:%Y-%m-%d %H:%M} → {merged[-1][0]:%Y-%m-%d %H:%M} UTC')
    print(f'wrote {args.out}')


if __name__ == '__main__':
    sys.exit(main())
