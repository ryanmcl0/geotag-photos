#!/usr/bin/env python3
"""
Build the "collections" pages (tiles → galleries) from already-processed trips.

Parallels process_all.py: process_all.py places photos on the map; build_collections.py
groups those same (already-compressed, already-hosted) photos into themed galleries —
starting with the China hub. It NEVER touches images; it emits a JSON of photo
references ({trip, id}) that the front-end resolves to the existing R2 / local webp.

The DEFAULT run is derived facets only (roads, bridges geofence, provinces, roofs) —
fast, deterministic, no AI. The CLIP "By Category" pass runs ONLY with --category,
and is cached by file hash: already-classified photos are never re-classified, so
re-running to refresh bridges/provinces never re-pays the CLIP cost. Without
--category, the previous category tile is carried forward unchanged.

Usage:
  ./build_collections.py                 # derived facets only (default; no AI)
  ./build_collections.py --category      # + CLIP "By Category" (cached, local, free)
  ./build_collections.py --force         # ignore the CLIP cache (with --category)
  ./build_collections.py --collection china

Output: web/collections/<id>.json
Config: config/classifications.json  +  the per-facet roster files it points at.
"""

import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import click

import photo_privacy

ROOT = Path(__file__).parent.resolve()
WEB_TRIPS = ROOT / 'web' / 'trips'
OUT_DIR = ROOT / 'web' / 'collections'
CLASSIFY_CONFIG = ROOT / 'config' / 'classifications.json'
CLIP_CACHE = ROOT / 'config' / '.classify_cache.json'
BRIDGE_VISITS = ROOT / 'config' / 'bridge_visits.json'

# One user-maintained file controls every tile cover on the site (hub facets,
# heroes, per-province/bridge/roof/road-leg subtiles, landing-page tiles).
# Values are local edit filepaths (or bare stems), resolved by filename.
try:
    TILE_COVERS = json.loads((ROOT / 'config' / 'tile_covers.json').read_text())
except (OSError, json.JSONDecodeError):
    TILE_COVERS = {}


def cover_spec(section: str, key: str, fallback=None):
    v = (TILE_COVERS.get(section) or {}).get(key)
    return v if v else fallback


_EDITS_MAP = None


def trips_for_spec(spec):
    """Filepath → candidate trip slugs via each trip's source edits path (longest
    match wins; a split trip and its -private half share one path) — disambiguates
    stems that exist in multiple trips (e.g. RM104106)."""
    global _EDITS_MAP
    if _EDITS_MAP is None:
        _EDITS_MAP = {}
        for mf in sorted(WEB_TRIPS.glob('*/manifest.json')):
            man = photo_privacy.load_full_manifest(mf.parent)
            src = ((man or {}).get('source') or {}).get('photos_path')
            if src:
                _EDITS_MAP.setdefault(src.rstrip('/'), []).append(mf.parent.name)
    best = None
    for path, slugs in _EDITS_MAP.items():
        if (spec == path or spec.startswith(path + '/')) and (best is None or len(path) > len(best[0])):
            best = (path, slugs)
    return sorted(best[1], key=len) if best else []   # main trip before its -private half

# DataV admin-1 Chinese names → the English names used in the rosters.
PROVINCE_ZH_EN = {
    '北京市': 'Beijing', '天津市': 'Tianjin', '河北省': 'Hebei', '山西省': 'Shanxi',
    '内蒙古自治区': 'Inner Mongolia', '辽宁省': 'Liaoning', '吉林省': 'Jilin',
    '黑龙江省': 'Heilongjiang', '上海市': 'Shanghai', '江苏省': 'Jiangsu', '浙江省': 'Zhejiang',
    '安徽省': 'Anhui', '福建省': 'Fujian', '江西省': 'Jiangxi', '山东省': 'Shandong',
    '河南省': 'Henan', '湖北省': 'Hubei', '湖南省': 'Hunan', '广东省': 'Guangdong',
    '广西壮族自治区': 'Guangxi', '海南省': 'Hainan', '重庆市': 'Chongqing', '四川省': 'Sichuan',
    '贵州省': 'Guizhou', '云南省': 'Yunnan', '西藏自治区': 'Tibet', '陕西省': 'Shaanxi',
    '甘肃省': 'Gansu', '青海省': 'Qinghai', '宁夏回族自治区': 'Ningxia',
    '新疆维吾尔自治区': 'Xinjiang', '台湾省': 'Taiwan',
    '香港特别行政区': 'Hong Kong', '澳门特别行政区': 'Macau',
}


