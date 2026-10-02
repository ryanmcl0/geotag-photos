#!/usr/bin/env python3
"""
Build web/trips/private_coverage.json, the public "coverage" layer.

Private trips (and photos hidden inside public trips) are gated server-side by
functions/_middleware.ts, so a locked visitor can't see them at all: no pin, no
cluster, no trace of having been there. This emits a deliberately thin, PUBLIC
file so those places can still show as plain pins on the map, with no photos,
no thumbnails and no place names.

What goes in:
  - fully private trips (index.json `public: false`) → one point per photo
    cluster; pending placeholders → their single `location`,
  - public trips with a split manifest (`filtered: true`) → one point per
    cluster whose photos are ALL hidden. Clusters with at least one public
    photo already have a visible photo marker, so a coverage pin there would
    only duplicate it.

What stays out (this file is served to everyone):
  - photo ids, thumbnails, display images, photo counts,
  - cluster/location names (building names especially),
  - exact coordinates: every point is rounded to ROUND_DP (~1.1 km) and
    deduplicated, so a pin says "was in this area", never "was on this roof".

Trip names + dates ARE included (they're already public in trips/index.json),
minus any rooftopping reference (_public_name).

A trip that covered two separate journeys can label its pins per part with a
`coverage_names` entry in config/trips.json:
    "coverage_names": {"match_building": "^Day \\d", "otherwise": "2024 China November"}
Pins whose photos' folder (`building`) matches keep the trip name and dates; the
rest get the `otherwise` name, dated by an optional `dates` {start, end} or else
from their own photos.

Run standalone (./private_coverage.py [--dry-run]) or via process_all.py /
deploy.py, both of which call build().
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
WEB_TRIPS = ROOT / 'web' / 'trips'
INDEX_PATH = WEB_TRIPS / 'index.json'
OUTPUT_PATH = WEB_TRIPS / 'private_coverage.json'
TRIPS_CONFIG = ROOT / 'config' / 'trips.json'

# Coordinate precision. 2dp is about 1.1 km of latitude. Enough to place a pin in the
# right part of a city, not enough to identify a specific building.
ROUND_DP = 2


def _round_pt(lat, lon):
    return (round(float(lat), ROUND_DP), round(float(lon), ROUND_DP))


def _trip_year(trip):
    m = re.match(r'^(\d{4})', trip.get('name', '') or '')
    return int(m.group(1)) if m else trip.get('year')


def _public_name(name):
    """Trip name safe for a locked visitor: no rooftopping references.

    '2024 China (rooftopping 2024)' -> '2024 China'. Drops any parenthetical
    mentioning rooftopping, then any stray 'rooftop*' word.
    """
    name = re.sub(r'\s*\([^)]*rooftop[^)]*\)', '', name or '', flags=re.I)
    name = re.sub(r'\s*\brooftop\w*', '', name, flags=re.I)
    return re.sub(r'\s{2,}', ' ', name).strip(' -')


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def _coverage_names():
    """slug -> `coverage_names` rule from config/trips.json (see module docstring)."""
    cfg = _load_json(TRIPS_CONFIG) or {}
    trips = cfg if isinstance(cfg, list) else [t for v in cfg.values() if isinstance(v, list) for t in v]
    return {_slugify(t['name']): t['coverage_names']
            for t in trips if isinstance(t, dict) and t.get('name') and t.get('coverage_names')}


def _part_name(photos, rule):
    """None = the trip's own name; otherwise the rule's alternate name. A cluster
    belongs to the main part when most of its photos' folders match."""
    if not rule or not photos:
        return None
    pat = re.compile(rule['match_building'])
    hits = sum(1 for p in photos if pat.search(p.get('building') or ''))
    return None if hits * 2 >= len(photos) else rule['otherwise']


def _cluster_photos(manifest, cluster):
    lookup = manifest.setdefault('_by_id', {p['id']: p for p in manifest.get('photos', [])})
    return [lookup[i] for i in cluster.get('photo_ids') or [] if i in lookup]


