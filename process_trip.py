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
import re
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
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif'}
DEFAULT_CLUSTER_RADIUS = 1000  # meters — merges shots from the same village/viewpoint into one marker

# Compression defaults — both dimensions cap the LONGER side, so portrait
# drone shots (4536x8064) get treated correctly. WebP Q90 keeps fine detail
# (snow texture, rock edges) sharp; lower qualities over-smooth.
DEFAULT_THUMBNAIL_LONGEST = 400
DEFAULT_DISPLAY_LONGEST = 2160
DEFAULT_FORMAT = 'webp'  # 'webp' or 'jpeg'
DEFAULT_QUALITY = 90

FORMAT_TO_EXT = {'jpeg': 'jpg', 'webp': 'webp'}
FORMAT_TO_PIL = {'jpeg': 'JPEG', 'webp': 'WEBP'}

PROJECT_ROOT = Path(__file__).parent.resolve()
DEFAULT_HOSTED_PHOTOS_DIR = PROJECT_ROOT / 'hosted-photos'
DEFAULT_TRIPS_DIR = PROJECT_ROOT / 'web' / 'trips'


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


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


def _simplify_coords(coords: list, tolerance: float) -> list:
    """Douglas-Peucker simplification. Tolerance is in degrees (~0.00001 ≈ 1m)."""
    if len(coords) <= 2:
        return coords

    def perp_dist(point, start, end):
        if start == end:
            return ((point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2) ** 0.5
        dx, dy = end[0] - start[0], end[1] - start[1]
        norm = (dx * dx + dy * dy) ** 0.5
        return abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0]) / norm

    dmax, idx = 0.0, 0
    for i in range(1, len(coords) - 1):
        d = perp_dist(coords[i], coords[0], coords[-1])
        if d > dmax:
            dmax, idx = d, i

    if dmax > tolerance:
        left = _simplify_coords(coords[:idx + 1], tolerance)
        right = _simplify_coords(coords[idx:], tolerance)
        return left[:-1] + right
    return [coords[0], coords[-1]]


def gpx_to_geojson(gpx_path: Path, split_gap_km: float = 5.0,
                   simplify_tolerance: float = 0.0001) -> dict:
    """
    Convert GPX file to GeoJSON for web display.

    Splits into multiple LineString features whenever consecutive trackpoints
    are more than split_gap_km apart — avoids drawing huge teleport lines
    across the map (e.g. between days/flights or GPS dropouts).

    simplify_tolerance: Douglas-Peucker tolerance in degrees (default 0.0001 ≈ 10m).
    Reduces file size by ~95% with no visible difference at map zoom levels.
    Pass 0 to disable.
    """
    with open(gpx_path, 'r') as f:
        gpx = gpxpy.parse(f)

    features = []
    for track in gpx.tracks:
        track_name = track.name or 'Track'
        sub_segments: list[list[list[float]]] = [[]]
        for segment in track.segments:
            for point in segment.points:
                if sub_segments[-1]:
                    last_lon, last_lat = sub_segments[-1][-1]
                    dist_m = haversine_distance(last_lat, last_lon,
                                                point.latitude, point.longitude)
                    if dist_m / 1000.0 > split_gap_km:
                        sub_segments.append([])
                sub_segments[-1].append([point.longitude, point.latitude])

        sub_segments = [s for s in sub_segments if len(s) > 1]
        for i, coords in enumerate(sub_segments):
            if simplify_tolerance > 0:
                coords = _simplify_coords(coords, simplify_tolerance)
            name = track_name if len(sub_segments) == 1 else f"{track_name} (seg {i+1})"
            features.append({
                'type': 'Feature',
                'properties': {'name': name},
                'geometry': {'type': 'LineString', 'coordinates': coords},
            })

    return {'type': 'FeatureCollection', 'features': features}


SKIP_SUBDIRS = {'compressed', 'phone', 'videos'}


