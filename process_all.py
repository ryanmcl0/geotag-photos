#!/usr/bin/env python3
"""
Process all trips listed in trips.json.

Skips trips that are already processed (manifest.json exists).
Use --force to reprocess everything, or --trip NAME to target one trip.

Usage:
  ./process_all.py                        # process new/unprocessed trips
  ./process_all.py --force                # reprocess all trips
  ./process_all.py --trip "Scotland"      # process one trip by name (partial match)
  ./process_all.py --dry-run              # show what would run without executing
  ./process_all.py --trip X --gps-only    # update GPS/clusters only (reuse images, bust EXIF cache)
"""

import json
import re
import sys
import subprocess
import tempfile
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).parent.resolve()
TRIPS_CONFIG = PROJECT_ROOT / 'config' / 'trips.json'
WEB_TRIPS_DIR = PROJECT_ROOT / 'web' / 'trips'


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


def is_processed(slug: str) -> bool:
    return (WEB_TRIPS_DIR / slug / 'manifest.json').exists()


def gather_gpx_files(gpx_entry) -> list[Path]:
    """Collect .gpx files from a path string or list of path strings."""
    paths = [gpx_entry] if isinstance(gpx_entry, str) else gpx_entry
    files = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            click.echo(f"  ⚠ GPX path not found: {path}", err=True)
            continue
        if path.is_file():
            files.append(path)
        else:
            found = sorted(path.glob('*.gpx')) + sorted(path.glob('*.GPX'))
            # Skip macOS AppleDouble sidecar files (._foo.gpx) — not real GPX.
            found = [f for f in found if not f.name.startswith('._')]
            if not found:
                click.echo(f"  ⚠ No .gpx files in: {path}", err=True)
            files.extend(found)
    return files


def merge_gpx_to_temp(gpx_files: list[Path]) -> Path:
    """Merge multiple GPX files into a single temp file. Returns the temp path."""
    try:
        import gpxpy
        import gpxpy.gpx
    except ImportError:
        click.echo("Error: gpxpy not installed", err=True)
        sys.exit(1)

    def read_gpx_text(path: Path) -> str:
        """Read a GPX file tolerant of non-UTF-8 encodings (some exporters emit
        UTF-16 or leave stray bytes). Falls back progressively, never raising."""
        raw = path.read_bytes()
        if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
            return raw.decode('utf-16')
        for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', errors='replace')

    combined = gpxpy.gpx.GPX()
    for f in gpx_files:
        try:
            gpx = gpxpy.parse(read_gpx_text(f))
        except Exception as e:
            click.echo(f"  ⚠ Skipping unreadable GPX {f.name}: {e}", err=True)
            continue
        for track in gpx.tracks:
            combined.tracks.append(track)

    tmp = tempfile.NamedTemporaryFile(suffix='.gpx', delete=False)
    Path(tmp.name).write_text(combined.to_xml())
    tmp.close()
    return Path(tmp.name)


def build_command(trip: dict, gpx_path: Path | None, skip_existing_images: bool = False) -> list[str]:
    cmd = [sys.executable, 'process_trip.py',
           '--name', trip['name'],
           '--photos', trip['edits']]

    if gpx_path:
        cmd += ['--gpx', str(gpx_path)]
    if skip_existing_images:
        cmd += ['--skip-existing-images']

    opts = trip.get('options', {})
    if opts.get('geosync'):
        cmd += ['--geosync', opts['geosync']]
    if opts.get('filter_raws'):
        cmd += ['--filter-by-raws-in', opts['filter_raws']]
    if opts.get('exclude_raws'):
        cmd += ['--exclude-raws-in', opts['exclude_raws']]
    if opts.get('fallback_location'):
        cmd += ['--fallback-location', opts['fallback_location']]
    if opts.get('cluster_radius'):
        cmd += ['--cluster-radius', str(opts['cluster_radius'])]
    if trip.get('kmz'):
        cmd += ['--kmz', trip['kmz']]
    if trip.get('raws'):
        cmd += ['--raws-root', trip['raws']]
        # Same tree also serves DJI GPS lookup (drone originals carry embedded GPS).
        cmd += ['--raws', trip['raws']]
    if opts.get('exclude_buildings'):
        buildings = opts['exclude_buildings']
        if isinstance(buildings, list):
            buildings = ';'.join(buildings)
        cmd += ['--exclude-buildings', buildings]
    if opts.get('exclude_edits_under'):
        excl = opts['exclude_edits_under']
        if isinstance(excl, list):
            excl = ';'.join(excl)
        cmd += ['--exclude-edits-under', excl]
    if opts.get('max_interp_gap_hours') is not None:
        cmd += ['--max-interp-gap-hours', str(opts['max_interp_gap_hours'])]
    if opts.get('split_offroute_private'):
        cmd += ['--split-offroute-private']
    if opts.get('private_cluster_radius'):
        cmd += ['--private-cluster-radius', str(opts['private_cluster_radius'])]
    if opts.get('gpx_route_subdir'):
        cmd += ['--gpx-route-subdir', opts['gpx_route_subdir']]
    if opts.get('route_snap_public_hours') is not None:
        cmd += ['--route-snap-public-hours', str(opts['route_snap_public_hours'])]
    if opts.get('no_fake_route'):
        cmd += ['--no-fake-route']
    if opts.get('strict_building_distance'):
        cmd += ['--strict-building-distance']

    # Always provide the building-coords file when present; process_trip ignores
    # it if there's no raw tree to derive building names from.
    locations = PROJECT_ROOT / 'config' / 'locations.json'
    if locations.exists():
        cmd += ['--locations-file', str(locations)]

    return cmd