def slugify(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', name.lower())
    return s.strip('-')


# ---------------------------------------------------------------- geo (point-in-polygon)

def _ring_contains(ring, x, y) -> bool:
    """Ray-casting: is (x=lon, y=lat) inside this ring?"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


class ProvinceIndex:
    """Point → province (English name) via the DataV admin-1 boundaries."""

    def __init__(self, geojson_path: Path):
        data = json.loads(geojson_path.read_text())
        self.provinces = []  # (name_en, [polygon...]) where polygon = (ext, [holes], bbox)
        for feat in data.get('features', []):
            name_zh = (feat.get('properties') or {}).get('name')
            name_en = PROVINCE_ZH_EN.get(name_zh)
            geom = feat.get('geometry') or {}
            if not name_en or not geom:
                continue
            polys = []
            gtype = geom.get('type')
            coords = geom.get('coordinates') or []
            raw_polys = coords if gtype == 'MultiPolygon' else [coords] if gtype == 'Polygon' else []
            for poly in raw_polys:
                if not poly:
                    continue
                ext = poly[0]
                holes = poly[1:]
                polys.append((ext, holes, _bbox(ext)))
            if polys:
                self.provinces.append((name_en, polys))

    def lookup(self, lat: float, lon: float):
        for name_en, polys in self.provinces:
            for ext, holes, (minx, miny, maxx, maxy) in polys:
                if lon < minx or lon > maxx or lat < miny or lat > maxy:
                    continue
                if _ring_contains(ext, lon, lat) and not any(_ring_contains(h, lon, lat) for h in holes):
                    return name_en
        return None


# ---------------------------------------------------------------- data loading

def load_trip_meta() -> dict:
    """slug → {name, year, public} from web/trips/index.json."""
    idx = json.loads((WEB_TRIPS / 'index.json').read_text())
    meta = {}
    for t in idx.get('trips', []):
        year = t.get('year')
        if not year and t.get('dates', {}).get('start'):
            year = int(t['dates']['start'][:4])
        meta[t['id']] = {'name': t.get('name'), 'year': year, 'public': t.get('public', False)}
    return meta


def load_member_photos(prov_index, exclude: set, echo) -> list:
    """Membership: with a province index, every geotagged photo inside a (non-excluded)
    province; with prov_index=None ('membership: all'), every geotagged photo, period."""
    trip_meta = load_trip_meta()
    records = []
    scanned = 0
    for manifest_file in sorted(WEB_TRIPS.glob('*/manifest.json')):
        slug = manifest_file.parent.name
        # Full manifest: after a privacy split, manifest.json is the filtered
        # public view; the complete photo set lives in manifest.all.json.
        manifest = photo_privacy.load_full_manifest(manifest_file.parent)
        if not manifest:
            continue
        meta = trip_meta.get(slug, {})
        for ph in manifest.get('photos', []):
            lat, lon = ph.get('lat'), ph.get('lon')
            if lat is None or lon is None:
                continue
            scanned += 1
            if prov_index is not None:
                province = prov_index.lookup(lat, lon)
                if not province or province in exclude:
                    continue
            else:
                province = None
            # Year from the PHOTO's timestamp, not the trip — trips straddle new year
            # (CNY trips) and edits folders can contain other-year strays, so trip-year
            # mislabels the province year filter.
            year = None
            if ph.get('timestamp'):
                try:
                    year = int(ph['timestamp'][:4])
                except ValueError:
                    pass
            if not year:
                year = meta.get('year')
            records.append({
                'trip': slug, 'id': ph['id'], 'lat': lat, 'lon': lon,
                'building': (ph.get('building') or '').strip(),
                'province': province, 'year': year,
                'ts': ph.get('timestamp') or '',
                'public': meta.get('public', False),
            })
    echo(f"  Scanned {scanned} geotagged photos → {len(records)} inside China "
         f"({len({r['province'] for r in records})} provinces, "
         f"{len({r['trip'] for r in records})} trips)")
    return records


# ---------------------------------------------------------------- helpers

# display-webp dimensions, cached across runs (header-only reads, but ~9k photos)
DIMS_CACHE = ROOT / 'config' / '.dims_cache.json'
try:
    _DIMS = json.loads(DIMS_CACHE.read_text())
except (OSError, json.JSONDecodeError):
    _DIMS = {}


def photo_dims(rec):
    key = f"{rec['trip']}/{rec['id']}"
    v = _DIMS.get(key)
    if v is None:
        try:
            from PIL import Image
            with Image.open(WEB_TRIPS / rec['trip'] / 'display' / f"{rec['id']}.webp") as im:
                v = [im.width, im.height]
        except Exception:
            v = [3, 2]
        _DIMS[key] = v
    return v


def save_dims_cache():
    DIMS_CACHE.write_text(json.dumps(_DIMS))


def _photo_ref(rec, with_year=False):
    w, h = photo_dims(rec)
    # 'ar' lets the front-end lay out justified rows (and size lightbox slides)
    # without probing images client-side.
    ref = {'trip': rec['trip'], 'id': rec['id'], 'ar': round(w / h, 3)}
    if with_year and rec.get('year'):
        ref['year'] = rec['year']
    return ref


def build_id_index(records):
    """id (exact + suffix-normalised, plus trip-qualified) → ref, for resolving
    cover filepaths. Trip-qualified keys disambiguate cross-trip stem collisions."""
    idx = {}
    for r in records:
        norm = re.split(r'-Enhanced|-NR|-SAI|-2$', r['id'])[0]
        idx.setdefault(r['id'], r)
        idx.setdefault(norm, r)
        idx[f"{r['trip']}/{r['id']}"] = r
        idx.setdefault(f"{r['trip']}/{norm}", r)
    return idx


def resolve_cover(spec, id_index, fallback_records):
    """A raw edit filepath → {trip,id}; 'auto'/None → first of fallback_records.
    The filepath's trip (via its edits dir) is tried first, then the bare stem."""
    if spec and spec != 'auto':
        stem = Path(spec).stem
        norm = re.split(r'-Enhanced|-NR|-SAI', stem)[0]
        keys = []
        for slug in (trips_for_spec(spec) if '/' in spec else []):
            keys += [f'{slug}/{stem}', f'{slug}/{norm}']
        keys += [stem, norm]
        for k in keys:
            rec = id_index.get(k)
            if rec:
                return _photo_ref(rec)
    if fallback_records:
        return _photo_ref(fallback_records[0])
    return None


def fmt_int(n):
    return f"{int(n):,}" if float(n).is_integer() else f"{n:,}"


def by_time(records):
    return sorted(records, key=lambda r: r.get('ts') or '9999')


def _is_landscape(rec):
    """Tiles are landscape — portrait covers crop badly."""
    w, h = photo_dims(rec)
    return w > h


def pick_cover(records):
    """Cover = first LANDSCAPE photo, drone aerials (DJI_*) preferred; else first."""
    if not records:
        return None
    ordered = by_time(records)
    cands = ([r for r in ordered if r['id'].upper().startswith('DJI')] +
             [r for r in ordered if not r['id'].upper().startswith('DJI')])
    for r in cands:
        if _is_landscape(r):
            return _photo_ref(r)
    return _photo_ref(cands[0])


def dist_km(lat1, lon1, lat2, lon2):
    """Equirectangular approximation — fine at gallery-geofence scales."""
    import math
    p = math.pi / 180
    x = (lat2 - lat1) * p
    y = (lon2 - lon1) * p * math.cos((lat1 + lat2) / 2 * p)
    return 6371 * math.hypot(x, y)


# ---------------------------------------------------------------- facet builders

def _blogs_for_trip(trip_slug):
    """Live blogs whose 'trips' include this trip slug → [{slug,title,public}], so a
    road-trip tile can link its write-up(s). Skips pending blogs (no page built)."""
    path = ROOT / 'config' / 'blogs.json'
    if not path.exists():
        return []
    try:
        blogs = json.loads(path.read_text()).get('blogs', [])
    except (OSError, json.JSONDecodeError):
        return []
    return [{'slug': b['slug'], 'title': b['title'], 'public': bool(b.get('public'))}
            for b in blogs
            if b.get('status') != 'pending' and trip_slug in (b.get('trips') or [])]


def facet_roads(facet, records, echo):
    roster = json.loads((ROOT / facet['roster']).read_text())
    by_trip = {}
    for r in records:
        by_trip.setdefault(r['trip'], []).append(r)
    subtiles, total_km = [], 0.0
    for leg in roster.get('trips', []):
        total_km += leg.get('km', 0) or 0
        slug = slugify(leg['trip'])
        photos = by_trip.get(slug, [])
        # One tile per LEG (legs sharing a trip — e.g. Guizhou Huajiang inside the
        # North Xinjiang trip — get their own tile, opening the same trip map).
        sub = {
            'id': slugify(leg['label']), 'title': leg['label'], 'done': True,
            'subtitle': f"{leg.get('car','')} · {leg.get('dates','')} {leg.get('year','')}".strip(' ·'),
            'infographic': f"{fmt_int(leg['km'])} km",
            'count': len(photos),
            'view': facet.get('subtile_view', 'map'), 'trip': slug,
            'cover': pick_cover(photos),
            'photos': [_photo_ref(p) for p in by_time(photos)],
        }
        blogs = _blogs_for_trip(slug)
        if blogs:
            sub['blogs'] = blogs
        subtiles.append(sub)
    info = facet['infographic']['format'].format(value=int(total_km))
    echo(f"  roads: {len(subtiles)} legs, {fmt_int(total_km)} km")
    return {'kind': 'tilegroup', 'infographic': info, 'subtiles': subtiles}, [r for r in records]


def facet_bridges(facet, records, echo):
    """Geofence: a photo belongs to a bridge iff it lies within radius_km of the
    bridge's roster lat/lon. (Token-matching the day-itinerary `building` strings
    swept in whole days of unrelated photos — parking garages and all.)
    config/bridge_visits.json sessions add same-visit photos whose GPS drifted
    outside the fence (canyon multipath scatters mid-visit shots kilometres out)."""
    roster = json.loads((ROOT / facet['roster']).read_text())
    session_ids = {}   # bridge name → [(trip, id), ...] in session order
    if BRIDGE_VISITS.exists():
        try:
            for s in json.loads(BRIDGE_VISITS.read_text()).get('sessions', []):
                session_ids.setdefault(s['bridge'], []).extend((s['trip'], pid) for pid in s['photos'])
        except (OSError, json.JSONDecodeError):
            pass
    rec_by_key = {(r['trip'], r['id']): r for r in records}
    subtiles, done_with_photos = [], 0
    cover_pool = []
    for b in roster.get('bridges', []):
        sub = {'id': slugify(b['name']), 'title': b['name'], 'done': b.get('done', False),
               'rank': b.get('rank'), 'height_m': b.get('height_m'),
               'province': b.get('province'), 'status': b.get('status')}
        if b.get('name_zh'):
            sub['name_zh'] = b['name_zh']
        if b.get('done'):
            lat, lon = b.get('lat'), b.get('lon')
            radius = b.get('radius_km', 3.0)
            if lat is None or lon is None:
                sub['photos'] = []
                sub['pending'] = 'No photos yet'
                echo(f"    ⚠ {b['name']}: done but no coords in roster — set lat/lon to populate")
            else:
                near = [(dist_km(lat, lon, r['lat'], r['lon']), r) for r in records]
                photos = by_time([r for d, r in near if d <= radius])
                have = {(p['trip'], p['id']) for p in photos}
                extra = [rec_by_key[k] for k in session_ids.get(b['name'], [])
                         if k in rec_by_key and k not in have]
                if extra:
                    photos = by_time(photos + extra)
                sub['photos'] = [_photo_ref(p) for p in photos]
                if photos:
                    # cover: roster `cover_id` override (a photo id) wins; otherwise the
                    # nearest LANDSCAPE drone shot to the bridge, else nearest landscape
                    pinned = next((r for r in photos if r['id'] == b.get('cover_id')), None)
                    if pinned:
                        sub['cover'] = _photo_ref(pinned)
                    else:
                        in_fence = [r for d, r in sorted([x for x in near if x[0] <= radius], key=lambda x: x[0])]
                        cands = ([r for r in in_fence if r['id'].upper().startswith('DJI')] +
                                 [r for r in in_fence if not r['id'].upper().startswith('DJI')])
                        sub['cover'] = _photo_ref(next((r for r in cands if _is_landscape(r)), cands[0]))
                    cover_pool.extend(photos)
                    done_with_photos += 1
                else:
                    sub['pending'] = 'No photos yet'
        else:
            sub['pending'] = 'Pending' + (f" · UC {b['uc_year']}" if b.get('uc_year') else '')
        subtiles.append(sub)
    total = len(roster.get('bridges', []))
    # Infographic counts bridges VISITED (roster done) — matches the masthead stat;
    # how many have photos populated yet is an implementation detail.
    visited = sum(1 for b in roster.get('bridges', []) if b.get('done'))
    info = facet['infographic']['format'].format(
        done=visited, total=total,
        ranked_done=sum(1 for b in roster.get('bridges', []) if b.get('done') and b.get('rank')),
        ranked_total=sum(1 for b in roster.get('bridges', []) if b.get('rank')))
    echo(f"  bridges: {visited}/{total} visited, {done_with_photos} with geofenced photos")
    for s in subtiles:
        if s['done'] and s.get('photos'):
            echo(f"    {s['title']}: {len(s['photos'])}")
    return {'kind': 'tilegroup', 'infographic': info, 'subtiles': subtiles}, cover_pool


def facet_provinces(facet, records, echo):
    roster = json.loads((ROOT / facet['roster']).read_text())
    visited = roster.get('visited', [])
    remaining = roster.get('remaining', [])
    total = roster.get('total', len(visited) + len(remaining))
    by_prov = {}
    for r in records:
        by_prov.setdefault(r['province'], []).append(r)
    subtiles, cover_pool = [], []
    for prov in visited:
        photos = by_prov.get(prov, [])
        cover_pool.extend(photos)
        subtiles.append({
            'id': slugify(prov), 'title': prov, 'done': True,
            'count': len(photos),
            'cover': pick_cover(photos),
            'photos': [_photo_ref(p, with_year=True) for p in by_time(photos)],
        })
    for prov in remaining:
        subtiles.append({'id': slugify(prov), 'title': prov, 'done': False, 'pending': 'Not yet visited'})
    # Data hygiene: provinces with photos that aren't in the visited roster.
    stray = sorted(set(by_prov) - set(visited))
    if stray:
        echo(f"  ⚠ provinces with photos but not in roster 'visited': {', '.join(stray)}")
    info = facet['infographic']['format'].format(visited=len(visited), total=total)
    # year filter options come from VISITED provinces' photos only (strays in
    # non-visited provinces — e.g. HK — shouldn't add phantom years to the bar)
    years = sorted({p['year'] for prov in visited for p in by_prov.get(prov, []) if p.get('year')},
                   reverse=True)
    echo(f"  provinces: {len(visited)}/{total} visited, years {years}")
    return {'kind': 'tilegroup', 'infographic': info, 'filters': facet.get('filters', []),
            'years': years, 'subtiles': subtiles}, cover_pool


def facet_roofs(facet, records, echo):
    """One tile per climbed building, grouped into height tiers. Matching is
    ROSTER-ONLY (no generic keyword fallback — that swept in random locations):
    each photo's `building` field is matched against every roster entry's `match`
    tokens, and the LONGEST matching token wins (so 'Greenland Center' can't steal
    'Wuhan Greenland Center' photos). Buildings without processed photos still
    count toward the headline stat but don't get a tile."""
    roster_path = facet.get('roster')
    roster = json.loads((ROOT / roster_path).read_text()) if roster_path else {}
    entries = roster.get('buildings', [])
    tiers = roster.get('tiers', [600, 500, 400, 300, 200])

    tokens = []   # (token_lower, entry_index)
    for i, b in enumerate(entries):
        for t in b.get('match', [b['name']]):
            tokens.append((t.lower(), i))

    photos_by_building = {}
    for r in records:
        bl = r['building'].lower()
        if not bl:
            continue
        best = None
        for t, i in tokens:
            if t in bl and (best is None or len(t) > len(best[0])):
                best = (t, i)
        if best:
            photos_by_building.setdefault(best[1], []).append(r)

    def tier_of(h):
        if h is None:
            return None
        for t in tiers:
            if h >= t:
                return t
        return 0

    def tier_title(t):
        if t is None:
            return 'Height TBC'
        if t == tiers[0]:
            return f'{t} m +'
        if t == 0:
            return f'Under {tiers[-1]} m'
        return f'{t}–{tiers[tiers.index(t) - 1]} m'

    by_tier = {}
    for i, b in enumerate(entries):
        by_tier.setdefault(tier_of(b.get('height_m')), []).append(i)

    sections, all_photos, with_photos = [], [], 0
    order = [t for t in tiers if t in by_tier] + ([0] if 0 in by_tier else []) + ([None] if None in by_tier else [])
    for t in order:
        subtiles = []
        idxs = sorted(by_tier[t], key=lambda i: -(entries[i].get('height_m') or 0))
        for i in idxs:
            b = entries[i]
            photos = by_time(photos_by_building.get(i, []))
            if not photos:
                continue   # counted in the stat, but no tile until its edits are processed
            all_photos.extend(photos)
            with_photos += 1
            bits = [f"{b['height_m']} m" if b.get('height_m') else None, b.get('city')]
            subtiles.append({
                'id': slugify(b['name']), 'title': b['name'], 'done': True,
                'height_m': b.get('height_m'), 'city': b.get('city'),
                'subtitle': ' · '.join(x for x in bits if x),
                'count': len(photos),
                'cover': pick_cover(photos),
                'years': sorted({p['year'] for p in photos if p.get('year')}, reverse=True),
                'photos': [_photo_ref(p) for p in photos],
            })
        if subtiles:
            sections.append({'id': f'tier-{t if t is not None else "tbc"}',
                             'title': tier_title(t), 'subtiles': subtiles})

    info = facet['infographic']['format'].format(value=len(entries))
    years = sorted({y for sec in sections for s in sec['subtiles'] for y in s['years']}, reverse=True)
    echo(f"  roofs: {len(entries)} buildings in roster, {with_photos} with photos "
         f"({len(all_photos)} photos, {len(sections)} height tiers)")
    return {'kind': 'tiered_tilegroup', 'infographic': info, 'years': years,
            'sections': sections}, all_photos


# ---------------------------------------------------------------- CLIP "By Category"

def facet_category(facet, records, force, echo):
    try:
        import torch
        from PIL import Image
        from transformers import CLIPModel, CLIPProcessor
    except ImportError:
        echo("  ⚠ category: torch/transformers not installed — skipping (run: pip install torch transformers)")
        return None, []

    cats = facet['categories']
    cache = {}
    if CLIP_CACHE.exists() and not force:
        try:
            cache = json.loads(CLIP_CACHE.read_text())
        except json.JSONDecodeError:
            cache = {}

    echo("  category: loading CLIP (openai/clip-vit-base-patch32)…")
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').to(device)
    proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')

    # transformers ≥5 returns a BaseModelOutputWithPooling whose pooler_output is the
    # already-projected joint-space embedding; older versions return the tensor directly.
    def emb(out):
        return out if torch.is_tensor(out) else out.pooler_output

    # Mean text embedding per category.
    cat_emb = {}
    for c in cats:
        inp = proc(text=c['clip_prompts'], return_tensors='pt', padding=True, truncation=True).to(device)
        with torch.no_grad():
            tf = emb(model.get_text_features(**inp))
            tf = tf / tf.norm(dim=-1, keepdim=True)
            cat_emb[c['id']] = tf.mean(dim=0)

    buckets = {c['id']: [] for c in cats}
    n_new = 0
    for i, r in enumerate(records):
        disp = WEB_TRIPS / r['trip'] / 'display' / f"{r['id']}.webp"
        if not disp.exists():
            continue
        key = str(disp)
        try:
            h = hashlib.md5(disp.read_bytes()).hexdigest()
        except OSError:
            continue
        cached = cache.get(key)
        if cached and cached.get('hash') == h and not force:
            cat_id = cached['category']
        else:
            try:
                img = Image.open(disp).convert('RGB')
            except Exception:
                continue
            inp = proc(images=img, return_tensors='pt').to(device)
            with torch.no_grad():
                imf = emb(model.get_image_features(**inp))
                imf = imf / imf.norm(dim=-1, keepdim=True)
            cat_id = max(cat_emb, key=lambda cid: (imf @ cat_emb[cid].unsqueeze(1)).item())
            cache[key] = {'hash': h, 'category': cat_id, 'backend': 'clip'}
            n_new += 1
            if n_new % 200 == 0:
                echo(f"    …classified {n_new} new images")
        buckets[cat_id].append(r)

    CLIP_CACHE.write_text(json.dumps(cache, indent=2))
    subtiles, cover_pool = [], []
    for c in cats:
        photos = buckets[c['id']]
        cover_pool.extend(photos)
        subtiles.append({
            'id': c['id'], 'title': c['title'], 'done': True,
            'cover': pick_cover(photos),
            'photos': [_photo_ref(p) for p in by_time(photos)],
        })
    echo(f"  category: {n_new} newly classified | " +
         ', '.join(f"{c['title'].split(':')[-1].strip()}={len(buckets[c['id']])}" for c in cats))
    return {'kind': 'tilegroup', 'subtiles': subtiles}, cover_pool


# ---------------------------------------------------------------- orchestration

FACET_BUILDERS = {
    'road_trips': facet_roads, 'bridges': facet_bridges,
    'province': facet_provinces, 'rooftopping': facet_roofs,
}


def build_stats(coll):
    """Headline numbers for the collection masthead — all from the roster files."""
    def roster_for(rule):
        f = next((f for f in coll['facets'] if f.get('rule') == rule), None)
        return json.loads((ROOT / f['roster']).read_text()) if f and f.get('roster') else {}

    stats = {}
    pr = roster_for('province')
    if pr:
        stats['provinces'] = {'visited': len(pr.get('visited', [])), 'total': pr.get('total', 0)}
    rt = roster_for('road_trips')
    if rt:
        stats['km'] = round(sum(l.get('km', 0) or 0 for l in rt.get('trips', [])))
    br = roster_for('bridges').get('bridges', [])
    if br:
        stats['bridges'] = {'visited': sum(1 for b in br if b.get('done')),
                            'ranked_done': sum(1 for b in br if b.get('done') and b.get('rank')),
                            'ranked_total': sum(1 for b in br if b.get('rank'))}
    rf = roster_for('rooftopping')
    if rf:
        blds = rf.get('buildings', [])
        stats['buildings'] = len(blds)
        # cities/countries breakdowns only for pure-rooftopping collections (the China
        # masthead has its own places stat from the regions roster)
        if not any(f.get('rule') == 'province' for f in coll['facets']):
            cities = {b.get('city') for b in blds if b.get('city')}
            countries = {b.get('country') for b in blds if b.get('country')}
            if cities:
                stats['cities'] = len(cities)
            if countries:
                stats['countries'] = len(countries)
    regions_path = coll.get('regions')
    if regions_path and (ROOT / regions_path).exists():
        rg = json.loads((ROOT / regions_path).read_text())
        stats['places'] = sum(len(set(v)) for v in rg.get('provinces', {}).values())
    return stats


@click.command()
@click.option('--collection', default=None, help='Build only this collection id (default: all in config)')
@click.option('--category', 'do_category', is_flag=True, help='Also run the CLIP "By Category" pass (slow, free)')
@click.option('--force', is_flag=True, help='Ignore the CLIP cache')
def main(collection, do_category, force):
    cfg = json.loads(CLASSIFY_CONFIG.read_text())
    colls = [c for c in cfg['collections'] if collection in (None, c['id'])]
    if not colls:
        click.echo(f"No collection '{collection}' in {CLASSIFY_CONFIG}", err=True)
        sys.exit(1)
    # Per-photo privacy first: splits public-trip manifests (rooftop / bridge-fence
    # photos) and refreshes the image-proxy index; the map feeds the public variants.
    click.echo("Syncing photo privacy…")
    private_map = photo_privacy.sync(echo=click.echo)
    for coll in colls:
        build_collection(coll, do_category, force, private_map)
    emit_site_stats(click.echo)
    save_dims_cache()


def _all_subtiles(tile):
    return (tile.get('subtiles') or []) + [s for sec in (tile.get('sections') or [])
                                           for s in (sec.get('subtiles') or [])]


def _locked_subtile(s, full_s, cover=None):
    """A gated sub-tile on the public page: a locked placeholder behind the See All
    password. It may carry a cover (set in config/tile_covers.json) — that photo is
    served as a cover only (photo_privacy.cover_serve_map keeps it off the map).
    Carries the FULL photo count so the tile still reads 'N photos · locked'."""
    out = {'id': s['id'], 'title': s['title'], 'locked': True}
    if full_s and full_s.get('count') is not None:
        out['count'] = full_s['count']
    if cover:
        out['cover'] = cover
    return out


def _mark_not_public(pub_tile, full_tile, id_index):
    """Reconcile a derived facet's public variant against the full build:
    - stamp every sub-tile with the FULL (public+private) photo count, so counts
      shown on tiles reflect the real total regardless of privacy;
    - sub-tiles that have photos in full but none in public contain ONLY private
      photos → render as a locked (password-gated) tile. The locked tile's cover
      comes from config/tile_covers.json (resolved against the FULL index, since the
      photo is private) so covers stay maintainable in one place."""
    full_by_id = {s['id']: s for s in _all_subtiles(full_tile)}
    covers = TILE_COVERS.get(pub_tile.get('id')) or {}
    def walk(subtiles):
        for i, s in enumerate(subtiles):
            f = full_by_id.get(s['id'])
            if f and f.get('count') is not None:
                s['count'] = f['count']
            if s.get('done') and not s.get('photos') and f and f.get('photos'):
                spec = covers.get(s.get('title'))
                cover = resolve_cover(spec, id_index, []) if spec else None
                subtiles[i] = _locked_subtile(s, f, cover)
    walk(pub_tile.get('subtiles') or [])
    for sec in pub_tile.get('sections') or []:
        walk(sec.get('subtiles') or [])


def _filter_tile_refs(tile, pub_ref_set):
    """Public copy of a carried (AI) tile: drop refs to private photos."""
    t = copy.deepcopy(tile)
    for i, s in enumerate(t.get('subtiles') or []):
        before = len(s.get('photos') or [])
        s['photos'] = [p for p in (s.get('photos') or []) if (p['trip'], p['id']) in pub_ref_set]
        if before and not s['photos']:
            t['subtiles'][i] = _locked_subtile(s, s)
        elif s.get('cover') and (s['cover']['trip'], s['cover']['id']) not in pub_ref_set:
            s['cover'] = next((p for p in s['photos'] if p.get('ar', 1) > 1), s['photos'][0]) if s['photos'] else None
    return t


def apply_subtile_covers(tile, id_index):
    """User-pinned per-subtile covers from config/tile_covers.json, keyed by the
    facet id section (provinces/bridges/roofs/roads) and the subtile title."""
    mapping = TILE_COVERS.get(tile['id']) or {}
    if not mapping:
        return
    for s in _all_subtiles(tile):
        spec = mapping.get(s.get('title'))
        if spec:
            ref = resolve_cover(spec, id_index, [])
            if ref:
                s['cover'] = ref


def _locked_stub(facet, full_tile, pub_records, spec):
    """Hub tile for a gated facet on the public page: cover + stat, no photo lists."""
    stub = {'id': full_tile['id'], 'title': full_tile['title'], 'kind': 'locked', 'locked': True}
    if full_tile.get('infographic'):
        stub['infographic'] = full_tile['infographic']
    if spec and spec != 'auto':
        # Hand-picked cover — kept as-is (whitelist it via photo_privacy force_public
        # if it's a protected photo, or the image proxy will block it for visitors).
        stub['cover'] = full_tile.get('cover')
    else:
        # Auto cover must be loadable by the public: pick from the facet's public pool.
        quiet = lambda *a, **k: None
        try:
            _, pool = FACET_BUILDERS[facet['rule']](facet, pub_records, quiet)
        except Exception:
            pool = []
        stub['cover'] = pick_cover(pool) or full_tile.get('cover')
    return stub


def build_collection(coll, do_category, force, private_map):
    click.echo(f"Building collection: {coll['title']}")
    prov_index = ProvinceIndex(ROOT / coll['province_geojson']) if coll.get('province_geojson') else None
    records = load_member_photos(prov_index, set(coll.get('exclude_provinces', [])), click.echo)
    for r in records:
        r['private'] = (not r['public']) or (r['id'] in private_map.get(r['trip'], frozenset()))
    id_index = build_id_index(records)

    # Gated collections (e.g. Rooftopping) ship one full file behind the See All
    # gate; public collections ship <id>.json (filtered) + <id>.all.json (gated).
    gated = coll.get('access') == 'gated'
    full_path = OUT_DIR / (f"{coll['id']}.json" if gated else f"{coll['id']}.all.json")

    # Previous output — used to carry the (expensive) AI category tile through
    # derived-only runs, so a plain `build_collections.py` never drops it.
    prev_tiles = {}
    for prev_path in (full_path, OUT_DIR / f"{coll['id']}.json"):
        if prev_path.exists():
            try:
                prev_tiles = {t['id']: t for t in json.loads(prev_path.read_text()).get('tiles', [])
                              if not t.get('locked')}
                break
            except (OSError, json.JSONDecodeError):
                pass

    tiles = []
    for facet in coll['facets']:
        if facet.get('enabled') is False:
            click.echo(f"  {facet['id']}: disabled in config — skipped")
            continue
        rule = facet.get('rule')
        if facet['type'] == 'ai':
            if not do_category:
                if facet['id'] in prev_tiles:
                    click.echo(f"  category: kept previous result (pass --category to re-run CLIP)")
                    kept = prev_tiles[facet['id']]
                    for s in kept.get('subtiles', []):  # normalize older outputs
                        if s.get('photos'):
                            s['done'] = True
                    tiles.append(kept)
                else:
                    click.echo(f"  category: skipped (pass --category to run CLIP)")
                continue
            cat_records = [r for r in records if r['public']] if facet.get('public_only') else records
            if facet.get('public_only'):
                click.echo(f"  category: public-only ({len(cat_records)}/{len(records)} photos)")
            result, pool = facet_category(facet, cat_records, force, click.echo)
        else:
            result, pool = FACET_BUILDERS[rule](facet, records, click.echo)
        if result is None:
            continue
        result['id'] = facet['id']
        result['title'] = facet['title']
        result['cover'] = resolve_cover(cover_spec(coll['id'], facet['id'], facet.get('cover')),
                                        id_index, pool)
        apply_subtile_covers(result, id_index)
        tiles.append(result)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = {
        'id': coll['id'], 'title': coll['title'], 'subtitle': coll.get('subtitle', ''),
        'stats': build_stats(coll),
        'generated': datetime.now().isoformat(timespec='seconds'),
    }
    hero_spec = cover_spec(coll['id'], 'hero', coll.get('hero_cover'))
    full_path.write_text(json.dumps(
        {**base, 'hero_cover': resolve_cover(hero_spec, id_index, records),
         'tiles': tiles}, indent=2, ensure_ascii=False))
    click.echo(f"\n✓ Wrote {full_path.relative_to(ROOT)} ({len(tiles)} tiles)")
    if gated:
        return

    # ---- public variant: no private photos, gated facets reduced to locked tiles
    pub_records = [r for r in records if not r['private']]
    pub_ref_set = {(r['trip'], r['id']) for r in pub_records}
    pub_index = build_id_index(pub_records)
    full_by_id = {t['id']: t for t in tiles}
    quiet = lambda *a, **k: None
    pub_tiles = []
    for facet in coll['facets']:
        if facet.get('enabled') is False:
            continue
        full_tile = full_by_id.get(facet['id'])
        if full_tile is None:
            continue
        spec = cover_spec(coll['id'], facet['id'], facet.get('cover'))
        if facet.get('locked'):
            pub_tiles.append(_locked_stub(facet, full_tile, pub_records, spec))
            continue
        if facet['type'] == 'ai':
            pub_tiles.append(_filter_tile_refs(full_tile, pub_ref_set))
            continue
        result, pool = FACET_BUILDERS[facet['rule']](facet, pub_records, quiet)
        result['id'] = facet['id']
        result['title'] = facet['title']
        # Hand-pinned covers (config/tile_covers.json) resolve against the FULL index,
        # so a private pick still resolves to the intended photo — it's served cover-only
        # via photo_privacy.cover_serve_map and never enters the public manifests/map.
        # The public `pool`/records remain the fallback for auto ('auto'/None) covers.
        result['cover'] = resolve_cover(spec, id_index, pool)
        apply_subtile_covers(result, id_index)
        _mark_not_public(result, full_tile, id_index)
        pub_tiles.append(result)

    pub_path = OUT_DIR / f"{coll['id']}.json"
    pub_path.write_text(json.dumps(
        {**base, 'hero_cover': resolve_cover(hero_spec, id_index, pub_records),
         'tiles': pub_tiles}, indent=2, ensure_ascii=False))
    locked_n = sum(1 for t in pub_tiles if t.get('locked'))
    click.echo(f"✓ Wrote {pub_path.relative_to(ROOT)} (public: {len(pub_records)}/{len(records)} photos, "
               f"{locked_n} locked tiles)")


def emit_site_stats(echo):
    """All-time travel stats for the landing page → web/collections/site_stats.json.
    Flights from config/travel_stats.json (user-maintained); driven km summed from the
    world + China road-trip rosters; countries from the processed trips index."""
    stats = {}
    ts_path = ROOT / 'config' / 'travel_stats.json'
    ts = {}
    if ts_path.exists():
        ts = json.loads(ts_path.read_text())
        stats['flights'] = ts.get('flights', {})
        stats['flown_km'] = ts.get('flown_km')
        stats['flown_time'] = ts.get('flown_time')
    wr_path = ROOT / 'config' / 'world_roofs.json'
    if wr_path.exists():
        stats['buildings'] = len(json.loads(wr_path.read_text()).get('buildings', []))
    driven, legs = 0.0, 0
    for f in ('world_road_trips.json', 'china_road_trips.json'):
        p = ROOT / 'config' / f
        if p.exists():
            trips = json.loads(p.read_text()).get('trips', [])
            driven += sum(t.get('km', 0) or 0 for t in trips)
            legs += len(trips)
    stats['driven_km'] = round(driven)
    stats['road_trips'] = legs
    # countries: the user-maintained figure is authoritative (the trips index only
    # covers processed trips and undercounts)
    if ts.get('countries_visited'):
        stats['countries'] = ts['countries_visited']
    else:
        idx_path = WEB_TRIPS / 'index.json'
        if idx_path.exists():
            idx = json.loads(idx_path.read_text())
            stats['countries'] = len({c for t in idx.get('trips', []) for c in (t.get('countries') or [])})

    # landing-page tile covers from config/tile_covers.json 'home': a photo
    # filepath resolves to {trip, id}; any other image file (e.g. a map
    # screenshot) is copied into web/previews/ and referenced by path.
    home = TILE_COVERS.get('home') or {}
    covers = {}
    if any(home.values()):
        idx = {}
        for mf in sorted(WEB_TRIPS.glob('*/manifest.json')):
            man = photo_privacy.load_full_manifest(mf.parent)
            for ph in (man or {}).get('photos', []):
                rec = {'trip': mf.parent.name, 'id': ph['id']}
                norm = re.split(r'-Enhanced|-NR|-SAI|-2$', ph['id'])[0]
                idx.setdefault(ph['id'], rec)
                idx.setdefault(norm, rec)
                idx[f"{rec['trip']}/{ph['id']}"] = rec
                idx.setdefault(f"{rec['trip']}/{norm}", rec)
        for key, spec in home.items():
            if not spec:
                continue
            stem = Path(spec).stem
            norm = re.split(r'-Enhanced|-NR|-SAI', stem)[0]
            rec = None
            for slug in (trips_for_spec(spec) if '/' in spec else []):
                rec = idx.get(f'{slug}/{stem}') or idx.get(f'{slug}/{norm}')
                if rec:
                    break
            rec = rec or idx.get(stem) or idx.get(norm)
            if rec:
                covers[key] = rec
                continue
            if (ROOT / 'web' / spec).exists():           # already a web-relative asset
                covers[key] = {'src': spec}
                continue
            p = Path(spec)
            if p.exists() and p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                import shutil
                dest_dir = ROOT / 'web' / 'previews'
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f'home-{key}{p.suffix.lower()}'
                shutil.copy2(p, dest)
                covers[key] = {'src': f'previews/{dest.name}'}
            else:
                echo(f"  ⚠ home cover '{key}': could not resolve {spec!r}")
    if covers:
        stats['covers'] = covers
    (OUT_DIR / 'site_stats.json').write_text(json.dumps(stats, indent=2))
    echo(f"✓ Wrote web/collections/site_stats.json "
         f"(flights={stats.get('flights', {}).get('total')}, driven={stats['driven_km']:,} km, "
         f"countries={stats.get('countries')})")


if __name__ == '__main__':
    main()