def find_photos(photo_dir: Path) -> list[Path]:
    """
    Recursively find supported photo files, skipping known non-edit subdirs
    (Compressed/ duplicates the parent, Phone/ and Videos/ aren't relevant).
    """
    photos = []
    for ext in SUPPORTED_EXTENSIONS:
        for pattern in (f'*{ext}', f'*{ext.upper()}'):
            for p in photo_dir.rglob(pattern):
                rel_parts = p.relative_to(photo_dir).parts[:-1]
                if any(part.lower() in SKIP_SUBDIRS for part in rel_parts):
                    continue
                photos.append(p)
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


def interpolate_gps(trackpoints: list[dict], photo_time: datetime,
                    max_time_delta_seconds: float = 7200,
                    max_interp_gap_seconds: float = 7200) -> Optional[dict]:
    """
    Find GPS coordinates for a photo timestamp by interpolating between trackpoints.

    Returns None when:
      - the photo is more than max_time_delta_seconds outside the GPX window, OR
      - the two bracketing trackpoints are more than max_interp_gap_seconds apart
        in time (recording was paused — interpolating across the gap would draw a
        straight line through places the photographer never went).
    In both cases the caller's fallback chain handles placement instead.
    """
    if not trackpoints:
        return None

    if photo_time.tzinfo is None:
        photo_time = photo_time.replace(tzinfo=timezone.utc)

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

    if prev_point and next_point:
        prev_time = prev_point['time']
        next_time = next_point['time']
        if prev_time.tzinfo is None:
            prev_time = prev_time.replace(tzinfo=timezone.utc)
        if next_time.tzinfo is None:
            next_time = next_time.replace(tzinfo=timezone.utc)
        total_delta = (next_time - prev_time).total_seconds()
        # Recording gap — don't interpolate across it
        if total_delta > max_interp_gap_seconds:
            return None
        photo_delta = (photo_time - prev_time).total_seconds()
        factor = photo_delta / total_delta if total_delta > 0 else 0
        lat = prev_point['lat'] + factor * (next_point['lat'] - prev_point['lat'])
        lon = prev_point['lon'] + factor * (next_point['lon'] - prev_point['lon'])
        return {'lat': lat, 'lon': lon}

    # Photo falls outside the GPX window. Only return a point if we're within
    # the tolerance — otherwise we'd pin it to a wildly wrong location.
    endpoint = prev_point or next_point
    if endpoint is None:
        return None
    endpoint_time = endpoint['time']
    if endpoint_time.tzinfo is None:
        endpoint_time = endpoint_time.replace(tzinfo=timezone.utc)
    if abs((photo_time - endpoint_time).total_seconds()) > max_time_delta_seconds:
        return None
    return {'lat': endpoint['lat'], 'lon': endpoint['lon']}


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


def _save_options(format_name: str, quality: int) -> dict:
    if format_name == 'jpeg':
        return {'quality': quality, 'optimize': True, 'progressive': True}
    if format_name == 'webp':
        return {'quality': quality, 'method': 6}
    raise ValueError(f"Unsupported format: {format_name}")


