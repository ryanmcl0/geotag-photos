#!/usr/bin/env python3
"""
Inject "Photos pending" placeholder trips into web/trips/index.json.

A placeholder is a trip in config/trips.json flagged with `"pending": true` — visited
but not edited yet, so it has no photos to process. Instead of going through
process_trip.py, it's written straight into the trips index as a lightweight entry the
frontend renders as a greyed pin / "Photos pending" tile (see web/js/app.js,
galleries-index.js, sidebar.js). It still counts toward the on-map country tally because
it carries `countries`.

Each pending entry in trips.json carries: name, dates {start,end}, countries (ISO-3166
alpha-2), location [lat, lon] for the map pin, and the block (public/private) it lives in.

To publish a placeholder for real: add its edits/gpx/raws paths and delete the placeholder
fields (pending/dates/countries/location), then process it normally. The slug is
slugify(name), so process_trip.py overwrites the pending index entry in place.

Used by process_all.py (keeps localhost preview in sync) and deploy.py (re-asserts on every
deploy). prune.py needs no special-casing: placeholders live in trips.json, so their slugs
are already in expected_slugs() and are never pruned.
"""

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
TRIPS_CONFIG = PROJECT_ROOT / 'config' / 'trips.json'


def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def _pending_from_config():
    """[(trip_dict, is_public)] for every trip flagged pending in config/trips.json."""
    if not TRIPS_CONFIG.exists():
        return []
    try:
        cfg = json.loads(TRIPS_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for block, is_public in (('public', True), ('private', False)):
        for t in cfg.get(block, []):
            if t.get('pending'):
                out.append((t, is_public))
    return out


def placeholder_slugs() -> set:
    """Slugs of all pending placeholders (kept for parity with prune.py; placeholders are
    already covered by prune's expected_slugs since they sit in trips.json)."""
    return {_slugify(t['name']) for t, _ in _pending_from_config()}


def _build_entry(trip: dict, is_public: bool) -> dict:
    """Turn a pending config trip into a trips/index.json entry."""
    name = trip['name']
    dates = trip.get('dates') or {}
    name_year = re.match(r'^(\d{4})', name)
    start = dates.get('start', '')
    year = int(name_year.group(1)) if name_year else (int(start[:4]) if start[:4].isdigit() else None)
    return {
        'id': _slugify(name),
        'name': name,
        'year': year,
        'dates': {'start': start, 'end': dates.get('end', start)},
        'photo_count': 0,
        'countries': trip.get('countries', []),
        'pending': True,
        'location': trip.get('location'),
        'public': bool(is_public),
    }


def apply_placeholders(index_path: Path, echo=print) -> list:
    """Upsert pending placeholders into index_path (web/trips/index.json) and drop any stale
    pending entries no longer flagged in config. Idempotent. Returns the placeholder slugs."""
    index_path = Path(index_path)
    pending = _pending_from_config()
    pending_slugs = {_slugify(t['name']) for t, _ in pending}

    if index_path.exists():
        index = json.loads(index_path.read_text())
    else:
        index = {'trips': []}
    trips = index.get('trips', [])

    # Drop stale pending entries (placeholder removed from config / promoted to a real trip,
    # in which case process_trip.py has already written a non-pending entry we must not touch).
    trips = [t for t in trips if not (t.get('pending') and t['id'] not in pending_slugs)]

    by_id = {t['id']: t for t in trips}
    for trip, is_public in pending:
        by_id[_slugify(trip['name'])] = _build_entry(trip, is_public)

    merged = list(by_id.values())
    merged.sort(key=lambda t: (t.get('dates') or {}).get('start', ''), reverse=True)
    index['trips'] = merged
    index_path.write_text(json.dumps(index, indent=2) + '\n')

    if pending:
        echo(f"    ✓ Applied {len(pending)} placeholder trip(s): {', '.join(sorted(pending_slugs))}")
    return sorted(pending_slugs)


def main():
    import argparse
    ap = argparse.ArgumentParser(description='Inject pending placeholder trips into the index')
    ap.add_argument('--index', default=str(PROJECT_ROOT / 'web' / 'trips' / 'index.json'),
                    help='Path to web/trips/index.json')
    args = ap.parse_args()
    print("📍 Applying placeholder trips...")
    apply_placeholders(Path(args.index))


if __name__ == '__main__':
    main()
