#!/usr/bin/env python3
"""
Travel Photography Map - Photo Processing Script

Processes photos from a trip:
1. Parses GPX tracks to extract GPS coordinates with timestamps
2. Matches photo timestamps to GPS locations
3. Geotags photos using ExifTool
4. Generates compressed thumbnails and display images
5. Creates manifest.json with trip metadata and photo clusters
"""

import os
import sys
import json
import subprocess
import shutil
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from math import radians, sin, cos, sqrt, atan2
from typing import Optional

import click
import gpxpy
from PIL import Image
from PIL.ExifTags import TAGS
from tqdm import tqdm


# Configuration
THUMBNAIL_SIZE = (300, 300)
DISPLAY_WIDTH = 1920
JPEG_QUALITY = 85
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif'}
DEFAULT_CLUSTER_RADIUS = 50  # meters


def parse_gpx(gpx_path: Path) -> list[dict]:
    """
    Parse GPX file and extract trackpoints with timestamps.

    Returns list of dicts with keys: lat, lon, time (datetime)
    """
    with open(gpx_path, 'r') as f:
        gpx = gpxpy.parse(f)

    trackpoints = []

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                if point.time:
                    trackpoints.append({
                        'lat': point.latitude,
                        'lon': point.longitude,
                        'time': point.time,
                        'elevation': point.elevation
                    })

    # Sort by time
    trackpoints.sort(key=lambda p: p['time'])

    return trackpoints


def gpx_to_geojson(gpx_path: Path) -> dict:
    """
    Convert GPX file to GeoJSON format for web display.
    """
    with open(gpx_path, 'r') as f:
        gpx = gpxpy.parse(f)

    features = []

    for track in gpx.tracks:
        coordinates = []
        for segment in track.segments:
            for point in segment.points:
                coordinates.append([point.longitude, point.latitude])

        if coordinates:
            features.append({
                'type': 'Feature',
                'properties': {
                    'name': track.name or 'Track'
                },
                'geometry': {
                    'type': 'LineString',
                    'coordinates': coordinates
                }
            })

    return {
        'type': 'FeatureCollection',
        'features': features
    }


def find_photos(photo_dir: Path) -> list[Path]:
    """
    Recursively find all supported photo files in directory.
    """
    photos = []

    for ext in SUPPORTED_EXTENSIONS:
        photos.extend(photo_dir.rglob(f'*{ext}'))
        photos.extend(photo_dir.rglob(f'*{ext.upper()}'))

    return sorted(set(photos))


