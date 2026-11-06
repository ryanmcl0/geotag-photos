#!/usr/bin/env python3
"""Phone photos on the place pages: which phone photos belong to which page.

Every page built from camera photos (trip galleries, provinces, bridges, roofs,
energy sites, mountains, roads) gets a local-only Phone tab. This works out its
contents by lining the phone library up against the camera timeline:

  1. Clocks. Phone timestamps are the phone's local wall time stamped "Z"; camera
     manifest timestamps are whatever the trip's pipeline left them as: UTC after
     a GPX geosync, the camera's raw local clock, or a UK-time Sony clock. Each
     camera trip's regime is measured, not assumed: phone photos with GPS give a
     direct fit (the offset that puts phone GPS on the camera track); without
     phone GPS the regime whose camera "local time" lines up with the most phone
     shots wins.
  2. Matching. Each phone photo is matched to the camera photo nearest in time
     (across all trips, in each trip's own clock regime). It joins that trip's
     gallery, filed under the same day, and every collection page that camera
     photo is on, if it was taken within MATCH_WINDOW of it (and, when the phone
     knows where it was, within MATCH_KM).
  3. Location. Phone GPS when present; otherwise the matched camera photo's
     position, or a time interpolation between the camera fixes either side.
     Provinces, cities and spots (place_names.py) are decided by this location,
     so a phone-only afternoon still lands in the right province and city.

Reels ride along: each edited reel (web/phone/reels/reels.json, local only) is
put on the pages its drone GPS reaches: the city and spot each point falls in,
and any bridge / building / mountain / site page with a photo within REEL_KM.
(Provinces and energy sites already come matched from the reel pipeline.)

Reads:  web/trips/*/manifest(.all).json, web/collections/*.json (built pages),
        web/phone/trips/*/manifest.json, config/people_local.json ("me"),
        web/phone/reels/reels.json
Writes: web/phone/places.json (git-ignored and never deployed, like all of
        web/phone/; the site only fetches it on localhost)

Run after build_collections.py and tools/build_phone_site.py:
    python3 tools/phone_places.py [--report]
"""
import bisect
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_privacy  # noqa: E402
from build_collections import PROVINCE_ZH_EN, ProvinceIndex, slugify  # noqa: E402
from place_names import PlaceIndex  # noqa: E402

WEB = ROOT / 'web'
PHONE_TRIPS = WEB / 'phone' / 'trips'
COLLECTIONS = WEB / 'collections'
PEOPLE_LOCAL = ROOT / 'config' / 'people_local.json'
PROVINCE_OVERRIDES = ROOT / 'config' / 'province_overrides.json'
CLASSIFY_CONFIG = ROOT / 'config' / 'classifications.json'
REELS = WEB / 'phone' / 'reels' / 'reels.json'
OUT = WEB / 'phone' / 'places.json'
REEL_KM = 1.0               # a reel point this close to a page's photo puts it on the page

MATCH_WINDOW = 45 * 60      # s: phone shot inherits the nearest camera shot's pages
MATCH_KM = 3.0              # phone GPS must agree with that camera shot
TRIP_WINDOW = 6 * 3600      # s: still part of the trip, between camera sessions
TRIP_SPAN_MAX = 30 * 86400  # s: longer "trips" are year folders (London roofs)
INTERP_GAP = 6 * 3600       # s: interpolate between camera fixes this close
INTERP_KM = 150             # ... and this far apart
OVERLAP_S = 600             # s: "same moment" for the clock-regime vote

# Messaging-app saves, screenshots: not photos of the place, and their
# timestamps are when they were received or captured on screen.
NOT_A_PLACE_PHOTO = re.compile(r'-WA\d|snapchat|screenshot', re.I)
DUP_SUFFIX = re.compile(r'(_\d+_|\s?\(\d+\)|-\d)$')

