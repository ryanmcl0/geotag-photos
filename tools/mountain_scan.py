#!/usr/bin/env python3
"""Find candidate mountains for every processed trip, for the Mountains roster.

Two signals per trip:
  * GPX HIGH POINTS: local elevation maxima along the recorded track with at
    least --prominence metres of drop either side (a summit or a pass that was
    actually walked/driven over), and
  * PHOTO CLUSTERS: where photos were taken (manifest clusters).

Each is looked up against OpenStreetMap peaks/saddles/volcanoes (Overpass) and
the nearest named features are listed, so a human can decide which of them were
climbed. Results are cached per trip under --out so Overpass is only hit once.

    ./venv/bin/python tools/mountain_scan.py
    ./venv/bin/python tools/mountain_scan.py --trip <slug> --prominence 80

The report (<out>/report.md) is what you read; <out>/<slug>.json has the raw
candidates for later scripting.
"""
import argparse
import glob
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_privacy  # noqa: E402

GPX_NS = '{http://www.topografix.com/GPX/1/1}'
OVERPASS_SERVERS = ['https://overpass-api.de/api/interpreter',
                    'https://z.overpass-api.de/api/interpreter']
UA = 'geotag-photos-mountain-scan/1.0'


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    dp = math.radians(lat2 - lat1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def gpx_files(spec):
    specs = spec if isinstance(spec, list) else [spec]
    out = []
    for s in specs:
        if os.path.isdir(s):
            out += sorted(glob.glob(os.path.join(s, '*.gpx')))
        elif os.path.exists(s):
            out.append(s)
    return out


def load_track(files):
    pts = []
    for f in files:
        try:
            tree = ET.parse(f)
        except ET.ParseError:
            continue
        for tp in tree.iter(GPX_NS + 'trkpt'):
            e = tp.find(GPX_NS + 'ele')
            t = tp.find(GPX_NS + 'time')
            if e is None:
                continue
            try:
                pts.append((t.text if t is not None else '', float(tp.get('lat')),
                            float(tp.get('lon')), float(e.text)))
            except (TypeError, ValueError):
                pass
    pts.sort(key=lambda p: p[0])
    return pts


def thin(pts, min_m=40):
    """Drop near-duplicate points so prominence scanning stays cheap."""
    out = []
    for p in pts:
        if not out or dist_m(out[-1][1], out[-1][2], p[1], p[2]) >= min_m:
            out.append(p)
    return out


def high_points(pts, prominence=100, min_sep_m=400):
    """Local maxima whose elevation drops >= prominence on both sides before a
    higher point is met (track ends count as a drop). Nearby maxima collapse to
    the highest one."""
    n = len(pts)
    if n < 3:
        return []
    ele = [p[3] for p in pts]
    cands = []
    for i in range(n):
        if (i > 0 and ele[i] < ele[i - 1]) or (i < n - 1 and ele[i] < ele[i + 1]):
            continue
        # left
        j, lo = i - 1, ele[i]
        while j >= 0 and ele[j] <= ele[i]:
            lo = min(lo, ele[j]); j -= 1
        left = ele[i] - lo if j < 0 else ele[i] - lo
        if j >= 0 and ele[i] - lo < prominence:
            continue
        j, lo = i + 1, ele[i]
        while j < n and ele[j] <= ele[i]:
            lo = min(lo, ele[j]); j += 1
        if j < n and ele[i] - lo < prominence:
            continue
        right = ele[i] - lo
        cands.append((i, min(left, right)))
    # merge maxima closer than min_sep_m: keep the highest
    cands.sort(key=lambda c: -ele[c[0]])
    kept = []
    for i, prom in cands:
        if all(dist_m(pts[i][1], pts[i][2], pts[k][1], pts[k][2]) >= min_sep_m for k, _ in kept):
            kept.append((i, prom))
    kept.sort(key=lambda c: pts[c[0]][0])
    return [{'time': pts[i][0], 'lat': round(pts[i][1], 5), 'lon': round(pts[i][2], 5),
             'ele': round(pts[i][3]), 'prominence': round(prom)} for i, prom in kept]


def overpass(query, retries=6):
    body = urllib.parse.urlencode({'data': query}).encode()   # POST: a trip can need 100+ clauses
    for attempt in range(retries):
        server = OVERPASS_SERVERS[attempt % len(OVERPASS_SERVERS)]   # rotate mirrors on failure
        try:
            req = urllib.request.Request(server, data=body, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:  # noqa: BLE001
            wait = 15 * (attempt + 1)
            print(f"    overpass error on {server.split('/')[2]} ({e}); retry in {wait}s", flush=True)
            time.sleep(wait)
    return {'elements': []}


def peaks_near(points, radii):
    """One Overpass query: named peaks/saddles/volcanoes around each point."""
    if not points:
        return []
    clauses = []
    for (lat, lon), r in zip(points, radii):
        clauses.append(f'node["natural"~"^(peak|saddle|volcano|hill)$"](around:{int(r)},{lat:.5f},{lon:.5f});')
    q = '[out:json][timeout:180];(' + ''.join(clauses) + ');out body;'
    data = overpass(q)
    feats = []
    for e in data.get('elements', []):
        tags = e.get('tags') or {}
        name = tags.get('name:en') or tags.get('name') or tags.get('int_name')
        if not name:
            continue
        ele = tags.get('ele')
        try:
            ele = round(float(str(ele).replace(',', '.').split()[0])) if ele else None
        except ValueError:
            ele = None
        feats.append({'id': e['id'], 'lat': e['lat'], 'lon': e['lon'], 'name': name,
                      'name_local': tags.get('name'), 'ele': ele, 'kind': tags.get('natural'),
                      'wikipedia': tags.get('wikipedia'), 'wikidata': tags.get('wikidata')})
    return feats


def nearest(feats, lat, lon, k=4):
    rows = sorted(((dist_m(lat, lon, f['lat'], f['lon']), f) for f in feats), key=lambda x: x[0])
    return [{**f, 'dist_m': round(d)} for d, f in rows[:k]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(ROOT / 'local_browse' / 'mountain_scan'))   # local, git-ignored
    ap.add_argument('--trip', default=None, help='substring of trip slug')
    ap.add_argument('--prominence', type=float, default=100)
    ap.add_argument('--peak-radius', type=float, default=700, help='m around a GPX high point')
    ap.add_argument('--photo-radius', type=float, default=1500, help='m around a photo cluster')
    ap.add_argument('--refresh', action='store_true', help='ignore cached Overpass results')
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    trips = json.loads((ROOT / 'config' / 'trips.json').read_text())
    by_slug = {}
    for group in ('public', 'private'):
        for t in trips.get(group, []):
            by_slug[slug_of(t['name'])] = t

    report = []
    for mf in sorted((ROOT / 'web' / 'trips').glob('*/manifest.json')):
        slug = mf.parent.name
        if args.trip and args.trip not in slug:
            continue
        cache = out / f'{slug}.json'
        if cache.exists() and not args.refresh:
            res = json.loads(cache.read_text())
        else:
            manifest = photo_privacy.load_full_manifest(mf.parent) or {}
            photos = [p for p in manifest.get('photos', []) if p.get('lat') is not None]
            if not photos:
                continue
            cfg = by_slug.get(slug) or {}
            track = thin(load_track(gpx_files(cfg['gpx']))) if cfg.get('gpx') else []
            hps = high_points(track, args.prominence) if track else []
            clusters = [{'location': c.get('location'), 'lat': c['lat'], 'lon': c['lon'],
                         'count': len(c.get('photo_ids', []))} for c in manifest.get('clusters', [])]
            # exact per-photo positions matter more than the cluster centre when a
            # cluster radius is huge (Etna: 3 km); use both.
            pts = [(h['lat'], h['lon']) for h in hps] + [(c['lat'], c['lon']) for c in clusters]
            radii = [args.peak_radius] * len(hps) + [args.photo_radius] * len(clusters)
            print(f"{slug}: {len(track)} track pts, {len(hps)} high points, {len(clusters)} clusters → overpass…", flush=True)
            feats = peaks_near(pts, radii)
            for h in hps:
                h['nearest'] = nearest(feats, h['lat'], h['lon'])
            for c in clusters:
                c['nearest'] = [f for f in nearest(feats, c['lat'], c['lon'], 6)
                                if f['dist_m'] <= args.photo_radius]
            res = {'slug': slug, 'high_points': hps, 'clusters': clusters,
                   'features': feats, 'photo_count': len(photos)}
            cache.write_text(json.dumps(res, indent=1, ensure_ascii=False))
            time.sleep(3)
        report.append(res)

    lines = ['# Mountain scan', '']
    for res in report:
        if not res['high_points'] and not any(c['nearest'] for c in res['clusters']):
            continue
        lines.append(f"## {res['slug']}  ({res['photo_count']} photos)")
        if res['high_points']:
            lines.append('GPX high points (summits / passes walked or driven over):')
            for h in res['high_points']:
                near = ', '.join(f"{f['name']}{' ' + str(f['ele']) + 'm' if f['ele'] else ''} [{f['kind']}] {f['dist_m']}m"
                                 for f in h['nearest'] if f['dist_m'] <= 1500) or '—'
                lines.append(f"- {h['time'][:16]} {h['ele']} m (prom {h['prominence']}) @ {h['lat']},{h['lon']} → {near}")
        rows = [c for c in res['clusters'] if c['nearest']]
        if rows:
            lines.append('Photo clusters with a named peak within reach:')
            for c in rows:
                near = ', '.join(f"{f['name']}{' ' + str(f['ele']) + 'm' if f['ele'] else ''} [{f['kind']}] {f['dist_m']}m"
                                 for f in c['nearest'])
                lines.append(f"- {c['location']} ({c['count']} photos) @ {c['lat']:.4f},{c['lon']:.4f} → {near}")
        lines.append('')
    (out / 'report.md').write_text('\n'.join(lines))
    print(f"\n✓ {out / 'report.md'}")


def slug_of(name):
    import re
    s = name.lower().replace(':', '-')
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s


if __name__ == '__main__':
    main()
