#!/usr/bin/env python3
"""
geo_index.py — Index photo folders into a location dataset.

Given a photo directory or Lightroom catalog (.lrcat), this script:
  1. Discovers all photo-containing folders
  2. Extracts GPS coordinates from EXIF data where available
  3. Falls back to geocoding the folder name via Nominatim (OpenStreetMap, free)
     or Google Places API (--geocoder google --api-key KEY, more accurate for
     specific building names)
  4. Writes a KML, GeoJSON, or CSV of resolved locations

Usage:
    # From a raw photo directory:
    python scripts/geo_index.py /Volumes/RYAN/Projects/Work --output locations.kml

    # From a Lightroom catalog:
    python scripts/geo_index.py ~/Pictures/Lightroom/Catalog.lrcat --output locations.geojson

    # Geocoding only (skip EXIF), using Google Places for better building names:
    python scripts/geo_index.py /path/to/photos --strategy geocode \\
        --geocoder google --api-key YOUR_KEY --output locations.kml

    # EXIF only, no network requests:
    python scripts/geo_index.py /path/to/photos --strategy exif --output locations.csv
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from statistics import median
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────

PHOTO_EXTS = {
    '.jpg', '.jpeg', '.raw', '.cr2', '.cr3', '.nef', '.arw',
    '.dng', '.orf', '.rw2', '.rw1', '.heic', '.heif', '.tif', '.tiff',
    '.png', '.webp',
}

# Folder names that are organisational, not locations
SKIP_FOLDERS = {
    '@eadir', '.thumbnails', '.spotlight-v100', '.trashes', '.fseventsd',
    'raw', 'jpeg', 'jpg', 'edited', 'selects', 'select', 'rejects',
    'backup', 'originals', 'export', 'print', 'web', 'social',
    '.lightroom', 'previews', 'smart previews', '.lrdata',
    'final', 'finals', 'lo res', 'hi res', 'video',
}

# Strip these words when cleaning folder names for geocoding
JUNK_WORDS = re.compile(
    r'\b(raw|jpeg|jpg|dng|cr2|cr3|nef|arw|edit|edited|selects?|final|finals?|'
    r'backup|shoot|session|export|web|print|lo|hi|high|low|res|video|photos?|'
    r'images?|pics?|photography|archive|collection|portfolio|'
    r'day|days|vol|part|set|batch|misc|untitled|camera|pictures?)\b',
    re.IGNORECASE
)

# Folder names that produce meaningless geocoding results — skip entirely
SKIP_GEOCODE = {
    'camera', 'photos', 'pictures', 'video', 'videos', 'me', 'misc',
    'untitled', 'desktop', 'downloads', 'documents', 'phone', 'iphone',
    'android', 'import', 'batch', 'set', 'vol', 'part',
}

DATE_PATTERNS = [
    re.compile(r'\b(19|20)\d{2}\b'),           # 4-digit year
    re.compile(r'\b\d{2}[-_]\d{2}[-_]\d{2,4}\b'),  # DD-MM-YYYY etc.
    re.compile(r'\b\d{8}\b'),                   # YYYYMMDD
    re.compile(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b', re.IGNORECASE),
]


# ── Folder discovery ────────────────────────────────────────────────────────────

def _has_photos(folder: Path, min_count: int = 1) -> bool:
    count = 0
    try:
        for f in folder.iterdir():
            if f.is_file() and f.suffix.lower() in PHOTO_EXTS:
                count += 1
                if count >= min_count:
                    return True
    except PermissionError:
        pass
    return False


def discover_from_directory(root: Path, min_photos: int = 1) -> list[dict]:
    """Walk a directory tree and return all folders that contain photos."""
    results = []

    def walk(path: Path):
        if path.name.lower() in SKIP_FOLDERS or path.name.startswith('.'):
            return
        try:
            entries = list(path.iterdir())
        except PermissionError:
            return

        if _has_photos(path, min_photos):
            # Use up to 3 parent folder names as geocoding context
            rel_parts = path.relative_to(root).parts
            context_parts = [p for p in rel_parts[:-1]
                             if p.lower() not in SKIP_FOLDERS][-3:]
            results.append({
                'path': str(path),
                'name': path.name,
                'context': ', '.join(reversed(context_parts)),  # nearest parent first
                'photo_count': sum(1 for f in entries
                                   if f.is_file() and f.suffix.lower() in PHOTO_EXTS),
            })

        for sub in sorted(e for e in entries if e.is_dir()):
            walk(sub)

    walk(root)
    return results


def discover_from_lightroom(lrcat: Path) -> list[dict]:
    """
    Query a Lightroom Classic catalog for folder paths and GPS centroids.

    Returns folders with 'lat'/'lon' already set where the catalog has EXIF GPS,
    and without coordinates where GPS is absent (to be resolved by geocoding).
    """
    conn = sqlite3.connect(f'file:{lrcat}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT
            fo.pathFromRoot                                      AS path,
            COUNT(DISTINCT lf.id_local)                         AS photo_count,
            COUNT(CASE WHEN e.hasGPS = 1 THEN 1 END)           AS gps_count,
            AVG(CASE WHEN e.hasGPS = 1 THEN e.gpsLatitude  END) AS avg_lat,
            AVG(CASE WHEN e.hasGPS = 1 THEN e.gpsLongitude END) AS avg_lon
        FROM AgLibraryFolder fo
        JOIN AgLibraryFile lf              ON lf.folder    = fo.id_local
        JOIN Adobe_images  i               ON i.rootFile   = lf.id_local
        LEFT JOIN AgHarvestedExifMetadata e ON e.image     = i.id_local
        GROUP BY fo.id_local, fo.pathFromRoot
        HAVING photo_count > 0
        ORDER BY fo.pathFromRoot
    """)

    rows = cur.fetchall()
    conn.close()

    results = []
    for row in rows:
        parts = [p for p in row['path'].replace('\\', '/').split('/') if p]
        name = parts[-1] if parts else ''
        if name.lower() in SKIP_FOLDERS or not name:
            continue

        # Use parent folders as context (e.g. "London" for "The Shard")
        context_parts = [p for p in parts[:-1]
                         if p.lower() not in SKIP_FOLDERS][-3:]
        entry = {
            'path': row['path'],
            'name': name,
            'context': ', '.join(reversed(context_parts)),
            'photo_count': row['photo_count'],
        }

        if row['gps_count'] and row['gps_count'] > 0:
            entry['lat'] = row['avg_lat']
            entry['lon'] = row['avg_lon']
            entry['source'] = 'exif_catalog'

        results.append(entry)

    return results


