#!/usr/bin/env python3
"""Build the local-only Phone mode dataset from the compressed phone library.

Reads:  /Volumes/RYAN/phone_browse/manifests/<slug>.jsonl  (from the phone
        photo compressor; one row per photo: src/name/date/lat/lon/w/h)
Writes: web/phone/trips/index.json                (same shape as web/trips/)
        web/phone/trips/phone-<slug>/manifest.json
        web/phone/trips/phone-<slug>/{display,thumbnails} -> NAS symlinks

Everything under web/phone/ is git-ignored and excluded from deploy; this
dataset only ever exists on machines with the NAS mounted. Trip ids are
prefixed "phone-" so they can never collide with camera trip ids (Posts
refs are {trip, id}).
"""
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAS = Path("/Volumes/RYAN/phone_browse")
OUT = PROJECT_ROOT / "web" / "phone" / "trips"
CLUSTER_RADIUS_M = 1000
THUMB_DIRNAME = "thumbs"  # compressor's output dir name; exposed as "thumbnails"


def clean_trip_name(src_path: str, slug: str) -> tuple[str, str]:
    """('2026', 'Norway') from a source path like /Volumes/RYAN/2026/04.26 Norway/Phone/x.jpg"""
    parts = Path(src_path).parts
    year, folder = parts[3], parts[4]
    name = re.sub(r"^[\d:.\- ]+", "", folder).strip() or folder
    return year, name


def iso(exif_date: str) -> str:
    d = datetime.strptime(exif_date, "%Y:%m:%d %H:%M:%S")
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def dist_m(lat1, lon1, lat2, lon2):
    dx = (lon2 - lon1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110540
    return math.hypot(dx, dy)


def cluster_photos(photos):
    clusters = []
    for p in photos:
        if p["lat"] is None:
            continue
        placed = False
        for c in clusters:
            if dist_m(p["lat"], p["lon"], c["lat"], c["lon"]) <= CLUSTER_RADIUS_M:
                c["photo_ids"].append(p["id"])
                n = len(c["photo_ids"])
                c["lat"] += (p["lat"] - c["lat"]) / n
                c["lon"] += (p["lon"] - c["lon"]) / n
                placed = True
                break
        if not placed:
            clusters.append({"lat": p["lat"], "lon": p["lon"], "photo_ids": [p["id"]]})
    return clusters


def main():
    import reverse_geocoder as rg

    manifests = sorted(NAS.glob("manifests/*.jsonl"))
    if not manifests:
        sys.exit("no phone manifests found (is the NAS mounted?)")

    trips_index = []
    for mpath in manifests:
        slug = mpath.stem
        if slug.startswith("_"):
            continue
        rows = [json.loads(l) for l in mpath.read_text().splitlines() if l.strip()]
        if not rows:
            continue
        rows.sort(key=lambda r: r["date"])
        year, disp_name = clean_trip_name(rows[0]["src"], slug)
        trip_id = f"phone-{slug}"
        tdir = OUT / trip_id
        tdir.mkdir(parents=True, exist_ok=True)

        photos = []
        for r in rows:
            photos.append({
                "id": r["name"],
                "source_filename": Path(r["src"]).name,
                "lat": r["lat"], "lon": r["lon"],
                "timestamp": iso(r["date"]),
                "placement": "exact" if r["lat"] is not None else "none",
                "gps_source": "exif" if r["lat"] is not None else "none",
                "thumbnail": f"thumbnails/{r['name']}.webp",
                "display": f"display/{r['name']}.webp",
                "camera_settings": {},
                "ar": round(r["w"] / r["h"], 3) if r.get("h") else 1.5,
            })

        clusters = cluster_photos(photos)
        countries = []
        if clusters:
            geo = rg.search([(c["lat"], c["lon"]) for c in clusters], mode=1, verbose=False)
            for c, g in zip(clusters, geo):
                c["location"] = g["name"]
                c["country"] = g["cc"]
                c["lat"] = round(c["lat"], 9)
                c["lon"] = round(c["lon"], 9)
            weighted = Counter()
            for c in clusters:
                weighted[c["country"]] += len(c["photo_ids"])
            countries = [cc for cc, _ in weighted.most_common()]

        dates = {"start": photos[0]["timestamp"][:10], "end": photos[-1]["timestamp"][:10]}
        manifest = {
            "trip_name": f"{year} {disp_name}",
            "dates": dates,
            "countries": countries,
            "source": {"photos_path": str(Path(rows[0]["src"]).parent), "gpx_path": None},
            "compression": {"format": "webp", "quality": 85,
                            "display_longest": 2160, "thumbnail_longest": 400},
            "route": None,
            "photos": photos,
            "clusters": clusters,
            "skipped": [],
        }
        (tdir / "manifest.json").write_text(json.dumps(manifest))
        # app.js unconditionally fetches route.geojson; an empty collection
        # avoids the dev server's HTML 404 fallback breaking res.json()
        (tdir / "route.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": []}))

        for link_name, target_dir in (("display", "display"), ("thumbnails", THUMB_DIRNAME)):
            link = tdir / link_name
            target = NAS / "photos" / slug / target_dir
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target)

        trips_index.append({
            "id": trip_id,
            "name": f"{year} {disp_name}",
            "year": int(year),
            "dates": dates,
            "photo_count": len(photos),
            "photo_count_all": len(photos),
            "countries": countries,
            "public": True,
            "path": f"trips/{trip_id}",
        })
        print(f"{trip_id}: {len(photos)} photos, {len(clusters)} clusters, {countries}")

    trips_index.sort(key=lambda t: t["dates"]["start"], reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.json").write_text(json.dumps({"trips": trips_index}))
    print(f"\nindex.json: {len(trips_index)} phone trips")


if __name__ == "__main__":
    main()
