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


def parse_kmz_route(kmz_path: Path) -> list[dict]:
    """
    Extract ordered waypoints from a KMZ file as {lat, lon} dicts.
    Skips 'Directions from...' routing artefacts and deduplicates adjacent
    identical coordinates.
    """
    import zipfile
    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(kmz_path, 'r') as z:
        kml_names = [n for n in z.namelist() if n.lower().endswith('.kml')]
        if not kml_names:
            raise ValueError(f"No KML file found in {kmz_path.name}")
        content = z.open(kml_names[0]).read().decode('utf-8')

    # Strip XML namespaces for simpler iteration
    content = re.sub(r' xmlns[^=]*="[^"]*"', '', content)
    root = ET.fromstring(content)

    points = []
    for pm in root.iter('Placemark'):
        name = (pm.findtext('name') or '').strip()
        if name.lower().startswith('directions from'):
            continue
        coord_text = pm.findtext('.//coordinates') or ''
        tokens = coord_text.strip().split()
        for token in tokens:
            parts = token.split(',')
            if len(parts) >= 2:
                try:
                    lon, lat = float(parts[0]), float(parts[1])
                    # Skip if identical to previous point
                    if not points or (abs(lat - points[-1]['lat']) > 1e-6 or
                                      abs(lon - points[-1]['lon']) > 1e-6):
                        points.append({'lat': lat, 'lon': lon})
                except ValueError:
                    pass

    return points


# Raw-directory names that are containers, not building/location names.
# Used to find the building name by walking up from a raw file's path.
GENERIC_RAW_DIRS = {
    'pictures', 'photos', 'photo', 'videos', 'video', 'edits', 'edit',
    'compressed', 'raw', 'raws', 'jpg', 'jpeg', 'me', 'phone', 'tourism', 'dcim', 'misc',
}

# Camera-card dumps and backup folders that aren't real locations
# (e.g. "100MSDCF", "100CANON", "4", "Camera Backup", "Card Dump").
_GENERIC_DIR_RE = re.compile(
    r'^\d+$|^\d+(msdcf|canon|nikon|olymp|_pana|nz\d*)$|backup|^ryan cam|sd card'
    r'|^edits?\s*\d+$',  # "Edits1", "Edit 2" etc. — numbered edit subfolders, not locations
    re.I,
)


def _is_generic_component(comp: str) -> bool:
    c = comp.strip().lower()
    return c in GENERIC_RAW_DIRS or bool(_GENERIC_DIR_RE.search(c))


def building_from_raw(raw_path: Path, raw_root: Path) -> Optional[str]:
    """
    Derive a building/location name from a raw file's path relative to the
    trip's raw root: the deepest directory component that isn't a generic
    container (Pictures/Photos/Edits, camera-card dumps, backups, etc.).

    e.g. <root>/Location Name/Pictures/IMG_001.ARW -> "Location Name"
         <root>/Country/Location - Building/x.ARW -> "Location - Building"
    Returns None for flat folders or photos that live only under generic dirs.
    """
    try:
        rel = raw_path.relative_to(raw_root)
    except ValueError:
        return None
    for comp in reversed(rel.parts[:-1]):  # skip the filename itself
        if not _is_generic_component(comp):
            return comp
    return None


def load_locations_file(path: Path) -> dict:
    """
    Load a building-name -> {lat, lon} map. Keys are normalised (lowercased,
    stripped) for case-insensitive lookup. Entries missing coords are skipped.
    """
    with open(path) as f:
        data = json.load(f)
    out = {}
    for name, info in data.items():
        if isinstance(info, dict) and info.get('lat') is not None and info.get('lon') is not None:
            out[name.strip().lower()] = {'lat': float(info['lat']), 'lon': float(info['lon'])}
    return out


def parse_location_names(trip_name: str) -> list[str]:
    """Extract individual, country-qualified location names from a trip name.

    Trip names follow "Country - Region, Region, Country - Region, ..." where a
    bare comma segment (no ' - ') continues the most recently named country. Each
    region is returned qualified with its country (e.g. "Region, Country") so
    geocoding is unambiguous.
    """
    loc = re.sub(r'^\d{4}[:\d]*\s+', '', trip_name).strip()
    out = []
    current_country = None
    for seg in loc.split(','):
        seg = seg.strip()
        if not seg:
            continue
        if ' - ' in seg:
            country, region = seg.split(' - ', 1)
            current_country = country.strip()
            region = region.strip()
            out.append(f'{region}, {current_country}' if current_country else region)
        elif current_country:
            out.append(f'{seg}, {current_country}')
        else:
            out.append(seg)
    return out


def geocode_single(query: str) -> Optional[dict]:
    """Geocode one location string via Nominatim. Returns {lat, lon} or None."""
    import urllib.request
    import urllib.parse
    import time
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({
        'q': query, 'format': 'json', 'limit': 1
    })
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'geotag-photos/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read())
            if results:
                return {'lat': float(results[0]['lat']), 'lon': float(results[0]['lon'])}
    except Exception:
        pass
    time.sleep(1.1)
    return None


def geocode_from_name(trip_name: str) -> Optional[dict]:
    """Geocode all location parts of a trip name and return their centroid."""
    coords = [geocode_single(n) for n in parse_location_names(trip_name)[:5]]
    coords = [c for c in coords if c]
    if not coords:
        return None
    return {
        'lat': sum(c['lat'] for c in coords) / len(coords),
        'lon': sum(c['lon'] for c in coords) / len(coords),
    }


def apply_exif_interpolation(processed_photos: list[dict]) -> int:
    """
    For no-GPX trips that have some photos with EXIF GPS (e.g. drone shots),
    use those as time-anchored control points to interpolate approximate positions
    for the remaining fallback_centroid photos.

    Photos between two EXIF anchors get a linearly interpolated position.
    Photos before/after all anchors snap to the nearest anchor.

    Returns count of photos updated.
    """
    import bisect

    anchors = sorted(
        [p for p in processed_photos if p['gps_source'] == 'exif'],
        key=lambda p: p['timestamp']
    )
    if not anchors:
        return 0

    anchor_times = [
        datetime.fromisoformat(p['timestamp'].replace('Z', '+00:00'))
        for p in anchors
    ]

    updated = 0
    for photo in processed_photos:
        if photo['gps_source'] != 'fallback_centroid':
            continue

        pt = datetime.fromisoformat(photo['timestamp'].replace('Z', '+00:00'))
        idx = bisect.bisect_left(anchor_times, pt)

        if idx == 0:
            photo['lat'] = anchors[0]['lat']
            photo['lon'] = anchors[0]['lon']
        elif idx >= len(anchors):
            photo['lat'] = anchors[-1]['lat']
            photo['lon'] = anchors[-1]['lon']
        else:
            before, after = anchors[idx - 1], anchors[idx]
            t0, t1 = anchor_times[idx - 1], anchor_times[idx]
            span = (t1 - t0).total_seconds()
            f = (pt - t0).total_seconds() / span if span > 0 else 0
            photo['lat'] = before['lat'] + f * (after['lat'] - before['lat'])
            photo['lon'] = before['lon'] + f * (after['lon'] - before['lon'])

        photo['gps_source'] = 'interpolated_from_exif'
        photo['placement'] = 'approximate'
        updated += 1

    return updated


FAKE_ROUTE_CLOSE_KM = 500


def max_pairwise_distance_km(locations: list[dict]) -> float:
    """Maximum haversine distance (km) between any pair of geocoded locations."""
    max_dist = 0.0
    for i in range(len(locations)):
        for j in range(i + 1, len(locations)):
            d = haversine_distance(
                locations[i]['lat'], locations[i]['lon'],
                locations[j]['lat'], locations[j]['lon']
            ) / 1000
            max_dist = max(max_dist, d)
    return max_dist


def interpolate_along_line(locations: list[dict], fraction: float) -> dict:
    """Return lat/lon at `fraction` (0–1) along a polyline through locations."""
    if len(locations) == 1:
        return {'lat': locations[0]['lat'], 'lon': locations[0]['lon']}
    segments, total = [], 0.0
    for i in range(len(locations) - 1):
        d = haversine_distance(locations[i]['lat'], locations[i]['lon'],
                               locations[i + 1]['lat'], locations[i + 1]['lon'])
        segments.append(d)
        total += d
    if total == 0:
        return {'lat': locations[0]['lat'], 'lon': locations[0]['lon']}
    target, cumulative = fraction * total, 0.0
    for i, seg_len in enumerate(segments):
        if cumulative + seg_len >= target or i == len(segments) - 1:
            f = (target - cumulative) / seg_len if seg_len > 0 else 0
            return {
                'lat': locations[i]['lat'] + f * (locations[i + 1]['lat'] - locations[i]['lat']),
                'lon': locations[i]['lon'] + f * (locations[i + 1]['lon'] - locations[i]['lon']),
            }
        cumulative += seg_len
    return {'lat': locations[-1]['lat'], 'lon': locations[-1]['lon']}