# Country → the zone its phones show. China is one zone (Xinjiang phones run on
# Beijing time). Multi-zone countries are split by longitude in zone_for().
COUNTRY_TZ = {
    'AE': 'Asia/Dubai', 'AL': 'Europe/Tirane', 'AT': 'Europe/Vienna',
    'BA': 'Europe/Sarajevo', 'BE': 'Europe/Brussels', 'BG': 'Europe/Sofia',
    'BH': 'Asia/Bahrain', 'CH': 'Europe/Zurich', 'CN': 'Asia/Shanghai',
    'CY': 'Asia/Nicosia', 'CZ': 'Europe/Prague', 'DE': 'Europe/Berlin',
    'DK': 'Europe/Copenhagen', 'EG': 'Africa/Cairo', 'EH': 'Africa/El_Aaiun',
    'ES': 'Europe/Madrid', 'FI': 'Europe/Helsinki', 'FR': 'Europe/Paris',
    'GB': 'Europe/London', 'GR': 'Europe/Athens', 'HK': 'Asia/Hong_Kong',
    'HR': 'Europe/Zagreb', 'IE': 'Europe/Dublin', 'IL': 'Asia/Jerusalem',
    'IN': 'Asia/Kolkata', 'IS': 'Atlantic/Reykjavik', 'IT': 'Europe/Rome',
    'JP': 'Asia/Tokyo', 'KG': 'Asia/Bishkek', 'KR': 'Asia/Seoul',
    'KZ': 'Asia/Almaty', 'LU': 'Europe/Luxembourg', 'LV': 'Europe/Riga',
    'MA': 'Africa/Casablanca', 'MN': 'Asia/Ulaanbaatar', 'MR': 'Africa/Nouakchott',
    'MY': 'Asia/Kuala_Lumpur', 'NL': 'Europe/Amsterdam', 'NO': 'Europe/Oslo',
    'PH': 'Asia/Manila', 'PL': 'Europe/Warsaw', 'PT': 'Europe/Lisbon',
    'RO': 'Europe/Bucharest', 'SE': 'Europe/Stockholm', 'SI': 'Europe/Ljubljana',
    'SK': 'Europe/Bratislava', 'TH': 'Asia/Bangkok', 'TN': 'Africa/Tunis',
    'TR': 'Europe/Istanbul', 'TW': 'Asia/Taipei', 'VN': 'Asia/Ho_Chi_Minh',
    'ME': 'Europe/Podgorica', 'RS': 'Europe/Belgrade', 'MK': 'Europe/Skopje',
    'HU': 'Europe/Budapest', 'EE': 'Europe/Tallinn', 'LT': 'Europe/Vilnius',
    'SG': 'Asia/Singapore', 'MO': 'Asia/Macau', 'LI': 'Europe/Vaduz',
    'MC': 'Europe/Monaco', 'SM': 'Europe/San_Marino', 'VA': 'Europe/Vatican',
}
HOME_TZ = 'Europe/London'


def zone_for(cc, lon):
    if cc == 'US':
        return ('America/Los_Angeles' if lon < -114 else 'America/Denver' if lon < -101
                else 'America/Chicago' if lon < -87 else 'America/New_York')
    if cc == 'ID':
        return 'Asia/Jakarta' if lon < 115 else 'Asia/Makassar' if lon < 127 else 'Asia/Jayapura'
    return COUNTRY_TZ.get(cc) or HOME_TZ


_ZONES = {}


def offset_s(zone, ts):
    """UTC offset in seconds of `zone` at epoch ts."""
    z = _ZONES.get(zone) or _ZONES.setdefault(zone, ZoneInfo(zone))
    return int(datetime.fromtimestamp(ts, z).utcoffset().total_seconds())


