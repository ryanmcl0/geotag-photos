# Travel Photography Map

Turn many years of travel photography into a single interactive world map — every place you've been, shown by the photos you took there.

You give the pipeline a folder of edited photos per trip; it works out **where each photo belongs**, generates web-sized images, clusters them into map markers, and publishes a static site (Leaflet front-end) where every trip is browsable by year and region. When a trip has a GPS track, photos snap to the route; when it doesn't, the pipeline falls back through a chain of strategies (drone GPS, the photo's folder/location name, a trip fallback) so older, GPS-less trips still land in the right place.

It's built around a *well-organised* archive — a decade of trips filed by year, trip, and location — and that structure is exactly what makes automatic placement possible. The real work is the **long tail of edge cases**: conventions that evolved over the years and per-trip quirks, each needing its own handling rather than one tidy one-size-fits-all format (see [The backfill](#the-backfill) below).

**Hosting in one line:** output is a static site + aggressively-compressed images, deployed **free** to **Cloudflare Pages + R2** — a full lifetime library of over 10,000 photos fits in the free tier with **zero egress fees**, and the whole map (plus a separate "private trips" tier) can sit behind a password. Details in [Hosting](#hosting) and [DEPLOYMENT.md](DEPLOYMENT.md).

## Project goals

- **Show, don't curate.** A lifetime of trips on one map, browsable by year/region/trip.
- **Place anything.** GPX, drone EXIF, folder-name lookup, or fallback — every photo gets a position, even with no GPS.
- **Free hosting friendly.** Output is small, static, and CDN-cacheable. Photos are aggressively compressed so the full library can fit in a free-tier object store. See [Hosting](#hosting) below.
- **Re-runnable & incremental.** Re-process a trip at any time; or update only what changed (`--update`). Outputs are deterministic and gitignored.

<a name="the-backfill"></a>
## The backfill — and why the toolset keeps growing

The map didn't start full; it's being **backfilled** from a deep archive, and that backfill is what drives most of the features here. Each new batch of older trips surfaces a problem the pipeline didn't handle yet, and the fix becomes a general capability:

| Backfill challenge | Feature it drove |
|---|---|
| Trips with **no GPS track** at all | the placement fallback chain — drone EXIF → **building/location lookup from folder names** → trip fallback |
| Folder structures that **changed over the years** (centralized edits vs. edits left in-tree next to raws, flat city folders vs. building-level) | folder-aware location extraction + `--only-edits-dirs` to ingest in-tree layouts without scooping up raws/camera dumps |
| Older trips needing **coordinates** for named places | a coordinates file keyed by folder name, populated from a curated places list + lookups |
| **Re-editing / adding / removing** photos in already-processed trips | incremental `--update` (delta re-encode + orphan cleanup) so a tweak doesn't re-run the whole trip |
| The library **growing past free-tier storage** | tiered compression and retroactive `recompress.py` (see [Compression](#compression)) |

So the project is less a one-shot script than a steadily generalizing toolset: every messy corner of the archive that gets mapped tends to leave behind a reusable feature.

## Hosting

The site itself is tiny (HTML + JS + manifest JSON). The photos are the budget.

**The chosen setup → Cloudflare Pages + R2, $0.** The static site (HTML/JS/JSON) goes to **Cloudflare Pages**; the compressed images go to **Cloudflare R2**. A lifetime library stays inside the free tier, the whole site can be password-gated, and there's no egress bill no matter how much traffic it gets. The full step-by-step deploy guide (credentials, `deploy.py`, password protection, custom domain) is in **[DEPLOYMENT.md](DEPLOYMENT.md)**.

### Why this split — hosts evaluated

| Host | Free quota | Verdict |
|---|---|---|
| GitHub Pages | 1 GB hard cap | Small libraries only (~1k photos) — too tight |
| Cloudflare Pages | Unlimited bandwidth, **25k file limit** | Great for the static site; file limit caps it at ~12k photos (thumb + display = 2 files each) if images live here too |
| Netlify | ~10 GB practical | Medium libraries |
| **Cloudflare R2** ✅ | **10 GB storage + 1M reads/mo, zero egress** | Chosen for images — large libraries, no bandwidth bill |
| Backblaze B2 + Cloudflare CDN | 10 GB + free egress to CDN | Equivalent alternative to R2 |

The deciding factor was **R2's zero egress fees** (see the cost projection below) plus pairing it with Pages for the static front-end. The shape:

1. Push `web/` (HTML/JS/JSON, no images) to **Cloudflare Pages**.
2. Sync `hosted-photos/` to **Cloudflare R2**.
3. Manifest image paths resolve to the bucket so browsers fetch photos straight from R2.

This split is intentional: the pipeline writes images and metadata to different roots, so swapping the image host is a path change, not a refactor. Two-tier visibility (public vs. password-gated private trips) is applied at deploy time — see [Public / private](#public--private).

### Scaling to 10k+ photos

This project is expected to grow well past 10,000 photos as more trips are added. Here's how the constraints shift at that scale:

**Cloudflare Pages file limit (25k files)** is the first wall you'll hit. At 2 files per photo (thumbnail + display), 25k files = ~12,500 photos. Options when you get there:
- Switch to **R2 with custom domain** for image serving — Pages only hosts the HTML/JS/JSON, images come straight from R2. No file limit applies to R2.
- This is already how `deploy.py` works (images proxied through Pages Functions) — you'd just move to direct R2 serving with a custom domain to avoid the proxy overhead at scale.

**R2 storage (10 GB free)** at Q90/2160px averages ~0.7 MB per display image. 10k photos ≈ 7 GB display + ~0.3 GB thumbnails = ~7.3 GB — still within the free tier. At ~14k photos you'd cross 10 GB and pay ~$0.015/GB/month beyond that (roughly $0.06/month per extra 4GB).

**Compression strategy at scale** — if storage becomes a concern, re-encoding the whole library with `recompress.py` is a single command:
```bash
# Drop to Q85/1920px — cuts ~25% per image, ~5.5 GB for 10k photos
./venv/bin/python recompress.py --trip all --quality 85 --display-longest 1920
```

**Page load time** — at 10k+ photos, loading all manifests on the all-trips view gets heavy (~10 MB of JSON). Consider splitting into per-year lazy loading if initial load feels slow.

**Rough cost projection**:

| Library size | Storage | Monthly R2 cost | Notes |
|---|---:|---:|---|
| ~4k photos (current) | ~3 GB | $0 | Within free tier |
| ~10k photos | ~7 GB | $0 | Still within free tier |
| ~14k photos | ~10 GB | ~$0.06/mo | Just over free tier |
| ~30k photos | ~21 GB | ~$0.17/mo | Full lifetime library |

The numbers above are not typos. Cloudflare R2 charges **zero egress fees** — unlike S3 or GCS which charge $0.08–0.09/GB for every image a visitor loads, R2 only charges for storage. A full lifetime library of 30,000 travel photos costs less than a cup of coffee per month to host, with unlimited bandwidth. Even a very active site with thousands of daily visitors would stay under $1/month.

---

# Technical / usage

## How it works

1. **Parse GPX** — extract trackpoints with UTC timestamps.
2. **Find photos** — scan the input directory, skipping known noise subdirs (`Compressed/`, `Phone/`, `Videos/`).
<a name="placement-priority"></a>
3. **Resolve GPS coordinates** per photo. Priority order:
   1. The photo's **own EXIF GPS tag** (DJI drone JPEGs have this baked in).
   2. The matching **DJI DNG** (pass `--raws` to enable).
   3. **GPX interpolation** between trackpoints surrounding the photo's timestamp.
   4. **Building / location lookup** — for trips without GPS, the photo's source folder name (derived from the raw directory tree via `--raws-root`) is looked up in a coordinates file (`config/locations.json`, gitignored) to place it at that named place. This is what powers no-GPX trips and gives clusters their titles.
   5. **Fallback location** — when all the above fail. Defaults to the *GPX centroid* (rough middle of the trip), or whatever you pass to `--fallback-location lat,lon`. Photos placed here are tagged `placement: approximate` in the manifest. Pass `--fallback-location none` to drop them instead.
   6. The manifest records a `skipped` array for every photo that didn't get exact placement, including the reason and hours outside the GPX window — useful for auditing after a long run.
5. **Compress** — write a small thumbnail (400 px longer-side) and a display image (2160 px longer-side WebP Q90 by default) to `hosted-photos/<slug>/`. Both caps apply to the *longer* side so portrait drone shots don't blow up. Originals are never touched.
6. **Cluster** — group photos within `--cluster-radius` metres (default 50) into a single marker.
7. **Publish** — write `manifest.json` + `route.geojson` to `web/trips/<slug>/`, symlink the compressed images in, and update `web/trips/index.json` and the year/trip HTML pages.

## Quick start

### Prerequisites

```bash
brew install exiftool
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

### Run a single trip

```bash
./venv/bin/python process_trip.py \
  --name "My Trip" \
  --gpx "/path/to/track.gpx" \
  --photos "/path/to/photos"
```

`--output` defaults to `web/trips/<slug>`, where `<slug>` is derived from `--name`. Override with `--output ...` if you want. `--gpx` is optional — omit it for trips with no GPS recording (see [Placement](#placement-priority)).

### Run many trips (`process_all.py` + `trips.json`)

For more than a one-off, list your trips in `config/trips.json` (gitignored — it holds your paths) and let the batch runner drive `process_trip.py` once per trip:

```bash
./venv/bin/python process_all.py                 # process unprocessed trips
./venv/bin/python process_all.py --trip "Name"   # scope to one (substring match)
```

Each entry looks like:

```jsonc
{
  "public":  [ { "name": "<trip-name>", "edits": "/path/to/photos", "gpx": "/path/to/track.gpx", "raws": "/path/to/originals", "options": { } } ],
  "private": [ { "name": "<trip-name>", "edits": "/path/to/photos", "options": { "fallback_location": "<lat>,<lon>" } } ]
}
```

`options` keys include `geosync`, `cluster_radius`, `fallback_location`, `filter_raws`, `only_edits_dirs`, `split_offroute_private`, and more (see `config/trips.example.json`). Trips in `public` show by default; `private` trips sit behind a second password gate (see [Public / private](#public--private)).

### Updating after edits change

If you add, replace, or delete edited photos in a trip's folder, reprocess just the delta with incremental update mode — no config change needed:

```bash
./venv/bin/python process_all.py --update
```

`--update` reprocesses only trips whose edits are newer than their last output, and within each, only the changed/new/deleted photos (re-encoding changed ones, reusing the rest, and removing orphaned images for deleted ones). It compares against a per-trip `source_state.json` baseline; the **first** `--update` on a trip processed before this feature adopts the current files as the baseline (so it catches *new* photos but not an *overwrite-in-place*). To stamp baselines up front without reprocessing:

```bash
./venv/bin/python process_all.py --reindex
```

To force a full re-run while reusing already-encoded images (e.g. after a logic change): `--force --skip-existing-images`.

### View the map

```bash
cd web && python3 -m http.server 8000
```

Open http://localhost:8000. All processed trips appear together. Each trip page lives at `/<year>/<slug>/`.

<a name="public--private"></a>
### Public / private

Trips are two-tiered: those in the `public` block of `trips.json` are visible to any visitor; those in `private` are hidden behind a second "see all" password. Deployment reads the public/private split and stamps a flag into the trip index so the frontend knows what to reveal. Both the trip config and the coordinates file are gitignored, so location data never enters the repo.

## CLI options

Common `process_trip.py` options (run `--help` for the full list):

```
--name TEXT                Trip name for display  [required]
--photos PATH              Path to photos directory  [required]
--gpx PATH                 Path to GPX file (omit for no-GPS trips)
--output PATH              Metadata output dir (default: web/trips/<slug>)
--hosted-photos-dir PATH   Root for compressed image storage (default: <project>/hosted-photos)
--geosync TEXT             Timezone offset for camera sync (e.g., +02:00)
--gpx-tolerance-hours FLOAT  Hours outside GPX window before fallback kicks in (default: 2)
--fallback-location LAT,LON  Lat,lon for photos w/ no GPS + outside GPX window
                             (default: GPX centroid; pass "none" to drop)
--cluster-radius INTEGER   Clustering radius in meters
--raws-root PATH           Raw directory tree — folder names become building/location titles
--only-edits-dirs          Keep only photos under a folder named "Edits" (in-tree layouts)
--skip-existing-images     Reuse encoded images; recompute placement/clusters/manifest only
--update / --reindex       Incremental delta reprocess / stamp update baseline
--raws PATH                Path to original DNG files for DJI drone GPS data
--format [webp|jpeg]       Image format for thumbnails/display (default: webp)
--quality INTEGER          Encoder quality 1-100 (default: 90)
--display-longest INTEGER  Max longer-side for display images in px (default: 2160)
--thumbnail-longest INTEGER Max longer-side for thumbnails in px (default: 400)
--test-mode PERCENT        Process only X% of photos (e.g., 10 for 10%)
--dry-run                  Preview without writing files
```

## Output layout

```
hosted-photos/                  # gitignored — the bytes
  <trip-slug>/
    thumbnails/<id>.webp        # ~30 KB each, ~400 px longer-side
    display/<id>.webp           # ~500-1100 KB each, ~2160 px longer-side

web/                            # tracked — the site
  index.html                    # all trips overview
  <year>/index.html             # one year of trips
  <year>/<trip-slug>/index.html # single trip
  trips/
    index.json                  # auto-generated trip directory
    <trip-slug>/
      manifest.json             # photo list + clusters + camera settings
      route.geojson             # GPX converted for Leaflet
      thumbnails -> ../../../hosted-photos/<trip-slug>/thumbnails   (symlink)
      display    -> ../../../hosted-photos/<trip-slug>/display      (symlink)
```

`hosted-photos/` and the symlinks under `web/trips/*/` are in `.gitignore` — the bytes never go into the repo.

## Compression

The defaults (`webp`, `q=90`, longest-side `2160 px`) are tuned to keep fine detail (snow texture, ridgelines, rock edges) intact on drone shots while still cutting library size by ~50% vs uncompressed source. The longer-side cap matters: portrait drone shots (4536×8064) would otherwise become 1920×3413 monsters that waste bytes on resolution the lightbox can't display.

Sample sizes per display image, mixed library average:

| Profile | Per display | 30k photos | Notes |
|---|---:|---:|---|
| `--quality 80 --display-longest 1600` | ~350 KB | ~10 GB | Visibly soft on detail-heavy drone shots |
| `--quality 85 --display-longest 1920` | ~540 KB | ~16 GB | OK for Sony, soft on DJI |
| `--quality 90 --display-longest 2160` **(default)** | ~700 KB | ~21 GB | Indistinguishable from source on most shots |
| `--quality 92 --display-longest 2400` | ~1100 KB | ~33 GB | True near-lossless |
| `--format jpeg --quality 92 --display-longest 2400` | ~1400 KB | ~42 GB | Fallback for hosts without WebP |

WebP is ~30% smaller than JPEG at the same dimensions and quality, and Safari has supported it since 14 — global browser support is ~97%. Decode is fast and there are no perceptual artifacts at Q80.

If a future host requires JPEG: `--format jpeg --quality 90`. No other code changes needed.

### Changing clustering retroactively

Cluster radii are baked into the manifest at process time, but you can change them across the whole library at any point without re-encoding photos:

```bash
# Re-cluster one trip
./venv/bin/python recluster.py --trip <trip-slug> --cluster-radius 25

# Re-cluster every trip in web/trips/
./venv/bin/python recluster.py --trip all --cluster-radius 20
```

Visual (Leaflet) clustering lives in `web/js/app.js` (`clusterRadius`, `disableClusteringAtZoom`) and is read on every page load — no rebuild needed, just refresh.

### Changing compression retroactively

`process_trip.py` is destructive on re-run (it expects GPX + source photos). To re-encode an already-processed library at different quality/format/dimensions without re-doing GPX matching, use `recompress.py`:

```bash
# One trip, bump to WebP Q95
./venv/bin/python recompress.py --trip <trip-slug> --quality 95

# Every trip in web/trips/, switch to JPEG for a host that doesn't support WebP
./venv/bin/python recompress.py --trip all --format jpeg --quality 90

# Smaller library — re-encode at lower quality
./venv/bin/python recompress.py --trip all --quality 82 --display-longest 1600
```

It reads each manifest's `source.photos_path` (recorded at first process), regenerates only the thumbnail + display images, updates the manifest's `compression` block and the per-photo `thumbnail`/`display` paths (handling extension changes), and recreates the symlinks. If the originals have moved, pass `--photos /new/path` to override.

The host-side workflow after a recompress is just: re-sync `hosted-photos/<trip>/` to your bucket. Manifest paths change with the format (`.webp` → `.jpg`) so the front-end picks up the new files automatically.

## Preparing input data

### GPX
- Strava: Activity → ⋯ → **Export GPX**
- Garmin Connect: Activity → ⋯ → Export → GPX
- Combine per-day tracks into one file or pass them individually; the script handles multiple `<trk>` segments inside one file.

### Photos
- Need valid EXIF `DateTimeOriginal`.
- Sub-folders called `Compressed/`, `Phone/`, or `Videos/` are skipped automatically.
- If your camera was set to local time, pass `--geosync` (e.g. `+02:00` for CET). For UTC cameras, omit it.
- Drone photos: pass `--raws <path-to-DNGs>` to use the drone's embedded GPS instead of GPX interpolation.

## Stack

- **Python** — Pillow (WebP/JPEG), gpxpy, Click, tqdm
- **ExifTool** — for writing geotags back to JPEGs
- **Frontend** — Leaflet, Leaflet.markercluster, GLightbox
