#!/usr/bin/env python3
"""Fix wrong drone-clock timestamps in trip manifests.

The DJI drone clock drifts or reverts to a stale sync date, so drone photos
land on completely wrong dates (and wrong gallery order) even though their
GPS is correct. For every trip: detect DJI photos whose dates fall outside
the trip's camera-photo date range, then recover each one's true local time
from WHERE it was taken — nearest timestamped GPX trackpoint to the photo's
GPS position (the trip timezone offset is inferred from the camera photos'
own manifest-time vs track-UTC relationship).

Applies to manifest.json + manifest.all.json + exif_cache.json (so the fix
survives reprocessing) and refreshes the trip's dates in trips/index.json.

Usage: fix_drone_dates.py [--apply] [--trip SLUG]   (default: dry-run report)
"""
import argparse
import bisect
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / "web" / "trips"
MAX_SNAP_KM = 3.0
OUTLIER_MARGIN = timedelta(hours=36)

TS = "%Y-%m-%dT%H:%M:%SZ"


def parse_gpx_points(gpx_path):
    """[(t_utc, lat, lon)] from a gpx file or a directory of them."""
    pts = []
    paths = []
    p = Path(gpx_path)
    if p.is_dir():
        paths = sorted(p.rglob("*.gpx"))
    elif p.exists():
        paths = [p]
    rx = re.compile(
        r'<trkpt[^>]*lat="([-\d.]+)"[^>]*lon="([-\d.]+)"[^>]*>(.*?)</trkpt>', re.S)
    trx = re.compile(r"<time>(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
    for path in paths:
        try:
            txt = path.read_text(errors="ignore")
        except OSError:
            continue
        for m in rx.finditer(txt):
            t = trx.search(m.group(3))
            if t:
                pts.append((datetime.strptime(t.group(1), "%Y-%m-%dT%H:%M:%S"),
                            float(m.group(1)), float(m.group(2))))
    pts.sort()
    return pts


def dist_km(lat1, lon1, lat2, lon2):
    dx = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110.54
    return math.hypot(dx, dy)


def nearest_point(pts, lat, lon):
    best = (None, 1e9)
    for t, plat, plon in pts:
        d = dist_km(lat, lon, plat, plon)
        if d < best[1]:
            best = (t, d)
    return best


def visit_candidates(pts, lat, lon, max_km=MAX_SNAP_KM, gap_min=30, k=6):
    """Distinct passes near (lat, lon): trips revisit places (loops, out-and-
    back), so a photo has one candidate time per pass, not just the nearest.
    Returns [(time, dist_km)] sorted by dist, at most k."""
    near = [(t, dist_km(lat, lon, plat, plon)) for t, plat, plon in pts
            if dist_km(lat, lon, plat, plon) <= max_km]
    near.sort()
    visits = []
    for t, d in near:
        if visits and (t - visits[-1][-1][0]).total_seconds() <= gap_min * 60:
            visits[-1].append((t, d))
        else:
            visits.append([(t, d)])
    cands = [min(v, key=lambda x: x[1]) for v in visits]
    cands.sort(key=lambda x: x[1])
    return cands[:k]


def is_dji(p):
    return p.get("source_filename", "").upper().startswith("DJI")


def analyze_trip(tdir):
    mp = tdir / "manifest.all.json"
    if not mp.exists():
        mp = tdir / "manifest.json"
    if not mp.exists():
        return None
    m = json.loads(mp.read_text())
    photos = m.get("photos", [])
    ref = [p for p in photos if not is_dji(p) and p.get("timestamp")]
    dji = [p for p in photos if is_dji(p) and p.get("timestamp")]
    if not dji or len(ref) < 5:
        return None
    ref_ts = sorted(datetime.strptime(p["timestamp"], TS) for p in ref)
    lo, hi = ref_ts[0] - OUTLIER_MARGIN, ref_ts[-1] + OUTLIER_MARGIN
    outliers = [p for p in dji
                if not lo <= datetime.strptime(p["timestamp"], TS) <= hi]
    return {"dir": tdir, "manifest": m, "n_dji": len(dji), "outliers": outliers,
            "ref_range": (ref_ts[0], ref_ts[-1]),
            "gpx": (m.get("source") or {}).get("gpx_path")}


def infer_tz_offset(manifest, pts):
    """Median (manifest local time - track UTC) over GPS-placed camera photos,
    rounded to 30 min. Falls back to sampling all placed photos."""
    samples = []
    for p in manifest.get("photos", []):
        if is_dji(p) or p.get("lat") is None or not p.get("timestamp"):
            continue
        t, d = nearest_point(pts, p["lat"], p["lon"])
        if t is None or d > MAX_SNAP_KM:
            continue
        local = datetime.strptime(p["timestamp"], TS)
        samples.append((local - t).total_seconds())
        if len(samples) >= 60:
            break
    if not samples:
        return None
    samples.sort()
    med = samples[len(samples) // 2]
    return timedelta(seconds=round(med / 1800) * 1800)


def slugify(name):
    out = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return out


def config_gpx_for(slug):
    """The manifest's gpx_path is often a dead temp file (merged GPX); the
    real source lives in config/trips.json."""
    cfg_p = ROOT / "config" / "trips.json"
    if not cfg_p.exists():
        return None
    cfg = json.loads(cfg_p.read_text())
    for grp in ("public", "private"):
        for t in cfg.get(grp, []):
            if slugify(t.get("name", "")) == slug and t.get("gpx"):
                return t["gpx"]
    return None


def correct_trip(info, apply):
    tdir = info["dir"]
    gpx = info["gpx"]
    if not gpx or not Path(gpx).exists():
        gpx = config_gpx_for(tdir.name)
        if gpx:
            print(f"   using config gpx: {gpx}")
    pts = parse_gpx_points(gpx) if gpx else []
    if pts:
        tz = infer_tz_offset(info["manifest"], pts)
        if tz is None:
            print("   ⚠️  could not infer timezone offset")
            return {}
        print(f"   tz offset inferred: {tz}")
    else:
        # No track: anchor on the placed camera photos instead — a drone
        # shot's true time is close to camera shots from the same spot.
        pts = [(datetime.strptime(p["timestamp"], TS), p["lat"], p["lon"])
               for p in info["manifest"].get("photos", [])
               if not is_dji(p) and p.get("lat") is not None and p.get("timestamp")]
        pts.sort()
        if len(pts) < 5:
            print("   ⚠️  no timestamped GPX and too few placed camera photos")
            return {}
        tz = timedelta(0)   # camera-photo anchors are already manifest-local
        print(f"   no GPX — anchoring on {len(pts)} placed camera photos")
    # The drone clock is wrong but RUNS correctly: within a "clock era"
    # (between syncs/resets) every photo shares one constant offset. Vote for
    # era offsets across all photos' candidate track visits, then correct by
    # original + era offset — burst spacing is preserved exactly and order
    # stays monotonic, immune to wrong-pass snapping at revisited spots.
    ordered = sorted(info["outliers"], key=lambda p: p["timestamp"])
    per_photo = []       # (photo, orig_dt, [candidate offsets seconds])
    for p in ordered:
        orig = datetime.strptime(p["timestamp"], TS)
        cands = (visit_candidates(pts, p["lat"], p["lon"], k=8)
                 if p.get("lat") is not None else [])
        per_photo.append((p, orig, [ (t - orig).total_seconds() for t, _ in cands ]))

    BIN = 1800  # 30-minute offset bins for era voting
    from collections import Counter, defaultdict
    support = Counter()
    for _, _, offs in per_photo:
        for o in {round(o / BIN) for o in offs}:
            support[o] += 1
    min_support = max(3, len(per_photo) // 20)
    strong = sorted(b for b, n in support.items() if n >= min_support)
    if not strong:
        # Last resort for short trips with no usable anchors: shift the whole
        # drone batch by one constant offset so its median lands on the
        # camera photos' median moment — relative spacing preserved.
        lo, hi = info["ref_range"]
        if (hi - lo) <= timedelta(days=2) and per_photo:
            origs = sorted(o for _, o, _ in per_photo)
            ref_mid = lo + (hi - lo) / 2
            shift = ref_mid - origs[len(origs) // 2]
            fixes = {photo["id"]: (orig + shift).strftime(TS)
                     for photo, orig, _ in per_photo}
            print(f"   fallback: centered batch on trip (shift {shift})")
            return apply_fixes(info, fixes, apply) if apply else fixes
        print("   ⚠️  no consistent clock offset found")
        return {}
    # merge bins closer than 2h into era groups (one slowly-drifting clock),
    # keep genuinely distinct sync states (>2h apart) separate
    groups = [[strong[0]]]
    for b in strong[1:]:
        if (b - groups[-1][-1]) * BIN <= 7200:
            groups[-1].append(b)
        else:
            groups.append([b])
    def group_of(off):
        for gi, g in enumerate(groups):
            if min(abs(off - b * BIN) for b in g) <= 5400:
                return gi
        return None

    # pick each photo's best candidate within its era group, preferring to
    # stay in the current group (clock re-syncs are rare events)
    current = None
    chosen = [None] * len(per_photo)
    grp = [None] * len(per_photo)
    for i, (photo, orig, offs) in enumerate(per_photo):
        cands = [(o, group_of(o)) for o in offs]
        cands = [(o, g) for o, g in cands if g is not None]
        if not cands:
            continue
        in_cur = [c for c in cands if c[1] == current]
        pick = in_cur[0] if in_cur else max(
            cands, key=lambda c: sum(support[b] for b in groups[c[1]]))
        chosen[i], grp[i] = pick
        current = pick[1]
    # backfill unmatched photos from sequence neighbours
    for i in range(len(chosen)):
        if chosen[i] is None:
            for j in list(range(i - 1, -1, -1)) + list(range(i + 1, len(chosen))):
                if chosen[j] is not None:
                    chosen[i], grp[i] = chosen[j], grp[j]
                    break
    # a real clock change is a contiguous run; absorb short blips (wrong-pass
    # matches) into the surrounding era
    MIN_RUN = 5
    runs = []
    for i, g in enumerate(grp):
        if runs and runs[-1][0] == g:
            runs[-1][1].append(i)
        else:
            runs.append([g, [i]])
    for ri, (g, idxs) in enumerate(runs):
        if len(idxs) >= MIN_RUN or g is None:
            continue
        neighbour = None
        for rj in (ri - 1, ri + 1):
            if 0 <= rj < len(runs) and len(runs[rj][1]) >= MIN_RUN:
                neighbour = runs[rj][0]
                break
        if neighbour is not None and neighbour != g:
            for i in idxs:
                grp[i] = neighbour
                chosen[i] = None   # offset resmoothed from the absorbing era
    for i in range(len(chosen)):
        if chosen[i] is None and grp[i] is not None:
            win = [chosen[j] for j in range(len(chosen))
                   if chosen[j] is not None and grp[j] == grp[i]]
            if win:
                win.sort()
                chosen[i] = win[len(win) // 2]

    # rolling-median smoothing within each era group absorbs snapping noise
    # (candidate time = nearest trackpoint of a visit, not the exact moment)
    W = 10
    smoothed = list(chosen)
    for i in range(len(chosen)):
        if chosen[i] is None:
            continue
        win = [chosen[j] for j in range(max(0, i - W), min(len(chosen), i + W + 1))
               if chosen[j] is not None and grp[j] == grp[i]]
        win.sort()
        smoothed[i] = win[len(win) // 2]

    fixes = {}
    skipped = 0
    eras_used = Counter()
    for (photo, orig, offs), off, g in zip(per_photo, smoothed, grp):
        if off is None:
            skipped += 1
            continue
        fixes[photo["id"]] = (orig + timedelta(seconds=off) + tz).strftime(TS)
        eras_used[g] += 1
    # final isotonic clamp: the drone clock never runs backwards, so within
    # the file sequence corrected times must not either
    prev = None
    for photo, orig, offs in per_photo:
        pid = photo["id"]
        if pid not in fixes:
            continue
        t = datetime.strptime(fixes[pid], TS)
        if prev is not None and t < prev:
            t = prev + timedelta(seconds=1)
            fixes[pid] = t.strftime(TS)
        prev = t
    print(f"   {len(fixes)} correctable, {skipped} skipped; "
          f"{len(eras_used)} clock state(s): "
          + ", ".join(f"~{timedelta(seconds=round(sum(b for b in groups[g]) / len(groups[g]) * BIN))} x{n}"
                      for g, n in eras_used.most_common()))
    seq = [(p["timestamp"], fixes[p["id"]]) for p in ordered if p["id"] in fixes]
    viol = sum(1 for a, b2 in zip(seq, seq[1:]) if b2[1] < a[1])
    print(f"   monotonicity: {viol} violations over {len(seq)} corrected")
    for pid, new in sorted(fixes.items())[:5]:
        old = next(p["timestamp"] for p in info["outliers"] if p["id"] == pid)
        print(f"     {pid[:30]:32s} {old} -> {new}")
    if len(fixes) > 5:
        print(f"     ... and {len(fixes) - 5} more")
    if not apply or not fixes:
        return fixes
    return apply_fixes(info, fixes, apply)


def apply_fixes(info, fixes, apply):
    tdir = info["dir"]
    if not apply or not fixes:
        return fixes

    for name in ("manifest.json", "manifest.all.json"):
        mp = tdir / name
        if not mp.exists():
            continue
        m = json.loads(mp.read_text())
        changed = 0
        for p in m.get("photos", []):
            if p["id"] in fixes:
                p["timestamp"] = fixes[p["id"]]
                changed += 1
        ts_all = sorted(p["timestamp"] for p in m.get("photos", []) if p.get("timestamp"))
        if ts_all:
            m["dates"] = {"start": ts_all[0][:10], "end": ts_all[-1][:10]}
        mp.write_text(json.dumps(m))
        print(f"   ✓ {name}: {changed} timestamps fixed, dates {m['dates']}")

    cache_p = tdir / "exif_cache.json"
    if cache_p.exists():
        cache = json.loads(cache_p.read_text())
        by_stem = {}
        for pid, new in fixes.items():
            by_stem[pid] = new
        changed = 0
        for path, entry in cache.items():
            stem = Path(path).stem
            for pid, new in by_stem.items():
                if stem == pid or stem.startswith(pid) or pid.startswith(stem):
                    entry["DateTimeOriginal"] = (
                        datetime.strptime(new, TS).strftime("%Y:%m:%d %H:%M:%S"))
                    changed += 1
                    break
        cache_p.write_text(json.dumps(cache))
        print(f"   ✓ exif_cache.json: {changed} entries updated")

    idx_p = TRIPS / "index.json"
    idx = json.loads(idx_p.read_text())
    m = json.loads((tdir / "manifest.json").read_text())
    for t in idx["trips"]:
        if t["id"] == tdir.name:
            t["dates"] = m["dates"]
    idx_p.write_text(json.dumps(idx))
    print("   ✓ index.json dates refreshed")
    return fixes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--trip")
    args = ap.parse_args()
    total = 0
    for tdir in sorted(TRIPS.iterdir()):
        if not tdir.is_dir() or (args.trip and tdir.name != args.trip):
            continue
        info = analyze_trip(tdir)
        if not info or not info["outliers"]:
            continue
        lo, hi = info["ref_range"]
        print(f"\n== {tdir.name}: {len(info['outliers'])}/{info['n_dji']} DJI photos "
              f"outside {lo:%Y-%m-%d}..{hi:%Y-%m-%d}")
        total += len(correct_trip(info, args.apply))
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {total} corrections")


if __name__ == "__main__":
    main()
