#!/usr/bin/env python3
"""
Pull timestamped GPS fixes out of photos, for use as GPX gap-fill anchors.

Reads metadata only (exiftool -fast2), non-recursively, from each directory
given. Two clocks matter and both are kept:

  - GPSDateTime  — from the GPS receiver, always UTC. Trustworthy.
  - DateTimeOriginal — the camera clock, whatever it was set to (DJI drones
    follow local time; the Sony bodies stay on UK time). Only used to place a
    photo in time when it carries no GPS timestamp.

Results are cached to JSON so later passes never touch the (slow) mount again.

Usage:
  python tools/reconstruction/photo_gps.py --out /tmp/fixes.json \
      --ext DNG --ext JPG "<dir>" ["<dir>" ...]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

TAGS = ['-GPSLatitude', '-GPSLongitude', '-GPSAltitude', '-GPSDateTime',
        '-DateTimeOriginal', '-CreateDate', '-Model']


def read_dir(directory, exts):
    cmd = ['exiftool', '-j', '-n', '-fast2', '-q', *TAGS]
    for e in exts:
        cmd += ['-ext', e]
    cmd.append(str(directory))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if not res.stdout.strip():
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        print(f'  ! unparseable exiftool output for {directory}', file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dirs', nargs='+')
    ap.add_argument('--out', required=True)
    ap.add_argument('--ext', action='append', default=None,
                    help='file extension to include (repeatable); default DNG+JPG')
    args = ap.parse_args()
    exts = args.ext or ['DNG', 'JPG']

    records, with_gps = [], 0
    for d in args.dirs:
        rows = read_dir(d, exts)
        n_gps = 0
        for r in rows:
            lat, lon = r.get('GPSLatitude'), r.get('GPSLongitude')
            rec = {
                'file': Path(r['SourceFile']).name,
                'dir': str(Path(r['SourceFile']).parent),
                'lat': lat, 'lon': lon,
                'ele': r.get('GPSAltitude'),
                'gps_time': r.get('GPSDateTime'),          # UTC
                'cam_time': r.get('DateTimeOriginal') or r.get('CreateDate'),
                'model': r.get('Model'),
            }
            if lat is not None and lon is not None:
                n_gps += 1
            records.append(rec)
        with_gps += n_gps
        print(f'{Path(d).name}: {len(rows)} files, {n_gps} with GPS')

    Path(args.out).write_text(json.dumps(records, indent=1))
    print(f'\n{len(records)} files ({with_gps} with GPS) → {args.out}')


if __name__ == '__main__':
    main()
