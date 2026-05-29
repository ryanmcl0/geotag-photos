# geotag-photos — Agent Context

This project builds a private, interactive geomap of Ryan's travel photography, deployed to Cloudflare Pages + R2. It is actively developed; this file captures the full architecture, folder conventions, pipeline, known quirks, and pending work so a new agent can operate without re-learning the project.

---

## What this project does

1. **Processes** edited photos from `/Volumes/RYAN/Edits/` (+ older in-tree locations) into compressed WebP thumbnails + display images
2. **Places** each photo on a map using, in priority order: EXIF GPS → GPX track interpolation → building-name lookup (raw dir name → `locations.json` → coordinates) → city-level fallback
3. **Clusters** placed photos into map markers, named after the building where possible
4. **Publishes** to Cloudflare Pages (HTML/JS/JSON) + R2 (compressed images)
5. **Two-tier access**: public trips visible by default; private trips hidden behind a "See All" password gate

---

## Key files

| File | Purpose |
|---|---|
| `trips.json` | Single source of truth — all trips (public + private), edits paths, GPX paths, raws roots, per-trip options. **Gitignored** |
| `process_trip.py` | Core processing script — placement, compression, clustering, manifest generation |
| `process_all.py` | Batch runner — reads `trips.json`, runs `process_trip.py` for each trip |
| `deploy.py` | Cloudflare deploy — syncs public flags → uploads to R2 → deploys Pages |
| `locations.json` | 234 building/location name → {lat, lon} entries. KML-matched + web-searched. **Gitignored** |
| `web/trips/index.json` | Trip index for the frontend — auto-generated, includes `public: true/false` flag per trip |
| `web/trips/<slug>/manifest.json` | Per-trip photo list, clusters, GPS sources, dates, countries |
| `hosted-photos/<slug>/` | Compressed WebP thumbnails + display images. **Gitignored** (deployed to R2) |
| `.env.deploy` | Cloudflare credentials. **Gitignored** |
| `safety-check/scan_nsfw.py` | Standalone safety scanner (nudenet, local). **Gitignored** — whole folder is |
| `notes.txt` | Ryan's running notes + backfill plans. **Gitignored** |

---

## Drive layout — `/Volumes/RYAN/`

The external drive `RYAN` holds all originals. It must be mounted for processing.

```
/Volumes/RYAN/
├── Edits/                          ← Central edits folder (used since ~2019)
│   ├── 2020 Via Alpina/            ← One folder per trip
│   ├── 2024 Mongolia/
│   ├── 2024 China/                 ← Multi-region bundle (see note below)
│   ├── 2024 China Nov/
│   ├── 2019 NYC/                   ← Private trip (not yet backfilled pre-2019)
│   └── ...
├── 2018/                           ← Year folders — raw originals
│   ├── Hong Kong/Pictures/         ← Pre-2019 flat city folder (no building-level)
│   ├── 150 Leadenhall Street/Edits/← PRE-2019: edits IN the trip folder, NOT centralized
│   └── ...
├── 2019/
│   ├── NYC/
│   │   ├── 8 Spruce Street/Pictures/  ← Ideal: city / building / Photos
│   │   ├── Carnegie Hall Tower/Pictures/
│   │   └── ...
│   └── Frankfurt/Edits/            ← Flat (no building-level subdirs)
├── 2022/
│   ├── Egypt/
│   │   ├── Iconic Tower/Photos/
│   │   └── ...
│   ├── Seoul/
│   │   ├── Gwanaksan/
│   │   └── ...
│   └── ...
├── 2023/
│   ├── Israel/
│   │   ├── Gibor Sport House/
│   │   └── ...
│   ├── Asia 23/                    ← Multi-country bundle
│   │   ├── Philippines/Manila - Gramercy Residence/
│   │   ├── Vietnam/Ha Long - Islands/
│   │   └── South Korea/Busan - LCT Residence/
│   └── ...
├── 2024/
│   ├── Asia 24/
│   │   ├── 02:24 Hong Kong/Building/
│   │   └── 03:24 China/...
│   ├── Asia 24 pt2/
│   │   ├── 06.2024 China/Xinjiang/Day N.../  ← Road trip route folders
│   │   ├── 06.2024 China/Guizhou/Building/   ← Off-route building folders
│   │   └── 08.2024 South Korea/Building/
│   └── China - Zhejiang, Shanghai.../        ← Nov China raws root
│       ├── Tianjin CTF Finance Centre/
│       ├── Pudong Shangri-La/
│       └── Xinjiang/Day N .../               ← Route folders under Xinjiang/
└── Projects : Work/
    └── travel locations/
        ├── photography_archive.kml            ← 570 curated building coordinates
        └── buildings.csv                      ← Same data, no coords (use KML instead)
```

