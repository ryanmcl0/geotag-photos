#!/usr/bin/env python3
"""
correlate_phone_gps.py — derive a GPS coordinate per location folder by matching
main-camera photo timestamps to GPS-tagged phone photos taken at the same time.

When a trip's main camera (Sony/etc.) has GPS off but you carried a phone (which
geotags), the phone acts as a sparse position reference. For each location-named
subfolder under the raws root, this finds the phone photo nearest in time to each
camera shot and reports the median coordinate — a single point per location (ideal
for one-cluster-per-building placement, e.g. feeding locations.json).

Cameras are often left on the wrong timezone — and the offset can even change
mid-trip if you reset the clock — so the camera→phone offset is auto-detected
per folder. Pure timestamp matching is ambiguous over dense, wide-area tracks, so
the trusted offset set is CALIBRATED against drone (DJI) GPS anchors when present.
NOTE: still imperfect for folders without their own drone anchor — verify the
coord (e.g. on the map) or pass --offset before trusting it. See
docs/no-gps-placement.md "Known limitations".

The phone GPS track is cached to <raws>/.phone_gps_track.json so re-runs are fast.

Usage:
  python3 correlate_phone_gps.py --raws "/Volumes/RYAN/2026/02-03.26 China CNY"
  python3 correlate_phone_gps.py --raws <root> --exclude Bridges --per-raw
  python3 correlate_phone_gps.py --raws <root> --offset 0 --out coords.json
"""

import bisect
import collections
import json
import os
import statistics
import subprocess
from datetime import datetime

import click

PHONE_EXTS = ('.heic', '.jpg', '.jpeg', '.png')
CAMERA_EXTS = ('.jpg', '.jpeg', '.arw', '.dng', '.raf', '.nef', '.cr2', '.cr3')
TRACK_CACHE = '.phone_gps_track.json'


def _epoch(s):
    try:
        return datetime.strptime(s[:19], '%Y:%m:%d %H:%M:%S').timestamp()
    except (ValueError, TypeError):
        return None


def _exif(files, tags):
    """Batch exiftool -j over files; returns list of dicts."""
    out = []
    for i in range(0, len(files), 400):
        r = subprocess.run(['exiftool', '-n', '-j'] + tags + files[i:i + 400],
                           capture_output=True, text=True)
        try:
            out += json.loads(r.stdout or '[]')
        except json.JSONDecodeError:
            pass
    return out


def _walk(root, exts, skip_dirs=()):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in skip_dirs]
        for f in fn:
            if f.lower().endswith(exts):
                yield os.path.join(dp, f)


def build_phone_track(raws, phone_dir):
    """[(epoch, lat, lon)] for GPS-tagged phone photos, cached by file count."""
    proot = os.path.join(raws, phone_dir)
    files = sorted(_walk(proot, PHONE_EXTS)) if os.path.isdir(proot) else []
    cache = os.path.join(raws, TRACK_CACHE)
    try:
        c = json.load(open(cache))
        if c.get('n') == len(files):
            return [tuple(p) for p in c['track']]
    except (OSError, json.JSONDecodeError):
        pass
    click.echo(f"  reading {len(files)} phone files for GPS...", err=True)
    track = []
    for d in _exif(files, ['-DateTimeOriginal', '-GPSLatitude', '-GPSLongitude']):
        t = _epoch(d.get('DateTimeOriginal'))
        la, lo = d.get('GPSLatitude'), d.get('GPSLongitude')
        if t and la is not None and lo is not None:
            track.append((t, la, lo))
    track.sort()
    try:
        json.dump({'n': len(files), 'track': track}, open(cache, 'w'))
    except OSError:
        pass
    return track


