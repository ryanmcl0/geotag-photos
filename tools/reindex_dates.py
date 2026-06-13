#!/usr/bin/env python3
"""
Recompute every trip's displayed date range, dropping wrong-clock EXIF outliers.

Some trips carry a few photos with a reset/incorrect camera clock (the classic
2015-01-01 default, or a second body on the wrong year). Those skewed the stored
date range — e.g. "2018 Hong Kong" showed "Jan 1, 2015 – Aug 3, 2018". This rewrites
each trip's `dates` in web/trips/index.json and in its manifest(s) using
process_trip.robust_date_range (the trip's name-year is authoritative: keep only
timestamps within ±1 year of it). Re-runnable; only writes when a date changes.

Usage:
  ./reindex_dates.py            # apply
  ./reindex_dates.py --dry-run  # show what would change
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo root (script lives in tools/)
sys.path.insert(0, str(ROOT))
from process_trip import robust_date_range

WEB_TRIPS = ROOT / 'web' / 'trips'


def manifest_dates(trip_dir: Path):
    """Recompute (start, end) from a trip's FULL photo set (manifest.all.json if the
    public manifest was split, else manifest.json). Only for NO-GPX trips — GPX trips
    derive their range from the (accurate) track, which we must not overwrite with the
    noisier photo-timestamp span."""
    mj = trip_dir / 'manifest.json'
    if not mj.exists():
        return None
    man = json.loads(mj.read_text())
    full = man
    if man.get('filtered') and (trip_dir / 'manifest.all.json').exists():
        full = json.loads((trip_dir / 'manifest.all.json').read_text())
    if ((full.get('source') or {}).get('gpx_path')):
        return None   # GPX trip — trust the track-derived dates
    name = full.get('trip_name') or man.get('trip_name') or trip_dir.name
    ts = [p['timestamp'] for p in full.get('photos', []) if p.get('timestamp')]
    s, e = robust_date_range(ts, name)
    return (s, e) if s else None


def main():
    dry = '--dry-run' in sys.argv
    index_path = WEB_TRIPS / 'index.json'
    index = json.loads(index_path.read_text())
    by_id = {t['id']: t for t in index.get('trips', [])}
    changed = 0

    for trip_dir in sorted(p for p in WEB_TRIPS.iterdir() if p.is_dir()):
        dr = manifest_dates(trip_dir)
        if not dr:
            continue
        start, end = dr
        new = {'start': start, 'end': end}

        # update both manifests
        for fname in ('manifest.json', 'manifest.all.json'):
            mf = trip_dir / fname
            if not mf.exists():
                continue
            man = json.loads(mf.read_text())
            if man.get('dates') != new:
                if not dry:
                    man['dates'] = new
                    mf.write_text(json.dumps(man, indent=2))

        # update index entry
        entry = by_id.get(trip_dir.name)
        if entry and entry.get('dates') != new:
            old = entry.get('dates')
            print(f"  {trip_dir.name}: {old} → {new}")
            if not dry:
                entry['dates'] = new
            changed += 1

    if not dry and changed:
        # keep the index sorted by (corrected) start date, most recent first
        index['trips'].sort(key=lambda t: t.get('dates', {}).get('start', ''), reverse=True)
        index_path.write_text(json.dumps(index, indent=2) + '\n')
    print(f"{'[dry-run] ' if dry else ''}{changed} trip date range(s) "
          f"{'would change' if dry else 'updated'}")


if __name__ == '__main__':
    main()
