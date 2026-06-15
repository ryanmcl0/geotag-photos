#!/usr/bin/env python3
"""Inject a per-photo aspect ratio ('ar') into every web/trips/*/manifest.json.

The gallery view lays out justified rows (and sizes lightbox slides) from 'ar'
without probing images client-side. Dimensions come from the local display webp
headers (fast, header-only reads of files the pipeline already produced).

Idempotent: photos that already have 'ar' are skipped. Re-run after a pipeline
rebuild to repopulate (process_trip.py does not currently emit 'ar').
"""
import json
import sys
from pathlib import Path

from PIL import Image

WEB_TRIPS = Path(__file__).resolve().parent.parent / 'web' / 'trips'


def ar_for(display_dir: Path, photo_id: str):
    for ext in ('webp', 'avif', 'jpg', 'jpeg', 'png'):
        f = display_dir / f'{photo_id}.{ext}'
        if f.exists():
            try:
                with Image.open(f) as im:
                    w, h = im.size
                return round(w / h, 3) if h else None
            except Exception:
                return None
    return None


def patch_manifest(path: Path) -> tuple[int, int]:
    data = json.loads(path.read_text())
    display_dir = path.parent / 'display'
    patched = missing = 0
    for p in data.get('photos', []):
        if 'ar' in p:
            continue
        ar = ar_for(display_dir, p['id'])
        if ar:
            p['ar'] = ar
            patched += 1
        else:
            missing += 1
    if patched:
        path.write_text(json.dumps(data, indent=2))
    return patched, missing


def main():
    manifests = sorted(WEB_TRIPS.rglob('manifest*.json'))
    if not manifests:
        print(f'No manifests under {WEB_TRIPS}', file=sys.stderr)
        return 1
    total_patched = total_missing = 0
    for m in manifests:
        patched, missing = patch_manifest(m)
        total_patched += patched
        total_missing += missing
        if patched or missing:
            flag = f'  ({missing} unresolved)' if missing else ''
            print(f'  {m.relative_to(WEB_TRIPS.parent)}: +{patched}{flag}')
    print(f'Done: {total_patched} photos tagged, {total_missing} unresolved.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
