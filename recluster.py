#!/usr/bin/env python3
"""
Recompute the manifest-side photo clusters for one or all already-processed
trips, without re-encoding any images. Fast (seconds for a whole library).

Usage:
  recluster.py --trip 2024-kyrgyzstan --cluster-radius 25
  recluster.py --trip all --cluster-radius 20
"""

import json
import sys
from pathlib import Path
from typing import Optional

import click

from process_trip import DEFAULT_CLUSTER_RADIUS, DEFAULT_TRIPS_DIR, cluster_photos


def recluster_trip(trip_dir: Path, cluster_radius: float) -> Optional[tuple]:
    manifest_path = trip_dir / 'manifest.json'
    if not manifest_path.exists():
        click.echo(f"  Skipping {trip_dir.name}: no manifest.json", err=True)
        return None
    with open(manifest_path) as f:
        manifest = json.load(f)

    old_count = len(manifest.get('clusters', []))
    new_clusters = cluster_photos(manifest['photos'], cluster_radius)
    manifest['clusters'] = new_clusters
    manifest.setdefault('clustering', {})
    manifest['clustering']['radius_meters'] = cluster_radius

    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    return old_count, len(new_clusters)


@click.command()
@click.option('--trip', required=True,
              help='Trip slug (e.g. 2024-kyrgyzstan) or "all"')
@click.option('--cluster-radius', default=DEFAULT_CLUSTER_RADIUS, type=float,
              help=f'Cluster radius in meters (default: {DEFAULT_CLUSTER_RADIUS})')
def main(trip: str, cluster_radius: float):
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

    click.echo(f"Reclustering {len(trip_dirs)} trip(s) at radius={cluster_radius}m\n")
    for d in trip_dirs:
        result = recluster_trip(d, cluster_radius)
        if result:
            old, new = result
            click.echo(f"  {d.name:<35}  {old:>5} -> {new:>5} clusters")
    click.echo("\nDone.")


if __name__ == '__main__':
    main()
