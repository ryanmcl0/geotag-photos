#!/usr/bin/env python3
"""
Classify bridge visit sessions for gallery visibility.

Groups each photographed bridge's photos (config/china_bridges.json) into visit
sessions and works out, from the trip GPX record, where each session was shot
from — so galleries can list photos taken from publicly accessible areas and
leave the rest unlisted.

Signals per session:
  deck line   — the bridge centerline from OSM (config/geo/bridge_lines.json,
                fetched once via Overpass), plus fast (>25 km/h) GPS crossings;
                photo distance-to-line = on the bridge vs around it.
  deck level  — median track elevation while crossing fast on the OSM line;
                photo elevation (track points near the photo time) relative to
                it places a photo at road level, well above, or well below.
                Note canyon rims often sit higher than the deck — high but far
                off the line is still a viewpoint.
  dwell shape — vertical range of slow-moving track near the line: arriving by
                car means one elevation however high; a big on-foot range means
                the session left road level.

Sessions are seeded by a 45-min timestamp gap over fence photos, then widened to
same-trip photos inside the session's time window up to 1.8× the fence radius —
GPS drift in canyons can scatter mid-session shots far outside the fence (they
belong to the visit and inherit its label). Where elevation data is solid,
individual photos are refined within their session (at road level → public).

Verdicts: viewpoint / deck_visit → public; above_deck / below_deck → not listed;
ambiguous (no elevation, e.g. the 2024 Guizhou GPX) → review, unlisted until
flipped. Output config/bridge_visits.json (gitignored): per-session photo ids,
evidence, auto verdict, and an `override` field (true=unlisted, false=public)
to correct a session; re-runs keep overrides. photo_privacy.py applies the
labels; fence photos in no session stay unlisted (fail-closed → re-run this
after processing new bridge trips). Drone (DJI) photos are always listed.

Usage: ./analyze_bridge_visits.py [--report]   (--report: print only, no file write)
"""

import json
import math
import sys
from bisect import bisect
from datetime import datetime, timedelta
from pathlib import Path

import gpxpy

ROOT = Path(__file__).resolve().parents[1]  # repo root (script lives in tools/)
sys.path.insert(0, str(ROOT))
import photo_privacy

BRIDGES = ROOT / 'config' / 'china_bridges.json'
LINES = ROOT / 'config' / 'geo' / 'bridge_lines.json'
TRIPS = ROOT / 'config' / 'trips.json'
OUT = ROOT / 'config' / 'bridge_visits.json'

SESSION_GAP_MIN = 45
ABOVE_DECK_M = 30       # ele above deck ⇒ above road level (when on the line)
BELOW_DECK_M = 35       # ele below deck ⇒ below road level (when on the line)
ON_LINE_M = 60          # close enough to the centerline to be "on the bridge"
OFF_LINE_M = 100        # median beyond this ⇒ viewpoint around the bridge
VERT_RANGE_M = 60       # slow-moving vertical range ⇒ session left road level
CAPTURE_FACTOR = 1.8    # session window picks up photos out to fence × this
WINDOW_PAD_MIN = 20
ELE_NOISE_GUARD = 2     # photos needed to confirm an elevation-based verdict


def dist_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    x = (lon2 - lon1) * p * math.cos((lat1 + lat2) / 2 * p)
    y = (lat2 - lat1) * p
    return 6371 * math.hypot(x, y)


def seg_dist_m(lat, lon, a, b):
    """Point to segment distance in metres (local equirectangular)."""
    kx = 111.32 * math.cos(math.radians(lat))
    ky = 110.57
    px, py = (lon - a[1]) * kx, (lat - a[0]) * ky
    vx, vy = (b[1] - a[1]) * kx, (b[0] - a[0]) * ky
    L2 = vx * vx + vy * vy
    t = 0 if L2 == 0 else max(0, min(1, (px * vx + py * vy) / L2))
    return math.hypot(px - t * vx, py - t * vy) * 1000


def line_dist_m(lat, lon, lines):
    best = None
    for line in lines:
        for i in range(1, len(line)):
            d = seg_dist_m(lat, lon, line[i - 1], line[i])
            if best is None or d < best:
                best = d
    return best


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


# ---------------------------------------------------------------- GPX

def trip_gpx_paths():
    """slug → gpx paths, from config/trips.json (public trips only — private trips
    aren't publicly visible, so their bridge photos need no per-photo handling)."""
    cfg = json.loads(TRIPS.read_text())
    import re
    slugify = lambda s: re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')
    out = {}
    for tr in cfg.get('public', []):
        if tr.get('gpx'):
            g = tr['gpx']
            out[slugify(tr['name'])] = g if isinstance(g, list) else [g]
    return out