# ── EXIF GPS extraction ─────────────────────────────────────────────────────────

def extract_gps_exif(folder: Path, sample_n: int = 20) -> Optional[tuple[float, float]]:
    """
    Sample up to sample_n photos from a folder and return the median GPS position.
    Requires exiftool on PATH. Returns None if no GPS found or exiftool unavailable.
    """
    photos = sorted(
        (f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in PHOTO_EXTS),
        key=lambda f: f.stat().st_size, reverse=True  # sample larger files first (more likely to have EXIF)
    )[:sample_n]

    if not photos:
        return None

    try:
        result = subprocess.run(
            ['exiftool', '-n', '-GPSLatitude', '-GPSLongitude', '-csv'] +
            [str(p) for p in photos],
            capture_output=True, text=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None  # exiftool not installed or timed out

    lats, lons = [], []
    for line in result.stdout.splitlines()[1:]:  # skip CSV header
        parts = line.split(',')
        if len(parts) < 3:
            continue
        try:
            lat, lon = float(parts[1]), float(parts[2])
            if lat != 0 or lon != 0:
                lats.append(lat)
                lons.append(lon)
        except ValueError:
            pass

    return (median(lats), median(lons)) if lats else None


# ── Folder name cleaning ────────────────────────────────────────────────────────

def _clean_text(s: str) -> str:
    """Strip dates, separators and photography jargon from any string."""
    s = re.sub(r'[\[\](){}]', ' ', s)  # remove brackets
    for pat in DATE_PATTERNS:
        s = pat.sub('', s)
    s = JUNK_WORDS.sub('', s)
    s = re.sub(r'[-_]+', ' ', s)
    # Remove leftover numeric tokens (e.g. "02", "3.26", ".26")
    s = re.sub(r'\b\d+(?:\.\d+)?\b', '', s)
    s = re.sub(r'\.\d+', '', s)        # decimal fragments like .26
    return re.sub(r'\s{2,}', ' ', s).strip()


def clean_name_for_geocoding(name: str, context: str = '') -> str:
    """
    Strip dates, underscores, and photography jargon from a folder name,
    then append parent-folder context so geocoding has enough signal.

    Example: "20190415_The_Shard_RAW" + context "London" → "The Shard, London"
    """
    cleaned = _clean_text(name)
    ctx = _clean_text(context)

    # If the name cleaned down to almost nothing, use context alone
    if len(cleaned) < 4:
        return ctx or cleaned

    if ctx:
        # Avoid repeating words already in the name (e.g. name="London Eye" ctx="London")
        ctx_words = {w.lower() for w in ctx.split()}
        name_words = {w.lower() for w in cleaned.split()}
        if not name_words.issubset(ctx_words):
            cleaned = f"{cleaned}, {ctx}"

    return cleaned.strip()


# ── Geocoding ───────────────────────────────────────────────────────────────────

def geocode_nominatim(query: str) -> Optional[tuple[float, float, str]]:
    """
    Geocode via Nominatim (OpenStreetMap). Free, no key required.
    Good for cities, districts, and well-known landmarks.
    Rate limit: 1 request/second (enforced by caller via --geocode-delay).
    """
    params = urllib.parse.urlencode({'q': query, 'format': 'json', 'limit': 1})
    url = f'https://nominatim.openstreetmap.org/search?{params}'
    req = urllib.request.Request(url, headers={'User-Agent': 'geo-index-script/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read())
        if results:
            r = results[0]
            return float(r['lat']), float(r['lon']), r.get('display_name', query)
    except Exception as e:
        print(f'    geocode error ({query}): {e}', file=sys.stderr)
    return None


def geocode_google_places(query: str, api_key: str) -> Optional[tuple[float, float, str]]:
    """
    Geocode via Google Places Text Search. Paid but highly accurate for
    specific building names (e.g. "The Shard London", "Burj Khalifa Dubai").
    """
    params = urllib.parse.urlencode({'query': query, 'key': api_key})
    url = f'https://maps.googleapis.com/maps/api/place/textsearch/json?{params}'
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get('results', [])
        if results:
            loc = results[0]['geometry']['location']
            name = results[0].get('name', query)
            return loc['lat'], loc['lng'], name
    except Exception:
        pass
    return None


def geocode(query: str, geocoder: str, api_key: Optional[str]) -> Optional[tuple[float, float, str]]:
    if not query or len(query) < 3:
        return None
    if geocoder == 'google':
        if not api_key:
            print('  ✗ --api-key required for Google geocoder', file=sys.stderr)
            return None
        return geocode_google_places(query, api_key)
    return geocode_nominatim(query)


# ── Output writers ──────────────────────────────────────────────────────────────

def _xe(s: str) -> str:
    """XML-escape a string."""
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


def write_kml(locations: list[dict], output: Path):
    placemarks = []
    for loc in locations:
        if 'lat' not in loc or loc.get('lat') is None:
            continue
        name = loc.get('name', '')
        desc_parts = [loc.get('path', ''), f"[{loc.get('source', '?')}]"]
        if loc.get('geocoded_name'):
            desc_parts.insert(0, loc['geocoded_name'])
        desc = ' | '.join(filter(None, desc_parts))
        placemarks.append(
            f'    <Placemark>\n'
            f'      <name>{_xe(name)}</name>\n'
            f'      <description>{_xe(desc)}</description>\n'
            f'      <Point><coordinates>{loc["lon"]},{loc["lat"]},0</coordinates></Point>\n'
            f'    </Placemark>'
        )
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        '  <Document>\n'
        '    <name>Photo Locations</name>\n'
        + '\n'.join(placemarks) + '\n'
        '  </Document>\n'
        '</kml>\n'
    )
    output.write_text(kml, encoding='utf-8')


def write_geojson(locations: list[dict], output: Path):
    features = []
    for loc in locations:
        if 'lat' not in loc or loc.get('lat') is None:
            continue
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [loc['lon'], loc['lat']]},
            'properties': {k: v for k, v in loc.items() if k not in ('lat', 'lon')},
        })
    output.write_text(
        json.dumps({'type': 'FeatureCollection', 'features': features}, indent=2),
        encoding='utf-8'
    )


