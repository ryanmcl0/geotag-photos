#!/usr/bin/env python3
"""
Build a Lightroom Classic smart collection file (.lrsmcol) from a folder of
exported JPGs.

Each photo becomes a nested rule of (filename contains <stem>) AND (capture
date is <EXIF date>), so duplicate filenames across trips (Sony's rolling
RM numbering) don't drag strangers into the collection the way a plain
filename text-filter paste does.

Import in Lightroom Classic: right-click in the Collections panel >
"Import Smart Collection Settings..." and pick the .lrsmcol file.

Usage:
  ./lr_smart_collection.py DIR [--name NAME] [--out FILE]

  DIR      Folder of JPGs (e.g. a posts_pull.py output folder).
  --name   Collection title (default: the folder name).
  --out    Output path (default: DIR/<name>.lrsmcol).

Capture dates come from EXIF DateTimeOriginal via exiftool (preferred, one
batch call) or Pillow. Photos with no readable date fall back to a
filename-only rule with a warning.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

PHOTO_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp')


def photo_stem(path):
    """Full edited stem with only the posts_pull order prefix stripped:
    05_RM100321-Enhanced-NR.jpg -> RM100321-Enhanced-NR. Keeping the edit
    suffix pins the rule to the exact variant that was edited (the AI-denoise
    Enhanced DNG vs the original raw)."""
    stem = Path(path).stem
    return re.sub(r'^\d{2,3}_', '', stem)


def capture_dates(files):
    """path -> 'YYYY-MM-DD' from EXIF DateTimeOriginal (None if unreadable)."""
    dates = {f: None for f in files}
    if shutil.which('exiftool'):
        out = subprocess.run(
            ['exiftool', '-json', '-DateTimeOriginal', '-d', '%Y-%m-%d',
             *[str(f) for f in files]],
            capture_output=True, text=True)
        for rec in json.loads(out.stdout or '[]'):
            d = rec.get('DateTimeOriginal')
            if d:
                dates[Path(rec['SourceFile'])] = d
        return dates
    try:
        from PIL import Image
        for f in files:
            try:
                exif = Image.open(f).getexif().get_ifd(0x8769)
                raw = exif.get(36867)   # DateTimeOriginal "YYYY:MM:DD HH:MM:SS"
                if raw:
                    dates[f] = raw[:10].replace(':', '-')
            except Exception:
                pass
    except ImportError:
        sys.exit('❌ Need exiftool or Pillow to read capture dates.')
    return dates


def lua(value, indent):
    pad = '\t' * indent
    if isinstance(value, LuaMixed):
        return lua_mixed(value, indent)
    if isinstance(value, str):
        return '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"')
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        lines = ['{']
        for k, v in value.items():
            lines.append(f'{pad}\t{k} = {lua(v, indent + 1)},')
        lines.append(pad + '}')
        return '\n'.join(lines)
    if isinstance(value, list):
        lines = ['{']
        for v in value:
            lines.append(f'{pad}\t{lua(v, indent + 1)},')
        lines.append(pad + '}')
        return '\n'.join(lines)
    raise TypeError(type(value))


class LuaMixed:
    """Lua table with list items followed by named keys (LR's rule-group shape)."""
    def __init__(self, items, **keys):
        self.items, self.keys = items, keys


def lua_mixed(node, indent):
    pad = '\t' * indent
    lines = ['{']
    for v in node.items:
        body = lua_mixed(v, indent + 1) if isinstance(v, LuaMixed) else lua(v, indent + 1)
        lines.append(f'{pad}\t{body},')
    for k, v in node.keys.items():
        lines.append(f'{pad}\t{k} = {lua(v, indent + 1)},')
    lines.append(pad + '}')
    return '\n'.join(lines)


def write_lrsmcol(files, name, out_path):
    """Write the smart collection for `files`; returns (#rules, [undated files])."""
    dates = capture_dates(files)
    rules, seen, undated = [], set(), []
    for f in files:
        stem, date = photo_stem(f), dates.get(f)
        if (stem, date) in seen:
            continue
        seen.add((stem, date))
        photo_rules = [{'criteria': 'filename', 'operation': 'all', 'value': stem, 'value2': ''}]
        if '-' not in stem:
            # Plain stem = the original raw was the edit source; keep its
            # AI-denoise duplicate (RMxxxxxx-Enhanced-NR.dng) out.
            photo_rules.append({'criteria': 'filename', 'operation': 'noneOf',
                                'value': 'Enhanced', 'value2': ''})
        if date is None:
            undated.append(f)
        else:
            photo_rules.append({'criteria': 'captureTime', 'operation': '==', 'value': date})
        rules.append(LuaMixed(photo_rules, combine='intersect'))

    doc = LuaMixed([], id=str(uuid.uuid4()).upper(), internalName=name, title=name,
                   type='LibrarySmartCollection',
                   value=LuaMixed(rules, combine='union'), version=0)
    out_path.write_text('s = ' + lua_mixed(doc, 0) + '\n')
    return len(rules), undated


def main():
    ap = argparse.ArgumentParser(description='Folder of JPGs -> Lightroom smart collection file.')
    ap.add_argument('dir', help='Folder of photos')
    ap.add_argument('--name', help='Collection title (default: folder name)')
    ap.add_argument('--out', help='Output .lrsmcol path')
    args = ap.parse_args()

    folder = Path(args.dir)
    if not folder.is_dir():
        sys.exit(f'❌ Not a directory: {folder}')
    files = sorted(f for f in folder.iterdir()
                   if f.is_file() and f.suffix.lower() in PHOTO_EXTS)
    if not files:
        sys.exit(f'❌ No photos in {folder}')
    name = args.name or folder.name
    out_path = Path(args.out) if args.out else folder / f'{name}.lrsmcol'
    n, undated = write_lrsmcol(files, name, out_path)
    for f in undated:
        print(f'⚠️  {f.name}: no EXIF capture date, matched by filename only')
    print(f'✓ {n} rule(s) → {out_path}')
    print('  Lightroom Classic: right-click the Collections panel > '
          '"Import Smart Collection Settings..."')


if __name__ == '__main__':
    main()