### Raw folder naming conventions (evolved over time)

| Era | Pattern | Building lookup works? |
|---|---|---|
| Pre-2019 | `<year>/<Building>/Edits/` — edits IN trip folder | Yes — parent of `Edits/` IS the building |
| Pre-2019 flat | `<year>/<City>/Pictures/` — no building subdirs | No — city-level fallback only |
| 2019+ | `<year>/<City>/<Building>/Pictures/` | Yes |
| 2022+ | `<year>/<Country>/<Building>/Photos/` | Yes |
| Road trips | `<year>/<trip>/Day N <location>/` | Yes — day folder = location name |
| Multi-country | `<year>/Asia 23/<Country>/<City - Building>/` | Yes — deepest non-generic dir |

### Known gotchas

- **Colons in folder names** (`2020:09 France - Paris`, `02:24 Hong Kong`) — valid macOS, breaks Windows/Linux/some sync tools. Not worth renaming retroactively, but new trips should use dashes
- **Stem collisions**: camera `RM10xxxx` counter rolls over, so stems repeat across trips within the same year. ALWAYS scope raw search to the specific trip subfolder, NEVER year-wide. The `rank()` function in `process_trip.py` prefers paths that yield a building name over generic paths to resolve ties deterministically
- **Generic raw dirs** to skip when extracting building names (defined in `GENERIC_RAW_DIRS`): `pictures`, `photos`, `video`, `edits`, `compressed`, `me`, `phone`, `tourism`, plus camera-card dumps (`100MSDCF`, numeric-only, `backup`, `cam`, `doobie`, `ricky everything` etc.)
- **Multi-trip Edits bundles**: `2024 China` edits contain Guizhou + Xinjiang + other regions. The `filter_raws` option scopes which photos to include; `gpx_route_subdir` forces a named subfolder's photos onto the GPX track

---

## `trips.json` structure

```json
{
  "public": [
    {
      "name": "2024 North Xinjiang + Guizhou",
      "edits": "/Volumes/RYAN/Edits/2024 China",
      "gpx": "/Volumes/RYAN/2024/Asia 24 pt2/06.2024 China/2024 North Xinjiang + Guizhou GPX",
      "raws": "/Volumes/RYAN/2024/Asia 24 pt2/06.2024 China",
      "options": {
        "filter_raws": "/Volumes/RYAN/2024/Asia 24 pt2/06.2024 China",
        "split_offroute_private": true,
        "gpx_route_subdir": "Xinjiang",
        "route_snap_public_hours": 3
      }
    }
  ],
  "private": [
    {
      "name": "2019 NYC",
      "edits": "/Volumes/RYAN/Edits/2019 NYC",
      "raws": "/Volumes/RYAN/2019/NYC",
      "options": { "cluster_radius": 100, "fallback_location": "40.7580,-73.9855" }
    }
  ]
}
```