def get_exif_datetime(photo_path: Path) -> Optional[datetime]:
    """
    Extract DateTimeOriginal from photo EXIF data.
    """
    try:
        with Image.open(photo_path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return None

            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTimeOriginal':
                    # Parse EXIF datetime format: "YYYY:MM:DD HH:MM:SS"
                    return datetime.strptime(value, '%Y:%m:%d %H:%M:%S')

    except Exception as e:
        click.echo(f"Warning: Could not read EXIF from {photo_path}: {e}", err=True)

    return None


def get_camera_settings(photo_path: Path) -> dict:
    """
    Extract camera settings from EXIF data.
    """
    settings = {}

    try:
        with Image.open(photo_path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return settings

            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)

                if tag == 'ISOSpeedRatings':
                    settings['iso'] = value
                elif tag == 'FNumber':
                    if hasattr(value, 'numerator'):
                        settings['aperture'] = f"f/{float(value):.1f}"
                    else:
                        settings['aperture'] = f"f/{value}"
                elif tag == 'ExposureTime':
                    if hasattr(value, 'numerator') and value.numerator == 1:
                        settings['shutter'] = f"1/{value.denominator}"
                    else:
                        settings['shutter'] = str(value)

    except Exception:
        pass

    return settings


def interpolate_gps(trackpoints: list[dict], photo_time: datetime) -> Optional[dict]:
    """
    Find GPS coordinates for a photo timestamp by interpolating between trackpoints.
    """
    if not trackpoints:
        return None

    # Make photo_time timezone-aware if it isn't
    if photo_time.tzinfo is None:
        photo_time = photo_time.replace(tzinfo=timezone.utc)

    # Find surrounding trackpoints
    prev_point = None
    next_point = None

    for point in trackpoints:
        point_time = point['time']
        if point_time.tzinfo is None:
            point_time = point_time.replace(tzinfo=timezone.utc)

        if point_time <= photo_time:
            prev_point = point
        elif next_point is None:
            next_point = point
            break

    # Return closest point or interpolate
    if prev_point and next_point:
        # Calculate interpolation factor
        prev_time = prev_point['time']
        next_time = next_point['time']
        if prev_time.tzinfo is None:
            prev_time = prev_time.replace(tzinfo=timezone.utc)
        if next_time.tzinfo is None:
            next_time = next_time.replace(tzinfo=timezone.utc)

        total_delta = (next_time - prev_time).total_seconds()
        photo_delta = (photo_time - prev_time).total_seconds()

        if total_delta > 0:
            factor = photo_delta / total_delta
        else:
            factor = 0

        # Linear interpolation
        lat = prev_point['lat'] + factor * (next_point['lat'] - prev_point['lat'])
        lon = prev_point['lon'] + factor * (next_point['lon'] - prev_point['lon'])

        return {'lat': lat, 'lon': lon}

    elif prev_point:
        return {'lat': prev_point['lat'], 'lon': prev_point['lon']}

    elif next_point:
        return {'lat': next_point['lat'], 'lon': next_point['lon']}

    return None


def geotag_photo(photo_path: Path, lat: float, lon: float) -> bool:
    """
    Write GPS coordinates to photo EXIF using ExifTool.
    """
    try:
        cmd = [
            'exiftool',
            '-overwrite_original',
            f'-GPSLatitude={lat}',
            f'-GPSLongitude={lon}',
            '-GPSLatitudeRef=N' if lat >= 0 else '-GPSLatitudeRef=S',
            '-GPSLongitudeRef=E' if lon >= 0 else '-GPSLongitudeRef=W',
            str(photo_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0

    except FileNotFoundError:
        click.echo("Error: exiftool not found. Install with: brew install exiftool", err=True)
        sys.exit(1)


def generate_thumbnail(photo_path: Path, output_path: Path) -> bool:
    """
    Generate thumbnail image (preserves aspect ratio, max 300px width).
    """
    try:
        with Image.open(photo_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            # Calculate new size maintaining aspect ratio
            ratio = min(THUMBNAIL_SIZE[0] / img.width, THUMBNAIL_SIZE[1] / img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))

            img.thumbnail(new_size, Image.Resampling.LANCZOS)
            img.save(output_path, 'JPEG', quality=JPEG_QUALITY, optimize=True)

        return True

    except Exception as e:
        click.echo(f"Warning: Could not create thumbnail for {photo_path}: {e}", err=True)
        return False


def generate_display_image(photo_path: Path, output_path: Path) -> bool:
    """
    Generate display-sized image (1920px width, preserves aspect ratio).
    """
    try:
        with Image.open(photo_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            # Only resize if larger than display width
            if img.width > DISPLAY_WIDTH:
                ratio = DISPLAY_WIDTH / img.width
                new_size = (DISPLAY_WIDTH, int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            img.save(output_path, 'JPEG', quality=JPEG_QUALITY, optimize=True)

        return True

    except Exception as e:
        click.echo(f"Warning: Could not create display image for {photo_path}: {e}", err=True)
        return False


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance between two GPS coordinates in meters.
    """
    R = 6371000  # Earth's radius in meters

    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))

    return R * c


def cluster_photos(photos: list[dict], radius: float) -> list[dict]:
    """
    Group photos into clusters based on proximity.
    """
    if not photos:
        return []

    # Sort by timestamp
    photos_sorted = sorted(photos, key=lambda p: p['timestamp'])

    clusters = []
    used = set()

    for i, photo in enumerate(photos_sorted):
        if photo['id'] in used:
            continue

        # Start new cluster
        cluster_photos = [photo]
        used.add(photo['id'])

        # Find nearby photos
        for j, other in enumerate(photos_sorted):
            if other['id'] in used:
                continue

            dist = haversine_distance(photo['lat'], photo['lon'], other['lat'], other['lon'])
            if dist <= radius:
                cluster_photos.append(other)
                used.add(other['id'])

        # Calculate cluster center
        avg_lat = sum(p['lat'] for p in cluster_photos) / len(cluster_photos)
        avg_lon = sum(p['lon'] for p in cluster_photos) / len(cluster_photos)

        clusters.append({
            'location': f'Location {len(clusters) + 1}',
            'lat': avg_lat,
            'lon': avg_lon,
            'photo_ids': [p['id'] for p in cluster_photos]
        })

    return clusters


def apply_timezone_offset(dt: datetime, offset_str: str) -> datetime:
    """
    Apply timezone offset to datetime.
    offset_str format: "+HH:MM" or "-HH:MM" or just "+HH" or "-HH"
    """
    if not offset_str:
        return dt

    sign = 1 if offset_str[0] == '+' else -1
    parts = offset_str[1:].split(':')
    hours = int(parts[0])
    minutes = int(parts[1]) if len(parts) > 1 else 0

    delta = timedelta(hours=hours, minutes=minutes)
    return dt + (sign * delta)


def get_exif_gps(photo_path: Path) -> Optional[dict]:
    """
    Extract GPS coordinates from photo EXIF using exiftool.
    More reliable for DNG files than Pillow.
    """
    try:
        cmd = ['exiftool', '-n', '-json', '-GPSLatitude', '-GPSLongitude', str(photo_path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)[0]
            lat = data.get('GPSLatitude')
            lon = data.get('GPSLongitude')
            if lat is not None and lon is not None:
                return {'lat': float(lat), 'lon': float(lon)}
    except Exception:
        pass
    return None


def find_dji_raw(photo_path: Path, raws_dir: Path) -> Optional[Path]:
    """
    Find matching DNG file for a DJI photo in raws directory.
    """
    if not photo_path.stem.startswith('DJI_'):
        return None

    # Check lowercase .dng
    dng_path = raws_dir / f'{photo_path.stem}.dng'
    if dng_path.exists():
        return dng_path

    # Check uppercase .DNG
    dng_path_upper = raws_dir / f'{photo_path.stem}.DNG'
    if dng_path_upper.exists():
        return dng_path_upper

    return None


def update_trips_index(output_path: Path, trip_name: str, dates: dict, photo_count: int):
    """
    Update the trips index file with the new trip.
    """
    # Path to web/trips/index.json
    web_dir = output_path.parent.parent
    index_path = web_dir / 'trips' / 'index.json'

    # Get trip folder name from output path
    trip_id = output_path.name

    # Extract year from start date
    year = int(dates['start'][:4])

    # Load existing index or create new one
    if index_path.exists():
        with open(index_path, 'r') as f:
            index = json.load(f)
    else:
        index = {'trips': []}

    # Remove existing entry for this trip if it exists
    index['trips'] = [t for t in index['trips'] if t.get('id') != trip_id]

    # Add new trip entry
    index['trips'].append({
        'id': trip_id,
        'name': trip_name,
        'year': year,
        'dates': dates,
        'photo_count': photo_count,
        'path': f'trips/{trip_id}'
    })

    # Sort by start date (most recent first)
    index['trips'].sort(key=lambda t: t['dates']['start'], reverse=True)

    # Save updated index
    with open(index_path, 'w') as f:
        json.dump(index, f, indent=2)

    return index_path


def generate_html_pages(output_path: Path, trip_name: str, trip_id: str, year: int):
    """
    Generate HTML pages for year and trip views.
    """
    web_dir = output_path.parent.parent

    # Create trip slug (remove year suffix if present)
    trip_slug = trip_id
    if trip_slug.endswith(f'-{year}'):
        trip_slug = trip_slug[:-5]

    # Year page directory
    year_dir = web_dir / str(year)
    year_dir.mkdir(exist_ok=True)

    # Trip page directory
    trip_dir = year_dir / trip_slug
    trip_dir.mkdir(exist_ok=True)

    # Generate year index.html if it doesn't exist or update it
    year_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{year} - Travel Photography Map</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.css"/>
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.Default.css"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/glightbox@3.2.0/dist/css/glightbox.min.css"/>
    <link rel="stylesheet" href="../css/styles.css"/>
</head>
<body>
    <div class="app-container">
        <nav class="sidebar" id="sidebar">
            <div class="sidebar-header">
                <h2>Travel Maps</h2>
            </div>
            <ul class="nav-list" id="nav-list"></ul>
        </nav>
        <button class="sidebar-toggle" id="sidebar-toggle">
            <span></span><span></span><span></span>
        </button>
        <main class="map-container">
            <div id="map"></div>
            <div id="trip-info" class="trip-info">
                <h1 id="trip-name">Loading...</h1>
                <p id="trip-dates"></p>
                <p id="photo-count"></p>
            </div>
            <button id="exif-toggle" class="exif-toggle" title="Toggle EXIF info"><span>i</span></button>
            <div id="gallery" class="gallery-hidden"></div>
        </main>
    </div>
    <script>
        const VIEW_CONFIG = {{
            mode: 'year',
            year: {year},
            tripId: null,
            basePath: '../'
        }};
    </script>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
    <script src="https://unpkg.com/leaflet.markercluster@1.4.1/dist/leaflet.markercluster.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/glightbox@3.2.0/dist/js/glightbox.min.js"></script>
    <script src="../js/sidebar.js"></script>
    <script src="../js/app.js"></script>
</body>
</html>
'''

    year_index_path = year_dir / 'index.html'
    with open(year_index_path, 'w') as f:
        f.write(year_html)

    # Generate trip index.html
    trip_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{trip_name} - Travel Photography Map</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.css"/>
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.Default.css"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/glightbox@3.2.0/dist/css/glightbox.min.css"/>
    <link rel="stylesheet" href="../../css/styles.css"/>
</head>
<body>
    <div class="app-container">
        <nav class="sidebar" id="sidebar">
            <div class="sidebar-header">
                <h2>Travel Maps</h2>
            </div>
            <ul class="nav-list" id="nav-list"></ul>
        </nav>
        <button class="sidebar-toggle" id="sidebar-toggle">
            <span></span><span></span><span></span>
        </button>
        <main class="map-container">
            <div id="map"></div>
            <div id="trip-info" class="trip-info">
                <h1 id="trip-name">Loading...</h1>
                <p id="trip-dates"></p>
                <p id="photo-count"></p>
            </div>
            <button id="exif-toggle" class="exif-toggle" title="Toggle EXIF info"><span>i</span></button>
            <div id="gallery" class="gallery-hidden"></div>
        </main>
    </div>
    <script>
        const VIEW_CONFIG = {{
            mode: 'trip',
            year: {year},
            tripId: '{trip_id}',
            basePath: '../../'
        }};
    </script>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
    <script src="https://unpkg.com/leaflet.markercluster@1.4.1/dist/leaflet.markercluster.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/glightbox@3.2.0/dist/js/glightbox.min.js"></script>
    <script src="../../js/sidebar.js"></script>
    <script src="../../js/app.js"></script>
</body>
</html>
'''

    trip_index_path = trip_dir / 'index.html'
    with open(trip_index_path, 'w') as f:
        f.write(trip_html)

    return year_index_path, trip_index_path


@click.command()
@click.option('--name', required=True, help='Trip name for display')
@click.option('--gpx', required=True, type=click.Path(exists=True), help='Path to GPX file')
@click.option('--photos', required=True, type=click.Path(exists=True), help='Path to photos directory')
@click.option('--output', required=True, type=click.Path(), help='Output directory')
@click.option('--geosync', default='', help='Timezone offset for camera sync (e.g., +02:00)')
@click.option('--cluster-radius', default=DEFAULT_CLUSTER_RADIUS, help='Clustering radius in meters')
@click.option('--raws', type=click.Path(exists=True), help='Path to original DNG files for DJI drone GPS data')
@click.option('--test-mode', type=int, metavar='PERCENT', help='Test mode: process only X% of photos (e.g., 10 for 10%)')
@click.option('--dry-run', is_flag=True, help='Preview without writing files')
def process_trip(name: str, gpx: str, photos: str, output: str,
                 geosync: str, cluster_radius: float, raws: str, test_mode: int, dry_run: bool):
    """
    Process trip photos and generate web-ready output.
    """
    gpx_path = Path(gpx)
    photos_path = Path(photos)
    output_path = Path(output)

    click.echo(f"Processing trip: {name}")
    click.echo(f"GPX file: {gpx_path}")
    click.echo(f"Photos directory: {photos_path}")
    click.echo(f"Output directory: {output_path}")

    if geosync:
        click.echo(f"Timezone offset: {geosync}")

    if raws:
        click.echo(f"DJI raws directory: {raws}")

    # Parse GPX
    click.echo("\nParsing GPX file...")
    trackpoints = parse_gpx(gpx_path)
    click.echo(f"Found {len(trackpoints)} trackpoints")

    if not trackpoints:
        click.echo("Error: No trackpoints found in GPX file", err=True)
        sys.exit(1)

    # Get trip date range
    trip_start = trackpoints[0]['time'].date().isoformat()
    trip_end = trackpoints[-1]['time'].date().isoformat()

    # Find photos
    click.echo("\nFinding photos...")
    photo_files = find_photos(photos_path)
    click.echo(f"Found {len(photo_files)} photos")

    if not photo_files:
        click.echo("Error: No photos found in directory", err=True)
        sys.exit(1)

    # Apply test mode sampling if enabled
    if test_mode:
        if test_mode < 1 or test_mode > 100:
            click.echo("Error: --test-mode must be between 1 and 100", err=True)
            sys.exit(1)

        original_count = len(photo_files)
        sample_size = max(1, int(len(photo_files) * test_mode / 100))
        photo_files = random.sample(photo_files, sample_size)
        click.echo(f"[TEST MODE] Sampling {test_mode}% of photos: {len(photo_files)}/{original_count}")

    # Create output directories
    if not dry_run:
        output_path.mkdir(parents=True, exist_ok=True)
        (output_path / 'thumbnails').mkdir(exist_ok=True)
        (output_path / 'display').mkdir(exist_ok=True)

    # Process photos
    click.echo("\nProcessing photos...")
    processed_photos = []
    failed_photos = []

    for photo_file in tqdm(photo_files, desc="Processing"):
        # Get photo timestamp
        photo_time = get_exif_datetime(photo_file)

        if not photo_time:
            failed_photos.append((photo_file, "No EXIF timestamp"))
            continue

        # Apply timezone offset
        if geosync:
            photo_time = apply_timezone_offset(photo_time, geosync)

        # Determine GPS source
        gps = None

        # For DJI photos with --raws provided, try to get GPS from original DNG
        if raws and photo_file.stem.startswith('DJI_'):
            raw_path = find_dji_raw(photo_file, Path(raws))
            if raw_path:
                gps = get_exif_gps(raw_path)
                if not gps:
                    click.echo(f"Warning: No GPS in DNG for {photo_file.name}", err=True)

        # Fallback to GPX interpolation for non-DJI or if DNG GPS failed
        if not gps:
            gps = interpolate_gps(trackpoints, photo_time)

        if not gps:
            failed_photos.append((photo_file, "No GPS data available"))
            continue

        # Generate photo ID
        photo_id = photo_file.stem

        # Geotag photo (on copy)
        if not dry_run:
            # Generate thumbnail
            thumb_path = output_path / 'thumbnails' / f'{photo_id}.jpg'
            generate_thumbnail(photo_file, thumb_path)

            # Generate display image
            display_path = output_path / 'display' / f'{photo_id}.jpg'
            generate_display_image(photo_file, display_path)

        # Get camera settings
        camera_settings = get_camera_settings(photo_file)

        # Add to processed list
        processed_photos.append({
            'id': photo_id,
            'lat': gps['lat'],
            'lon': gps['lon'],
            'timestamp': photo_time.isoformat() + 'Z',
            'thumbnail': f'thumbnails/{photo_id}.jpg',
            'display': f'display/{photo_id}.jpg',
            'camera_settings': camera_settings
        })

    # Report results
    click.echo(f"\nProcessed: {len(processed_photos)} photos")
    click.echo(f"Failed: {len(failed_photos)} photos")

    if failed_photos:
        click.echo("\nFailed photos:")
        for photo, reason in failed_photos[:10]:
            click.echo(f"  - {photo.name}: {reason}")
        if len(failed_photos) > 10:
            click.echo(f"  ... and {len(failed_photos) - 10} more")

    # Generate clusters
    click.echo("\nGenerating clusters...")
    clusters = cluster_photos(processed_photos, cluster_radius)
    click.echo(f"Created {len(clusters)} clusters")

    # Generate manifest
    manifest = {
        'trip_name': name,
        'dates': {
            'start': trip_start,
            'end': trip_end
        },
        'route': 'route.geojson',
        'photos': processed_photos,
        'clusters': clusters
    }

    if not dry_run:
        # Save manifest
        manifest_path = output_path / 'manifest.json'
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)
        click.echo(f"\nSaved manifest: {manifest_path}")

        # Convert GPX to GeoJSON
        geojson_data = gpx_to_geojson(gpx_path)
        geojson_path = output_path / 'route.geojson'
        with open(geojson_path, 'w') as f:
            json.dump(geojson_data, f, indent=2)
        click.echo(f"Saved route: {geojson_path}")

        # Update trips index
        index_path = update_trips_index(
            output_path,
            name,
            manifest['dates'],
            len(processed_photos)
        )
        click.echo(f"Updated trips index: {index_path}")

        # Generate year and trip HTML pages
        year = int(manifest['dates']['start'][:4])
        trip_id = output_path.name
        year_page, trip_page = generate_html_pages(output_path, name, trip_id, year)
        click.echo(f"Generated year page: {year_page}")
        click.echo(f"Generated trip page: {trip_page}")

    click.echo("\nDone!")

    if test_mode:
        click.echo(f"\n[TEST MODE - processed only {test_mode}% of photos]")

    if dry_run:
        click.echo("\n[DRY RUN - no files were written]")


if __name__ == '__main__':
    process_trip()