def load_track(paths, echo):
    pts = []
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += [f for f in sorted(p.rglob('*.gpx')) if not f.name.startswith('._')]
        elif p.exists():
            files.append(p)
        else:
            echo(f"    ⚠ GPX path missing: {p}")
    for f in files:
        try:
            g = gpxpy.parse(f.open())
        except Exception as e:
            echo(f"    ⚠ {f.name}: {e}")
            continue
        for trk in g.tracks:
            for seg in trk.segments:
                for pt in seg.points:
                    if pt.time:
                        pts.append((pt.time.replace(tzinfo=None), pt.latitude, pt.longitude, pt.elevation))
    pts.sort()
    return pts


class Track:
    def __init__(self, pts):
        self.pts = pts
        self.times = [p[0] for p in pts]

    def window(self, t0, t1):
        return self.pts[bisect(self.times, t0):bisect(self.times, t1)]

    def ele_at(self, t, pad_s=300):
        eles = [p[3] for p in self.window(t - timedelta(seconds=pad_s), t + timedelta(seconds=pad_s))
                if p[3] is not None]
        return median(eles)


# ---------------------------------------------------------------- per-bridge geometry

def bridge_geometry(b, lines_cfg, tracks, echo):
    """(centerline polylines, deck elevation). The OSM centerline is the deck-level
    reference; the fast (>25 km/h) GPS crossings add the actually-driven line for
    bridges OSM lacks (and approaches), so photo line-distance = min over both."""
    osm = (lines_cfg.get(b['name']) or {}).get('lines') or []
    radius = b.get('radius_km', 3.0)

    def in_fence(p):
        return dist_km(b['lat'], b['lon'], p[1], p[2]) <= radius

    fast = []  # fence points at driving speed = crossings + approach road
    for tr in tracks.values():
        prev = None
        for p in tr.pts:
            if in_fence(p):
                if prev is not None:
                    dt = (p[0] - prev[0]).total_seconds()
                    if 0 < dt <= 60:
                        v = dist_km(prev[1], prev[2], p[1], p[2]) / (dt / 3600)
                        if v > 25:
                            fast.append(p)
                prev = p
            else:
                prev = None

    # deck elevation only from crossings of the OSM bridge way itself — fast points
    # elsewhere in the fence are canyon/approach roads at unrelated elevations
    deck = None
    if osm:
        near_eles = [p[3] for p in fast if p[3] is not None
                     and line_dist_m(p[1], p[2], osm) <= ON_LINE_M]
        deck = median(near_eles)

    lines = list(osm)
    if fast:
        lines.append([[p[1], p[2]] for p in fast])
        if not osm:
            echo(f"    (no OSM way — deck line from {len(fast)} fast GPS points)")
            near_eles = [p[3] for p in fast if p[3] is not None]
            deck = median(near_eles)
    return lines, deck


# ---------------------------------------------------------------- sessions & verdicts

def build_sessions(fence_photos, trip_photos, b):
    """Seed sessions from fence photos (45-min gaps), then widen each to same-trip
    photos inside the time window up to CAPTURE_FACTOR × the fence radius (GPS
    drift scatters mid-session shots outside the fence — they belong to the visit)."""
    seeds = []
    for ph in sorted(fence_photos, key=lambda p: p['timestamp']):
        t = datetime.fromisoformat(ph['timestamp'].replace('Z', ''))
        if seeds and (t - seeds[-1][-1]['_t']).total_seconds() <= SESSION_GAP_MIN * 60:
            seeds[-1].append({**ph, '_t': t})
        else:
            seeds.append([{**ph, '_t': t}])

    capture_km = b.get('radius_km', 3.0) * CAPTURE_FACTOR
    pad = timedelta(minutes=WINDOW_PAD_MIN)
    out = []
    for sess in seeds:
        # iterative: captured photos extend the window (drifted-GPS shots often
        # trail the last in-fence photo by more than one pad)
        while True:
            t0, t1 = sess[0]['_t'] - pad, sess[-1]['_t'] + pad
            have = {p['id'] for p in sess}
            extra = []
            for ph in trip_photos[sess[0]['trip']]:
                if ph['id'] in have:
                    continue
                t = datetime.fromisoformat(ph['timestamp'].replace('Z', ''))
                if t0 <= t <= t1 and dist_km(b['lat'], b['lon'], ph['lat'], ph['lon']) <= capture_km:
                    extra.append({**ph, '_t': t})
            if not extra:
                break
            sess = sorted(sess + extra, key=lambda p: p['_t'])
        out.append(sess)
    return out


