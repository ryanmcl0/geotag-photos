#!/usr/bin/env python3
"""List trips whose drone photos (DJI_*) fall on days outside the trip's own date range.

A DJI drone that loses its time sync keeps counting from a stale date, so its frames land on a
wrong day in the gallery (and in the wrong order on the map) even though their GPS is right. This
is the quick check for that, read-only over the processed manifests: per trip, the drone days that
sit outside the trip's dates, with a count and the first and last stamped frame.

The fix is a per-trip clock correction in config/trips.json, never an edit to the originals:
    "time_overrides": [{"filename_prefix": "DJI_", "stamped_from": "YYYY-MM-DD",
                        "stamped_to": "YYYY-MM-DD", "shift_seconds": N, "comment": "how N was measured"}]
then `process_all.py --trip <name> --reindex`. Measure N from evidence (phone photos/videos with GPS
at the same spots, the GPX passing the drone's own GPS positions, the light in the frames);
tools/fix_drone_dates.py proposes per-photo corrections from the GPX alone.

Usage: tools/drone_clock_scan.py [--margin-days 1] [--all]   (--all also reads manifest.all.json)
"""
import argparse
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

TRIPS = Path(__file__).resolve().parent.parent / "web" / "trips"


def day(ts):
    try:
        return date.fromisoformat((ts or "")[:10])
    except ValueError:
        return None


def scan(margin, use_all):
    out = []
    for d in sorted(p for p in TRIPS.iterdir() if p.is_dir()):
        m_path = d / "manifest.all.json" if use_all and (d / "manifest.all.json").exists() else d / "manifest.json"
        if not m_path.exists():
            continue
        try:
            m = json.loads(m_path.read_text())
        except ValueError:
            continue
        dates = m.get("dates") or {}
        start, end = day(dates.get("start")), day(dates.get("end"))
        if not start or not end:
            continue
        lo, hi = start - timedelta(days=margin), end + timedelta(days=margin)
        bad = defaultdict(list)
        for p in m.get("photos", []):
            if not str(p.get("id", "")).upper().startswith("DJI"):
                continue
            t = day(p.get("timestamp"))
            if t and not (lo <= t <= hi):
                bad[t].append(p["id"])
        if bad:
            out.append((d.name, start, end, bad))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--margin-days", type=int, default=1, help="days either side of the trip still counted as in range")
    ap.add_argument("--all", action="store_true", help="read manifest.all.json (private photos too) where present")
    a = ap.parse_args()
    found = scan(a.margin_days, a.all)
    if not found:
        print("No drone photos outside their trip's dates.")
        return
    for slug, start, end, bad in found:
        n = sum(len(v) for v in bad.values())
        print(f"{slug}  (trip {start} → {end}): {n} drone photo(s) on {len(bad)} day(s) outside it")
        for t in sorted(bad):
            ids = sorted(bad[t])
            print(f"    {t}  {len(ids):3d}  {ids[0]}" + (f" … {ids[-1]}" if len(ids) > 1 else ""))


if __name__ == "__main__":
    main()