def _fit_longest(img: Image.Image, max_long: int) -> Image.Image:
    """Resize so the longer side is at most max_long, preserving aspect ratio."""
    longest = max(img.width, img.height)
    if longest <= max_long:
        return img
    ratio = max_long / longest
    new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def generate_thumbnail(photo_path: Path, output_path: Path,
                       max_long: int, format_name: str, quality: int) -> bool:
    """Generate thumbnail (longer side capped at max_long, preserves aspect ratio)."""
    try:
        with Image.open(photo_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img = _fit_longest(img, max_long)
            img.save(output_path, FORMAT_TO_PIL[format_name],
                     **_save_options(format_name, quality))
        return True
    except Exception as e:
        click.echo(f"Warning: Could not create thumbnail for {photo_path}: {e}", err=True)
        return False


def generate_display_image(photo_path: Path, output_path: Path,
                           max_long: int, format_name: str, quality: int) -> bool:
    """Generate display image (longer side capped at max_long, preserves aspect ratio)."""
    try:
        with Image.open(photo_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img = _fit_longest(img, max_long)
            img.save(output_path, FORMAT_TO_PIL[format_name],
                     **_save_options(format_name, quality))
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
                lat, lon = float(lat), float(lon)
                # Reject (0,0) "Null Island" — the drone's no-fix sentinel, not a real location
                if abs(lat) < 0.001 and abs(lon) < 0.001:
                    return None
                return {'lat': lat, 'lon': lon}
    except Exception:
        pass
    return None


def get_exif_datetime_via_exiftool(photo_path: Path) -> Optional[datetime]:
    """Read DateTimeOriginal via exiftool — works on RAW (.ARW/.DNG) where PIL fails."""
    try:
        cmd = ['exiftool', '-s', '-DateTimeOriginal', '-json', str(photo_path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)[0]
            dt_str = data.get('DateTimeOriginal')
            if dt_str:
                # Strip any timezone suffix like "+00:00" if present
                dt_str = dt_str.split('+')[0].split('Z')[0].strip()
                return datetime.strptime(dt_str, '%Y:%m:%d %H:%M:%S')
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
            <div id="trip-info" class="trip-info">
                <h1 id="trip-name">Loading...</h1>
                <p id="trip-dates"></p>
                <p id="photo-count"></p>
            </div>
            <ul class="nav-list" id="nav-list"></ul>
        </nav>
        <button class="sidebar-toggle" id="sidebar-toggle">
            <span></span><span></span><span></span>
        </button>
        <main class="map-container">
            <div id="map"></div>
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
            <div id="trip-info" class="trip-info">
                <h1 id="trip-name">Loading...</h1>
                <p id="trip-dates"></p>
                <p id="photo-count"></p>
            </div>
            <ul class="nav-list" id="nav-list"></ul>
        </nav>
        <button class="sidebar-toggle" id="sidebar-toggle">
            <span></span><span></span><span></span>
        </button>
        <main class="map-container">
            <div id="map"></div>
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
@click.option('--output', default=None, type=click.Path(),
              help='Metadata output dir (default: web/trips/<slug>)')
@click.option('--hosted-photos-dir', default=None, type=click.Path(),
              help='Root for compressed image storage (default: <project>/hosted-photos)')
@click.option('--geosync', default='', help='Timezone offset for camera sync (e.g., +02:00)')
@click.option('--gpx-tolerance-hours', default=2.0, type=float,
              help='Photos this far outside the GPX window get fallback placement (default: 2)')
@click.option('--gpx-split-gap-km', default=5.0, type=float,
              help='Split route into separate lines when consecutive trackpoints are >X km apart (default: 5)')
@click.option('--max-interp-gap-hours', default=2.0, type=float,
              help="Don't interpolate across GPX recording gaps longer than this; snap to nearest trackpoint by time instead (default: 2)")
@click.option('--filter-by-raws-in', 'filter_by_raws_in', default=None, type=click.Path(exists=True, path_type=Path),
              help='Only process edited photos whose stem exists somewhere under this folder. '
                   'Used to scope an Edits folder that bundles multiple trips.')
@click.option('--fallback-location', default=None, metavar='LAT,LON',
              help='Lat,lon to pin photos with no GPS that are outside the GPX window. '
                   'Defaults to nearest-by-time placed photo (or GPX centroid if none in range). '
                   'Pass "none" to drop them instead.')
@click.option('--nearest-photo-max-hours', default=6.0, type=float,
              help='Max time gap (h) when using nearest-by-time fallback. Beyond this, fall back to centroid (default: 6)')
@click.option('--cluster-radius', default=DEFAULT_CLUSTER_RADIUS, help='Clustering radius in meters')
@click.option('--raws', type=click.Path(exists=True), help='Path to original DNG files for DJI drone GPS data')
@click.option('--format', 'format_name', default=DEFAULT_FORMAT,
              type=click.Choice(['webp', 'jpeg'], case_sensitive=False),
              help=f'Image format for thumbnails/display (default: {DEFAULT_FORMAT})')
@click.option('--quality', default=DEFAULT_QUALITY, type=click.IntRange(1, 100),
              help=f'JPEG/WebP encoder quality 1-100 (default: {DEFAULT_QUALITY})')
@click.option('--display-longest', default=DEFAULT_DISPLAY_LONGEST, type=int,
              help=f'Max length of longer side for display images (default: {DEFAULT_DISPLAY_LONGEST}px)')
@click.option('--thumbnail-longest', default=DEFAULT_THUMBNAIL_LONGEST, type=int,
              help=f'Max length of longer side for thumbnails (default: {DEFAULT_THUMBNAIL_LONGEST}px)')
@click.option('--skip-existing-images', is_flag=True,
              help='Reuse already-generated thumbnails/display images. Only recomputes GPS placement, clusters, and manifest. Fast re-run after logic changes.')
@click.option('--test-mode', type=int, metavar='PERCENT', help='Test mode: process only X% of photos (e.g., 10 for 10%)')
@click.option('--dry-run', is_flag=True, help='Preview without writing files')
def process_trip(name: str, gpx: str, photos: str, output: Optional[str],
                 hosted_photos_dir: Optional[str],
                 geosync: str, gpx_tolerance_hours: float, gpx_split_gap_km: float,
                 max_interp_gap_hours: float,
                 filter_by_raws_in: Optional[Path],
                 fallback_location: Optional[str], nearest_photo_max_hours: float,
                 cluster_radius: float, raws: str,
                 format_name: str, quality: int, display_longest: int, thumbnail_longest: int,
                 skip_existing_images: bool, test_mode: int, dry_run: bool):
    """
    Process trip photos and generate web-ready output.

    Compressed thumbnails/display images go to hosted-photos/<slug>/ (gitignored).
    Metadata (manifest.json, route.geojson) goes to web/trips/<slug>/.
    Symlinks at web/trips/<slug>/{thumbnails,display} point to hosted-photos/<slug>/.
    """
    gpx_path = Path(gpx)
    photos_path = Path(photos)

    slug = slugify(name)
    if output:
        output_path = Path(output)
    else:
        output_path = DEFAULT_TRIPS_DIR / slug
    trip_id = output_path.name

    hosted_root = Path(hosted_photos_dir) if hosted_photos_dir else DEFAULT_HOSTED_PHOTOS_DIR
    hosted_photos_path = hosted_root / trip_id

    format_name = format_name.lower()
    image_ext = FORMAT_TO_EXT[format_name]

    click.echo(f"Processing trip: {name}")
    click.echo(f"GPX file: {gpx_path}")
    click.echo(f"Photos directory: {photos_path}")
    click.echo(f"Output directory: {output_path}")
    click.echo(f"Hosted photos directory: {hosted_photos_path}")
    click.echo(f"Image format: {format_name.upper()} q{quality}, "
               f"display longest≤{display_longest}px, thumb longest≤{thumbnail_longest}px")

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

    # Optionally filter by stems present somewhere under a raws root, and build
    # a stem→raw-path index so we can re-read DateTimeOriginal from the raw
    # (Lightroom JPG exports sometimes carry corrupted dates).
    raw_index: dict = {}
    if filter_by_raws_in:
        click.echo(f"Filtering by raws under: {filter_by_raws_in}")
        for ext in SUPPORTED_EXTENSIONS | {'.arw', '.dng', '.cr2', '.nef', '.raf'}:
            for pat in (f'*{ext}', f'*{ext.upper()}'):
                for p in Path(filter_by_raws_in).rglob(pat):
                    raw_index.setdefault(p.stem, p)
        before = len(photo_files)
        photo_files = [p for p in photo_files if p.stem in raw_index]
        click.echo(f"  Kept {len(photo_files)} of {before} photos that have a matching raw")
        click.echo(f"  Will read DateTimeOriginal from the raw file when available")

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
        hosted_photos_path.mkdir(parents=True, exist_ok=True)
        (hosted_photos_path / 'thumbnails').mkdir(exist_ok=True)
        (hosted_photos_path / 'display').mkdir(exist_ok=True)

        # Symlink web/trips/<slug>/{thumbnails,display} -> hosted-photos/<slug>/...
        for sub in ('thumbnails', 'display'):
            link = output_path / sub
            target = hosted_photos_path / sub
            if link.is_symlink() or link.exists():
                if link.is_symlink() or link.is_file():
                    link.unlink()
                else:
                    shutil.rmtree(link)
            link.symlink_to(os.path.relpath(target, output_path))

    # Process photos
    click.echo("\nProcessing photos...")
    processed_photos = []
    failed_photos = []
    skipped_records: list = []  # structured records persisted to manifest
    gps_source_counts: dict = {}
    gpx_tolerance_seconds = gpx_tolerance_hours * 3600

    # GPX window in UTC, used to compute "how far out of range" deltas
    _t0 = trackpoints[0]['time']
    _t1 = trackpoints[-1]['time']
    if _t0.tzinfo is None: _t0 = _t0.replace(tzinfo=timezone.utc)
    if _t1.tzinfo is None: _t1 = _t1.replace(tzinfo=timezone.utc)
    gpx_window_start, gpx_window_end = _t0, _t1

    # Decide fallback location for photos with no GPS + outside GPX window.
    # Default chain: nearest placed photo → nearest GPX endpoint → centroid.
    # User can override with an explicit lat,lon, or disable with "none".
    fallback_gps: Optional[dict] = None
    fallback_source = None
    if fallback_location and fallback_location.lower() == 'none':
        pass  # explicit opt-out → drop them
    elif fallback_location:
        try:
            lat_s, lon_s = fallback_location.split(',')
            fallback_gps = {'lat': float(lat_s.strip()), 'lon': float(lon_s.strip())}
            fallback_source = 'cli'
        except ValueError:
            click.echo(f"Error: --fallback-location must be 'lat,lon' or 'none'", err=True)
            sys.exit(1)
    else:
        # Sentinel — actual placement chosen per-photo in second pass.
        fallback_gps = {
            'lat': sum(p['lat'] for p in trackpoints) / len(trackpoints),
            'lon': sum(p['lon'] for p in trackpoints) / len(trackpoints),
        }
        fallback_source = 'default_chain'
    if fallback_source == 'default_chain':
        click.echo(f"Fallback chain: nearest placed photo (≤{nearest_photo_max_hours}h) "
                   f"→ nearest GPX trackpoint by time → GPX centroid")
    elif fallback_gps:
        click.echo(f"Fallback location ({fallback_source}): "
                   f"{fallback_gps['lat']:.4f}, {fallback_gps['lon']:.4f}")
    else:
        click.echo("Fallback location: disabled (--fallback-location=none)")

    for photo_file in tqdm(photo_files, desc="Processing"):
        # Get photo timestamp — prefer the raw file's timestamp when available,
        # since edited JPGs sometimes carry corrupted DateTimeOriginal.
        raw_match = raw_index.get(photo_file.stem)
        photo_time = None
        if raw_match is not None:
            photo_time = get_exif_datetime_via_exiftool(raw_match)
        if photo_time is None:
            photo_time = get_exif_datetime(photo_file)

        if not photo_time:
            failed_photos.append((photo_file, "No EXIF timestamp"))
            skipped_records.append({
                'id': photo_file.stem,
                'source_filename': photo_file.name,
                'reason': 'no_exif_timestamp',
            })
            continue

        # Apply timezone offset
        if geosync:
            photo_time = apply_timezone_offset(photo_time, geosync)

        # Determine GPS source — priority: EXIF on the JPG, then DJI DNG, then GPX.
        gps = get_exif_gps(photo_file)
        gps_source = 'exif' if gps else None

        if not gps and raws and photo_file.stem.startswith('DJI_'):
            raw_path = find_dji_raw(photo_file, Path(raws))
            if raw_path:
                gps = get_exif_gps(raw_path)
                if gps:
                    gps_source = 'dng'

        if not gps:
            gps = interpolate_gps(trackpoints, photo_time, gpx_tolerance_seconds,
                                  max_interp_gap_hours * 3600)
            if gps:
                gps_source = 'gpx'

        # Compute how far out of the GPX window the photo is (for diagnostics either way)
        pt = photo_time if photo_time.tzinfo else photo_time.replace(tzinfo=timezone.utc)
        if pt < gpx_window_start:
            delta_h = (gpx_window_start - pt).total_seconds() / 3600
            direction = 'before'
        elif pt > gpx_window_end:
            delta_h = (pt - gpx_window_end).total_seconds() / 3600
            direction = 'after'
        else:
            delta_h = 0.0
            direction = 'inside'

        placement = 'exact'
        pending_fallback = False
        if not gps:
            if fallback_gps is None:
                failed_photos.append((photo_file, "No EXIF GPS and outside GPX window (fallback disabled)"))
                skipped_records.append({
                    'id': photo_file.stem,
                    'source_filename': photo_file.name,
                    'timestamp': pt.isoformat(),
                    'reason': 'no_gps_outside_gpx_window',
                    'hours_outside_window': round(delta_h, 2),
                    'direction': direction,
                    'placement': 'dropped',
                })
                continue
            # Defer to second pass — placeholder coords, real ones filled below
            gps = {'lat': fallback_gps['lat'], 'lon': fallback_gps['lon']}
            gps_source = 'pending_fallback'
            placement = 'approximate'
            pending_fallback = True
            skipped_records.append({
                'id': photo_file.stem,
                'source_filename': photo_file.name,
                'timestamp': pt.isoformat(),
                'reason': 'no_gps_outside_gpx_window',
                'hours_outside_window': round(delta_h, 2),
                'direction': direction,
                'placement': 'approximate',
            })

        gps_source_counts[gps_source] = gps_source_counts.get(gps_source, 0) + 1

        # Generate photo ID
        photo_id = photo_file.stem

        # Generate resized images (unless reusing existing ones)
        if not dry_run:
            thumb_path = hosted_photos_path / 'thumbnails' / f'{photo_id}.{image_ext}'
            display_path = hosted_photos_path / 'display' / f'{photo_id}.{image_ext}'
            if not (skip_existing_images and thumb_path.exists() and display_path.exists()):
                generate_thumbnail(photo_file, thumb_path, thumbnail_longest, format_name, quality)
                generate_display_image(photo_file, display_path, display_longest, format_name, quality)

        # Get camera settings
        camera_settings = get_camera_settings(photo_file)

        # Add to processed list
        processed_photos.append({
            'id': photo_id,
            'source_filename': photo_file.name,
            'lat': gps['lat'],
            'lon': gps['lon'],
            'timestamp': photo_time.isoformat() + 'Z',
            'placement': placement,
            'gps_source': gps_source,
            'thumbnail': f'thumbnails/{photo_id}.{image_ext}',
            'display': f'display/{photo_id}.{image_ext}',
            'camera_settings': camera_settings
        })

    # Build a time-sorted index of GPX trackpoints for "nearest trackpoint by time"
    tp_times = []
    tp_coords = []
    for p in trackpoints:  # already sorted by time
        t = p['time']
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        tp_times.append(t)
        tp_coords.append((p['lat'], p['lon']))

    def nearest_trackpoint_by_time(pt):
        import bisect
        idx = bisect.bisect_left(tp_times, pt)
        best = None
        for cand in (idx, idx - 1):
            if 0 <= cand < len(tp_times):
                gap = abs((tp_times[cand] - pt).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, tp_coords[cand])
        return best[1] if best else None

    # Second pass: resolve "pending_fallback" photos using nearest placed photo by time
    placed_photos = [p for p in processed_photos if p['gps_source'] != 'pending_fallback']
    if placed_photos:
        placed_by_time = sorted(placed_photos,
                                key=lambda p: datetime.fromisoformat(p['timestamp'].replace('Z','+00:00')))
        placed_times = [datetime.fromisoformat(p['timestamp'].replace('Z','+00:00')) for p in placed_by_time]
        import bisect
        nearest_max_seconds = nearest_photo_max_hours * 3600
        nearest_count = centroid_count = 0
        skipped_by_id = {s['id']: s for s in skipped_records}
        for p in processed_photos:
            if p['gps_source'] != 'pending_fallback':
                continue
            pt = datetime.fromisoformat(p['timestamp'].replace('Z','+00:00'))
            idx = bisect.bisect_left(placed_times, pt)
            candidates = []
            if idx < len(placed_times):
                candidates.append((abs((placed_times[idx] - pt).total_seconds()), placed_by_time[idx]))
            if idx > 0:
                candidates.append((abs((placed_times[idx-1] - pt).total_seconds()), placed_by_time[idx-1]))
            candidates.sort()
            if candidates and candidates[0][0] <= nearest_max_seconds:
                gap_s, neighbor = candidates[0]
                p['lat'] = neighbor['lat']
                p['lon'] = neighbor['lon']
                p['gps_source'] = 'nearest_photo'
                sk = skipped_by_id.get(p['id'])
                if sk is not None:
                    sk['placed_at'] = {'lat': neighbor['lat'], 'lon': neighbor['lon']}
                    sk['fallback_source'] = 'nearest_photo'
                    sk['nearest_photo_id'] = neighbor['id']
                    sk['nearest_photo_gap_minutes'] = round(gap_s/60, 1)
                nearest_count += 1
            elif fallback_source == 'default_chain':
                # No close-enough photo → snap to nearest GPX trackpoint by time.
                # Handles recording gaps: a photo taken after GPS stopped snaps to
                # the last known position, not a straight-line interpolation.
                near = nearest_trackpoint_by_time(pt)
                if near is not None:
                    p['lat'], p['lon'] = near
                    p['gps_source'] = 'gpx_nearest_time'
                    sk = skipped_by_id.get(p['id'])
                    if sk is not None:
                        sk['placed_at'] = {'lat': near[0], 'lon': near[1]}
                        sk['fallback_source'] = 'gpx_nearest_time'
                else:
                    p['lat'] = fallback_gps['lat']
                    p['lon'] = fallback_gps['lon']
                    p['gps_source'] = 'fallback_centroid'
                    centroid_count += 1
            else:
                # Explicit --fallback-location was given → use it
                p['lat'] = fallback_gps['lat']
                p['lon'] = fallback_gps['lon']
                p['gps_source'] = 'fallback_centroid'
                sk = skipped_by_id.get(p['id'])
                if sk is not None:
                    sk['placed_at'] = dict(fallback_gps)
                    sk['fallback_source'] = 'centroid'
                centroid_count += 1
        gps_source_counts.pop('pending_fallback', None)
        if nearest_count: gps_source_counts['nearest_photo'] = nearest_count
        if centroid_count: gps_source_counts['fallback_centroid'] = centroid_count
        gpx_nearest_count = sum(1 for p in processed_photos if p['gps_source'] == 'gpx_nearest_time')
        if gpx_nearest_count: gps_source_counts['gpx_nearest_time'] = gpx_nearest_count
    else:
        # No placed photos at all — every pending becomes centroid
        for p in processed_photos:
            if p['gps_source'] == 'pending_fallback':
                p['gps_source'] = 'fallback_centroid'
        if gps_source_counts.get('pending_fallback'):
            gps_source_counts['fallback_centroid'] = gps_source_counts.pop('pending_fallback')

    # Report results
    click.echo(f"\nProcessed: {len(processed_photos)} photos")
    if gps_source_counts:
        breakdown = ', '.join(f'{src}={n}' for src, n in sorted(gps_source_counts.items()))
        click.echo(f"GPS sources: {breakdown}")
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
        'source': {
            'photos_path': str(photos_path),
            'gpx_path': str(gpx_path),
        },
        'compression': {
            'format': format_name,
            'quality': quality,
            'display_longest': display_longest,
            'thumbnail_longest': thumbnail_longest,
        },
        'route': 'route.geojson',
        'photos': processed_photos,
        'clusters': clusters,
        'skipped': skipped_records,
    }

    if not dry_run:
        # Save manifest
        manifest_path = output_path / 'manifest.json'
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)
        click.echo(f"\nSaved manifest: {manifest_path}")

        # Convert GPX to GeoJSON (splits on big jumps to avoid teleport lines)
        geojson_data = gpx_to_geojson(gpx_path, split_gap_km=gpx_split_gap_km)
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
