#!/usr/bin/env python3
"""
Process all trips listed in trips.json.

Skips trips that are already processed (manifest.json exists).
Use --force to reprocess everything, or --trip NAME to target one trip.

Usage:
  ./process_all.py                        # process new/unprocessed trips
  ./process_all.py --update               # delta: reprocess only trips whose edits changed
  ./process_all.py --force                # reprocess all trips
  ./process_all.py --trip "Scotland"      # process one trip by name (partial match)
  ./process_all.py --dry-run              # show what would run without executing
  ./process_all.py --trip X --gps-only    # update GPS/clusters only (reuse images, bust EXIF cache)

--update reprocesses only trips whose source edits changed since they were last processed —
this includes a NEW or MODIFIED edit (newer than the manifest) AND a DELETED edit (e.g. after
duplicate cleanup), detected by comparing the manifest's photos to the edits on disk. Each
selected trip runs in process_trip --update mode (delta re-encode + orphan-image cleanup),
so removed edits drop out of the manifest and hosted-photos; a following deploy then prunes
them from R2. See trip_is_dirty().
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

# Source edit extensions that become photos (mirror process_trip.SUPPORTED_EXTENSIONS).
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif'}


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


def is_processed(slug: str) -> bool:
    return (WEB_TRIPS_DIR / slug / 'manifest.json').exists()


def trip_is_dirty(trip: dict, slug: str) -> bool:
    """A processed trip is 'dirty' if its source edits changed since it was last processed:
      • any edit file is newer than the manifest (a new or modified edit), OR
      • a photo in the manifest no longer has a matching source edit on disk (a deleted
        edit — e.g. duplicate cleanup, which never bumps any mtime so the check above can't
        see it). Manifest photo 'id' == edit-file stem (process_trip.py:2453), so we compare
        manifest ids against the stems currently present under the edits tree.
    Used by --update to select only trips whose edits changed. Stat-only walk (plus one
    manifest read); call only in --update."""
    man = WEB_TRIPS_DIR / slug / 'manifest.json'
    if not man.exists():
        return True  # never processed → needs processing
    man_mtime = man.stat().st_mtime
    edits = Path(trip['edits'])
    if not edits.exists():
        return False
    current_stems = set()
    for p in edits.rglob('*'):
        try:
            if not p.is_file() or p.name.startswith('.'):
                continue
            if p.stat().st_mtime > man_mtime:
                return True  # new or modified edit
            if p.suffix.lower() in IMAGE_EXTS:
                current_stems.add(p.stem)
        except OSError:
            continue
    # Deletions: any manifest photo whose source edit stem is gone from the edits tree.
    try:
        manifest = json.loads(man.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return any(ph.get('id') not in current_stems for ph in manifest.get('photos', []))


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


def build_command(trip: dict, gpx_path: Path | None, skip_existing_images: bool = False,
                  update: bool = False, reindex: bool = False) -> list[str]:
    cmd = [sys.executable, 'process_trip.py',
           '--name', trip['name'],
           '--photos', trip['edits']]

    if gpx_path:
        cmd += ['--gpx', str(gpx_path)]
    if skip_existing_images:
        cmd += ['--skip-existing-images']
    if update:
        cmd += ['--update']
    if reindex:
        cmd += ['--reindex']

    opts = trip.get('options', {})
    if opts.get('geosync'):
        cmd += ['--geosync', opts['geosync']]
    if opts.get('filter_raws'):
        cmd += ['--filter-by-raws-in', opts['filter_raws']]
    if opts.get('exclude_raws'):
        cmd += ['--exclude-raws-in', opts['exclude_raws']]
    if opts.get('fallback_location'):
        cmd += ['--fallback-location', opts['fallback_location']]
    if opts.get('untimed_to_fallback'):
        cmd += ['--untimed-to-fallback']
    if opts.get('untimed_label'):
        cmd += ['--untimed-label', opts['untimed_label']]
    if opts.get('cluster_radius'):
        cmd += ['--cluster-radius', str(opts['cluster_radius'])]
    if opts.get('burst_time_window') is not None:
        cmd += ['--burst-time-window', str(opts['burst_time_window'])]
    if opts.get('burst_max_spread') is not None:
        cmd += ['--burst-max-spread', str(opts['burst_max_spread'])]
    if opts.get('geotag_overrides'):
        import json as _json
        cmd += ['--geotag-overrides', _json.dumps(opts['geotag_overrides'])]
    if opts.get('round_trip'):
        cmd += ['--round-trip']
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
    if opts.get('exclude_photos'):
        excl = opts['exclude_photos']
        if isinstance(excl, list):
            excl = ';'.join(excl)
        cmd += ['--exclude-photos', excl]
    if opts.get('only_edits_dirs'):
        cmd += ['--only-edits-dirs']
    if opts.get('exclude_raw_subdirs'):
        subdirs = opts['exclude_raw_subdirs']
        if isinstance(subdirs, list):
            subdirs = ';'.join(subdirs)
        cmd += ['--exclude-raw-subdirs', subdirs]
    if opts.get('max_interp_gap_hours') is not None:
        cmd += ['--max-interp-gap-hours', str(opts['max_interp_gap_hours'])]
    if opts.get('max_gap_interp_km') is not None:
        cmd += ['--max-gap-interp-km', str(opts['max_gap_interp_km'])]
    if opts.get('phone_gps'):
        cmd += ['--phone-gps']
        if opts.get('phone_gps_dir'):
            cmd += ['--phone-gps-dir', opts['phone_gps_dir']]
        if opts.get('phone_gps_threshold_min') is not None:
            cmd += ['--phone-gps-threshold-min', str(opts['phone_gps_threshold_min'])]
        if opts.get('phone_gps_offset_hours') is not None:
            cmd += ['--phone-gps-offset-hours', str(opts['phone_gps_offset_hours'])]
    if opts.get('split_offroute_private'):
        cmd += ['--split-offroute-private']
    if opts.get('private_cluster_radius'):
        cmd += ['--private-cluster-radius', str(opts['private_cluster_radius'])]
    if opts.get('gpx_route_subdir'):
        cmd += ['--gpx-route-subdir', opts['gpx_route_subdir']]
    if opts.get('route_snap_public_hours') is not None:
        cmd += ['--route-snap-public-hours', str(opts['route_snap_public_hours'])]
    if opts.get('private_locations'):
        locs = opts['private_locations']
        if isinstance(locs, list):
            locs = ';'.join(locs)
        cmd += ['--private-locations', locs]
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
@click.option('--update', 'update', is_flag=True,
              help='Incremental: process unprocessed trips PLUS already-processed trips whose edits '
                   'changed (any source newer than its manifest). Each runs in process_trip --update '
                   'mode (delta re-encode + orphan cleanup). First --update on a trip adopts baseline.')
@click.option('--reindex', 'reindex', is_flag=True,
              help='Stamp source-state baselines on the selected (already-processed) trips without '
                   'reprocessing — encodes only missing images. Pair with --trip to target one.')
@click.option('--no-prune', 'no_prune', is_flag=True,
              help='Do not remove trips deleted from config/trips.json from local web/trips + '
                   'hosted-photos (full runs prune by default to keep localhost in sync).')
def process_all(force: bool, trip_filter: str | None, dry_run: bool, skip_existing_images: bool,
                gps_only: bool, update: bool, reindex: bool, no_prune: bool):
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

    # Keep local state in sync with config: remove trips deleted from trips.json from
    # web/trips/index.json + web/trips/ + hosted-photos/ so the localhost preview reflects
    # removals without a deploy. Runs before the "nothing to process" return so a pure
    # removal still propagates. Scoped (--trip) runs skip it; R2 is left to deploy.py.
    if not trip_filter and not no_prune:
        from prune import prune_removed_trips
        click.echo("Pruning trips removed from config...")
        prune_removed_trips(dry_run=dry_run, echo=click.echo)
        click.echo("")

    to_process, already_done = [], []
    for trip, is_public in all_trips:
        slug = slugify(trip['name'])
        if reindex:
            # Baseline-stamp every selected trip (only meaningful for processed ones,
            # but harmless otherwise).
            to_process.append((trip, is_public))
        elif update:
            # Unprocessed → process; processed → only if dirty (edits changed).
            if not is_processed(slug):
                to_process.append((trip, is_public))
            elif trip_is_dirty(trip, slug):
                click.echo(f"  Δ dirty (edits changed): {trip['name']}")
                to_process.append((trip, is_public))
            else:
                already_done.append(trip['name'])
        elif not force and is_processed(slug):
            already_done.append(trip['name'])
        else:
            to_process.append((trip, is_public))

    if already_done:
        click.echo(f"Skipping {len(already_done)} unchanged/already-processed trip(s) "
                   f"({'--force to reprocess' if not update else 'no edit changes detected'})")

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

            cmd = build_command(trip, gpx_path, skip_existing_images=skip_existing_images,
                                update=update, reindex=reindex)
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

    # Refresh the collections (China hub etc.) so newly processed photos land in
    # their bridges/provinces/roads galleries automatically. Derived facets only —
    # no AI runs here; the category tile is carried forward. Run
    # build_collections.py --category yourself to (re-)run CLIP.
    try:
        click.echo("\nUpdating collections (build_collections)...")
        subprocess.run([sys.executable, 'build_collections.py'], cwd=str(PROJECT_ROOT))
    except Exception as e:
        click.echo(f"⚠ build_collections failed: {e}", err=True)

    # Tag each manifest photo with its aspect ratio so the trip gallery view can
    # lay out justified rows without probing images client-side.
    try:
        click.echo("\nTagging photo aspect ratios (backfill_manifest_ar)...")
        subprocess.run([sys.executable, 'tools/backfill_manifest_ar.py'], cwd=str(PROJECT_ROOT))
    except Exception as e:
        click.echo(f"⚠ backfill_manifest_ar failed: {e}", err=True)


if __name__ == '__main__':
    process_all()