def nearest_factory(track):
    times = [p[0] for p in track]

    def nearest(t):
        i = bisect.bisect_left(times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(track):
                dt = abs(track[j][0] - t)
                if best is None or dt < best[0]:
                    best = (dt, track[j][1], track[j][2])
        return best  # (gap_seconds, lat, lon) or None
    return nearest


def _haversine(la1, lo1, la2, lo2):
    from math import radians, sin, cos, asin, sqrt
    la1, lo1, la2, lo2 = map(radians, (la1, lo1, la2, lo2))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


def _offset_scores(times, nearest, candidates):
    """For one folder: {offset: (tight_count, spread_m, lat, lon)} over tight (<10min)
    matches. spread = median distance of matched points to their centroid (compactness)."""
    out = {}
    for off in candidates:
        pts = [(nb[1], nb[2]) for t in times
               if (nb := nearest(t + off * 3600)) and nb[0] <= 600]
        if len(pts) < 2:
            out[off] = (len(pts), 9e9, None, None)
            continue
        mla = statistics.median(p[0] for p in pts)
        mlo = statistics.median(p[1] for p in pts)
        spread = statistics.median(_haversine(mla, mlo, la, lo) for la, lo in pts)
        out[off] = (len(pts), spread, mla, mlo)
    return out


def resolve_offsets(folder_times, nearest, anchors=None,
                    candidates=range(-14, 15), max_spread=2000, anchor_km=1.5):
    """Pick a camera→phone offset per folder.

    Pure timestamp matching is ambiguous when the phone track is dense over a wide area
    (a wrong offset can map several folders compactly onto the SAME wrong cluster). So the
    set of trusted offsets is CALIBRATED against ground truth where available:
      - `anchors` = {folder: (lat, lon)} of known coords (e.g. drone GPS taken on location).
        A candidate offset is "valid" only if, for some anchor folder, the phone-correlated
        centroid at that offset lands within `anchor_km` of the anchor.
      - With no anchors, fall back to cross-folder consensus (best for >=2 folders).
    Each folder then picks its best tight+compact offset from the valid set."""
    scores = {d: _offset_scores([t for _, t in ts], nearest, candidates)
              for d, ts in folder_times.items()}

    def best_for(d, allowed):
        cand = [(o, s[0]) for o, s in scores[d].items()
                if o in allowed and s[0] >= 2 and s[1] <= max_spread]
        return max(cand, key=lambda x: x[1])[0] if cand else None

    valid = set()
    if anchors:
        for d, (alat, alon) in anchors.items():
            if d not in scores:
                continue
            for o, (cnt, spr, la, lo) in scores[d].items():
                if cnt >= 2 and la is not None and _haversine(la, lo, alat, alon) <= anchor_km * 1000:
                    valid.add(o)
    if not valid:                       # no anchors / none matched: cross-folder consensus
        votes = collections.Counter()
        for d in folder_times:
            b = best_for(d, set(candidates))
            if b is not None:
                votes[b] += 1
        valid = {o for o, v in votes.items() if v >= 2} or set(votes)
    chosen = {d: best_for(d, valid) for d in folder_times}
    return chosen, valid


@click.command()
@click.option('--raws', required=True, help='Trip raws root (location subfolders + phone folder)')
@click.option('--phone-dir', default='Phone', help='Phone subfolder name (default: Phone)')
@click.option('--offset', default=None, type=int,
              help='Force camera→phone hour offset for all folders (default: auto, per-folder)')
@click.option('--window', default=60, type=int, help='Max match gap in minutes (default: 60)')
@click.option('--exclude', multiple=True, help='Subfolder names to skip (repeatable)')
@click.option('--per-raw', is_flag=True, help='Also list matched raws + their phone GPS')
@click.option('--out', default=None, help='Write folder->coord JSON here')
def main(raws, phone_dir, offset, window, exclude, per_raw, out):
    raws = raws.rstrip('/')
    skip = set(exclude) | {phone_dir}
    track = build_phone_track(raws, phone_dir)
    if not track:
        click.echo("No GPS-tagged phone photos found — cannot correlate.", err=True)
        raise SystemExit(1)
    nearest = nearest_factory(track)
    click.echo(f"phone GPS points: {len(track)}", err=True)

    folders = sorted(d for d in os.listdir(raws)
                     if os.path.isdir(os.path.join(raws, d)) and d not in skip)
    folder_times = {}
    for d in folders:
        rows = _exif(list(_walk(os.path.join(raws, d), CAMERA_EXTS)), ['-DateTimeOriginal'])
        folder_times[d] = [(os.path.basename(r['SourceFile']), _epoch(r.get('DateTimeOriginal')))
                           for r in rows if _epoch(r.get('DateTimeOriginal'))]

    if offset is not None:
        chosen = {d: offset for d in folders}
    else:
        # ground-truth anchors from drone (DJI) GPS taken on location
        anchors = {}
        for d in folders:
            dji = [f for f in _walk(os.path.join(raws, d), ('.jpg', '.dng'))
                   if 'dji' in os.path.basename(f).lower()]
            rows = _exif(dji[:8], ['-GPSLatitude', '-GPSLongitude']) if dji else []
            pts = [(r['GPSLatitude'], r['GPSLongitude']) for r in rows
                   if r.get('GPSLatitude') is not None and r.get('GPSLongitude') is not None]
            if pts:
                anchors[d] = (statistics.median(p[0] for p in pts),
                              statistics.median(p[1] for p in pts))
        chosen, valid = resolve_offsets(folder_times, nearest, anchors=anchors)
        click.echo(f"drone-GPS anchors: {len(anchors)} folder(s); "
                   f"valid clock offset(s): "
                   + (", ".join(f"{o:+d}h" for o in sorted(valid)) or "none"), err=True)

    win = window * 60
    results = {}
    print(f"\n{'location folder':38} {'coord (median)':22} off  gap  n/total")
    print("-" * 82)
    for d in folders:
        off = chosen.get(d)
        if off is None:
            off = 0
        matched = []
        for name, t in folder_times[d]:
            nb = nearest(t + off * 3600)
            if nb and nb[0] <= win:
                matched.append((name, nb[0], nb[1], nb[2]))
        total = len(folder_times[d])
        if matched:
            la = round(statistics.median(m[2] for m in matched), 6)
            lo = round(statistics.median(m[3] for m in matched), 6)
            g = int(statistics.median(m[1] for m in matched) / 60)
            results[d] = {'lat': la, 'lon': lo, 'offset_h': off,
                          'median_gap_min': g, 'matched': len(matched), 'total': total}
            print(f"{d[:38]:38} {f'{la},{lo}':22} {off:+d}h {g:3}m  {len(matched)}/{total}")
            if per_raw:
                for name, gap, la2, lo2 in matched[:8]:
                    print(f"      {name:24} {int(gap/60):3}m -> {la2:.6f},{lo2:.6f}")
        else:
            results[d] = None
            print(f"{d[:38]:38} {'— no phone match':22} {off:+d}h        0/{total}")

    if out:
        json.dump(results, open(out, 'w'), indent=2)
        click.echo(f"\nwrote {out}", err=True)


if __name__ == '__main__':
    main()