def write_csv(locations: list[dict], output: Path):
    import csv
    fields = ['name', 'lat', 'lon', 'source', 'photo_count', 'context', 'geocoded_name', 'path']
    with open(output, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(locations)


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Build a KML/GeoJSON/CSV of photo locations from a directory or Lightroom catalog.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('input', help='Photo directory or .lrcat Lightroom catalog path')
    parser.add_argument('--output', '-o', default='locations.kml',
                        help='Output path (.kml / .geojson / .csv) [default: locations.kml]')
    parser.add_argument('--strategy', choices=['exif', 'geocode', 'both'], default='both',
                        help='Coordinate source: exif=from photos, geocode=from folder name, '
                             'both=exif preferred with geocode fallback [default: both]')
    parser.add_argument('--geocoder', choices=['nominatim', 'google'], default='nominatim',
                        help='Geocoding service [default: nominatim]')
    parser.add_argument('--api-key', metavar='KEY',
                        help='API key for Google geocoder')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Print geocode queries as they run')
    parser.add_argument('--min-photos', type=int, default=1,
                        help='Min photos per folder to include [default: 1]')
    parser.add_argument('--sample', type=int, default=20,
                        help='Max photos to sample per folder for EXIF [default: 20]')
    parser.add_argument('--geocode-delay', type=float, default=1.1,
                        help='Seconds between geocode requests (Nominatim requires ≥1s) [default: 1.1]')
    parser.add_argument('--no-resolve', action='store_true',
                        help='Include unresolved folders in output (with no coordinates)')
    parser.add_argument('--limit', type=int, default=None,
                        help='Process only the first N folders (useful for testing)')
    parser.add_argument('--filter', metavar='PATTERN', dest='filter_pattern',
                        help='Only process folders whose path contains PATTERN (case-insensitive)')
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()

    if not input_path.exists():
        print(f'Error: {input_path} does not exist', file=sys.stderr)
        sys.exit(1)

    # ── 1. Discover folders ──
    is_lrcat = input_path.suffix.lower() == '.lrcat'
    print(f'{"Lightroom catalog" if is_lrcat else "Directory"}: {input_path}')
    print('Discovering folders...')

    folders = (discover_from_lightroom(input_path) if is_lrcat
               else discover_from_directory(input_path, args.min_photos))

    if args.filter_pattern:
        pat = args.filter_pattern.lower()
        folders = [f for f in folders if pat in f['path'].lower() or pat in f['name'].lower()]
        print(f'Filter "{args.filter_pattern}": {len(folders)} matching folder(s)')

    if args.limit:
        folders = folders[:args.limit]
        print(f'Limit: processing first {len(folders)} folder(s)')

    print(f'Found {len(folders)} folder(s) to process\n')

    # ── 2. Resolve coordinates ──
    locations = []
    resolved = 0
    geocode_calls = 0

    for i, folder in enumerate(folders, 1):
        prefix = f'  [{i:>{len(str(len(folders)))}}/{len(folders)}]'
        name = folder['name']

        # Already resolved (e.g. from Lightroom EXIF query)
        if 'lat' in folder:
            print(f'{prefix} {name:<40} EXIF catalog  ({folder["lat"]:.4f}, {folder["lon"]:.4f})')
            locations.append(folder)
            resolved += 1
            continue

        lat = lon = source = geocoded_name = None

        # EXIF GPS from photo files
        if args.strategy in ('exif', 'both') and not is_lrcat:
            gps = extract_gps_exif(Path(folder['path']), args.sample)
            if gps:
                lat, lon = gps
                source = 'exif'

        # Geocode folder name as fallback
        if lat is None and args.strategy in ('geocode', 'both'):
            if name.lower().strip() in SKIP_GEOCODE:
                query = ''
            else:
                # For Lightroom catalogs, parent folder names are usually trip/subject
                # organizers (e.g. "Bridges", "China CNY"), not geographic context —
                # using them corrupts the query. For filesystem dirs, parent folders
                # tend to be city/country names and help disambiguate.
                ctx = '' if is_lrcat else folder.get('context', '')
                query = clean_name_for_geocoding(name, ctx)
            if query and len(query) >= 4:
                if getattr(args, 'verbose', False):
                    print(f'    geocoding: {query!r}', file=sys.stderr)
                time.sleep(args.geocode_delay)  # always throttle, not just on success
                result = geocode(query, args.geocoder, args.api_key)
                if result:
                    lat, lon, geocoded_name = result
                    source = f'geocode:{args.geocoder}'
                    geocode_calls += 1

        if lat is not None:
            entry = {**folder, 'lat': lat, 'lon': lon, 'source': source}
            if geocoded_name:
                entry['geocoded_name'] = geocoded_name
            locations.append(entry)
            resolved += 1
            label = geocoded_name or f'({lat:.4f}, {lon:.4f})'
            print(f'{prefix} {name:<40} {source:<22} {label}')
        else:
            print(f'{prefix} {name:<40} no coords')
            if args.no_resolve:
                locations.append(folder)

    # ── 3. Write output ──
    print(f'\nResolved {resolved}/{len(folders)} folders')
    ext = output_path.suffix.lower()
    if ext == '.geojson':
        write_geojson(locations, output_path)
    elif ext == '.csv':
        write_csv(locations, output_path)
    else:
        write_kml(locations, output_path)

    print(f'Written → {output_path}')
    if geocode_calls:
        print(f'({geocode_calls} geocode request(s); '
              f'Google Places recommended for specific building names)')


if __name__ == '__main__':
    main()