def epoch(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def km(lat1, lon1, lat2, lon2):
    p = math.radians
    x = (p(lon2) - p(lon1)) * math.cos(p((lat1 + lat2) / 2))
    return 6371 * math.hypot(x, p(lat2) - p(lat1))


def countries(points):
    """[(lat, lon)] → [cc], offline (single process: its pool breaks under stdin)."""
    if not points:
        return []
    import reverse_geocoder as rg
    return [r['cc'] for r in rg.search(points, mode=1, verbose=False)]


# ---------------------------------------------------------------- loading

def load_camera():
    """{trip: [shot]} sorted by time; shot = dict(ts, lat, lon, id, building)."""
    trips = {}
    for mf in sorted((WEB / 'trips').glob('*/manifest.json')):
        m = photo_privacy.load_full_manifest(mf.parent)
        shots = []
        for ph in (m or {}).get('photos', []):
            if not ph.get('timestamp'):
                continue
            shots.append({'ts': epoch(ph['timestamp']), 'lat': ph.get('lat'), 'lon': ph.get('lon'),
                          'id': ph['id'], 'building': (ph.get('building') or '').strip()})
        if shots:
            trips[mf.parent.name] = sorted(shots, key=lambda s: s['ts'])
    fixes = [s for shots in trips.values() for s in shots if s['lat'] is not None]
    for s, cc in zip(fixes, countries([(s['lat'], s['lon']) for s in fixes])):
        s['zone'] = zone_for(cc, s['lon'])
    # a shot without a position takes the zone of the nearest positioned shot
    for shots in trips.values():
        last = None
        for s in shots:
            last = s.get('zone') or last
            s.setdefault('zone', last)
        nxt = None
        for s in reversed(shots):
            if s['zone'] is None:
                s['zone'] = nxt
            nxt = s['zone'] or nxt
        for s in shots:
            s['zone'] = s['zone'] or HOME_TZ
    return trips


def load_phone():
    out, seen = [], set()
    for mf in sorted(PHONE_TRIPS.glob('phone-*/manifest.json')):
        bucket = mf.parent.name
        if bucket == 'phone-curated':
            continue        # a hand-picked set of photos that live in other buckets
        photos = json.loads(mf.read_text()).get('photos', [])
        for ph in sorted(photos, key=lambda p: len(p['id'])):
            if not ph.get('timestamp') or NOT_A_PLACE_PHOTO.search(ph['id']):
                continue
            # the library holds re-exported copies ("IMG_2555_1_"): same name stem,
            # same moment, same shape → one photo (the plainest name, sorted first)
            dup = (DUP_SUFFIX.sub('', ph['id']), ph['timestamp'], ph.get('ar'))
            if dup in seen:
                continue
            seen.add(dup)
            out.append({'t': bucket, 'i': ph['id'], 'ar': ph.get('ar'),
                        'local': epoch(ph['timestamp']),   # wall time, stamped Z
                        'lat': ph.get('lat'), 'lon': ph.get('lon')})
    return out


def load_pages():
    """(trip, id) → {page key}, from every built collection (the gated/full variant
    where there is one). Key = "<collection>/<facet>/<subtile>"."""
    try:
        coll_ids = [c['id'] for c in json.loads(CLASSIFY_CONFIG.read_text())['collections']]
    except (OSError, json.JSONDecodeError, KeyError):
        coll_ids = []
    pages = defaultdict(set)
    for cid in coll_ids:
        path = next((p for p in (COLLECTIONS / f'{cid}.all.json', COLLECTIONS / f'{cid}.json')
                     if p.exists()), None)
        if not path:
            continue
        for tile in json.loads(path.read_text()).get('tiles', []):
            subs = list(tile.get('subtiles') or []) + [
                s for sec in tile.get('sections') or [] for s in sec.get('subtiles') or []]
            for s in subs:
                for p in s.get('photos') or []:
                    pages[(p['trip'], p['id'])].add(f"{cid}/{tile['id']}/{s['id']}")
            for p in tile.get('photos') or []:
                pages[(p['trip'], p['id'])].add(f"{cid}/{tile['id']}")
    return pages


def load_place_pages():
    """place_names key → page key ("places/cities/<id>"), from the built Places
    collection (each city / spot page lists the keys it covers)."""
    path = COLLECTIONS / 'places.all.json'
    out = {}
    if not path.exists():
        return out
    for tile in json.loads(path.read_text()).get('tiles', []):
        for sec in tile.get('sections') or []:
            for s in sec.get('subtiles') or []:
                for k in s.get('keys') or []:
                    out[k] = f"places/{tile['id']}/{s['id']}"
    return out


def load_me_camera():
    """{trip: [photo ids]} of the camera / drone photos the face index puts "me" in (people_local.json, g 0
    public / 1 gated): the Me chip narrows camera photos too, not only phone ones."""
    try:
        people = json.loads(PEOPLE_LOCAL.read_text()).get('people', [])
    except (OSError, json.JSONDecodeError):
        return {}
    me = next((p for p in people if p.get('key') == 'me'), None)
    out = defaultdict(set)
    for p in (me or {}).get('photos', []):
        if p.get('g') in (0, 1):
            out[p['t']].add(p['i'])
    return {t: sorted(ids) for t, ids in sorted(out.items())}


def load_me():
    try:
        people = json.loads(PEOPLE_LOCAL.read_text()).get('people', [])
    except (OSError, json.JSONDecodeError):
        return set()
    me = next((p for p in people if p.get('key') == 'me'), None)
    # by name stem: face rec may have tagged a copy load_phone() dropped
    return {(p['t'], DUP_SUFFIX.sub('', p['i'])) for p in (me or {}).get('photos', []) if p.get('g') == 2}


# ---------------------------------------------------------------- clocks

UK = 'Europe/London'
REGIMES = {
    # camera "local wall time" for a shot, per regime
    'utc':   lambda s: s['ts'] + offset_s(s['zone'], s['ts']),
    'local': lambda s: s['ts'],
    'uk':    lambda s: s['ts'] - offset_s(UK, s['ts']) + offset_s(s['zone'], s['ts']),
}


def nearest(sorted_ts, t):
    """Index of the value in sorted_ts nearest t (sorted_ts non-empty)."""
    i = bisect.bisect_left(sorted_ts, t)
    if i == 0:
        return 0
    if i == len(sorted_ts):
        return i - 1
    return i if sorted_ts[i] - t < t - sorted_ts[i - 1] else i - 1


def gps_median(locals_, shots, phone_fixes):
    """Median km between phone GPS and the positioned camera shot nearest in
    (calibrated) local time, within 15 min; None with fewer than 8 pairs."""
    pos = sorted(((t, s) for t, s in zip(locals_, shots) if s['lat'] is not None), key=lambda x: x[0])
    if not pos:
        return None
    pts = [t for t, _ in pos]
    d = []
    for t, la, lo in phone_fixes:
        j = nearest(pts, t)
        if abs(pts[j] - t) <= 900:
            d.append(km(la, lo, pos[j][1]['lat'], pos[j][1]['lon']))
    if len(d) < 8:
        return None
    d.sort()
    return d[len(d) // 2]


def calibrate(shots, phone_local, phone_fixes):
    """→ (regime, camera local time per shot, how).

    The three clock conventions are scored by phone GPS when there is enough of it
    (median distance to the camera track), else by how many phone shots land on a
    camera moment. A free constant offset ('fit:<s>') measured from phone GPS
    catches clocks that fit no convention (a camera left on China time), but only
    wins when it is clearly closer: around one city every offset looks close."""
    cands = {name: [fn(s) for s in shots] for name, fn in REGIMES.items()}
    dist = {name: gps_median(loc, shots, phone_fixes) for name, loc in cands.items()} \
        if len(phone_fixes) >= 8 else {}
    if dist and any(v is not None for v in dist.values()):
        best_fit = None
        for q in range(-14 * 4, 14 * 4 + 1):           # quarter hours
            loc = [s['ts'] + q * 900 for s in shots]
            med = gps_median(loc, shots, phone_fixes)
            if med is not None and (best_fit is None or med < best_fit[0]):
                best_fit = (med, q * 900, loc)
        name = min((k for k in dist if dist[k] is not None), key=lambda k: (dist[k], k != 'utc'))
        how = 'gps ' + ' '.join(f'{k}={v:.2f}km' for k, v in dist.items() if v is not None)
        if best_fit and best_fit[0] < 1.0 and dist[name] > 1.0 and best_fit[0] < 0.5 * dist[name]:
            return f'fit:{best_fit[1]}', best_fit[2], how + f' fit {best_fit[1] / 3600:+.2f}h={best_fit[0]:.2f}km'
        if dist[name] <= 5.0:
            return name, cands[name], how
    votes = {}
    for name, loc in cands.items():
        loc = sorted(loc)
        votes[name] = sum(1 for t in phone_local if abs(loc[nearest(loc, t)] - t) <= OVERLAP_S)
    # ties and silence go to 'utc', the geosync pipeline's normal output
    name = max(votes, key=lambda k: (votes[k], k == 'utc'))
    return name, cands[name], 'overlap ' + ' '.join(f'{k}={v}' for k, v in votes.items())


# ---------------------------------------------------------------- main

def build(report=False):
    camera = load_camera()
    phone = load_phone()
    pages = load_pages()
    me = load_me()
    prov_index = ProvinceIndex(ROOT / 'config' / 'geo' / 'china_provinces.geojson')
    # a camera shot's hand-set province (border bridges) carries over to the phone
    # shots matched to it, for the province and the city alike
    try:
        prov_fix = json.loads(PROVINCE_OVERRIDES.read_text())
    except (OSError, json.JSONDecodeError):
        prov_fix = {}
    prov_code = {}
    for f in json.loads((ROOT / 'config' / 'geo' / 'china_provinces.geojson').read_text()).get('features', []):
        prov_code[PROVINCE_ZH_EN.get(f['properties'].get('name'))] = f['properties'].get('adcode')
    place_index = PlaceIndex()
    place_pages = load_place_pages()

    phone_by_local = sorted(phone, key=lambda p: p['local'])
    plocal = [p['local'] for p in phone_by_local]

    # per trip: calibrated camera local times
    timelines = []   # (trip, shots, locals) with locals aligned to shots
    for trip, shots in camera.items():
        lo = bisect.bisect_left(plocal, shots[0]['ts'] - 16 * 3600)
        hi = bisect.bisect_right(plocal, shots[-1]['ts'] + 16 * 3600)
        window = phone_by_local[lo:hi]
        fixes = [(p['local'], p['lat'], p['lon']) for p in window if p['lat'] is not None]
        regime, locs, how = calibrate(shots, [p['local'] for p in window], fixes)
        order = sorted(range(len(shots)), key=lambda k: locs[k])
        shots = [shots[k] for k in order]
        locs = [locs[k] for k in order]
        timelines.append((trip, shots, locs))
        if report:
            print(f'  {trip:58s} {regime:10s} {how}')

    entries, page_photos = [], defaultdict(list)
    stats = Counter()
    for p in phone:
        # nearest camera shot over all trips, each in its own clock
        best = None
        for trip, shots, locs in timelines:
            if not locs[0] - TRIP_WINDOW <= p['local'] <= locs[-1] + TRIP_WINDOW:
                continue
            j = nearest(locs, p['local'])
            gap = abs(locs[j] - p['local'])
            if best is None or gap < best[0]:
                best = (gap, trip, shots, locs, j)

        keys, folder, ts, fixed = set(), '', None, None
        lat, lon = p['lat'], p['lon']
        located = 'gps' if lat is not None else None
        if best:
            gap, trip, shots, locs, j = best
            shot = shots[j]
            span = locs[-1] - locs[0]
            in_trip = gap <= TRIP_WINDOW or (span <= TRIP_SPAN_MAX and locs[0] <= p['local'] <= locs[-1])
            # phone GPS far from the trip's nearest shot: not on this trip after all
            if in_trip and lat is not None and shot['lat'] is not None and gap <= TRIP_WINDOW \
                    and km(lat, lon, shot['lat'], shot['lon']) > 300:
                in_trip = False
            if in_trip:
                keys.add(f'trip/{trip}')
                folder = shot['building']
                ts = shot['ts'] + (p['local'] - locs[j])   # in the camera manifest's clock
                near = gap <= MATCH_WINDOW and (
                    lat is None or shot['lat'] is None or km(lat, lon, shot['lat'], shot['lon']) <= MATCH_KM)
                if near:
                    keys |= {k for k in pages.get((trip, shot['id']), ()) if '/provinces/' not in k}
                    fixed = (prov_fix.get(trip) or {}).get(shot['id'])
                if lat is None:
                    lat, lon, located = locate(shots, locs, p['local'])
        prov = fixed or (prov_index.lookup(lat, lon) if lat is not None else None)
        if prov:
            keys.add(f'china/provinces/{slugify(prov)}')
        place = place_index.lookup(lat, lon, prov_code.get(fixed)) if lat is not None else None
        if place:
            keys |= {place_pages[k] for k in (place['city_key'], place['spot_key']) if k in place_pages}
        stats['located_' + (located or 'none')] += 1
        if not keys:
            stats['unplaced'] += 1
            continue
        if ts is None:   # off every trip (a GPS-only province match): wall time → UTC
            ts = p['local'] - offset_s(COUNTRY_TZ['CN'] if prov else HOME_TZ, p['local'])
        e = {'t': p['t'], 'i': p['i'], 'ar': p['ar'], 'ts': iso(ts)}
        if folder:
            e['f'] = folder
        if (p['t'], DUP_SUFFIX.sub('', p['i'])) in me:
            e['me'] = 1
            stats['me'] += 1
        idx = len(entries)
        entries.append(e)
        for k in keys:
            page_photos[k].append(idx)
        stats['placed'] += 1

    for k in page_photos:
        page_photos[k].sort(key=lambda i: entries[i]['ts'])
    reels = reel_pages(camera, pages, place_index, place_pages)
    doc = {'schema': 'phone-places.v1', 'generated': iso(datetime.now().timestamp()),
           'photos': entries, 'pages': dict(sorted(page_photos.items())), 'reels': reels,
           'me_camera': load_me_camera()}
    OUT.write_text(json.dumps(doc, separators=(',', ':')))
    print(f'phone_places: {len(phone)} phone photos → {stats["placed"]} on {len(page_photos)} pages '
          f'({stats["me"]} of me); {stats["unplaced"]} matched nothing')
    print(f'  location: {stats["located_gps"]} gps, {stats["located_camera"]} from the camera shot, '
          f'{stats["located_interp"]} interpolated, {stats["located_none"]} unknown')
    print(f'  reels: {len(reels)} placed on {len({k for ks in reels.values() for k in ks})} more pages')
    print(f'  wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)')
    return doc


def reel_pages(camera, pages, place_index, place_pages):
    """{reel id: [page keys]} from each reel's drone GPS points."""
    try:
        reels = json.loads(REELS.read_text()).get('reels', [])
    except (OSError, json.JSONDecodeError):
        return {}
    # positioned camera photos on a ~1 km grid, with the (non-province, non-road)
    # pages each one is on; provinces come from the reel pipeline, a road trip
    # page spans whole provinces
    cell = 0.01
    grid = defaultdict(list)
    for trip, shots in camera.items():
        for sh in shots:
            keys = [k for k in pages.get((trip, sh['id']), ())
                    if '/provinces/' not in k and '/roads/' not in k]
            if keys and sh['lat'] is not None:
                grid[(int(sh['lat'] // cell), int(sh['lon'] // cell))].append((sh['lat'], sh['lon'], keys))
    reach = int(REEL_KM / 111 / cell) + 1
    out = {}
    for r in reels:
        keys = set()
        for lat, lon in ((r.get('gps') or {}).get('points') or []):
            place = place_index.lookup(lat, lon)
            if place:
                keys |= {place_pages[k] for k in (place['city_key'], place['spot_key']) if k in place_pages}
            gy, gx = int(lat // cell), int(lon // cell)
            for dy in range(-reach, reach + 1):
                for dx in range(-reach, reach + 1):
                    for plat, plon, pkeys in grid.get((gy + dy, gx + dx), ()):
                        if km(lat, lon, plat, plon) <= REEL_KM:
                            keys.update(pkeys)
        if keys:
            out[r['id']] = sorted(keys)
    return out


def locate(shots, locs, t):
    """Position for a phone shot with no GPS, from the camera timeline."""
    j = nearest(locs, t)
    if abs(locs[j] - t) <= MATCH_WINDOW and shots[j]['lat'] is not None:
        return shots[j]['lat'], shots[j]['lon'], 'camera'
    # positioned shots either side
    i = bisect.bisect_right(locs, t)
    before = next((k for k in range(i - 1, -1, -1) if shots[k]['lat'] is not None), None)
    after = next((k for k in range(i, len(shots)) if shots[k]['lat'] is not None), None)
    if before is None or after is None:
        return None, None, None
    a, b = shots[before], shots[after]
    if locs[after] - locs[before] > 2 * INTERP_GAP or t - locs[before] > INTERP_GAP \
            or locs[after] - t > INTERP_GAP or km(a['lat'], a['lon'], b['lat'], b['lon']) > INTERP_KM:
        return None, None, None
    f = (t - locs[before]) / max(1, locs[after] - locs[before])
    return a['lat'] + f * (b['lat'] - a['lat']), a['lon'] + f * (b['lon'] - a['lon']), 'interp'


if __name__ == '__main__':
    build(report='--report' in sys.argv)