def apply_fake_route(processed_photos: list[dict], trip_name: str,
                     explicit_locations: Optional[list[str]] = None) -> Optional[dict]:
    """
    For no-GPX trips with multiple locations in the name, assigns approximate
    coordinates to photos that have no real GPS and generates a fake route line.

    Close locations (< 500km): photos interpolated along the line by timestamp order.
    Distant locations: photos split by the N-1 largest time gaps and pinned per segment.

    Mutates processed_photos in place. Returns route GeoJSON or None if not applicable.
    """
    names = explicit_locations if explicit_locations else parse_location_names(trip_name)
    if len(names) < 2:
        return None

    click.echo(f"\nMultiple locations detected: {', '.join(names)}")
    click.echo("Geocoding for fake route...")
    locations = []
    for name in names[:6]:
        coords = geocode_single(name)
        if coords:
            locations.append({'name': name, **coords})
            click.echo(f"  {name} → {coords['lat']:.3f}, {coords['lon']:.3f}")

    if len(locations) < 2:
        return None

    eligible = [p for p in processed_photos
                if p['gps_source'] in ('fallback_centroid', 'pending_fallback', 'geocoded')]

    max_dist = max_pairwise_distance_km(locations)
    click.echo(f"Max span: {max_dist:.0f} km", nl=False)

    click.echo(" — ", nl=False)
    by_time = sorted(eligible, key=lambda p: p['timestamp']) if eligible else []

    if max_dist < FAKE_ROUTE_CLOSE_KM:
        # Close together: interpolate photos along route line and draw it
        click.echo("interpolating along route")
        n = len(by_time)
        for i, photo in enumerate(by_time):
            coords = interpolate_along_line(locations, i / (n - 1) if n > 1 else 0.5)
            photo['lat'], photo['lon'] = coords['lat'], coords['lon']
            photo['gps_source'] = 'fake_route_interpolated'
            photo['placement'] = 'approximate'
        line_coords = [[loc['lon'], loc['lat']] for loc in locations]
        return {
            'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'properties': {'name': 'Estimated route'},
                          'geometry': {'type': 'LineString', 'coordinates': line_coords}}]
        }
    else:
        # Far apart: pin photos per segment, no connecting line (would cross oceans/continents)
        n_locs = len(locations)
        click.echo(f"splitting into {n_locs} segments by time gaps (no route line)")
        if by_time:
            gaps = []
            for i in range(1, len(by_time)):
                t0 = datetime.fromisoformat(by_time[i - 1]['timestamp'].replace('Z', '+00:00'))
                t1 = datetime.fromisoformat(by_time[i]['timestamp'].replace('Z', '+00:00'))
                gaps.append(((t1 - t0).total_seconds(), i))
            gaps.sort(reverse=True)
            split_at = sorted(idx for _, idx in gaps[:n_locs - 1])

            segments, start = [], 0
            for split_i in split_at + [len(by_time)]:
                segments.append(by_time[start:split_i])
                start = split_i

            for seg_idx, (segment, loc) in enumerate(zip(segments, locations)):
                click.echo(f"  Segment {seg_idx + 1}/{n_locs}: {loc['name']} ({len(segment)} photos)")
                for photo in segment:
                    photo['lat'], photo['lon'] = loc['lat'], loc['lon']
                    photo['gps_source'] = 'fake_route_segment'
                    photo['placement'] = 'approximate'

        return {'type': 'FeatureCollection', 'features': []}


