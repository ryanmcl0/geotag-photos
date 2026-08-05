#!/usr/bin/env python3
"""Snap geotagged photos to numbered roads (OpenStreetMap) for the Highways facet.

For every geotagged photo in web/trips/*/manifest(.all).json, finds the nearest
OSM way carrying a Chinese route number (G/S/X/Y ref) within --radius metres and
writes config/road_assignments.json:

    {"radius_m": 250, "photos": {"<trip>/<id>": ["G217", "S315", ...]}}

refs are sorted nearest-first; build_collections.facet_highways picks the first
one that appears in the config/china_highways.json roster.

OSM data is fetched per 0.5-degree cell around the photos and cached as JSON in
--cache-dir (default config/geo/.osm_road_cache/, gitignored) so re-runs after
new trips only fetch new cells. The public Overpass API is rate-limited and
intermittently busy: the fetch retries patiently and a partial run is safe to
re-run — finished cells are never refetched.

Usage:
    python3 tools/road_scan.py                 # fetch missing cells + assign
    python3 tools/road_scan.py --no-fetch      # assign from cached cells only
"""
import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_privacy  # noqa: E402  (repo module)

WEB_TRIPS = ROOT / 'web' / 'trips'
OUT = ROOT / 'config' / 'road_assignments.json'
ENDPOINT = 'https://overpass-api.de/api/interpreter'
GRID = 0.5
PAD = 0.06          # deg halo: photos at a cell edge still see the road across it
REF_RE = re.compile(r'^[GSXY][0-9]')


# G/S/X/Y route numbers are a Chinese convention — scanning other countries just
# burns Overpass quota (and Poland's S-roads would collide), so clamp to China.
BBOX = (18.0, 73.0, 54.0, 135.0)     # min_lat, min_lon, max_lat, max_lon


def load_photos():
    """[(trip, id, lat, lon)] for every geotagged photo (full manifests) in BBOX."""
    photos = []
    for mf in sorted(WEB_TRIPS.glob('*/manifest.json')):
        manifest = photo_privacy.load_full_manifest(mf.parent)
        if not manifest:
            continue
        for ph in manifest.get('photos', []):
            lat, lon = ph.get('lat'), ph.get('lon')
            if lat is None or lon is None:
                continue
            if BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]:
                photos.append((mf.parent.name, ph['id'], lat, lon))
    return photos


def cell_of(lat, lon):
    return (round(lat / GRID) * GRID, round(lon / GRID) * GRID)


def fetch_cell(lat, lon, cache_dir, tries=12):
    path = cache_dir / f'{lat:.2f}_{lon:.2f}.json'
    if path.exists():
        return True
    s, n = lat - GRID / 2 - PAD, lat + GRID / 2 + PAD
    w, e = lon - GRID / 2 - PAD, lon + GRID / 2 + PAD
    q = (f'[out:json][timeout:240][bbox:{s:.4f},{w:.4f},{n:.4f},{e:.4f}];'
         'way["highway"]["ref"~"^[GSXY][0-9]"];out tags geom;')
    body = urllib.parse.urlencode({'data': q}).encode()
    for a in range(tries):
        try:
            req = urllib.request.Request(ENDPOINT, data=body,
                                         headers={'User-Agent': 'geotag-photos/1.0 (personal archive)'})
            with urllib.request.urlopen(req, timeout=300) as resp:
                els = json.loads(resp.read())['elements']
            path.write_text(json.dumps(els))
            return True
        except Exception as ex:                       # 429/504 while the API is busy
            print(f'    retry {a + 1}: {ex}', flush=True)
            time.sleep(8 + 4 * a)
    return False


def seg_dist_m(plat, plon, a, b):
    k = math.cos(math.radians(plat))
    px, py = plon * k, plat
    ax, ay = a['lon'] * k, a['lat']
    bx, by = b['lon'] * k, b['lat']
    dx, dy = bx - ax, by - ay
    t = 0.0 if (dx == 0 and dy == 0) else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) * 111320


def clean_refs(raw):
    """'G6;G7' → ['G6','G7'];  'G315(旧)' → ['G315'];  keep only G/S/X/Y numbers."""
    out = []
    for part in re.split(r'[;,]', raw or ''):
        part = re.sub(r'[（(].*$', '', part.strip()).strip()
        if part and REF_RE.match(part) and part not in out:
            out.append(part)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--radius', type=int, default=250, help='max metres from photo to road (default 250)')
    ap.add_argument('--cache-dir', default=str(ROOT / 'config' / 'geo' / '.osm_road_cache'))
    ap.add_argument('--no-fetch', action='store_true', help='use cached cells only, fetch nothing')
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    photos = load_photos()
    print(f'{len(photos)} geotagged photos')
    by_cell = {}
    for p in photos:
        by_cell.setdefault(cell_of(p[2], p[3]), []).append(p)

    # fetch every cell that touches a photo (neighbours included via PAD on bbox)
    cells = sorted(by_cell, key=lambda c: -len(by_cell[c]))
    if not args.no_fetch:
        missing = [c for c in cells if not (cache_dir / f'{c[0]:.2f}_{c[1]:.2f}.json').exists()]
        print(f'{len(cells)} cells, {len(cells) - len(missing)} cached, {len(missing)} to fetch')
        for i, (la, lo) in enumerate(missing):
            ok = fetch_cell(la, lo, cache_dir)
            print(f'  cell {i + 1}/{len(missing)} {la:.2f},{lo:.2f}: {"ok" if ok else "FAILED"}', flush=True)

    # index ways per cell (photo → own cell + 8 neighbours, so border roads count)
    def ways_for(cell):
        la, lo = cell
        out = []
        for dla in (-GRID, 0, GRID):
            for dlo in (-GRID, 0, GRID):
                p = cache_dir / f'{la + dla:.2f}_{lo + dlo:.2f}.json'
                if p.exists():
                    out.append(p)
        return out

    way_cache = {}
    assignments = {}
    snapped = 0
    for cell, plist in by_cell.items():
        ways = []
        for p in ways_for(cell):
            if p not in way_cache:
                els = json.loads(p.read_text())
                way_cache[p] = [(clean_refs(e['tags'].get('ref')), e.get('geometry') or [])
                                for e in els if e.get('tags', {}).get('ref')]
            ways.extend(way_cache[p])
        if not ways:
            continue
        for trip, pid, lat, lon in plist:
            best = {}    # ref → distance
            for refs, g in ways:
                if not refs or len(g) < 2:
                    continue
                # coarse bbox reject before exact segment distance
                if not any(abs(pt['lat'] - lat) < 0.01 and abs(pt['lon'] - lon) < 0.012 for pt in g[::4]):
                    continue
                d = min(seg_dist_m(lat, lon, g[j], g[j + 1]) for j in range(len(g) - 1))
                if d <= args.radius:
                    for ref in refs:
                        if ref not in best or d < best[ref]:
                            best[ref] = d
            if best:
                snapped += 1
                assignments[f'{trip}/{pid}'] = [r for r, _ in sorted(best.items(), key=lambda x: x[1])][:3]

    OUT.write_text(json.dumps({'_comment': 'GENERATED by tools/road_scan.py — do not hand-edit. '
                                           'Photo → nearest numbered road(s) within radius_m, nearest first.',
                               'radius_m': args.radius, 'photos': assignments}, indent=1))
    print(f'{snapped}/{len(photos)} photos within {args.radius} m of a numbered road → {OUT}')


if __name__ == '__main__':
    main()
