# Travel Photography Map

A decade of travel photography turned into one website: an interactive world map, trip galleries, long-form blogs and a deep dive into China, all generated from folders of edited photos.

Live at **[ryanmcl.com](https://ryanmcl.com)**. Over 15,000 photos from 57 countries, more than 90 trips and about 53,000 km of road trips so far, with older trips still being backfilled.

You point the pipeline at a trip's edited photos. It works out **where each photo was taken**, makes web-sized images, groups them into map markers and publishes a static site. When a trip has a GPS track, photos snap to the route. When it doesn't, the pipeline falls back through drone GPS, the photo's folder name, location history and a trip fallback, so even trips from 2015 land in the right place.

The whole thing runs on **Cloudflare Pages + R2 for free**: a static site plus compressed images, with no bandwidth bill.

## What's on the site

- **Map.** Every trip on one map, with routes, photo clusters, satellite / street / topo layers and a year filter. Links can open a single trip, a year, a filtered set of photos, or a single spot (`map.html?mode=trip&trip=<slug>&at=lat,lon`).
- **Galleries.** One justified-grid gallery per trip, with a title, regions, a short description and buttons to the trip's map and blog. Photos open in a full-screen lightbox.
- **China.** A hub built from the same photos, organised by theme rather than by trip:
  - **Provinces**: every province visited, with a progress count and a gallery each.
  - **On the Roads**: each road trip with its car, dates, distance and route.
  - **Bridges**: the world's highest bridges, ranked, with construction status for the ones still being built.
  - **Energy & Infrastructure**: dams, solar and wind farms and other big engineering.
  - **Stories**: the China blogs.
- **Mountains.** Every summit, volcano and pass, ranked by height with a year filter and search, like the Urbex page. Named folders match by name; the rest match by geofence against the photos' positions, so a long through-hike with no per-peak folders is broken out peak by peak from its GPS track (`tools/mountain_scan.py` finds the candidates, `config/mountains.json` is the roster).
- **Blogs.** Long-form trip write-ups with inline photo grids, a route map and a day-by-day rail. Any paragraph can be linked to directly.
- **Search.** One box over everything: trips, places on the map, the China pages and blog text. Typing a country or a Chinese province opens a place card showing everywhere it appears on the site. It tolerates typos, and `/` opens it from any page.

## Access

The site sits behind a password, and QR invite links let people in without sharing it (see [QR_ACCESS.md](QR_ACCESS.md)). A few trips and pages that aren't ready to share need a second password. The same rules apply everywhere a visitor could reach them: pages, data files, search results and the image proxy.

## How a trip gets onto the map

1. **Read the track.** Parse the trip's GPX (or several merged Strava days) into timestamped trackpoints.
2. **Find the photos.** Scan the trip's edits folder, skipping noise folders (`Compressed/`, `Phone/`, `Videos/`).
<a name="placement-priority"></a>
3. **Place each photo.** In priority order:
   1. The photo's **own GPS tag** (drone JPEGs carry one).
   2. The matching **drone DNG** (with `--raws`).
   3. **GPX interpolation** between the trackpoints either side of the photo's time, after correcting the camera clock (`--geosync`).
   4. **Folder name lookup**: for trips without a track, the photo's folder in the raw tree names the place, and `config/locations.json` gives its coordinates. These names also become the cluster titles on the map.
   5. **Trip fallback**: anything left goes to the track's centre or a given point, tagged `approximate` in the manifest.
4. **Fix the stragglers.** `geotag_overrides` in `config/trips.json` re-place or relabel individual photos (by name or number range) without touching the source files, for drifting clocks and mislabelled folders.
5. **Compress.** A 400 px thumbnail and a 2160 px display image per photo, WebP Q90 by default, into `hosted-photos/<slug>/`. Originals are never touched.
6. **Cluster and publish.** Group nearby photos into markers and write `manifest.json` + `route.geojson` to `web/trips/<slug>/`, then refresh the trip index.

Then the site-level builders run over every processed trip:

| Builder | What it makes |
|---|---|
| `build_collections.py` | The China hub (provinces, roads, bridges, energy), the Urbex and Mountains pages from rosters in `config/`, plus landing-page stats, gallery covers and descriptions |
| `build_blogs.py` | Blog pages from Markdown drafts, with photos resolved against the trip manifests |
| `build_search.py` | The search indexes behind `search.html` |
| `placeholder_trips.py` | "Photos pending" pins for trips that are planned or not yet edited |

## The backfill, and why the toolset keeps growing

The map didn't start full. It's being backfilled from a deep archive, and most features here exist because a batch of older trips broke an assumption:

| Backfill problem | What it led to |
|---|---|
| Trips with **no GPS track** | The placement fallback chain and folder-name lookup |
| Tracks with **gaps**, or only a phone's location history | `tools/reconstruction/`: rebuild a GPX from Google Timeline, merge partial Strava days, fill holes along the road |
| **Camera clocks** left on the wrong time zone | `--geosync`, clock checks against phone photos, and `geotag_overrides` |
| Folder layouts that **changed over the years** | Folder-aware location names and `--only-edits-dirs` for edits stored next to raws |
| **Re-editing** photos in finished trips | Incremental `--update` and `tools/sync_post_edits.py` |
| The library **outgrowing the free tier** | Tiered compression and `tools/recompress.py` |

So it's less a one-off script than a toolset that grows with the archive: each messy corner that gets mapped leaves behind something reusable.

---

# Usage

## Setup

```bash
brew install exiftool
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

## Process trips

List trips in `config/trips.json` (gitignored, since it holds your paths; see `config/trips.example.json`):

```jsonc
{
  "public":  [ { "name": "<trip name>", "edits": "/path/to/edits", "gpx": "/path/to/track.gpx", "raws": "/path/to/raws", "options": { } } ],
  "private": [ { "name": "<trip name>", "edits": "/path/to/edits", "options": { "fallback_location": "<lat>,<lon>" } } ]
}
```

Trips in the second block only show with the second password. Then:

```bash
./venv/bin/python process_all.py                  # process new trips
./venv/bin/python process_all.py --trip "Name"     # one trip (substring match)
./venv/bin/python process_all.py --update          # only what changed since last run
./venv/bin/python process_all.py --trip X --reindex  # apply config-only changes (overrides, labels) in seconds
```

`options` keys include `geosync`, `cluster_radius`, `fallback_location`, `filter_raws`, `only_edits_dirs` and `geotag_overrides`. A single trip can also be run directly:

```bash
./venv/bin/python process_trip.py --name "My Trip" --photos /path/to/edits --gpx /path/to/track.gpx
```

`process_all.py` rebuilds the site collections at the end of a run. To rebuild them on their own:

```bash
./venv/bin/python build_collections.py   # China hub, stats, covers (+ the search index)
./venv/bin/python build_blogs.py         # blogs
```

## Run it locally

```bash
./serve.sh        # http://localhost:8788
```

This runs the site under Wrangler with the same middleware as production, so passwords and gating behave as they will live. For a quick look without any of that, `cd web && python3 -m http.server 8000` also works.

## Deploy

```bash
set -a; source .env.deploy; set +a
venv/bin/python deploy.py                 # everything
venv/bin/python deploy.py --skip-images   # code and data only
```

`deploy.py` refreshes the stats, covers and search indexes, uploads changed images to R2 (by size, deleting orphans), pushes the site to the Pages repo that Cloudflare builds from, and backs up the gitignored `config/` to a separate repo. The first-time Cloudflare setup is in [DEPLOYMENT.md](DEPLOYMENT.md).

## CLI options

Common `process_trip.py` options (`--help` for the full list):

```
--name TEXT                  Trip name  [required]
--photos PATH                Edited photos directory  [required]
--gpx PATH                   GPX file (omit for trips without a track)
--geosync TEXT               Camera time zone offset, e.g. +02:00
--raws PATH                  Drone DNGs, for their embedded GPS
--raws-root PATH             Raw directory tree; folder names become place names
--fallback-location LAT,LON  Where unplaced photos go ("none" drops them)
--cluster-radius INTEGER     Marker clustering radius in metres
--only-edits-dirs            Only photos under a folder named "Edits"
--update / --reindex         Incremental reprocess / config-only refresh
--skip-existing-images       Reuse encoded images, recompute everything else
--format [webp|jpeg]         Image format (default: webp)
--quality INTEGER            Encoder quality (default: 90)
--display-longest INTEGER    Display image longer side in px (default: 2160)
--dry-run                    Preview without writing files
```

## Output layout

```
hosted-photos/<slug>/            # gitignored: the image bytes
  thumbnails/<id>.webp           # ~30 KB, 400 px longer side
  display/<id>.webp              # ~500-1100 KB, 2160 px longer side

web/                             # the site
  index.html, map.html, galleries.html, gallery.html,
  china.html, blogs.html, search.html, about.html
  trips/index.json               # trip directory
  trips/<slug>/manifest.json     # photos, clusters, camera settings
  trips/<slug>/route.geojson     # the route
  collections/*.json             # generated: China hub, stats, covers, search (gitignored)
  blogs/<slug>.html              # generated blog pages
functions/                       # Cloudflare Pages middleware and the image proxy
```

## Hosting and compression

Pages serves the site (HTML, JS, JSON); R2 holds the images and serves them through a small Pages Function. R2 has **no egress fees**, so traffic never costs anything, and 10 GB of storage is free.

| Profile | Per display image | Notes |
|---|---:|---|
| `--quality 85 --display-longest 1920` | ~540 KB | Fine on most cameras, soft on drone detail |
| `--quality 90 --display-longest 2160` **(default)** | ~700 KB | Indistinguishable from source on most shots |
| `--quality 92 --display-longest 2400` | ~1100 KB | Near-lossless |

At the default, 15,000 photos come to just under 9 GB. The size caps apply to the longer side, so portrait drone shots don't balloon. To re-encode an existing library without re-placing anything:

```bash
./venv/bin/python tools/recompress.py --trip all --quality 85 --display-longest 1920
./venv/bin/python tools/recluster.py --trip all --cluster-radius 25   # change marker clustering
```

Beyond the free 10 GB, R2 charges about $0.015 per GB per month, so even a 30,000-photo library would cost well under $1 a month.

## Local tools

A set of owner-side tools lives in `tools/` and runs only on the laptop:

- **Pickers** (small local web pages) for tile covers, gallery highlights, bridge photos and province corrections.
- **Lightroom plugins** to import photos from a list and sync edits made for posts.
- **People index**: local face clustering across the archive.
- **Phone library** (`tools/build_phone_site.py`): browse phone photos by trip or month, never deployed.
- **Video browser** (`video_browse/`): search clip proxies and build Resolve projects.

## Preparing input

- **GPX**: export from Strava or Garmin Connect. Several days can go in one file or be merged with `tools/reconstruction/gpx_merge.py`.
- **No GPX?** `tools/reconstruction/timeline_to_gpx.py` builds one from a Google Timeline export.
- **Photos** need EXIF `DateTimeOriginal`. Set `--geosync` if the camera was on local time.

## Stack

- **Python**: Pillow, gpxpy, Click, tqdm, ExifTool
- **Front end**: plain JavaScript, Leaflet + markercluster, PhotoSwipe
- **Hosting**: Cloudflare Pages, Pages Functions and R2