**`options` fields:**
- `geosync` — timezone offset for camera sync (e.g. `+08:00`)
- `filter_raws` — only process edits whose stem exists under this path (scopes multi-trip bundles)
- `cluster_radius` — metres (default 1000; use 100–150 for dense city/building trips)
- `fallback_location` — `"lat,lon"` for photos with no GPS
- `split_offroute_private` — `true` to split GPX trips: on-route photos stay public, off-route go to `<slug>-private`
- `gpx_route_subdir` — folder name (e.g. `"Xinjiang"`) whose photos are forced onto the GPX track regardless of recording gaps
- `route_snap_public_hours` — route-snapped photos within N hours of the track count as public (default 3)
- `private_cluster_radius` — cluster radius for the off-route private split (default 150)

---

## Location pipeline (priority order)

For each edited photo:
1. **EXIF GPS** on the JPG (`gps_source: exif`)
2. **DJI DNG GPS** — for DJI_ prefixed files, read GPS from the matching `.dng` raw (`gps_source: dng`)
3. **GPX interpolation** — match timestamp to GPX track; drops photos more than `gpx_tolerance_hours` outside the window (`gps_source: gpx`)
4. **Route-subdir forced** — if `--gpx-route-subdir` matches a path component, clamp to track regardless of gap (`gps_source: gpx`, via `interpolate_gps_clamped`)
5. **Building lookup** — raw file path → `building_from_raw()` → building name → `locations.json` lookup (`gps_source: building`)
6. **Nearest placed photo** — snap to the closest already-placed photo within `nearest_photo_max_hours` (`gps_source: nearest_photo`)
7. **Nearest GPX trackpoint by time** (`gps_source: gpx_nearest_time`; stamped with `snap_gap_hours`)
8. **Fallback centroid / explicit fallback** (`gps_source: fallback_centroid`)

**Split rule** (when `split_offroute_private: true`): public = `gpx` ∪ `on_route=true` ∪ `gpx_nearest_time` within `route_snap_public_hours`. Everything else → `<slug>-private`.

---

## Building-name extraction (`building_from_raw`)

Given a raw file's absolute path and the trip's raw root, walks path components from deepest to root, skipping `GENERIC_RAW_DIRS` and camera-card dump patterns, returns the first meaningful component. This becomes the cluster label and the key into `locations.json`.

Examples:
```
<root>/50 West Street/Pictures/_RM12642.ARW  →  "50 West Street"
<root>/Philippines/Manila - Gramercy Residence/x.ARW  →  "Manila - Gramercy Residence"
<root>/Xinjiang/Day 3 Wensu - Aksu onwards/x.ARW  →  "Day 3 Wensu - Aksu onwards"
<root>/Cam/DSC00001.ARW  →  None  (camera-card dump, falls back to city-level)
```

---

## `locations.json`

234 building/location name → `{lat, lon, source}` entries. **Gitignored** (reveals private locations).

Sources:
- `kml` — matched from `/Volumes/RYAN/Projects : Work/travel locations/photography_archive.kml` (570 curated placemarks with precise coords)
- `websearch` — looked up via web search during the 2025-05-29 backfill session
- `known` — established landmark coords from training knowledge
- `approx` — approximate / area-level (lower confidence; 15 entries flagged for future validation)

**To add more buildings:** run `process_trip.py --dump-buildings --raws-root <path>` to list building names, match against KML, then use WebSearch for gaps. The KML already covers most of London, NYC, Seoul, Bangkok, Dubai, Cairo, HK, KL, Tel Aviv, Oslo, Vienna, Istanbul, and Chinese supertalls. Do NOT use Nominatim API — web search gives better accuracy for building-level coordinates.

---

## Public / private two-tier map

- **Public trips**: shown by default on the map for any visitor (after site password)
- **Private trips**: hidden; revealed by a second "See All" password (`CF_ALL_PASSWORD`). Stored in the `private` block of `trips.json`. Slugs ending in `-private` are automatically flagged private by `deploy.py`
- **`sync_public_flags()`** in `deploy.py` stamps `public: true/false` into `index.json` by matching each trip's manifest `source.photos_path` against `trips.json` public paths. Run this before any local preview: `python3 -c "import deploy; deploy.sync_public_flags()"`
- **Local dev**: `source .env.deploy && npx wrangler pages dev web/ --binding CF_SITE_PASSWORD="$CF_SITE_PASSWORD" --binding CF_ALL_PASSWORD="$CF_ALL_PASSWORD" --port 8789`

