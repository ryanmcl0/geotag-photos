#!/usr/bin/env python3
"""
Hard block on photos that must never be uploaded.

Distinct from photo_privacy.py's `force_private`, which only gates a photo
behind the See All password — the bytes are still uploaded and still served to
anyone holding the cookie. Entries here are never uploaded at all, and are
deleted from R2 if a previous deploy put them there.

Source of truth: **config/blocklist.json** (gitignored like the rest of
config/, but mirrored to the private backup repo by deploy.py, so the block
survives a fresh clone). safety-check/blocklist.json is also read when present,
so a local review session takes effect immediately; `--promote` folds it into
config/ for durability. Both files are local-only.

    {"blocked": {"<trip-slug>": ["<photo-id>", ...]}}

Matching is deliberately wide. A photo is blocked if EITHER
  - (trip slug, photo id) matches exactly, or
  - the bare photo id matches any blocked id, in any trip.
The second rule is what stops an entry slipping back in after being re-imported
under a different trip slug (different libraries use different slugs for the
same trip). Bare-id hits are printed at deploy time, because a genuine filename
collision between two cameras would also be caught by it — see the RM-reuse
gotcha.

Usage:
    ./blocklist.py                       # what is blocked, and where it bites
    ./blocklist.py --promote             # fold the local list into config/
    ./blocklist.py --purge-r2 [--dry-run]
    ./blocklist.py --strip-manifests [--dry-run]

deploy.py loads this and checks every upload, so no manual step is needed for
the block itself.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
CONFIG_PATH = ROOT / 'config' / 'blocklist.json'
LOCAL_PATH = ROOT / 'safety-check' / 'blocklist.json'
WEB_TRIPS = ROOT / 'web' / 'trips'
HOSTED = ROOT / 'hosted-photos'


class Blocklist:
    def __init__(self, pairs: set, ids: set, sources: list):
        self.pairs = pairs          # {(slug, photo_id)}
        self.ids = ids              # {photo_id} — matched across every trip
        self.sources = sources

    def __bool__(self):
        return bool(self.pairs)

    def __len__(self):
        return len(self.pairs)

    def is_blocked(self, slug: str, photo_id: str) -> bool:
        return (slug, photo_id) in self.pairs or photo_id in self.ids

    def why(self, slug: str, photo_id: str) -> str:
        if (slug, photo_id) in self.pairs:
            return 'listed'
        return 'id match from another trip' if photo_id in self.ids else ''


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return (json.loads(path.read_text()).get('blocked') or {})
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"  ⚠️  {path.name} unreadable ({e}) — treating as empty", file=sys.stderr)
        return {}


def load() -> Blocklist:
    pairs, ids, sources = set(), set(), []
    for path in (CONFIG_PATH, LOCAL_PATH):
        blocked = _read(path)
        if not blocked:
            continue
        n = 0
        for slug, photo_ids in blocked.items():
            for pid in photo_ids:
                pairs.add((slug, pid))
                ids.add(pid)
                n += 1
        sources.append(f"{path.relative_to(ROOT)} ({n})")
    return Blocklist(pairs, ids, sources)


def promote() -> int:
    """Merge the local list into config/ so the block survives a fresh clone."""
    merged = {}
    for path in (CONFIG_PATH, LOCAL_PATH):
        for slug, pids in _read(path).items():
            merged.setdefault(slug, set()).update(pids)
    out = {slug: sorted(v) for slug, v in sorted(merged.items())}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({
        'note': 'never upload these; enforced by blocklist.py via deploy.py',
        'count': sum(len(v) for v in out.values()),
        'blocked': out,
    }, indent=2) + '\n')
    return sum(len(v) for v in out.values())


def local_hits(bl: Blocklist) -> list:
    """Blocked photos that actually exist in the deployable tree right now."""
    hits = []
    if not bl or not HOSTED.is_dir():
        return hits
    for trip_dir in sorted(HOSTED.iterdir()):
        if not trip_dir.is_dir():
            continue
        for img in sorted(trip_dir.rglob('*.webp')):
            if bl.is_blocked(trip_dir.name, img.stem):
                hits.append((trip_dir.name, img.stem,
                             str(img.relative_to(HOSTED)), bl.why(trip_dir.name, img.stem)))
    return hits


def strip_manifests(bl: Blocklist, dry_run: bool = False) -> int:
    """Drop blocked ids from every trip manifest, so a blocked photo does not
    leave a broken tile in a gallery. Cluster photo_ids are pruned too, and
    clusters left empty are removed."""
    if not bl or not WEB_TRIPS.is_dir():
        return 0
    removed = 0
    for trip_dir in sorted(WEB_TRIPS.iterdir()):
        if not trip_dir.is_dir():
            continue
        for name in ('manifest.json', 'manifest.all.json'):
            path = trip_dir / name
            if not path.exists():
                continue
            try:
                man = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            drop = {p['id'] for p in man.get('photos', [])
                    if bl.is_blocked(trip_dir.name, p['id'])}
            if not drop:
                continue
            removed += len(drop)
            print(f"    {'[dry-run] ' if dry_run else ''}{trip_dir.name}/{name}: "
                  f"removing {len(drop)} blocked photo(s)")
            if dry_run:
                continue
            man['photos'] = [p for p in man.get('photos', []) if p['id'] not in drop]
            for c in man.get('clusters', []):
                c['photo_ids'] = [i for i in c.get('photo_ids', []) if i not in drop]
            man['clusters'] = [c for c in man.get('clusters', []) if c.get('photo_ids')]
            path.write_text(json.dumps(man, indent=2))
    return removed


def purge_r2(bl: Blocklist, dry_run: bool = False) -> int:
    """Delete every R2 object whose photo id is blocked. Safe to re-run."""
    if not bl:
        print('  blocklist empty — nothing to purge')
        return 0
    try:
        import boto3
    except ImportError:
        print('Error: boto3 not installed', file=sys.stderr)
        return -1
    need = ['CF_R2_ENDPOINT', 'CF_R2_ACCESS_KEY_ID', 'CF_R2_SECRET_KEY', 'CF_R2_BUCKET']
    if any(not os.getenv(v) for v in need):
        print(f"Error: missing env ({', '.join(v for v in need if not os.getenv(v))}). "
              f"Run:  set -a; . ./.env.deploy; set +a", file=sys.stderr)
        return -1
    s3 = boto3.client('s3', endpoint_url=os.environ['CF_R2_ENDPOINT'],
                      aws_access_key_id=os.environ['CF_R2_ACCESS_KEY_ID'],
                      aws_secret_access_key=os.environ['CF_R2_SECRET_KEY'],
                      region_name='auto')
    bucket = os.environ['CF_R2_BUCKET']
    doomed = []
    paginator = s3.get_paginator('list_objects_v2')
    scanned = 0
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get('Contents', []):
            key = obj['Key']
            scanned += 1
            parts = key.split('/')
            if len(parts) < 2 or not key.endswith('.webp'):
                continue
            slug, stem = parts[0], Path(parts[-1]).stem
            if bl.is_blocked(slug, stem):
                doomed.append(key)
    print(f"  scanned {scanned} R2 objects; {len(doomed)} match the blocklist")
    for k in doomed:
        print(f"    {'[dry-run] ' if dry_run else ''}delete {k}")
    if doomed and not dry_run:
        for i in range(0, len(doomed), 1000):
            s3.delete_objects(Bucket=bucket,
                              Delete={'Objects': [{'Key': k} for k in doomed[i:i + 1000]]})
        print(f"  ✓ deleted {len(doomed)} object(s) from R2")
    return len(doomed)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--promote', action='store_true',
                    help='merge the local list into config/blocklist.json')
    ap.add_argument('--purge-r2', action='store_true', help='delete blocked keys from R2')
    ap.add_argument('--strip-manifests', action='store_true',
                    help='remove blocked ids from web/trips/*/manifest*.json')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if args.promote:
        n = promote()
        print(f"✓ config/blocklist.json now blocks {n} photo(s)")

    bl = load()
    print(f"Blocklist: {len(bl.pairs)} entries, {len(bl.ids)} distinct ids")
    for s in bl.sources:
        print(f"  from {s}")
    if not bl:
        print('  (empty — nothing is blocked)')
        return

    hits = local_hits(bl)
    print(f"\nPresent in hosted-photos/ (would be uploaded): {len(hits)}")
    for _slug, _pid, rel, why in hits[:40]:
        print(f"  {rel}  [{why}]")

    if args.strip_manifests:
        print('\nStripping manifests:')
        n = strip_manifests(bl, args.dry_run)
        print(f"  {n} manifest entr(ies) removed")
    if args.purge_r2:
        print('\nPurging R2:')
        purge_r2(bl, args.dry_run)


if __name__ == '__main__':
    main()
