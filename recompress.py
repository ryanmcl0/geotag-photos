#!/usr/bin/env python3
"""
Re-encode the thumbnail + display images for one or all already-processed trips,
using new format / quality / dimension settings. Skips GPX matching and clustering —
just regenerates the bytes in hosted-photos/<slug>/ from the originals.

Usage:
  recompress.py --trip 2024-kyrgyzstan --quality 92
  recompress.py --trip all --format jpeg --quality 92
  recompress.py --trip 2024-kyrgyzstan --photos "/new/path/to/originals"
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import click
from tqdm import tqdm

from process_trip import (
    DEFAULT_DISPLAY_LONGEST,
    DEFAULT_FORMAT,
    DEFAULT_QUALITY,
    DEFAULT_THUMBNAIL_LONGEST,
    DEFAULT_TRIPS_DIR,
    DEFAULT_HOSTED_PHOTOS_DIR,
    FORMAT_TO_EXT,
    SUPPORTED_EXTENSIONS,
    generate_display_image,
    generate_thumbnail,
)


def find_source_file(photos_root: Path, photo: dict) -> Optional[Path]:
    """Locate the original source file for a photo entry.

    Prefers explicit `source_filename` (newer manifests). Falls back to
    rglob by `id` (matches any supported ext) for older manifests.
    """
    name = photo.get('source_filename')
    if name:
        candidate = photos_root / name
        if candidate.exists():
            return candidate
        for hit in photos_root.rglob(name):
            return hit
    pid = photo['id']
    for ext in SUPPORTED_EXTENSIONS:
        for cand in (photos_root.rglob(f'{pid}{ext}'),
                     photos_root.rglob(f'{pid}{ext.upper()}')):
            for hit in cand:
                return hit
    return None


def recompress_trip(trip_dir: Path, *, photos_override: Optional[Path],
                    hosted_photos_root: Path, format_name: str, quality: int,
                    display_longest: int, thumbnail_longest: int,
                    keep_old_files: bool) -> bool:
    manifest_path = trip_dir / 'manifest.json'
    if not manifest_path.exists():
        click.echo(f"  Skipping {trip_dir.name}: no manifest.json", err=True)
        return False

    with open(manifest_path) as f:
        manifest = json.load(f)

    photos_root = photos_override or Path(manifest.get('source', {}).get('photos_path', ''))
    if not photos_root or not photos_root.exists():
        click.echo(f"  Skipping {trip_dir.name}: source photos dir not found "
                   f"({photos_root or '<unknown>'}). Pass --photos to override.", err=True)
        return False

    image_ext = FORMAT_TO_EXT[format_name]
    hosted = hosted_photos_root / trip_dir.name
    thumbs_dir = hosted / 'thumbnails'
    display_dir = hosted / 'display'
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    display_dir.mkdir(parents=True, exist_ok=True)

    # Wipe old encoded images if format changed (or if requested)
    old_format = manifest.get('compression', {}).get('format')
    if old_format and old_format != format_name and not keep_old_files:
        for d in (thumbs_dir, display_dir):
            for f in d.iterdir():
                if f.is_file():
                    f.unlink()

    click.echo(f"\n[{trip_dir.name}] source: {photos_root}")
    click.echo(f"  -> {format_name.upper()} q{quality}, "
               f"display ≤{display_longest}px, thumb ≤{thumbnail_longest}px")

    missing, ok = [], 0
    for photo in tqdm(manifest['photos'], desc=trip_dir.name):
        src = find_source_file(photos_root, photo)
        if not src:
            missing.append(photo['id'])
            continue
        pid = photo['id']
        thumb_path = thumbs_dir / f'{pid}.{image_ext}'
        display_path = display_dir / f'{pid}.{image_ext}'
        if generate_thumbnail(src, thumb_path, thumbnail_longest, format_name, quality) \
           and generate_display_image(src, display_path, display_longest, format_name, quality):
            photo['thumbnail'] = f'thumbnails/{pid}.{image_ext}'
            photo['display'] = f'display/{pid}.{image_ext}'
            ok += 1

    manifest['compression'] = {
        'format': format_name,
        'quality': quality,
        'display_longest': display_longest,
        'thumbnail_longest': thumbnail_longest,
    }
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    # Re-create symlinks under web/trips/<slug>/ (they may be stale or missing)
    import os
    for sub in ('thumbnails', 'display'):
        link = trip_dir / sub
        target = hosted / sub
        if link.is_symlink() or link.exists():
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link)
        link.symlink_to(os.path.relpath(target, trip_dir))

    click.echo(f"  Done: {ok}/{len(manifest['photos'])} re-encoded. "
               f"{len(missing)} missing source.")
    if missing[:5]:
        click.echo(f"  Missing examples: {', '.join(missing[:5])}"
                   + ("..." if len(missing) > 5 else ""))
    return True


@click.command()
@click.option('--trip', required=True,
              help='Trip slug (e.g. 2024-kyrgyzstan) or "all" for every trip in web/trips/')
@click.option('--photos', 'photos_override', default=None, type=click.Path(exists=True, path_type=Path),
              help='Override source photos dir (defaults to manifest.source.photos_path)')
@click.option('--hosted-photos-dir', default=None, type=click.Path(path_type=Path),
              help=f'Root for compressed image storage (default: <project>/hosted-photos)')
@click.option('--format', 'format_name', default=DEFAULT_FORMAT,
              type=click.Choice(['webp', 'jpeg'], case_sensitive=False))
@click.option('--quality', default=DEFAULT_QUALITY, type=click.IntRange(1, 100))
@click.option('--display-longest', default=DEFAULT_DISPLAY_LONGEST, type=int)
@click.option('--thumbnail-longest', default=DEFAULT_THUMBNAIL_LONGEST, type=int)
@click.option('--keep-old-files', is_flag=True,
              help='Do not delete old-format files when format changes')
def main(trip: str, photos_override: Optional[Path], hosted_photos_dir: Optional[Path],
         format_name: str, quality: int, display_longest: int, thumbnail_longest: int,
         keep_old_files: bool):
    format_name = format_name.lower()
    hosted_root = hosted_photos_dir or DEFAULT_HOSTED_PHOTOS_DIR

    if trip == 'all':
        trip_dirs = sorted([d for d in DEFAULT_TRIPS_DIR.iterdir()
                            if d.is_dir() and (d / 'manifest.json').exists()])
    else:
        trip_dirs = [DEFAULT_TRIPS_DIR / trip]
        if not trip_dirs[0].exists():
            click.echo(f"Error: trip not found: {trip_dirs[0]}", err=True)
            sys.exit(1)

    if not trip_dirs:
        click.echo("No trips found to recompress.")
        return

    click.echo(f"Recompressing {len(trip_dirs)} trip(s)...")
    success = 0
    for d in trip_dirs:
        if recompress_trip(d, photos_override=photos_override,
                           hosted_photos_root=hosted_root, format_name=format_name,
                           quality=quality, display_longest=display_longest,
                           thumbnail_longest=thumbnail_longest,
                           keep_old_files=keep_old_files):
            success += 1
    click.echo(f"\nDone. {success}/{len(trip_dirs)} trips re-encoded.")


if __name__ == '__main__':
    main()
