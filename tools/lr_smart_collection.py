#!/usr/bin/env python3
"""
Build a Lightroom Classic smart collection file (.lrsmcol) from a folder of
exported JPGs.

Each photo becomes a nested rule of (filename contains <name>) AND (capture
date is <date>), so duplicate filenames across trips (Sony's rolling RM
numbering) don't drag strangers into the collection the way a plain
filename text-filter paste does.

When the Lightroom catalog is readable (LR_CATALOG, default
~/Pictures/Lightroom/Lightroom Catalog.lrcat, opened read-only), each photo
is first looked up there and the rule pinned to the exact catalog file:
full filename with extension (RM100323.ARW, so the -Enhanced-NR.dng or a
-Edit-2.tif sibling can't match) and the catalog's own capture date (which
can differ from a clock-corrected export). Photos not found in the catalog
fall back to the stem + EXIF-date rule below.

Import in Lightroom Classic: right-click in the Collections panel >
"Import Smart Collection Settings..." and pick the .lrsmcol file.

Usage:
  ./lr_smart_collection.py DIR [--name NAME] [--out FILE]

  DIR      Folder of JPGs (e.g. a ./post.py pull output folder).
  --name   Collection title (default: the folder name).
  --out    Output path (default: DIR/<name>.lrsmcol).

Capture dates come from EXIF DateTimeOriginal via exiftool (preferred, one
batch call) or Pillow. Exports saved without metadata can be given a date by
the caller (post.py uses the trip's exif_cache.json); photos still undated
fall back to a filename-only rule with a warning. Copy suffixes (" (2)",
"-2") on an export are tried both ways against the catalog, since "-2" is
sometimes an export duplicate and sometimes a real catalog file.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

PHOTO_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp')


def photo_stem(path):
    """Full edited stem with only the post-pull order prefix stripped:
    05_RM100321-Enhanced-NR.jpg -> RM100321-Enhanced-NR. Keeping the edit
    suffix pins the rule to the exact variant that was edited (the AI-denoise
    Enhanced DNG vs the original raw)."""
    return re.sub(r'^\d{2,3}_', '', Path(path).stem)


def stem_candidates(stem):
    """Catalog names an export may have come from, most specific first.
    Finder " (2)" and LR export "-2" duplicate suffixes are usually not in
    the catalog (RM101061-2.jpg came from RM101061.DNG), but "-2" sometimes
    is (DSC04476-Edit-2.tif), so try the full stem before the stripped one."""
    out = [stem]
    stripped = re.sub(r'(?: \(\d+\)|-\d{1,2})$', '', stem)
    if stripped != stem:
        out.append(stripped)
    return out


DEFAULT_CATALOG = Path.home() / 'Pictures/Lightroom/Lightroom Catalog.lrcat'
_catalog_cache = {}


def catalog_index(path=None):
    """lowercased baseName -> [(filename.ext, 'YYYY-MM-DD' | None)] for every
    photo in the LR catalog, or None when it can't be read. Read-only; safe
    while Lightroom has the catalog open."""
    path = Path(path or os.environ.get('LR_CATALOG') or DEFAULT_CATALOG)
    if path in _catalog_cache:
        return _catalog_cache[path]
    index = None
    if path.exists():
        try:
            db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            rows = db.execute("""
                SELECT DISTINCT f.baseName, f.extension, substr(i.captureTime, 1, 10)
                FROM Adobe_images i JOIN AgLibraryFile f ON f.id_local = i.rootFile""")
            index = {}
            for base, ext, date in rows:
                if not base:
                    continue
                name = f'{base}.{ext}' if ext else base
                date = date if date and re.match(r'\d{4}-\d{2}-\d{2}$', date) else None
                index.setdefault(base.lower(), []).append((name, date))
            db.close()
        except sqlite3.Error as e:
            print(f'⚠️  Lightroom catalog not readable ({e}); using filename rules only')
            index = None
    _catalog_cache[path] = index
    return index


def resolve_in_catalog(index, stem, date):
    """(filename.ext, catalog date) of the one catalog photo this export came
    from, or None if it isn't in the catalog or can't be told apart."""
    if not index:
        return None
    for cand in stem_candidates(stem):
        hits = list(set(index.get(cand.lower(), [])))
        if not hits:
            continue
        if date:
            same_day = [h for h in hits if h[1] == date]
            # No same-day hit: the export's date may be clock-corrected; a
            # name that is unique in the catalog is still safe to trust.
            hits = same_day or (hits if len(hits) == 1 else [])
        if len(hits) == 1:
            return hits[0]
        return None
    return None


def self_dated(stem):
    """DJI_20240913064105_0716_D: the name embeds the capture time, so it is
    unique on its own and needs no date rule (a cached date can be
    clock-corrected and would then match nothing)."""
    return re.match(r'DJI_\d{14}', stem) is not None


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


def write_lrsmcol(files, name, out_path, fallback_dates=None, notes=None):
    """Write the smart collection for `files`; returns (#rules, [undated files]).
    `fallback_dates` maps file -> 'YYYY-MM-DD' for files whose own EXIF has
    no capture date. If `notes` is a list, (file, message) pairs are appended
    for photos that could not be pinned to one catalog file."""
    dates = capture_dates(files)
    for f in files:
        if dates.get(f) is None and not self_dated(photo_stem(f)):
            dates[f] = (fallback_dates or {}).get(f)
    index = catalog_index()
    rules, seen, undated = [], set(), []
    for f in files:
        stem, date = photo_stem(f), dates.get(f)
        hit = resolve_in_catalog(index, stem, date)
        if hit:
            key = hit
            photo_rules = [{'criteria': 'filename', 'operation': 'all', 'value': hit[0], 'value2': ''}]
            if hit[1]:
                photo_rules.append({'criteria': 'captureTime', 'operation': '==', 'value': hit[1]})
            if key not in seen:
                seen.add(key)
                rules.append(LuaMixed(photo_rules, combine='intersect'))
            continue
        if notes is not None and index is not None:
            names = [c for c in stem_candidates(stem) if c.lower() in index]
            notes.append((f, 'not in the Lightroom catalog' if not names
                          else 'several catalog photos share this name and date'))
        stem = stem_candidates(stem)[-1] if re.search(r' \(\d+\)$', stem) else stem
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
            if not self_dated(stem):
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
    notes = []
    n, undated = write_lrsmcol(files, name, out_path, notes=notes)
    for f, why in notes:
        print(f'⚠️  {f.name}: {why}')
    for f in undated:
        print(f'⚠️  {f.name}: no EXIF capture date, matched by filename only')
    print(f'✓ {n} rule(s) → {out_path}')
    print('  Lightroom Classic: right-click the Collections panel > '
          '"Import Smart Collection Settings..."')


if __name__ == '__main__':
    main()