def judge(sess, b, lines, deck, track):
    t0, t1 = sess[0]['_t'], sess[-1]['_t']
    dur_min = (t1 - t0).total_seconds() / 60
    rows = []
    for ph in sess:
        ld = line_dist_m(ph['lat'], ph['lon'], lines) if lines else None
        ele = track.ele_at(ph['_t']) if track else None
        rows.append((ph['id'], ld, ele))

    line_ds = [ld for _, ld, _ in rows if ld is not None]
    med_line = median(line_ds)

    def is_above(ld, e):
        return (e is not None and deck is not None and ld is not None
                and e >= deck + ABOVE_DECK_M and ld <= ON_LINE_M)

    def is_below(ld, e):
        return (e is not None and deck is not None and ld is not None
                and e <= deck - BELOW_DECK_M and ld <= ON_LINE_M)

    def is_road_level(ld, e):
        return (e is not None and deck is not None and ld is not None
                and abs(e - deck) < ABOVE_DECK_M and ld <= ON_LINE_M)

    n_above = sum(1 for _, ld, e in rows if is_above(ld, e))
    n_below = sum(1 for _, ld, e in rows if is_below(ld, e))
    n_road = sum(1 for _, ld, e in rows if is_road_level(ld, e))
    ele_cov = sum(1 for _, _, e in rows if e is not None)

    # Slow-moving track shape near the line: a drive-up viewpoint dwells at ONE
    # elevation however high; a big on-foot vertical range means the session
    # left road level. (Slow filter: passing traffic at rim/approach elevations
    # can't fake it.)
    vert_range = None
    if track and lines:
        w = track.window(t0 - timedelta(minutes=20), t1 + timedelta(minutes=20))
        slow = []
        for i in range(1, len(w)):
            dt = (w[i][0] - w[i - 1][0]).total_seconds()
            if not (0 < dt <= 90) or w[i][3] is None:
                continue
            v = dist_km(w[i - 1][1], w[i - 1][2], w[i][1], w[i][2]) / (dt / 3600)
            if v < 10 and line_dist_m(w[i][1], w[i][2], lines) <= 200:
                slow.append(w[i][3])
        if len(slow) >= 8:
            s = sorted(slow)
            vert_range = s[-5] - s[max(0, len(s) // 10)]   # 5th-highest vs 10th pctile

    ev = {'dur_min': round(dur_min), 'med_line_m': round(med_line) if med_line is not None else None,
          'n_above': n_above, 'n_below': n_below, 'n_road': n_road,
          'ele_photos': f"{ele_cov}/{len(sess)}",
          'vert_range_m': round(vert_range) if vert_range is not None else None}

    if n_above >= ELE_NOISE_GUARD or (vert_range is not None and vert_range >= VERT_RANGE_M):
        verdict, private = 'above_deck', True
    elif n_below >= ELE_NOISE_GUARD:
        verdict, private = 'below_deck', True
    elif med_line is not None and med_line > OFF_LINE_M and not (n_above or n_below):
        verdict, private = 'viewpoint', False
    elif (n_road >= max(ELE_NOISE_GUARD, len(sess) // 2) and dur_min <= 90
          and not (n_above or n_below)):
        verdict, private = 'deck_visit', False
    else:
        verdict, private = 'review', True   # ambiguous → unlisted until reviewed

    # Per-photo refinement inside the session: solid elevation evidence beats the
    # session default (e.g. road-level shots before/after an above-deck session).
    refined = {}
    for pid, ld, e in rows:
        if is_above(ld, e) or is_below(ld, e):
            if not private:
                refined[pid] = True
        elif is_road_level(ld, e):
            if private:
                refined[pid] = False
    return verdict, private, refined, ev


# ---------------------------------------------------------------- main

def main():
    report_only = '--report' in sys.argv
    echo = print
    bridges = [b for b in json.loads(BRIDGES.read_text()).get('bridges', [])
               if b.get('done') and b.get('lat') is not None]
    lines_cfg = json.loads(LINES.read_text()) if LINES.exists() else {}

    gpx_paths = trip_gpx_paths()
    trip_meta = photo_privacy.load_trip_meta()

    # all geotagged non-drone photos per public trip + fence membership per bridge
    trip_photos = {}
    fence_by_bridge = {}
    for slug, public in trip_meta.items():
        if not public:
            continue
        man = photo_privacy.load_full_manifest(photo_privacy.WEB_TRIPS / slug)
        if not man:
            continue
        for ph in man.get('photos', []):
            if ph.get('lat') is None or ph['id'].upper().startswith('DJI'):
                continue
            rec = {**ph, 'trip': slug}
            trip_photos.setdefault(slug, []).append(rec)
            for b in bridges:
                if dist_km(b['lat'], b['lon'], ph['lat'], ph['lon']) <= b.get('radius_km', 3.0):
                    fence_by_bridge.setdefault(b['name'], []).append(rec)
                    break

    echo("Loading GPX tracks…")
    tracks = {}
    slugs = {p['trip'] for phs in fence_by_bridge.values() for p in phs}
    for slug in sorted(slugs):
        if slug in gpx_paths:
            pts = load_track(gpx_paths[slug], echo)
            tracks[slug] = Track(pts)
            n_ele = sum(1 for p in pts if p[3] is not None)
            echo(f"  {slug}: {len(pts)} points ({'with' if n_ele else 'NO'} elevation)")
        else:
            echo(f"  ⚠ {slug}: no GPX in config — sessions will be 'review'")

    prev_overrides = {}
    if OUT.exists():
        try:
            for s in json.loads(OUT.read_text()).get('sessions', []):
                if s.get('override') is not None:
                    for pid in s.get('photos', []):
                        prev_overrides[(s['trip'], pid)] = s['override']
        except (OSError, json.JSONDecodeError):
            pass

    sessions_out = []
    assigned = set()
    echo("\nbridge / session                              dur    line   evidence                            verdict")
    for b in bridges:
        fence = [p for p in fence_by_bridge.get(b['name'], []) if (p['trip'], p['id']) not in assigned]
        if not fence:
            continue
        notes = []
        lines, deck = bridge_geometry(b, lines_cfg, tracks, notes.append)
        echo(f"\n{b['name']}  (deck≈{round(deck) if deck is not None else '?'} m)")
        for n in notes:
            echo(n)
        for sess in build_sessions(fence, trip_photos, b):
            sess = [p for p in sess if (p['trip'], p['id']) not in assigned]
            if not sess:
                continue
            trip = sess[0]['trip']
            verdict, private, refined, ev = judge(sess, b, lines, deck, tracks.get(trip))
            ids = [p['id'] for p in sess]
            assigned.update((trip, pid) for pid in ids)
            ov = {prev_overrides[(trip, pid)] for pid in ids if (trip, pid) in prev_overrides}
            override = ov.pop() if len(ov) == 1 else None
            sessions_out.append({
                'bridge': b['name'], 'trip': trip,
                'start': sess[0]['_t'].isoformat(), 'end': sess[-1]['_t'].isoformat(),
                'n_photos': len(ids), 'photos': ids,
                'auto_verdict': verdict, 'private_auto': private,
                'refined': refined, 'override': override, 'evidence': ev,
            })
            eff = override if override is not None else private
            n_flip = len(refined) if override is None else 0
            mark = ' *override*' if override is not None else (f' ({n_flip} refined)' if n_flip else '')
            echo(f"  {trip} {sess[0]['_t']:%Y-%m-%d %H:%M} n={len(ids):3d}   "
                 f"{ev['dur_min']:4d}m  {str(ev['med_line_m']):>5s}m  "
                 f"abv={ev['n_above']} blw={ev['n_below']} road={ev['n_road']} "
                 f"rng={ev['vert_range_m']} ele={ev['ele_photos']:>7s}  "
                 f"{verdict} → {'UNLISTED' if eff else 'public'}{mark}")

    def eff_count(s):
        if s['override'] is not None:
            return s['n_photos'] if s['override'] else 0
        base = s['private_auto']
        return sum(1 for pid in s['photos'] if s['refined'].get(pid, base))
    n_priv = sum(eff_count(s) for s in sessions_out)
    n_all = sum(s['n_photos'] for s in sessions_out)
    echo(f"\n{len(sessions_out)} sessions, {n_all} photos → {n_priv} unlisted / {n_all - n_priv} public")
    if not report_only:
        OUT.write_text(json.dumps({
            '_comment': ("Per-session visibility labels for bridge photos, generated by "
                         "analyze_bridge_visits.py — edit 'override' (true=unlisted, false=public) "
                         "to correct a session; re-runs keep overrides. photo_privacy.py applies "
                         "the labels; fence photos in no session stay unlisted."),
            'generated': datetime.now().isoformat(timespec='seconds'),
            'sessions': sessions_out,
        }, indent=2, ensure_ascii=False))
        echo(f"✓ Wrote {OUT.relative_to(ROOT)} — review, set overrides, then re-run "
             f"./photo_privacy.py && ./build_collections.py")


if __name__ == '__main__':
    main()
