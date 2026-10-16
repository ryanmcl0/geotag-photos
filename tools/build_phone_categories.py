#!/usr/bin/env python3
"""Build the Categories tab of the local-only Phone library.

A category is a hand-curated set of phone photos drawn from any number of
phone trips ("Funny translations", "Funny cars"...). It renders as a tile on
the Categories tab of galleries.html?library=phone and opens like a trip
gallery, but its photos stay where they are: each one is a {trip, id} ref
into an existing phone trip, so nothing is copied or re-linked on the NAS.

Reads:  local_browse/phone_categories.json   (the curated lists, git-ignored)
        web/phone/trips/<trip>/manifest.json  (ar, timestamp, trip name)
Writes: web/phone/categories/index.json
        web/phone/categories/<id>/manifest.json

phone_categories.json:
    {"categories": [{
        "id": "cat-china-funny-cars",          # stable, used in the URL
        "name": "Funny cars",
        "group": "China",                        # section heading on the tab
        "description": "...",                    # optional, shown on the tile
        "cover": {"trip": "phone-...", "id": "IMG_1234"},   # optional
        "photos": [{"trip": "phone-...", "id": "IMG_1234", "note": "Range Orver"}]
    }]}

A photo's optional "note" becomes its lightbox caption. Everything under
web/phone/ is git-ignored and never deployed; rerun this after editing the
JSON (no NAS reads, only the local manifests).
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "local_browse" / "phone_categories.json"
TRIPS = ROOT / "web" / "phone" / "trips"
OUT = ROOT / "web" / "phone" / "categories"


def main():
    if not SRC.exists():
        sys.exit(f"missing {SRC}")
    cats = json.loads(SRC.read_text()).get("categories", [])

    names = {t["id"]: t["name"] for t in json.loads((TRIPS / "index.json").read_text())["trips"]}
    manifests = {}

    def lookup(trip, pid):
        if trip not in manifests:
            path = TRIPS / trip / "manifest.json"
            manifests[trip] = ({p["id"]: p for p in json.loads(path.read_text())["photos"]}
                               if path.exists() else {})
        return manifests[trip].get(pid)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    index = []
    for cat in cats:
        photos, missing = [], []
        for ref in cat.get("photos", []):
            src = lookup(ref["trip"], ref["id"])
            if not src:
                missing.append(f'{ref["trip"]}/{ref["id"]}')
                continue
            photos.append({
                "trip": ref["trip"],
                "trip_name": names.get(ref["trip"], ref["trip"]),
                "id": ref["id"],
                "ar": src.get("ar"),
                "timestamp": src.get("timestamp"),
                "note": ref.get("note", ""),
            })
        if missing:
            print(f'  {cat["id"]}: {len(missing)} missing: {", ".join(missing[:5])}')
        photos.sort(key=lambda p: p["timestamp"] or "")
        dates = [p["timestamp"][:10] for p in photos if p["timestamp"]]

        d = OUT / cat["id"]
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(
            {"trip_name": cat["name"], "category": True, "photos": photos}, indent=1))
        index.append({
            "id": cat["id"],
            "name": cat["name"],
            "group": cat.get("group", ""),
            "description": cat.get("description", ""),
            "category": True,
            "public": True,
            "path": f'categories/{cat["id"]}',
            "dates": {"start": min(dates, default=""), "end": max(dates, default="")},
            "photo_count": len(photos),
            "cover": cat.get("cover") or (
                {"trip": photos[0]["trip"], "id": photos[0]["id"]} if photos else None),
        })
        print(f'  {cat["id"]}: {len(photos)} photos')

    (OUT / "index.json").write_text(json.dumps({"categories": index}, indent=1))
    print(f"wrote {len(index)} categories -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