@click.command()
@click.option('--force', is_flag=True, help='Reprocess already-processed trips')
@click.option('--trip', 'trip_filter', default=None, metavar='NAME',
              help='Process only trips whose name contains NAME (case-insensitive)')
@click.option('--dry-run', is_flag=True, help='Show what would run without executing')
@click.option('--skip-existing-images', is_flag=True,
              help='Reuse already-generated thumbnails/display images (only recompute placement/clusters/manifest)')
@click.option('--gps-only', is_flag=True,
              help='GPS/cluster update only: busts EXIF cache, reuses existing images. '
                   'Use after geotag_by_raws_dirs.py to apply new coordinates without re-encoding.')
def process_all(force: bool, trip_filter: str | None, dry_run: bool, skip_existing_images: bool, gps_only: bool):
    """Process all trips listed in trips.json."""
    if gps_only:
        force = True
        skip_existing_images = True
    if not TRIPS_CONFIG.exists():
        click.echo("Error: config/trips.json not found. Copy config/trips.example.json to config/trips.json and fill it in.", err=True)
        sys.exit(1)

    config = json.loads(TRIPS_CONFIG.read_text())
    public_trips = [(t, True) for t in config.get('public', [])]
    private_trips = [(t, False) for t in config.get('private', [])]
    all_trips = public_trips + private_trips

    if trip_filter:
        all_trips = [(t, p) for t, p in all_trips if trip_filter.lower() in t['name'].lower()]
        if not all_trips:
            click.echo(f"No trips matching '{trip_filter}'", err=True)
            sys.exit(1)

    to_process, already_done = [], []
    for trip, is_public in all_trips:
        slug = slugify(trip['name'])
        if not force and is_processed(slug):
            already_done.append(trip['name'])
        else:
            to_process.append((trip, is_public))

    if already_done:
        click.echo(f"Skipping {len(already_done)} already-processed trip(s) (--force to reprocess)")

    if not to_process:
        click.echo("Nothing to process.")
        return

    click.echo(f"\nWill process {len(to_process)} trip(s):")
    for t, is_public in to_process:
        tag = 'GPX' if t.get('gpx') else 'no GPX'
        vis = 'public' if is_public else 'private'
        click.echo(f"  [{tag}] [{vis}] {t['name']}")

    if dry_run:
        click.echo("\n[dry-run — no processing done]")
        return

    click.echo()
    failed = []
    for trip, is_public in to_process:
        click.echo(f"{'='*60}")
        click.echo(f"  {trip['name']}")
        click.echo(f"{'='*60}")

        if gps_only:
            cache = WEB_TRIPS_DIR / slugify(trip['name']) / 'exif_cache.json'
            if cache.exists():
                cache.unlink()
                click.echo(f"  Busted EXIF cache")

        tmp_gpx = None
        try:
            gpx_path = None
            if trip.get('gpx'):
                gpx_files = gather_gpx_files(trip['gpx'])
                if not gpx_files:
                    click.echo("  ⚠ No GPX files found — running in no-GPX mode")
                elif len(gpx_files) == 1:
                    gpx_path = gpx_files[0]
                else:
                    click.echo(f"  Merging {len(gpx_files)} GPX files...")
                    tmp_gpx = merge_gpx_to_temp(gpx_files)
                    gpx_path = tmp_gpx

            cmd = build_command(trip, gpx_path, skip_existing_images=skip_existing_images)
            result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
            if result.returncode != 0:
                click.echo(f"\n  ✗ Failed (exit {result.returncode})", err=True)
                failed.append(trip['name'])
        except Exception as e:
            # One bad trip shouldn't abort the whole batch.
            click.echo(f"\n  ✗ Error: {e}", err=True)
            failed.append(trip['name'])
        finally:
            if tmp_gpx and tmp_gpx.exists():
                tmp_gpx.unlink()

    click.echo(f"\n{'='*60}")
    done = len(to_process) - len(failed)
    click.echo(f"Done — {done}/{len(to_process)} trips processed successfully")
    if failed:
        click.echo(f"Failed: {', '.join(failed)}", err=True)

    try:
        import deploy
        deploy.sync_public_flags()
    except Exception as e:
        click.echo(f"⚠ sync_public_flags failed: {e}", err=True)


if __name__ == '__main__':
    process_all()
