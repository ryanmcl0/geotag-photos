#!/usr/bin/env python3
"""
Show the EXIF date range for all JPGs in an edits folder.
Useful to anchor GPX reconstruction to the correct calendar dates.

Usage:
    python tools/reconstruction/exif_dates.py "/path/to/edits"
"""
import subprocess, sys, re
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print("Usage: exif_dates.py <edits_dir>")
        sys.exit(1)

    edits_dir = Path(sys.argv[1])
    jpgs = sorted(edits_dir.glob('*.jpg')) + sorted(edits_dir.glob('*.JPG'))
    if not jpgs:
        print(f"No JPG files found in {edits_dir}")
        sys.exit(1)

    result = subprocess.run(
        ['exiftool', '-DateTimeOriginal', '-s3'] + [str(p) for p in jpgs],
        capture_output=True, text=True
    )
    dates = sorted(d for d in result.stdout.splitlines() if re.match(r'20\d\d:', d))

    if not dates:
        print("No DateTimeOriginal found in photos.")
        sys.exit(1)

    print(f"Photos: {len(jpgs)}")
    print(f"First:  {dates[0]}")
    print(f"Last:   {dates[-1]}")
    print()

    # Group by day
    days = {}
    for d in dates:
        day = d[:10].replace(':', '-')
        days[day] = days.get(day, 0) + 1
    for day, count in sorted(days.items()):
        print(f"  {day}: {count} photos")

if __name__ == '__main__':
    main()
