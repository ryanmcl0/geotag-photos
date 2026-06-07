#!/usr/bin/env python3
"""
Prune trips that are no longer in config/trips.json.

config/trips.json is the source of truth for which trips exist. Anything that was
processed/published but is no longer configured is an orphan, removed from:
  - web/trips/index.json   (what the map lists — drives localhost preview too)
  - web/trips/<slug>/       (per-trip manifest/route/symlinks)
  - hosted-photos/<slug>/   (local compressed images)
  - R2  <slug>/ prefix      (only when a caller passes an s3 client + bucket)

Shared by process_all.py (local-only: keeps localhost preview in sync) and deploy.py
(adds R2 cleanup). Has no boto3/Cloudflare dependency — the R2 client is injected.

Standalone:
  ./prune.py              # local prune (index + web/trips + hosted-photos)
  ./prune.py --dry-run    # show what would be removed
  ./prune.py --force      # bypass the bulk-delete safety guard
  ./prune.py --r2         # also delete from R2 (reads CF_* from the environment)
"""

import json
import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
TRIPS_CONFIG = PROJECT_ROOT / 'config' / 'trips.json'
WEB_TRIPS = PROJECT_ROOT / 'web' / 'trips'
HOSTED = PROJECT_ROOT / 'hosted-photos'


def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def expected_slugs():
    """Slugs the config says should exist: slugify(name) for every public+private trip,
    plus the auto-derived '<slug>-private' off-route split for each. None if unreadable."""
    if not TRIPS_CONFIG.exists():
        return None
    try:
        cfg = json.loads(TRIPS_CONFIG.read_text())
    except Exception:
        return None
    expected = set()
    for block in ('public', 'private'):
        for t in cfg.get(block, []):
            s = _slugify(t['name'])
            expected.add(s)
            expected.add(f"{s}-private")
    return expected


def find_orphans():
    """(orphan_slugs, expected_slugs). orphan_slugs is None if config is unreadable."""
    expected = expected_slugs()
    if not expected:
        return None, None
    index_path = WEB_TRIPS / 'index.json'
    indexed = set()
    if index_path.exists():
        indexed = {t['id'] for t in json.loads(index_path.read_text()).get('trips', [])}
    web = {p.name for p in WEB_TRIPS.iterdir() if p.is_dir()} if WEB_TRIPS.exists() else set()
    hosted = {p.name for p in HOSTED.iterdir() if p.is_dir()} if HOSTED.exists() else set()
    return sorted((indexed | web | hosted) - expected), expected


def _r2_trip_prefixes(s3, r2_bucket):
    """Top-level '<slug>/' prefixes present in the R2 bucket (one LIST with Delimiter,
    not a full object enumeration)."""
    prefixes = set()
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=r2_bucket, Delimiter='/'):
        for cp in page.get('CommonPrefixes', []):
            prefixes.add(cp['Prefix'].rstrip('/'))
    return prefixes


def prune_removed_trips(*, s3=None, r2_bucket=None, dry_run=False, force=False, echo=print):
    """Remove orphaned trips. Pass s3 (a boto3 R2 client) + r2_bucket to also clean R2;
    omit them for a local-only prune. echo lets callers route output (print / click.echo).
    Returns the list of pruned slugs."""
    orphans, expected = find_orphans()
    if orphans is None:
        echo("    ⚠️  Can't read config/trips.json — skipping prune")
        return []

    # When R2 is available, also reconcile against the bucket itself: a trip whose local
    # files are already gone but still has objects on R2 (e.g. removed in an earlier run
    # before the local dirs were cleaned) leaves no local trace for find_orphans to catch.
    if s3 is not None and r2_bucket:
        r2_orphans = _r2_trip_prefixes(s3, r2_bucket) - expected
        orphans = sorted(set(orphans) | r2_orphans)

    if not orphans:
        echo("    ✓ No removed trips to prune")
        return []

    # Guard: removing more than it keeps (or >5 at once) is almost certainly a bad/
    # truncated config, not an intentional bulk removal. Refuse unless forced.
    if not force and (len(orphans) > 5 or len(orphans) >= len(expected)):
        echo(f"    ⚠️  {len(orphans)} orphaned trips would be removed (keeping {len(expected)}). "
             f"That's a lot — refusing without force.")
        for o in orphans:
            echo(f"         - {o}")
        return []

    echo(f"    Removing {len(orphans)} trip(s) no longer in config: {', '.join(orphans)}")
    if dry_run:
        echo("    [dry-run] no changes made")
        return orphans

    index_path = WEB_TRIPS / 'index.json'
    if index_path.exists():
        index = json.loads(index_path.read_text())
        index['trips'] = [t for t in index.get('trips', []) if t['id'] not in orphans]
        index_path.write_text(json.dumps(index, indent=2) + '\n')

    for slug in orphans:
        for base in (WEB_TRIPS, HOSTED):
            d = base / slug
            if d.exists():
                shutil.rmtree(d)

    if s3 is not None and r2_bucket:
        for slug in orphans:
            keys = []
            for page in s3.get_paginator('list_objects_v2').paginate(Bucket=r2_bucket, Prefix=f"{slug}/"):
                keys += [{'Key': o['Key']} for o in page.get('Contents', [])]
            for i in range(0, len(keys), 1000):
                s3.delete_objects(Bucket=r2_bucket, Delete={'Objects': keys[i:i + 1000]})
            if keys:
                echo(f"      ✓ R2: deleted {len(keys)} objects under {slug}/")

    echo(f"    ✓ Pruned {len(orphans)} removed trip(s)")
    return orphans


def _r2_client_from_env():
    """Build an R2 boto3 client + bucket from CF_* env vars (lazy import)."""
    import os
    import boto3
    s3 = boto3.client('s3', endpoint_url=os.getenv('CF_R2_ENDPOINT'),
                       aws_access_key_id=os.getenv('CF_R2_ACCESS_KEY_ID'),
                       aws_secret_access_key=os.getenv('CF_R2_SECRET_KEY'), region_name='auto')
    return s3, os.getenv('CF_R2_BUCKET')


def main():
    import argparse
    ap = argparse.ArgumentParser(description='Prune trips removed from config/trips.json')
    ap.add_argument('--dry-run', action='store_true', help='Show what would be removed')
    ap.add_argument('--force', action='store_true', help='Bypass the bulk-delete safety guard')
    ap.add_argument('--r2', action='store_true', help='Also delete from R2 (reads CF_* from env)')
    args = ap.parse_args()
    s3 = bucket = None
    if args.r2:
        s3, bucket = _r2_client_from_env()
    print("🧹 Pruning trips removed from config...")
    prune_removed_trips(s3=s3, r2_bucket=bucket, dry_run=args.dry_run, force=args.force)


if __name__ == '__main__':
    main()