def _private_trip_points(trip):
    """Coverage points for a fully private trip: one per photo cluster.
    Each point is (cell, country, photos)."""
    if trip.get('pending'):
        loc = trip.get('location')
        if not (isinstance(loc, list) and len(loc) == 2):
            return []
        countries = trip.get('countries') or []
        return [(_round_pt(loc[0], loc[1]), countries[0] if countries else None, [])]

    manifest = _load_json(WEB_TRIPS / trip['id'] / 'manifest.json')
    if not manifest:
        return []
    return [(_round_pt(c['lat'], c['lon']), c.get('country'), _cluster_photos(manifest, c))
            for c in manifest.get('clusters', [])
            if c.get('lat') is not None and c.get('lon') is not None]


def _hidden_trip_points(trip):
    """Coverage points for a PUBLIC trip's hidden photos.

    Only clusters that are entirely hidden count: a cluster holding even one
    public photo is already on the map as a photo marker. Points landing in the
    same rounded cell as a public cluster are dropped too, so coverage pins
    never sit on top of a visible marker.
    """
    trip_dir = WEB_TRIPS / trip['id']
    public_manifest = _load_json(trip_dir / 'manifest.json')
    if not public_manifest or not public_manifest.get('filtered'):
        return []
    full_manifest = _load_json(trip_dir / 'manifest.all.json')
    if not full_manifest:
        return []

    public_ids = {p['id'] for p in public_manifest.get('photos', [])}
    public_cells = {_round_pt(c['lat'], c['lon'])
                    for c in public_manifest.get('clusters', [])
                    if c.get('lat') is not None and c.get('lon') is not None}

    points = []
    for cluster in full_manifest.get('clusters', []):
        if cluster.get('lat') is None or cluster.get('lon') is None:
            continue
        ids = cluster.get('photo_ids') or []
        if not ids or any(pid in public_ids for pid in ids):
            continue          # nothing hidden here, or already visible
        cell = _round_pt(cluster['lat'], cluster['lon'])
        if cell in public_cells:
            continue          # a visible marker already covers this area
        points.append((cell, cluster.get('country'), _cluster_photos(full_manifest, cluster)))
    return points


def build(dry_run=False, echo=print) -> dict:
    """Write private_coverage.json. Returns the emitted payload."""
    index = _load_json(INDEX_PATH)
    if not index:
        echo(f"  ⚠ {INDEX_PATH} unreadable, skipping coverage")
        return {'trips': []}

    rules = _coverage_names()
    out_trips = []
    for trip in index.get('trips', []):
        is_private = trip.get('public') is False
        raw = _private_trip_points(trip) if is_private else _hidden_trip_points(trip)
        if not raw:
            continue

        # Split into named parts (usually just the one), then dedupe each by
        # rounded cell, keeping the first country seen for that cell.
        rule = rules.get(trip['id'])
        parts = {}
        for cell, country, photos in raw:
            part = parts.setdefault(_part_name(photos, rule), {'cells': {}, 'times': []})
            part['cells'].setdefault(cell, country)
            part['times'] += [p['timestamp'][:10] for p in photos if p.get('timestamp')]

        for part_name, part in parts.items():
            points = [{'lat': lat, 'lon': lon, **({'country': c} if c else {})}
                      for (lat, lon), c in sorted(part['cells'].items())]
            times = sorted(part['times'])
            if part_name is None:
                dates = trip.get('dates', {})
            elif rule.get('dates'):
                dates = rule['dates']
            else:
                dates = {'start': times[0], 'end': times[-1]} if times else {}
            name = part_name or trip.get('name', '')
            out_trips.append({
                'id': trip['id'],
                'name': _public_name(name),
                'year': _trip_year({'name': name}) or _trip_year(trip),
                'dates': dates,
                'countries': sorted({p['country'] for p in points if p.get('country')}),
                'private_trip': is_private,
                'points': points,
            })

    payload = {
        'precision_dp': ROUND_DP,
        'trips': sorted(out_trips, key=lambda t: (t['id'], t['name'])),
    }
    n_points = sum(len(t['points']) for t in payload['trips'])
    n_hidden = sum(1 for t in payload['trips'] if not t['private_trip'])

    if dry_run:
        echo(f"  [dry-run] would write {OUTPUT_PATH.name}: "
             f"{n_points} pins across {len(payload['trips'])} trips")
        return payload

    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + '\n')
    echo(f"  ✓ {OUTPUT_PATH.name}: {n_points} pins across {len(payload['trips'])} trips "
         f"({n_hidden} public trip{'s' if n_hidden != 1 else ''} with hidden photos)")
    return payload


if __name__ == '__main__':
    build(dry_run='--dry-run' in sys.argv)