---

## Private trips (23 total as of 2026-05-29)

Processed and in the private tier behind "See All":
2018 HK, 2018 Paris, 2019 Frankfurt, 2019 NYC, 2019 Scotland, 2020:09 France-Paris, 2020:12 UAE-Dubai, 2022:04 Egypt, 2022:05 Malaysia, 2022:06 Poland, 2022:07 Thailand, 2022:12 South Korea, 2023:04 Israel, 2023:05 Hamburg, 2023:07 Asia (VN/KR/PH), 2023:10 Norway, 2023:11 Austria, 2023:11 Sweden, 2023:11 Turkey, 2024 HK, 2024 Korea, 2024:08 Denmark, 2025:09 Austria.

**Skipped (user to handle manually):** All UK trips (2019 UK, 2020 UK, 2021–2023 UK, 2025 UK) — their building folders sit at year-level mixed with other trips, making raw scoping fragile.

---

## China trips (split public/private)

Two 2024 China trips use `split_offroute_private`:

| Trip | Public | Private |
|---|---|---|
| 2024 North Xinjiang + Guizhou | Xinjiang road trip (328 photos, 81 clusters) | Guizhou buildings (123 photos, 8 clusters) |
| 2024 South Xinjiang | Xinjiang road trip (1127 photos, 200 clusters) | East China skyscrapers (237 photos, 14 clusters) |

**2025 China CNY** and **2026 China CNY** — fully public, no split.

---

## Pre-2019 backfill pool (NOT yet on map)

~160 folders / ~2,493 edited images have edits in-tree (`<year>/<Building>/Edits/`) and were NEVER copied to the central `/Volumes/RYAN/Edits/`. These are the 2016–2018 London rooftopping / crane / urbex era. The pipeline already supports them — the parent of `Edits/` is the building name, feeding directly into the building-lookup system. Full folder list in `notes.txt` section "Pre-2019 backfill pool".

---

## Safety scanner

`safety-check/scan_nsfw.py` — standalone, local, gitignored. Uses nudenet (onnxruntime). Scans compressed thumbnails, writes `safety-check/report.json`.

```bash
pip install nudenet
python3 safety-check/scan_nsfw.py --model nudenet          # scan all
python3 safety-check/scan_nsfw.py --quarantine              # quarantine flagged >= 0.7
python3 safety-check/scan_nsfw.py --restore                 # undo
```

Last full scan: 2026-05-29 — 9,120 images, 6 flagged (all false positives, none quarantined).

---

## Typical workflow

```bash
# 1. Add trip to trips.json (public or private block)
# 2. Process
python3 process_all.py                         # all unprocessed trips
python3 process_all.py --trip "2024 Korea"     # one trip
python3 process_all.py --force --skip-existing-images --trip "Xinjiang"  # reprocess, reuse images

# 3. Preview locally
python3 -c "import deploy; deploy.sync_public_flags()"
source .env.deploy && npx wrangler pages dev web/ \
  --binding CF_SITE_PASSWORD="$CF_SITE_PASSWORD" \
  --binding CF_ALL_PASSWORD="$CF_ALL_PASSWORD" --port 8789

# 4. Safety scan before publishing
python3 safety-check/scan_nsfw.py --model nudenet

# 5. Deploy
source .env.deploy && python3 deploy.py
```

---

## Recommended folder convention going forward

```
/Volumes/RYAN/Edits/YYYY-MM Country - Location/     ← one per trip, no colons
/Volumes/RYAN/<year>/YYYY-MM Country - Location/<Building Name>/Photos/
```

Avoid colons in names (macOS only), personal names as folder names (`Ricky`, `Cam`), and bundling multiple countries into one Edits folder.