def get_countries_from_photos(photos: list[dict]) -> list[str]:
    """
    Reverse-geocode countries from placed photos. Prefers real GPS (EXIF/DNG)
    but falls back to building/approximate coords — all resolve to the right
    country even if the within-city position is only approximate.
    """
    try:
        import reverse_geocoder as rg
    except ImportError:
        return []
    pts = [(p['lat'], p['lon']) for p in photos
           if p.get('gps_source') in ('exif', 'dng')]
    if not pts:  # no real GPS — use whatever placement we have
        pts = [(p['lat'], p['lon']) for p in photos]
    if not pts:
        return []
    step = max(1, len(pts) // 20)
    results = rg.search(pts[::step][:20], verbose=False)
    return sorted(set(r['cc'] for r in results if r.get('cc')))


def get_countries_from_gpx(gpx_path: Path, n_samples: int = 20) -> list[str]:
    """Sample points along the GPX track and reverse-geocode to unique country names."""
    try:
        import reverse_geocoder as rg
    except ImportError:
        return []

    with open(gpx_path, 'r') as f:
        gpx = gpxpy.parse(f)

    all_points = [
        (pt.latitude, pt.longitude)
        for track in gpx.tracks
        for segment in track.segments
        for pt in segment.points
    ]
    if not all_points:
        return []

    # Sample evenly across the track
    step = max(1, len(all_points) // n_samples)
    samples = all_points[::step][:n_samples]

    results = rg.search(samples, verbose=False)
    countries = sorted(set(r['cc'] for r in results if r.get('cc')))
    return countries


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


def batch_read_exif(paths: list[Path]) -> dict[str, dict]:
    """Read DateTimeOriginal, GPS, and camera settings for many files in ONE
    exiftool invocation. Returns {str(path): {field: value, ...}}.

    Calling exiftool once per file has ~50-100ms startup overhead per call;
    batching 1000 photos saves several minutes on a typical trip reprocess.
    """
    if not paths:
        return {}
    try:
        cmd = [
            'exiftool', '-n', '-json', '-DateTimeOriginal',
            '-GPSLatitude', '-GPSLongitude',
            '-ISOSpeedRatings', '-FNumber', '-ExposureTime',
        ] + [str(p) for p in paths]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode not in (0, 1):  # 1 = some files had warnings, still ok
            return {}
        records = json.loads(result.stdout) if result.stdout.strip() else []
        return {r.get('SourceFile', ''): r for r in records}
    except Exception:
        return {}


def _parse_exif_datetime(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        dt_str = dt_str.split('+')[0].split('Z')[0].strip()
        return datetime.strptime(dt_str, '%Y:%m:%d %H:%M:%S')
    except Exception:
        return None


def _parse_exif_gps(record: dict) -> Optional[dict]:
    lat = record.get('GPSLatitude')
    lon = record.get('GPSLongitude')
    if lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    if abs(lat) < 0.001 and abs(lon) < 0.001:
        return None
    return {'lat': lat, 'lon': lon}


def _parse_camera_settings(record: dict) -> dict:
    settings = {}
    iso = record.get('ISOSpeedRatings')
    if iso is not None:
        settings['iso'] = iso
    fn = record.get('FNumber')
    if fn is not None:
        settings['aperture'] = f"f/{float(fn):.1f}"
    et = record.get('ExposureTime')
    if et is not None:
        et = float(et)
        if et > 0 and et < 1:
            settings['shutter'] = f"1/{round(1/et)}"
        else:
            settings['shutter'] = str(et)
    return settings


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


def interpolate_gps_clamped(trackpoints: list[dict], photo_time: datetime) -> Optional[dict]:
    """
    Like interpolate_gps but NEVER returns None: interpolates across recording gaps
    of any size and clamps to the first/last trackpoint when the photo is outside
    the track's time window. Used to force 'route' photos onto the GPX track.
    """
    if not trackpoints:
        return None
    if photo_time.tzinfo is None:
        photo_time = photo_time.replace(tzinfo=timezone.utc)

    prev_point = next_point = None
    for point in trackpoints:
        pt = point['time'] if point['time'].tzinfo else point['time'].replace(tzinfo=timezone.utc)
        if pt <= photo_time:
            prev_point = point
        elif next_point is None:
            next_point = point
            break

    if prev_point and next_point:
        t0 = prev_point['time'] if prev_point['time'].tzinfo else prev_point['time'].replace(tzinfo=timezone.utc)
        t1 = next_point['time'] if next_point['time'].tzinfo else next_point['time'].replace(tzinfo=timezone.utc)
        span = (t1 - t0).total_seconds()
        # Clamp to nearest endpoint across large gaps rather than interpolating to a
        # wrong intermediate position (e.g. a 7-day gap where the route is unknown).
        _MAX_INTERP_GAP = 6 * 3600  # 6 hours — beyond this, snap to nearest endpoint
        if span > _MAX_INTERP_GAP:
            gap_to_prev = (photo_time - t0).total_seconds()
            gap_to_next = (t1 - photo_time).total_seconds()
            ep = prev_point if gap_to_prev <= gap_to_next else next_point
            return {'lat': ep['lat'], 'lon': ep['lon']}
        f = (photo_time - t0).total_seconds() / span if span > 0 else 0
        return {'lat': prev_point['lat'] + f * (next_point['lat'] - prev_point['lat']),
                'lon': prev_point['lon'] + f * (next_point['lon'] - prev_point['lon'])}
    ep = prev_point or next_point
    return {'lat': ep['lat'], 'lon': ep['lon']}


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


def cluster_anchor_point(cluster_photos: list[dict]) -> tuple[float, float]:
    """
    Return an actual photo coordinate for the marker anchor.

    Averaging works for tight GPS clusters, but for approximate/fallback groups it
    can create a synthetic point between real places. A medoid keeps the marker
    pinned to one of the concrete coordinates produced by the placement pipeline.
    """
    if len(cluster_photos) == 1:
        photo = cluster_photos[0]
        return photo['lat'], photo['lon']

    # If a fallback/building/city placement produced identical coordinates for a
    # whole group, keep that exact coordinate instead of doing floating math.
    coord_counts: dict[tuple[float, float], int] = {}
    for photo in cluster_photos:
        key = (round(photo['lat'], 7), round(photo['lon'], 7))
        coord_counts[key] = coord_counts.get(key, 0) + 1
    most_common_key, most_common_count = max(coord_counts.items(), key=lambda item: item[1])
    if most_common_count > 1:
        for photo in cluster_photos:
            if (round(photo['lat'], 7), round(photo['lon'], 7)) == most_common_key:
                return photo['lat'], photo['lon']

    def total_distance(photo: dict) -> float:
        return sum(
            haversine_distance(photo['lat'], photo['lon'], other['lat'], other['lon'])
            for other in cluster_photos
        )

    anchor = min(cluster_photos, key=total_distance)
    return anchor['lat'], anchor['lon']


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

        anchor_lat, anchor_lon = cluster_anchor_point(cluster_photos)

        # Name the cluster after the most common building among its photos, if any.
        # Left as None here when no building is known — filled with a city name
        # (or a generic label) by name_unlabeled_clusters() below.
        building_names = [p['building'] for p in cluster_photos if p.get('building')]
        location = max(set(building_names), key=building_names.count) if building_names else None

        clusters.append({
            'location': location,
            'lat': anchor_lat,
            'lon': anchor_lon,
            'photo_ids': [p['id'] for p in cluster_photos]
        })

    name_unlabeled_clusters(clusters)
    return clusters


def name_unlabeled_clusters(clusters: list[dict]) -> None:
    """
    Fill in cluster names that have no building label, in place. Falls back to the
    nearest city via offline reverse-geocoding (no network), then to a generic
    "Location N" label if even that is unavailable.
    """
    unlabeled = [c for c in clusters if not c.get('location')]
    cities: dict = {}
    if unlabeled:
        try:
            import reverse_geocoder as rg
            results = rg.search([(c['lat'], c['lon']) for c in unlabeled], verbose=False)
            for c, r in zip(unlabeled, results):
                name = (r.get('name') or '').strip()
                if name:
                    cities[id(c)] = name
        except Exception:
            pass

    counter = 0
    for c in clusters:
        if c.get('location'):
            continue
        city = cities.get(id(c))
        if city:
            c['location'] = city
        else:
            counter += 1
            c['location'] = f'Location {counter}'


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
    Find the matching original DJI file for a DJI edit, to read embedded GPS.

    The drone writes GPS into both its DNG and JPG originals; the JPG edit
    exported from Lightroom usually has it stripped. Some drones shoot DNG,
    others (Mini/Air) only JPG, so match both. Searches recursively because
    drone originals often live in a 'Drone/' subfolder of the raws tree.
    GPS-bearing extensions are preferred over JPG.
    """
    if not photo_path.stem.startswith('DJI_'):
        return None

    stem = photo_path.stem
    # Flat check first (fast path), preferring DNG over JPG.
    for ext in ('.dng', '.DNG', '.jpg', '.JPG', '.jpeg', '.JPEG'):
        cand = raws_dir / f'{stem}{ext}'
        if cand.exists():
            return cand

    # Recursive fallback — drone originals commonly sit under a subfolder.
    matches = []
    for ext in ('.dng', '.DNG', '.jpg', '.JPG', '.jpeg', '.JPEG'):
        matches.extend(raws_dir.rglob(f'{stem}{ext}'))
    if matches:
        rank = {'.dng': 0, '.jpeg': 1, '.jpg': 1}
        matches.sort(key=lambda p: rank.get(p.suffix.lower(), 2))
        return matches[0]

    return None


def update_trips_index(output_path: Path, trip_name: str, dates: dict, photo_count: int, countries: list = None):
    """
    Update the trips index file with the new trip.
    """
    # Path to web/trips/index.json
    web_dir = output_path.parent.parent
    index_path = web_dir / 'trips' / 'index.json'

    # Get trip folder name from output path
    trip_id = output_path.name

    # Prefer year from trip name (immune to corrupted EXIF dates)
    name_year = re.match(r'^(\d{4})', trip_name)
    year = int(name_year.group(1)) if name_year else int(dates['start'][:4])

    # Load existing index or create new one
    if index_path.exists():
        with open(index_path, 'r') as f:
            index = json.load(f)
    else:
        index = {'trips': []}

    # Preserve fields set externally (e.g. public/private flag stamped by deploy.py)
    # before replacing the entry so a reprocess never wipes them.
    existing = {t['id']: t for t in index['trips']}
    index['trips'] = [t for t in index['trips'] if t.get('id') != trip_id]

    entry = {
        'id': trip_id,
        'name': trip_name,
        'year': year,
        'dates': dates,
        'photo_count': photo_count,
        'path': f'trips/{trip_id}'
    }
    if countries:
        entry['countries'] = countries
    # Carry over any extra fields from the previous entry (public flag, etc.)
    for k, v in existing.get(trip_id, {}).items():
        if k not in entry:
            entry[k] = v
    index['trips'].append(entry)

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
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe.min.css"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/default-skin/default-skin.min.css"/>
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
            <div class="pswp" tabindex="-1" role="dialog" aria-hidden="true">
                <div class="pswp__bg"></div>
                <div class="pswp__scroll-wrap">
                    <div class="pswp__container">
                        <div class="pswp__item"></div>
                        <div class="pswp__item"></div>
                        <div class="pswp__item"></div>
                    </div>
                    <div class="pswp__ui pswp__ui--hidden">
                        <div class="pswp__top-bar">
                            <div class="pswp__counter"></div>
                            <button class="pswp__button pswp__button--close" title="Close (Esc)"></button>
                            <button class="pswp__button pswp__button--zoom" title="Zoom in/out"></button>
                            <div class="pswp__preloader"><div class="pswp__preloader__icn"><div class="pswp__preloader__cut"><div class="pswp__preloader__donut"></div></div></div></div>
                        </div>
                        <button class="pswp__button pswp__button--arrow--left" title="Previous (arrow left)"></button>
                        <button class="pswp__button pswp__button--arrow--right" title="Next (arrow right)"></button>
                        <div class="pswp__caption"><div class="pswp__caption__center"></div></div>
                    </div>
                </div>
            </div>
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
    <script src="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe-ui-default.min.js"></script>
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
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe.min.css"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/default-skin/default-skin.min.css"/>
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
            <div class="pswp" tabindex="-1" role="dialog" aria-hidden="true">
                <div class="pswp__bg"></div>
                <div class="pswp__scroll-wrap">
                    <div class="pswp__container">
                        <div class="pswp__item"></div>
                        <div class="pswp__item"></div>
                        <div class="pswp__item"></div>
                    </div>
                    <div class="pswp__ui pswp__ui--hidden">
                        <div class="pswp__top-bar">
                            <div class="pswp__counter"></div>
                            <button class="pswp__button pswp__button--close" title="Close (Esc)"></button>
                            <button class="pswp__button pswp__button--zoom" title="Zoom in/out"></button>
                            <div class="pswp__preloader"><div class="pswp__preloader__icn"><div class="pswp__preloader__cut"><div class="pswp__preloader__donut"></div></div></div></div>
                        </div>
                        <button class="pswp__button pswp__button--arrow--left" title="Previous (arrow left)"></button>
                        <button class="pswp__button pswp__button--arrow--right" title="Next (arrow right)"></button>
                        <div class="pswp__caption"><div class="pswp__caption__center"></div></div>
                    </div>
                </div>
            </div>
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
    <script src="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe-ui-default.min.js"></script>
    <script src="../../js/sidebar.js"></script>
    <script src="../../js/app.js"></script>
</body>
</html>
'''

    trip_index_path = trip_dir / 'index.html'
    with open(trip_index_path, 'w') as f:
        f.write(trip_html)

    return year_index_path, trip_index_path


def write_private_trip(off_photos, base_output_path, base_hosted_path, image_ext,
                       cluster_radius, format_name, quality, display_longest, thumbnail_longest,
                       base_name, photos_path):
    """
    Write off-route photos as a separate '<slug>-private' trip. Moves their
    compressed images out of the public hosted dir into the private one (so the
    public R2 prefix never holds them), then builds manifest/clusters/index/pages.
    Returns (private_slug, n_photos, n_clusters, countries) or None.
    """
    if not off_photos:
        return None
    private_slug = base_output_path.name + '-private'
    private_name = base_name + ' — private'
    priv_output = base_output_path.parent / private_slug
    priv_hosted = base_hosted_path.parent / private_slug
    priv_output.mkdir(parents=True, exist_ok=True)
    (priv_hosted / 'thumbnails').mkdir(parents=True, exist_ok=True)
    (priv_hosted / 'display').mkdir(parents=True, exist_ok=True)

    for sub in ('thumbnails', 'display'):
        link = priv_output / sub
        target = priv_hosted / sub
        if link.is_symlink() or link.exists():
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link)
        link.symlink_to(os.path.relpath(target, priv_output))

    # Move images from the public hosted dir into the private one
    for p in off_photos:
        for sub in ('thumbnails', 'display'):
            src = base_hosted_path / sub / f"{p['id']}.{image_ext}"
            dst = priv_hosted / sub / f"{p['id']}.{image_ext}"
            if src.exists():
                shutil.move(str(src), str(dst))

    clusters = cluster_photos(off_photos, cluster_radius)
    countries = get_countries_from_photos(off_photos)
    ts = sorted(p['timestamp'] for p in off_photos)
    dates = {'start': ts[0][:10], 'end': ts[-1][:10]}
    manifest = {
        'trip_name': private_name,
        'dates': dates,
        'countries': countries,
        'source': {'photos_path': str(photos_path), 'gpx_path': None},
        'compression': {'format': format_name, 'quality': quality,
                        'display_longest': display_longest, 'thumbnail_longest': thumbnail_longest},
        'route': 'route.geojson',
        'photos': off_photos,
        'clusters': clusters,
        'skipped': [],
    }
    (priv_output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    (priv_output / 'route.geojson').write_text(
        json.dumps({'type': 'FeatureCollection', 'features': []}, indent=2))
    update_trips_index(priv_output, private_name, dates, len(off_photos), countries=countries)
    m = re.match(r'^(\d{4})', private_name)
    year = int(m.group(1)) if m else int(dates['start'][:4])
    generate_html_pages(priv_output, private_name, private_slug, year)
    return private_slug, len(off_photos), len(clusters), countries


@click.command()
@click.option('--name', required=True, help='Trip name for display')
@click.option('--gpx', required=False, default=None, type=click.Path(), help='Path to GPX file (omit for no-GPX trips — uses EXIF GPS + geocoded fallback)')
@click.option('--kmz', 'kmz_path_str', default=None, type=click.Path(), help='KMZ/KML route file — used for route display and photo placement in no-GPX mode')
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
@click.option('--exclude-raws-in', 'exclude_raws_in', default=None, type=click.Path(exists=True, path_type=Path),
              help='Drop edited photos whose stem exists somewhere under this folder. '
                   'Inverse of --filter-by-raws-in; carves a region out of a bundled Edits folder '
                   '(e.g. exclude one trip\'s raws from a bundle sharing one Edits dir).')
@click.option('--raws-root', 'raws_root', default=None, type=click.Path(exists=True, path_type=Path),
              help='Index raw files under this folder (stem→path) for building-name lookup and '
                   'accurate timestamps, WITHOUT filtering out edits that have no matching raw. '
                   'The raw directory names become building/location names via --locations-file.')
@click.option('--locations-file', 'locations_file', default=None, type=click.Path(exists=True, path_type=Path),
              help='JSON map of building/location name → {lat, lon}. Photos with no EXIF GPS are '
                   'placed at their raw-folder building coords when found here (gps_source=building).')
@click.option('--dump-buildings', is_flag=True,
              help='Discovery mode: list the building/location names derived from --raws-root for '
                   'the edited photos, with photo counts, then exit without processing.')
@click.option('--exclude-buildings', 'exclude_buildings', default=None, metavar='NAME,NAME,...',
              help='Comma-separated building names to drop entirely (photos are not placed or included).')
@click.option('--exclude-edits-under', 'exclude_edits_under', default=None, metavar='NAME,NAME,...',
              help='Comma-separated edit-folder names; drop any edit whose path contains one as a '
                   'component. Carves region subfolders out of a multi-region edits bundle.')
@click.option('--only-edits-dirs', 'only_edits_dirs', is_flag=True,
              help='Keep only photos that live under a folder literally named "Edits". Used for the '
                   'pre-2019 in-tree backfill, where the edits sit in <year>/<Building>/Edits/ '
                   'alongside raws/camera-card dumps that must NOT be picked up.')
@click.option('--split-offroute-private', is_flag=True,
              help='Split a GPX trip in two: photos placed by the GPX track (on-route) stay in this '
                   'trip (public); all other photos (off-route building/drone/fallback shots) are '
                   'written to a separate "<slug>-private" trip at building level.')
@click.option('--private-cluster-radius', default=150, type=float,
              help='Cluster radius (m) for the off-route private split (default: 150, building-level)')
@click.option('--gpx-route-subdir', default=None,
              help='Raw-path folder name marking on-route photos (e.g. "Route Folder"). These are forced '
                   'onto the GPX track (real GPS kept, else interpolated/clamped) and treated as public '
                   'in --split-offroute-private, regardless of GPS-recording gaps.')
@click.option('--route-snap-public-hours', default=3.0, type=float,
              help='In --split-offroute-private, route-snapped (gpx_nearest_time) photos within this '
                   'many hours of the track count as public/on-route (default: 3)')
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
@click.option('--fake-route-locations', default=None, metavar='LOC1,LOC2,...',
              help='Explicitly set location names for fake route (overrides name parsing). E.g. "Location1,Location2"')
@click.option('--no-fake-route', 'no_fake_route', is_flag=True,
              help='Disable the no-GPX fake-route geocoding (Pass 4b). Photos without a building/EXIF '
                   'match stay at the fallback location instead of being pinned to geocoded trip-name '
                   'places. Use for building-specific trips where trip-name geocoding misfires.')
@click.option('--strict-building-distance', 'strict_building_distance', is_flag=True,
              help='Discard a building-coord match that is >2000km from the trip fallback location '
                   '(treats it as a wrong-city collision for generic hotel-chain names). OFF by default '
                   'so multi-country trips keep their legitimately-distant building matches. Enable only '
                   'for single-region trips that suffer generic-name collisions.')
@click.option('--skip-existing-images', is_flag=True,
              help='Reuse already-generated thumbnails/display images. Only recomputes GPS placement, clusters, and manifest. Fast re-run after logic changes.')
@click.option('--update', 'update', is_flag=True,
              help='Incremental update: reprocess only the delta vs the last run. Re-encodes/re-reads '
                   'EXIF for NEW or CHANGED (mtime/size) source edits, reuses everything else, and '
                   'deletes orphaned hosted images for edits that were removed. On the first run for an '
                   'existing trip (no source_state.json) it ADOPTS the current artifacts as baseline '
                   '(only encodes anything actually missing). Tracks state in <output>/source_state.json.')
@click.option('--reindex', 'reindex', is_flag=True,
              help='Write/refresh <output>/source_state.json from the current sources WITHOUT '
                   'reprocessing (adopt-baseline only; encodes only missing images). Use to stamp a '
                   'baseline on already-processed trips so a later --update detects real deltas.')
@click.option('--test-mode', type=int, metavar='PERCENT', help='Test mode: process only X% of photos (e.g., 10 for 10%)')
@click.option('--dry-run', is_flag=True, help='Preview without writing files')
def process_trip(name: str, gpx: str, photos: str, output: Optional[str],
                 hosted_photos_dir: Optional[str],
                 geosync: str, gpx_tolerance_hours: float, gpx_split_gap_km: float,
                 max_interp_gap_hours: float,
                 filter_by_raws_in: Optional[Path], exclude_raws_in: Optional[Path],
                 raws_root: Optional[Path], locations_file: Optional[Path], dump_buildings: bool,
                 exclude_buildings: Optional[str], exclude_edits_under: Optional[str],
                 only_edits_dirs: bool,
                 split_offroute_private: bool, private_cluster_radius: float,
                 gpx_route_subdir: Optional[str], route_snap_public_hours: float,
                 fallback_location: Optional[str], nearest_photo_max_hours: float,
                 cluster_radius: float, raws: str,
                 format_name: str, quality: int, display_longest: int, thumbnail_longest: int,
                 fake_route_locations: Optional[str], no_fake_route: bool,
                 strict_building_distance: bool, kmz_path_str: Optional[str],
                 skip_existing_images: bool, update: bool, reindex: bool,
                 test_mode: int, dry_run: bool):
    """
    Process trip photos and generate web-ready output.

    Compressed thumbnails/display images go to hosted-photos/<slug>/ (gitignored).
    Metadata (manifest.json, route.geojson) goes to web/trips/<slug>/.
    Symlinks at web/trips/<slug>/{thumbnails,display} point to hosted-photos/<slug>/.
    """
    gpx_path = Path(gpx) if gpx else None
    no_gpx_mode = gpx_path is None
    photos_path = Path(photos)

    if gpx_path and not gpx_path.exists():
        click.echo(f"Error: GPX file not found: {gpx_path}", err=True)
        sys.exit(1)

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

    # Reprocessing a split trip: pull any previously-quarantined private images
    # back into the public hosted dir so the split re-partitions cleanly (idempotent).
    if split_offroute_private and not dry_run:
        prev_priv = hosted_root / (trip_id + '-private')
        if prev_priv.exists():
            hosted_photos_path.mkdir(parents=True, exist_ok=True)
            for sub in ('thumbnails', 'display'):
                src_dir = prev_priv / sub
                if src_dir.is_dir():
                    (hosted_photos_path / sub).mkdir(parents=True, exist_ok=True)
                    for img in src_dir.iterdir():
                        shutil.move(str(img), str(hosted_photos_path / sub / img.name))
            shutil.rmtree(prev_priv, ignore_errors=True)
            for stale in (output_path.parent / (trip_id + '-private'),):
                if stale.exists():
                    shutil.rmtree(stale, ignore_errors=True)
            click.echo(f"Consolidated previous '{trip_id}-private' images back for re-split")

    click.echo(f"Processing trip: {name}")
    click.echo(f"GPX file: {gpx_path or 'none (no-GPX mode — EXIF GPS + geocoded fallback)'}")
    click.echo(f"Photos directory: {photos_path}")
    click.echo(f"Output directory: {output_path}")
    click.echo(f"Hosted photos directory: {hosted_photos_path}")
    click.echo(f"Image format: {format_name.upper()} q{quality}, "
               f"display longest≤{display_longest}px, thumb longest≤{thumbnail_longest}px")

    if geosync:
        click.echo(f"Timezone offset: {geosync}")

    if raws:
        click.echo(f"DJI raws directory: {raws}")

    # Parse GPX (or skip in no-GPX mode)
    if not no_gpx_mode:
        click.echo("\nParsing GPX file...")
        trackpoints = parse_gpx(gpx_path)
        click.echo(f"Found {len(trackpoints)} trackpoints")
        if not trackpoints:
            click.echo("Error: No trackpoints found in GPX file", err=True)
            sys.exit(1)
        trip_start = trackpoints[0]['time'].date().isoformat()
        trip_end = trackpoints[-1]['time'].date().isoformat()
    else:
        click.echo("\nNo-GPX mode — dates will be derived from EXIF timestamps")
        trackpoints = []
        trip_start = trip_end = None  # filled in after processing photos

    # Find photos
    click.echo("\nFinding photos...")
    photo_files = find_photos(photos_path)
    click.echo(f"Found {len(photo_files)} photos")

    # Pre-2019 backfill: when the edits root is a whole year folder, restrict to
    # photos living under an "Edits" subfolder so the year's raws / camera-card
    # dumps (which also include JPGs) are not picked up.
    if only_edits_dirs:
        before = len(photo_files)
        photo_files = [p for p in photo_files
                       if any(part.lower() == 'edits' for part in p.parts)]
        click.echo(f"  --only-edits-dirs: kept {len(photo_files)} of {before} "
                   f"(under an 'Edits/' folder)")

    # Optionally filter by stems present somewhere under a raws root, and build
    # a stem→raw-path index so we can re-read DateTimeOriginal from the raw
    # (Lightroom JPG exports sometimes carry corrupted dates).
    # Trailing suffixes Lightroom/edit exports add that the raw file lacks.
    _edit_suffix_re = re.compile(r'-(Enhanced-NR|Enhanced|Edit|Pano|HDR|NR|copy|\d+)$', re.I)

    def base_stem(stem: str) -> str:
        prev = None
        while prev != stem:
            prev = stem
            stem = _edit_suffix_re.sub('', stem)
        return stem

    # Lower rank = preferred. Real raws beat JPGs; a path that yields a building
    # name beats one under a generic dir (the raw tree often has an Edits/ mirror
    # of JPGs sharing the same stems as the building-folder raws).
    _ext_rank = {'.arw': 0, '.dng': 1, '.cr2': 2, '.nef': 3, '.raf': 4,
                 '.tif': 5, '.tiff': 5, '.png': 6, '.jpeg': 7, '.jpg': 8}

    # stem → [all raw paths sharing that stem]. We keep every candidate (not just
    # the best-ranked) so find_raw can disambiguate stem collisions using the
    # edit's own folder — the camera counter rolls over, so the same stem
    # (e.g. RM107445) can exist in several trip folders.
    raw_index: dict = {}
    raw_base_index: dict = {}  # base_stem → [raw paths], for suffix-tolerant matching
    raw_scan_root = raws_root or filter_by_raws_in
    if raw_scan_root:
        root_p = Path(raw_scan_root)
        label = "Filtering by raws under" if filter_by_raws_in else "Indexing raws under"
        click.echo(f"{label}: {raw_scan_root}")

        def rank(p: Path) -> tuple:
            has_building = building_from_raw(p, root_p) is not None
            return (0 if has_building else 1, _ext_rank.get(p.suffix.lower(), 9), str(p))

        def add(idx: dict, key: str, p: Path):
            idx.setdefault(key, []).append(p)

        for ext in SUPPORTED_EXTENSIONS | {'.arw', '.dng', '.cr2', '.nef', '.raf'}:
            for pat in (f'*{ext}', f'*{ext.upper()}'):
                for p in Path(raw_scan_root).rglob(pat):
                    add(raw_index, p.stem, p)
                    add(raw_base_index, base_stem(p.stem), p)
    else:
        root_p = None
        def rank(p: Path) -> tuple:
            return (1, _ext_rank.get(p.suffix.lower(), 9), str(p))

    def _meaningful_tokens(path: Path) -> set:
        """Lowercased, non-generic folder names along a path — used to match an
        edit's folder to a candidate raw's folder."""
        return {part.lower() for part in path.parent.parts
                if not _is_generic_component(part)}

    def find_raw(stem: str, edit_path: Optional[Path] = None) -> Optional[Path]:
        cands = raw_index.get(stem) or raw_base_index.get(base_stem(stem))
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        # Stem collision: prefer the raw whose folder shares a location name with
        # the edit's folder. Fall back to rank.
        edit_tokens = _meaningful_tokens(edit_path) if edit_path else set()
        return min(cands, key=lambda p: (-len(edit_tokens & _meaningful_tokens(p)), rank(p)))

    if filter_by_raws_in:
        before = len(photo_files)
        _TIMESTAMP_COLLISION_DAYS = 30  # if raw timestamp differs from edit by >30 days, it's a stem collision

        def _raw_matches_edit(edit_path: Path) -> bool:
            raw = find_raw(edit_path.stem, edit_path)
            if raw is None:
                return False
            raw_ts = get_exif_datetime_via_exiftool(raw)
            if raw_ts is None:
                return True  # can't verify, assume match
            edit_ts = get_exif_datetime(edit_path) or get_exif_datetime_via_exiftool(edit_path)
            if edit_ts is None:
                return True  # can't verify, assume match
            return abs((raw_ts - edit_ts).days) <= _TIMESTAMP_COLLISION_DAYS

        photo_files = [p for p in photo_files if _raw_matches_edit(p)]
        click.echo(f"  Kept {len(photo_files)} of {before} photos with matching raw (timestamp-verified)")
        click.echo(f"  Will read DateTimeOriginal from the raw file when available")

    # Drop edits that live under an excluded subfolder of the edits bundle — used to
    # carve specific regions out of a multi-region edits bundle. Matches edit-path
    # folder names (case-insensitive, exact component match) — deterministic
    # regardless of raw stem collisions.
    if exclude_edits_under:
        excl = [e.strip().lower() for e in exclude_edits_under.split(';') if e.strip()]
        before = len(photo_files)
        photo_files = [p for p in photo_files
                       if not any(part.lower() in excl for part in p.parts)]
        click.echo(f"  Excluded edits under {excl}: dropped {before - len(photo_files)}, kept {len(photo_files)}")

    # Drop edits whose stem appears under an excluded raws folder — carves a region
    # out of a bundled Edits folder by raw membership (inverse of --filter-by-raws-in).
    if exclude_raws_in:
        excl_root = Path(exclude_raws_in)
        excl_stems: set = set()
        for ext in SUPPORTED_EXTENSIONS | {'.arw', '.dng', '.cr2', '.nef', '.raf'}:
            for pat in (f'*{ext}', f'*{ext.upper()}'):
                for p in excl_root.rglob(pat):
                    excl_stems.add(p.stem)
                    excl_stems.add(base_stem(p.stem))
        before = len(photo_files)
        photo_files = [p for p in photo_files
                       if p.stem not in excl_stems and base_stem(p.stem) not in excl_stems]
        click.echo(f"  Excluded edits under raws {excl_root.name}: dropped {before - len(photo_files)}, kept {len(photo_files)}")

    if not photo_files:
        click.echo("Error: No photos found in directory", err=True)
        sys.exit(1)

    # Split on ';' (not ',') so building/folder names that contain commas
    # (e.g. "Himachal Pradesh, Ladakh") survive intact.
    exclude_building_set: set = set()
    if exclude_buildings:
        exclude_building_set = {b.strip().lower() for b in exclude_buildings.split(';') if b.strip()}
        click.echo(f"Excluding buildings: {sorted(exclude_building_set)}")

    # Load building/location coordinate map (KML + web-search derived)
    building_coords: dict = {}
    if locations_file:
        building_coords = load_locations_file(locations_file)
        click.echo(f"Loaded {len(building_coords)} building locations from {locations_file.name}")

    # Discovery mode: report building names derived from the raw folders, then exit.
    if dump_buildings:
        if not raw_scan_root:
            click.echo("Error: --dump-buildings requires --raws-root", err=True)
            sys.exit(1)
        counts: dict = {}
        no_building = 0
        no_raw = 0
        for pf in photo_files:
            raw = find_raw(pf.stem, pf)
            if raw is None:
                no_raw += 1
                continue
            bname = building_from_raw(raw, Path(raw_scan_root))
            if bname:
                counts[bname] = counts.get(bname, 0) + 1
            else:
                no_building += 1
        click.echo(f"\n=== Buildings for: {name} ===")
        click.echo(f"raw root: {raw_scan_root}")
        click.echo(f"edits: {len(photo_files)} | matched raw: {len(photo_files) - no_raw} | "
                   f"no raw: {no_raw} | under generic dir only: {no_building}")
        for bname in sorted(counts):
            in_kml = building_coords.get(bname.strip().lower()) is not None
            mark = '✓' if in_kml else ' '
            click.echo(f"  [{mark}] {counts[bname]:4d}  {bname}")
        # Machine-readable line for tooling
        click.echo("BUILDINGS_JSON=" + json.dumps({'trip': name, 'raw_root': str(raw_scan_root),
                                                    'buildings': counts}))
        return

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
    clock_corrected_count = 0  # route photos whose wrong date we corrected via day folder
    gpx_tolerance_seconds = gpx_tolerance_hours * 3600

    # GPX window in UTC, used to compute "how far out of range" deltas
    if trackpoints:
        _t0 = trackpoints[0]['time']
        _t1 = trackpoints[-1]['time']
        if _t0.tzinfo is None: _t0 = _t0.replace(tzinfo=timezone.utc)
        if _t1.tzinfo is None: _t1 = _t1.replace(tzinfo=timezone.utc)
        gpx_window_start, gpx_window_end = _t0, _t1
    else:
        gpx_window_start = gpx_window_end = None

    # Distinct calendar days covered by the track, in order. "Day N" route folders
    # map to day_dates[N-1] — used to date-correct route photos whose camera clock
    # was wrong (timestamp outside the track window) so they still interpolate onto
    # the right segment.
    day_dates = []
    if trackpoints:
        _seen = set()
        for _p in trackpoints:
            _t = _p['time']
            _d = (_t if _t.tzinfo else _t.replace(tzinfo=timezone.utc)).date()
            if _d not in _seen:
                _seen.add(_d)
                day_dates.append(_d)
        day_dates.sort()

    # Decide fallback location for photos with no GPS.
    # With GPX: default chain → nearest placed photo → nearest trackpoint → centroid.
    # Without GPX: auto-geocode from trip name (or --fallback-location override).
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
    elif no_gpx_mode:
        click.echo("Auto-geocoding fallback location from trip name...")
        geocoded = geocode_from_name(name)
        if geocoded:
            fallback_gps = geocoded
            fallback_source = 'geocoded'
            click.echo(f"  → {fallback_gps['lat']:.4f}, {fallback_gps['lon']:.4f}")
        else:
            click.echo("  ⚠ Geocoding failed — photos without EXIF GPS will be dropped")
    else:
        # GPX mode: sentinel centroid, real placement chosen per-photo in second pass
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

    # Build raw-match index upfront (needed for cache keys).
    raw_matches = {pf: find_raw(pf.stem, pf) for pf in photo_files}

    # --- Incremental update: per-source freshness state (mtime+size) ---
    # Tracked in <output>/source_state.json (separate from exif_cache.json so the
    # latter's format is untouched). See docs/incremental-update-design.md.
    update_mode = update or reindex
    source_state_path = output_path / 'source_state.json'
    _had_state = source_state_path.exists()
    prev_state: dict = {}
    if _had_state:
        try:
            prev_state = json.loads(source_state_path.read_text())
        except Exception:
            prev_state = {}

    def _stat_pair(p: Path):
        try:
            st = p.stat()
            return [int(st.st_mtime), st.st_size]
        except OSError:
            return None

    current_state = {str(pf): _stat_pair(pf) for pf in photo_files} if update_mode else {}
    # First --update on a pre-existing trip (no state file) ADOPTS current artifacts as
    # the baseline; --reindex always adopts. Adopt = treat nothing as "changed", so only
    # genuinely-missing images get encoded.
    adopt = reindex or (update and not _had_state)

    def _changed(pf: Path) -> bool:
        if not update or adopt:
            return False
        return prev_state.get(str(pf)) != current_state.get(str(pf))

    changed_edits = {pf for pf in photo_files if _changed(pf)} if update_mode else set()
    if update_mode:
        if adopt:
            click.echo(f"Update: adopting baseline for {len(photo_files)} sources "
                       f"({'--reindex' if reindex else 'first --update'}); encoding only missing images")
        else:
            click.echo(f"Update: {len(changed_edits)} changed/new of {len(photo_files)} "
                       f"sources will re-encode + re-read EXIF; rest reused")

    # EXIF cache: persisted between runs as <output>/exif_cache.json so that
    # location-only reprocesses don't re-read the external drive.
    exif_cache_path = output_path / 'exif_cache.json'
    exif_cache: dict = {}
    if exif_cache_path.exists():
        try:
            exif_cache = json.loads(exif_cache_path.read_text())
        except Exception:
            exif_cache = {}

    # Changed sources (update mode): drop stale EXIF so it's re-read from the drive,
    # along with the matched raw (its timestamp/GPS may have changed too).
    for pf in changed_edits:
        exif_cache.pop(str(pf), None)
        _r = raw_matches.get(pf)
        if _r is not None:
            exif_cache.pop(str(_r), None)

    cache_hits = sum(1 for pf in photo_files if str(pf) in exif_cache)
    uncached_edits = [pf for pf in photo_files if str(pf) not in exif_cache]
    uncached_raws  = list({r for pf in uncached_edits
                           if (r := raw_matches[pf]) is not None
                           and str(r) not in exif_cache})

    if cache_hits:
        click.echo(f"EXIF cache: {cache_hits} hits, reading {len(uncached_edits)} new edits / {len(uncached_raws)} new raws from drive...")
    else:
        click.echo(f"Pre-reading EXIF (batch) for {len(uncached_edits)} edits + {len(uncached_raws)} raws...")

    new_exif = {}
    if uncached_edits:
        new_exif.update(batch_read_exif(uncached_edits))
    if uncached_raws:
        new_exif.update(batch_read_exif(uncached_raws))

    # Merge into cache and persist (so next run is even faster).
    exif_cache.update(new_exif)
    if not dry_run and new_exif:
        try:
            output_path.mkdir(parents=True, exist_ok=True)
            exif_cache_path.write_text(json.dumps(exif_cache))
        except Exception:
            pass

    exif_edits = exif_cache
    exif_raws  = exif_cache

    for photo_file in tqdm(photo_files, desc="Processing"):
        # Get photo timestamp — prefer the raw file's timestamp when available,
        # since edited JPGs sometimes carry corrupted DateTimeOriginal.
        raw_match = raw_matches[photo_file]
        photo_time = None
        if raw_match is not None:
            photo_time = _parse_exif_datetime(
                exif_raws.get(str(raw_match), {}).get('DateTimeOriginal'))
        if photo_time is None:
            photo_time = _parse_exif_datetime(
                exif_edits.get(str(photo_file), {}).get('DateTimeOriginal'))
        if photo_time is None:
            photo_time = get_exif_datetime(photo_file)  # PIL fallback

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
        gps = _parse_exif_gps(exif_edits.get(str(photo_file), {}))
        gps_source = 'exif' if gps else None

        if not gps and raws and photo_file.stem.startswith('DJI_'):
            raw_path = find_dji_raw(photo_file, Path(raws))
            if raw_path:
                gps = _parse_exif_gps(exif_raws.get(str(raw_path), {})) \
                      or get_exif_gps(raw_path)  # fallback if not in batch (raws dir ≠ raw_scan_root)
                if gps:
                    gps_source = 'dng'

        if not gps:
            gps = interpolate_gps(trackpoints, photo_time, gpx_tolerance_seconds,
                                  max_interp_gap_hours * 3600)
            if gps:
                gps_source = 'gpx'

        # Derive a building/location label from the raw folder whenever we can.
        # Used to NAME the cluster (independent of how GPS was resolved, so
        # EXIF/DNG/GPX-placed photos still inherit their building name) and to read
        # the "Day N" number for date-correcting route photos below.
        building_name = None
        if raw_match is not None and raw_scan_root:
            building_name = building_from_raw(raw_match, Path(raw_scan_root))
        # Fall back to deriving the label from the edits directory structure
        # (e.g. /Edits/Trip Name/Subfolder/IMG_001.jpg → "Subfolder").
        if not building_name:
            building_name = building_from_raw(photo_file, photos_path)

        if exclude_building_set and building_name and building_name.strip().lower() in exclude_building_set:
            continue

        # Route-subdir photos belong to the documented GPX journey → force them
        # on-route (public). Skips building-coord lookup below.
        #
        # Crucially we only FORCE photos that lack real GPS onto the track — a
        # photo with its own EXIF/DNG GPS (e.g. a drone shot) is already at its
        # true position, so clamping it onto the track would only make it worse
        # (and wrong, if the camera clock was off).
        on_route = bool(gpx_route_subdir) and raw_match is not None and \
            any(part.lower() == gpx_route_subdir.lower() for part in raw_match.parts)
        if on_route and trackpoints and gps_source not in ('exif', 'dng'):
            # If the camera clock was wrong, the timestamp falls outside the GPX
            # window even though the "Day N" folder says exactly which trip day this
            # is. Correct the DATE to that day (keeping time-of-day) so the photo
            # interpolates accurately onto the route instead of clamping to one end.
            _pt = photo_time if photo_time.tzinfo else photo_time.replace(tzinfo=timezone.utc)
            outside = gpx_window_start is None or not (gpx_window_start <= _pt <= gpx_window_end)
            _m_day = re.match(r'\s*day\s*0*(\d+)', building_name or '', re.I)
            _day_n = int(_m_day.group(1)) if _m_day else None
            if outside and _day_n and 1 <= _day_n <= len(day_dates):
                photo_time = datetime.combine(day_dates[_day_n - 1], photo_time.time())
                clock_corrected_count += 1
            # Force onto the track (clamp across recording gaps) using the
            # possibly-corrected timestamp.
            clamped = interpolate_gps_clamped(trackpoints, photo_time)
            if clamped:
                gps = clamped
                gps_source = 'gpx'

        # No real GPS yet — place at the building's coords derived from the raw folder.
        if not on_route and not gps and building_coords and building_name:
            bc = building_coords.get(building_name.strip().lower())
            # Generic building names (Marriott, Hilton, …) collide across cities in
            # locations.json. With --strict-building-distance, treat a match that's
            # implausibly far (>2000km) from the trip fallback as a wrong-city collision
            # and skip it. OFF by default: multi-country trips (e.g. Country1+Country2) have
            # legitimately distant buildings and must NOT discard them.
            if strict_building_distance and bc and fallback_source == 'cli' and fallback_gps and \
                    haversine_distance(bc['lat'], bc['lon'], fallback_gps['lat'], fallback_gps['lon']) > 2_000_000:
                bc = None
            if bc:
                gps = {'lat': bc['lat'], 'lon': bc['lon']}
                gps_source = 'building'

        # Compute how far out of the GPX window the photo is (for diagnostics either way)
        pt = photo_time if photo_time.tzinfo else photo_time.replace(tzinfo=timezone.utc)
        if gpx_window_start is None:
            delta_h = 0.0
            direction = 'no_gpx'
        elif pt < gpx_window_start:
            delta_h = (gpx_window_start - pt).total_seconds() / 3600
            direction = 'before'
        elif pt > gpx_window_end:
            delta_h = (pt - gpx_window_end).total_seconds() / 3600
            direction = 'after'
        else:
            delta_h = 0.0
            direction = 'inside'

        # Building-derived coords are folder-level, not real GPS — mark approximate.
        placement = 'approximate' if gps_source == 'building' else 'exact'
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

        # Generate resized images (unless reusing existing ones). Reuse when
        # --skip-existing-images OR --update/--reindex and the images already exist —
        # but in --update, a CHANGED source still re-encodes.
        if not dry_run:
            thumb_path = hosted_photos_path / 'thumbnails' / f'{photo_id}.{image_ext}'
            display_path = hosted_photos_path / 'display' / f'{photo_id}.{image_ext}'
            reuse_img = ((skip_existing_images or update_mode)
                         and thumb_path.exists() and display_path.exists()
                         and photo_file not in changed_edits)
            if not reuse_img:
                generate_thumbnail(photo_file, thumb_path, thumbnail_longest, format_name, quality)
                generate_display_image(photo_file, display_path, display_longest, format_name, quality)

        # Get camera settings
        camera_settings = _parse_camera_settings(exif_edits.get(str(photo_file), {})) \
                          or get_camera_settings(photo_file)

        # Add to processed list
        photo_entry = {
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
        }
        if building_name:
            photo_entry['building'] = building_name
        if on_route:
            photo_entry['on_route'] = True
        processed_photos.append(photo_entry)

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
        return best  # (gap_seconds, (lat, lon)) or None

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
                    gap_s, coords = near
                    p['lat'], p['lon'] = coords
                    p['gps_source'] = 'gpx_nearest_time'
                    p['snap_gap_hours'] = round(gap_s / 3600, 2)
                    sk = skipped_by_id.get(p['id'])
                    if sk is not None:
                        sk['placed_at'] = {'lat': coords[0], 'lon': coords[1]}
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

    # No-GPX: apply all position refinements BEFORE clustering so clusters are correct
    fake_route_geojson = None
    if no_gpx_mode:
        # Pass 3: interpolate from EXIF GPS anchors (drone shots etc.)
        exif_placed = apply_exif_interpolation(processed_photos)
        if exif_placed:
            gps_source_counts['interpolated_from_exif'] = exif_placed
            gps_source_counts['fallback_centroid'] = gps_source_counts.get('fallback_centroid', 0) - exif_placed
            if gps_source_counts['fallback_centroid'] <= 0:
                gps_source_counts.pop('fallback_centroid', None)

        # Pass 4a: KMZ route — distribute remaining fallback photos along real route geometry
        if kmz_path_str:
            kmz_path = Path(kmz_path_str)
            click.echo(f"\nParsing KMZ route: {kmz_path.name}...")
            kmz_points = parse_kmz_route(kmz_path)
            click.echo(f"  {len(kmz_points)} waypoints")
            # KMZ overrides EXIF interpolation — use actual route over drone-anchor guesses
            eligible_kmz = [p for p in processed_photos
                            if p['gps_source'] in ('fallback_centroid', 'interpolated_from_exif')]
            if eligible_kmz and len(kmz_points) >= 2:
                by_time = sorted(eligible_kmz, key=lambda p: p['timestamp'])
                n = len(by_time)
                for i, photo in enumerate(by_time):
                    frac = i / (n - 1) if n > 1 else 0.5
                    coords = interpolate_along_line(kmz_points, frac)
                    photo['lat'], photo['lon'] = coords['lat'], coords['lon']
                    photo['gps_source'] = 'kmz_route_interpolated'
                    photo['placement'] = 'approximate'
                click.echo(f"  Placed {n} photos along KMZ route")
                for src in ('fallback_centroid', 'interpolated_from_exif'):
                    gps_source_counts.pop(src, None)
                gps_source_counts['kmz_route_interpolated'] = n
            line_coords = [[p['lon'], p['lat']] for p in kmz_points]
            fake_route_geojson = {
                'type': 'FeatureCollection',
                'features': [{'type': 'Feature', 'properties': {'name': 'Planned route'},
                              'geometry': {'type': 'LineString', 'coordinates': line_coords}}]
            }
        elif no_fake_route:
            # Building-specific trip: skip geocoded fake route. Non-building/EXIF photos
            # keep their fallback placement (fallback_location or centroid).
            click.echo("\nFake-route geocoding disabled (--no-fake-route) — using building/fallback placement only")
            fake_route_geojson = {'type': 'FeatureCollection', 'features': []}
        else:
            # Pass 4b: fake route — place remaining photos at geocoded locations + build route line
            explicit = [l.strip() for l in fake_route_locations.split(',')] \
                       if fake_route_locations else None
            fake_route_geojson = apply_fake_route(processed_photos, name, explicit) \
                                 or {'type': 'FeatureCollection', 'features': []}

    # Split off-route photos into a separate private trip (GPX trips only).
    # Public/on-route = GPX-interpolated, OR in the --gpx-route-subdir, OR snapped
    # to the track within --route-snap-public-hours. Everything else is off-route.
    off_route_photos = []
    if split_offroute_private and not no_gpx_mode:
        def _is_public(p):
            if p.get('on_route'):
                return True
            if p['gps_source'] == 'gpx':
                return True
            if p['gps_source'] == 'gpx_nearest_time' and \
                    p.get('snap_gap_hours', 1e9) <= route_snap_public_hours:
                return True
            return False
        public_photos = [p for p in processed_photos if _is_public(p)]
        off_route_photos = [p for p in processed_photos if not _is_public(p)]
        processed_photos = public_photos
        click.echo(f"\nSplit: {len(processed_photos)} on-route (public), "
                   f"{len(off_route_photos)} off-route (private)")
        from collections import Counter as _Counter
        gps_source_counts = dict(_Counter(p['gps_source'] for p in processed_photos))

    # Derive date range from EXIF when no GPX
    if trip_start is None:
        timestamps = sorted(p['timestamp'] for p in processed_photos)
        trip_start = timestamps[0][:10] if timestamps else datetime.now().date().isoformat()
        trip_end = timestamps[-1][:10] if timestamps else trip_start

    # Report results
    click.echo(f"\nProcessed: {len(processed_photos)} photos")
    if gps_source_counts:
        breakdown = ', '.join(f'{src}={n}' for src, n in sorted(gps_source_counts.items()))
        click.echo(f"GPS sources: {breakdown}")
    if clock_corrected_count:
        click.echo(f"Clock-corrected route photos (wrong camera date → day folder): {clock_corrected_count}")
    click.echo(f"Failed: {len(failed_photos)} photos")

    if failed_photos:
        click.echo("\nFailed photos:")
        for photo, reason in failed_photos[:10]:
            click.echo(f"  - {photo.name}: {reason}")
        if len(failed_photos) > 10:
            click.echo(f"  ... and {len(failed_photos) - 10} more")

    # Generate clusters (after all position refinements)
    click.echo("\nGenerating clusters...")
    clusters = cluster_photos(processed_photos, cluster_radius)
    click.echo(f"Created {len(clusters)} clusters")

    # Detect countries
    click.echo("\nDetecting countries...")
    if not no_gpx_mode:
        countries = get_countries_from_gpx(gpx_path)
    else:
        countries = get_countries_from_photos(processed_photos)
    if countries:
        click.echo(f"Countries: {', '.join(countries)}")

    # Generate manifest
    manifest = {
        'trip_name': name,
        'dates': {
            'start': trip_start,
            'end': trip_end
        },
        'countries': countries,
        'source': {
            'photos_path': str(photos_path),
            'gpx_path': str(gpx_path) if gpx_path else None,
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

        # --- Incremental update: orphan cleanup + persist source state ---
        if update_mode:
            # Delete hosted images for sources no longer in the manifest (deleted edits).
            # Skipped for split trips: off-route images move to a separate -private dir,
            # so the main manifest doesn't list them and they'd be wrongly culled.
            if not split_offroute_private:
                kept_ids = {p['id'] for p in processed_photos}
                removed = 0
                for sub in ('thumbnails', 'display'):
                    d = hosted_photos_path / sub
                    if d.is_dir():
                        for img in d.iterdir():
                            if img.is_file() and img.stem not in kept_ids:
                                img.unlink()
                                removed += 1
                if removed:
                    click.echo(f"Update: removed {removed} orphaned image file(s)")
            # Persist the new baseline (built from the CURRENT sources, so deleted
            # edits naturally drop out of the state).
            try:
                source_state_path.write_text(json.dumps(current_state))
                click.echo(f"Update: wrote source state for {len(current_state)} sources")
            except Exception as e:
                click.echo(f"Warning: could not write {source_state_path}: {e}", err=True)

        # Convert GPX to GeoJSON (fake_route_geojson already built above for no-GPX trips)
        if not no_gpx_mode:
            geojson_data = gpx_to_geojson(gpx_path, split_gap_km=gpx_split_gap_km)
        else:
            geojson_data = fake_route_geojson
        geojson_path = output_path / 'route.geojson'
        with open(geojson_path, 'w') as f:
            json.dump(geojson_data, f, indent=2)
        click.echo(f"Saved route: {geojson_path}")

        # Update trips index
        index_path = update_trips_index(
            output_path,
            name,
            manifest['dates'],
            len(processed_photos),
            countries=countries,
        )
        click.echo(f"Updated trips index: {index_path}")

        # Generate year and trip HTML pages — prefer year from name over EXIF
        name_year_m = re.match(r'^(\d{4})', name)
        year = int(name_year_m.group(1)) if name_year_m else int(manifest['dates']['start'][:4])
        trip_id = output_path.name
        year_page, trip_page = generate_html_pages(output_path, name, trip_id, year)
        click.echo(f"Generated year page: {year_page}")
        click.echo(f"Generated trip page: {trip_page}")

        # Write the off-route photos as a separate private trip
        if split_offroute_private and off_route_photos:
            result = write_private_trip(
                off_route_photos, output_path, hosted_photos_path, image_ext,
                private_cluster_radius, format_name, quality, display_longest, thumbnail_longest,
                name, photos_path)
            if result:
                ps, pn_photos, pn_clusters, pcc = result
                click.echo(f"Wrote private split: {ps} "
                           f"({pn_photos} photos, {pn_clusters} clusters, countries={pcc})")

    click.echo("\nDone!")

    if test_mode:
        click.echo(f"\n[TEST MODE - processed only {test_mode}% of photos]")

    if dry_run:
        click.echo("\n[DRY RUN - no files were written]")


if __name__ == '__main__':
    process_trip()
