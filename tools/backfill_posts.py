#!/usr/bin/env python3
"""Backfill post drafts from exported folders under /Volumes/RYAN/Edits/Posts.

These folders predate the Posts feature (and its NN_ order prefixes), so each
exported JPEG is resolved back to its {trip, id} manifest ref:

  1. by filename stem (plus -Enhanced-NR / -Edit variants), then
  2. disambiguated by EXIF DateTimeOriginal against the manifest timestamp -
     stems repeat across trips (the camera rolls its counter over), so the
     capture time is what actually identifies a photo. A whole/half-hour
     difference is accepted as a geosync clock correction.
  3. Stems that match nothing fall back to a global capture-time lookup,
     which catches files renamed after export.

Carousel order is the export mtime order (the closest thing to the original
ordering that survives; nothing else was recorded).

Usage:
  ./venv/bin/python tools/backfill_posts.py --list          # resolve, print, save nothing
  ./venv/bin/python tools/backfill_posts.py --push prod     # create the drafts
Options: --posted/--no-posted (default: mark posted), --platform ig|xhs.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import photo_privacy                                    # noqa: E402
from auto_curate_posts import load_env, push            # noqa: E402

POSTS_ROOT = Path('/Volumes/RYAN/Edits/Posts')
IMG_EXT = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp')
SUFFIXES = ('-Enhanced-NR-2', '-Enhanced-NR', '-Edit', '-Enhanced', '-2')
MAX_PHOTOS = 20


def build_index():
    """id -> [{trip, id, ts, ar}] and ts -> [same] over every full manifest."""
    by_id, by_ts = {}, {}
    for mf in sorted((ROOT / 'web' / 'trips').glob('*/manifest.json')):
        m = photo_privacy.load_full_manifest(mf.parent)
        if not m:
            continue
        for p in m.get('photos', []):
            rec = {'trip': mf.parent.name, 'id': p['id'],
                   'ts': (p.get('timestamp') or '')[:19], 'ar': p.get('ar')}
            by_id.setdefault(p['id'], []).append(rec)
            if rec['ts']:
                by_ts.setdefault(rec['ts'], []).append(rec)
    return by_id, by_ts


def exif_times(files):
    """path -> 'YYYY-MM-DDTHH:MM:SS' from DateTimeOriginal (one exiftool run)."""
    if not files:
        return {}
    out = subprocess.run(
        ['exiftool', '-q', '-j', '-DateTimeOriginal', *[str(f) for f in files]],
        capture_output=True, text=True)
    times = {}
    try:
        for rec in json.loads(out.stdout or '[]'):
            raw = (rec.get('DateTimeOriginal') or '')[:19]
            if len(raw) == 19:
                times[rec['SourceFile']] = raw.replace(':', '-', 2).replace(' ', 'T')
    except json.JSONDecodeError:
        pass
    return times


def stem_variants(stem):
    yield stem
    for suf in SUFFIXES:
        if stem.endswith(suf):
            yield stem[:-len(suf)]


def near(ts_a, ts_b, tol_min=2):
    """Same instant, or offset by a whole/half hour (geosync clock fix)."""
    try:
        a = datetime.strptime(ts_a, '%Y-%m-%dT%H:%M:%S')
        b = datetime.strptime(ts_b, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None
    delta = abs((a - b).total_seconds())
    if delta <= tol_min * 60:
        return 'exact'
    for half_hours in range(1, 29):          # +/- 14h in half-hour steps
        off = half_hours * 1800
        if abs(delta - off) <= tol_min * 60:
            return 'offset'
    return None


def same_clock(ts_a, ts_b):
    """Same wall-clock time on a different date: the camera's date was misset
    (e.g. a 2022 trip stamped 2014) and the pipeline corrected it from GPS."""
    return len(ts_a) == 19 and len(ts_b) == 19 and ts_a[11:] == ts_b[11:]


def dji_stamp(stem):
    """DJI_YYYYMMDDHHMMSS_NNNN_D -> the ISO time encoded in the filename."""
    m = re.match(r'DJI_(\d{14})_', stem)
    if not m:
        return None
    d = m.group(1)
    return f'{d[0:4]}-{d[4:6]}-{d[6:8]}T{d[8:10]}:{d[10:12]}:{d[12:14]}'


def resolve(path, ts, by_id, by_ts):
    """-> (rec, how) or (None, reason)."""
    cands = []
    for st in stem_variants(path.stem):
        cands.extend(by_id.get(st, []))
    if ts:
        exact = [c for c in cands if near(ts, c['ts']) == 'exact']
        if len(exact) == 1:
            return exact[0], 'stem+time'
        if len(exact) > 1:
            return exact[0], 'stem+time(dup)'
        shifted = [c for c in cands if near(ts, c['ts']) == 'offset']
        if len(shifted) == 1:
            return shifted[0], 'stem+clockshift'
        # renamed since export: find it by capture time alone
        hits = by_ts.get(ts, [])
        if len(hits) == 1:
            return hits[0], 'time only'
        if len(hits) > 1:
            return hits[0], 'time only(dup)'
        # A DJI filename carries its own capture stamp, so those stems are
        # globally unique (verified: no collisions across every manifest). The
        # manifest time often differs because the drone clock was corrected
        # against the GPX track - the stem still identifies the photo.
        if dji_stamp(path.stem) and len(cands) == 1:
            return cands[0], 'dji filename stamp'
        # Same wall-clock on another date = the camera's date was misset and
        # the pipeline fixed it; still the same photo when the stem is unique.
        clock = [c for c in cands if same_clock(ts, c['ts'])]
        if len(clock) == 1:
            return clock[0], 'stem+dateshift'
        # A lone stem candidate whose capture time disagrees is NOT this photo:
        # the counter rolls over, so the real source simply is not indexed.
        if cands:
            return None, (f'stem matches {cands[0]["trip"]} but its capture time is '
                          f'{cands[0]["ts"]}, not {ts}')
        return None, 'not in any manifest'
    if len(cands) == 1:
        return cands[0], 'stem only (no EXIF time)'
    if cands:
        return None, f'ambiguous stem ({len({c["trip"] for c in cands})} trips), no capture time'
    return None, 'not in any manifest'


def folder_posts(folders, by_id, by_ts, platform, posted):
    posts, report = [], []
    for rel in folders:
        d = POSTS_ROOT / rel
        if not d.is_dir():
            report.append((rel, [], [f'missing folder {d}']))
            continue
        files = sorted([p for p in d.iterdir()
                        if p.suffix.lower() in IMG_EXT and not p.name.startswith('.')],
                       key=lambda p: p.stat().st_mtime)
        times = exif_times(files)
        refs, problems = [], []
        seen = set()
        for f in files:
            rec, how = resolve(f, times.get(str(f), ''), by_id, by_ts)
            if not rec:
                problems.append(f'{f.name}: {how}')
                continue
            key = (rec['trip'], rec['id'])
            if key in seen:
                problems.append(f'{f.name}: duplicate of another file in this folder')
                continue
            seen.add(key)
            ref = {'trip': rec['trip'], 'id': rec['id']}
            if isinstance(rec.get('ar'), (int, float)):
                ref['ar'] = rec['ar']
            refs.append((ref, how))
        report.append((rel, refs, problems))
        if not refs:
            continue
        name = rel.split('/')[-1]
        if name.isdigit():          # Posted/0 .. Posted/4 need a real title
            name = f'Posted {name}'
        cap = MAX_PHOTOS if platform == 'ig' else 18
        kept = [r for r, _ in refs][:cap]
        if len(refs) > cap:
            problems.append(f'trimmed {len(refs) - cap} over the {cap} cap')
        post = {
            'id': 'b' + hashlib.sha1(rel.encode()).hexdigest()[:10],
            'name': name,
            'created': datetime.fromtimestamp(d.stat().st_mtime).isoformat() + 'Z',
            'photos': kept,
        }
        if platform == 'xhs':
            post['platform'] = 'xhs'
        if posted:
            post['posted'] = True
        posts.append(post)
    return posts, report


DEFAULT_FOLDERS = [
    '5 Asia v1', '6 Asia v1', '10 Europe activities', '11 TW Earthquake',
    '12 Far wide dump', '13 Norway bridge', 'NW China', 'roofs road',
    'Posted/0', 'Posted/1', 'Posted/2', 'Posted/3', 'Posted/4', 'Posted/7 NYC',
    'Posted/8 Chinese Time of Life', 'Posted/9 Far and Wide', 'Posted/Exploring',
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--list', action='store_true', help='resolve and print only')
    ap.add_argument('--push', metavar='TARGET', help="'local', 'prod' or a base URL")
    ap.add_argument('--platform', default='ig', choices=['ig', 'xhs'])
    ap.add_argument('--posted', dest='posted', action='store_true', default=True)
    ap.add_argument('--no-posted', dest='posted', action='store_false')
    ap.add_argument('--folders', nargs='*', help='override the folder list (relative to Posts/)')
    args = ap.parse_args()

    if not POSTS_ROOT.exists():
        sys.exit(f'✗ {POSTS_ROOT} not mounted')
    by_id, by_ts = build_index()
    print(f'Indexed {len(by_id)} photo ids from the manifests')
    posts, report = folder_posts(args.folders or DEFAULT_FOLDERS, by_id, by_ts,
                                 args.platform, args.posted)

    total = unresolved = 0
    for rel, refs, problems in report:
        how = {}
        for _, h in refs:
            how[h] = how.get(h, 0) + 1
        total += len(refs)
        unresolved += len([p for p in problems if ': ' in p])
        print(f"\n{rel}: {len(refs)} resolved  {how}")
        for p in problems:
            print(f'    ! {p}')
    print(f'\n{len(posts)} posts, {total} photos resolved, {unresolved} unresolved')
    if args.list or not args.push:
        return
    push(posts, args.push, mode='append', which='main')


if __name__ == '__main__':
    main()
