# Travel Photography Map

A personal project to turn many years of travel photos into a single interactive world map.

For each trip, you point the script at a GPX track and a folder of photos. It interpolates GPS coordinates onto every photo by matching EXIF timestamps to the GPX trackpoints, generates web-sized thumbnails and display images, and registers the trip with the front-end Leaflet map. Every trip gets a coloured route line and a cluster of photo markers along it.

## Project goals

- **Show, don't curate.** A lifetime of trips on one map, browsable by year/region/trip.
- **Free hosting friendly.** Output is small, static, and CDN-cacheable. Photos are aggressively compressed so the full library can fit in a free-tier object store. See [Hosting](#hosting) below.
- **Re-runnable.** Re-process a trip at any time. Outputs are deterministic and gitignored.
- **One source of truth per trip.** GPX route + photo folder in, manifest + compressed images out.

## How it works

1. **Parse GPX** — extract trackpoints with UTC timestamps.
2. **Find photos** — scan the input directory, skipping known noise subdirs (`Compressed/`, `Phone/`, `Videos/`).
3. **Resolve GPS coordinates** per photo. Priority order:
   1. The photo's **own EXIF GPS tag** (DJI drone JPEGs have this baked in).
   2. The matching **DJI DNG** (pass `--raws` to enable).
   3. **GPX interpolation** between trackpoints surrounding the photo's timestamp.
   4. **Fallback location** — when 1-3 all fail. Defaults to the *GPX centroid* (rough geographic middle of the trip), or whatever you pass to `--fallback-location lat,lon`. Photos placed here are tagged `placement: approximate` in the manifest so the frontend can style them differently. Pass `--fallback-location none` to drop them instead.
   5. The manifest records a `skipped` array for every photo that didn't get exact placement, including the reason and hours outside the GPX window — useful for auditing after a long run.
5. **Compress** — write a small thumbnail (400 px longer-side) and a display image (2160 px longer-side WebP Q90 by default) to `hosted-photos/<slug>/`. Both caps apply to the *longer* side so portrait drone shots don't blow up. Originals are never touched.
6. **Cluster** — group photos within `--cluster-radius` metres (default 50) into a single marker.
7. **Publish** — write `manifest.json` + `route.geojson` to `web/trips/<slug>/`, symlink the compressed images in, and update `web/trips/index.json` and the year/trip HTML pages.

## Quick start

### Prerequisites

```bash
brew install exiftool
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

### Run

```bash
./venv/bin/python process_trip.py \
  --name "2024 Kyrgyzstan" \
  --gpx "/path/to/2024 Kyrgyzstan.gpx" \
  --photos "/path/to/Edits/2024 Kyrgyzstan"
```

`--output` defaults to `web/trips/<slug>`, where `<slug>` is derived from `--name`. Override with `--output ...` if you want.

### View the map

```bash
cd web && python3 -m http.server 8000
```

Open http://localhost:8000. All processed trips appear together. Each trip page lives at `/<year>/<slug>/`.

## CLI options

```
--name TEXT                Trip name for display  [required]
--gpx PATH                 Path to GPX file  [required]
--photos PATH              Path to photos directory  [required]
--output PATH              Metadata output dir (default: web/trips/<slug>)
--hosted-photos-dir PATH   Root for compressed image storage (default: <project>/hosted-photos)
--geosync TEXT             Timezone offset for camera sync (e.g., +02:00)
--gpx-tolerance-hours FLOAT  Hours outside GPX window before fallback kicks in (default: 2)
--fallback-location LAT,LON  Lat,lon for photos w/ no GPS + outside GPX window
                             (default: GPX centroid; pass "none" to drop)
--cluster-radius INTEGER   Clustering radius in meters (default: 50)
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
./venv/bin/python recluster.py --trip 2024-kyrgyzstan --cluster-radius 25

# Re-cluster every trip in web/trips/
./venv/bin/python recluster.py --trip all --cluster-radius 20
```

Visual (Leaflet) clustering lives in `web/js/app.js` (`clusterRadius`, `disableClusteringAtZoom`) and is read on every page load — no rebuild needed, just refresh.

### Changing compression retroactively

`process_trip.py` is destructive on re-run (it expects GPX + source photos). To re-encode an already-processed library at different quality/format/dimensions without re-doing GPX matching, use `recompress.py`:

```bash
# One trip, bump to WebP Q95
./venv/bin/python recompress.py --trip 2024-kyrgyzstan --quality 95

# Every trip in web/trips/, switch to JPEG for a host that doesn't support WebP
./venv/bin/python recompress.py --trip all --format jpeg --quality 90

# Smaller library — re-encode at lower quality
./venv/bin/python recompress.py --trip all --quality 82 --display-longest 1600
```

It reads each manifest's `source.photos_path` (recorded at first process), regenerates only the thumbnail + display images, updates the manifest's `compression` block and the per-photo `thumbnail`/`display` paths (handling extension changes), and recreates the symlinks. If the originals have moved, pass `--photos /new/path` to override.

The host-side workflow after a recompress is just: re-sync `hosted-photos/<trip>/` to your bucket. Manifest paths change with the format (`.webp` → `.jpg`) so the front-end picks up the new files automatically.

## Hosting

The site itself is tiny (HTML + JS + manifest JSON). The photos are the budget.

| Host | Free quota | Suitable for |
|---|---|---|
| GitHub Pages | 1 GB hard cap | Small libraries only (~1k photos) |
| Cloudflare Pages | Unlimited bandwidth, **25k file limit** | ~12k photos max (thumb + display = 2 files each) |
| Netlify | ~10 GB practical | Medium libraries |
| **Cloudflare R2** | **10 GB storage + 1M reads/mo** | Large libraries; pair with Pages for the HTML |
| **Backblaze B2** + Cloudflare CDN | 10 GB + free egress to CDN | Same idea as R2 |

For "tens of thousands of photos" the realistic shape is:

1. Push the `web/` directory to **Cloudflare Pages** (the HTML/JS/JSON, no images).
2. Sync `hosted-photos/` to **Cloudflare R2** or **Backblaze B2**.
3. Replace the symlinks (or the manifest paths) with absolute CDN URLs so the browser fetches images straight from the bucket.

This split is intentional: the script writes images and metadata to different roots so swapping the image host is a path change, not a refactor.

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
