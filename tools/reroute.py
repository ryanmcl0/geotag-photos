#!/usr/bin/env python3
"""
Regenerate route.geojson for one or all already-processed trips, using the
current gpx_to_geojson() logic. Fast — no image re-encoding.

Use when:
  - You changed --gpx-split-gap-km and want the change applied retroactively
  - You bumped/changed the GPX-to-GeoJSON conversion logic in process_trip.py
"""

import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root (script lives in tools/)
from process_trip import DEFAULT_TRIPS_DIR, gpx_to_geojson


def reroute_trip(trip_dir: Path, split_gap_km: float) -> bool:
    manifest_path = trip_dir / 'manifest.json'
    if not manifest_path.exists():
        click.echo(f"  Skipping {trip_dir.name}: no manifest.json", err=True)
        return False
    with open(manifest_path) as f:
        manifest = json.load(f)

    gpx_path = Path(manifest.get('source', {}).get('gpx_path', ''))
    if not gpx_path or not gpx_path.exists():
        click.echo(f"  Skipping {trip_dir.name}: GPX file not found "
                   f"({gpx_path or '<unknown>'})", err=True)
        return False

    geojson_data = gpx_to_geojson(gpx_path, split_gap_km=split_gap_km)
    n_features = len(geojson_data['features'])
    with open(trip_dir / 'route.geojson', 'w') as f:
        json.dump(geojson_data, f, indent=2)

    manifest.setdefault('routing', {})
    manifest['routing']['split_gap_km'] = split_gap_km
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    click.echo(f"  {trip_dir.name:<35}  {n_features:>3} line feature(s)")
    return True


@click.command()
@click.option('--trip', required=True,
              help='Trip slug (e.g. 2024-kyrgyzstan) or "all"')
@click.option('--split-gap-km', default=5.0, type=float,
              help='Split route on consecutive trackpoints >X km apart (default: 5)')
def main(trip: str, split_gap_km: float):
    if trip == 'all':
        trip_dirs = sorted([d for d in DEFAULT_TRIPS_DIR.iterdir()
                            if d.is_dir() and (d / 'manifest.json').exists()])
    else:
        trip_dirs = [DEFAULT_TRIPS_DIR / trip]
        if not trip_dirs[0].exists():
            click.echo(f"Error: trip not found: {trip_dirs[0]}", err=True)
            sys.exit(1)

    if not trip_dirs:
        click.echo("No trips found.")
        return

    click.echo(f"Rerouting {len(trip_dirs)} trip(s) at split_gap_km={split_gap_km}\n")
    for d in trip_dirs:
        reroute_trip(d, split_gap_km)
    click.echo("\nDone.")


if __name__ == '__main__':
    main()
