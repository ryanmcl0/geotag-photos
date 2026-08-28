# Video Browser (local-only)

Fast local browser for the terabytes of video on the NAS, in the spirit of the
photo site: a one-time ingest pass turns clips into tiny local proxies +
posters + hover filmstrips, so browsing never touches the NAS. Curate clips
into "videos" (like posts), then one click builds a DaVinci Resolve project
with the full-res originals.

Nothing here is ever deployed. `local_videos/` (index, cuts, exports) is
gitignored; `config/video_browse.json` rides along in the private
website-configs backup like the rest of config/. Proxy/poster/filmstrip
storage lives wherever config `media_dir` points (default
`local_videos/media`; set it to a path on the NAS or an external drive when
the proxy library outgrows the internal disk). To move it later: copy the
folder, update `media_dir`, restart the server. Proxy filenames are keyed by
clip id, so nothing else cares where they live. Browsing needs whatever volume
holds the proxies mounted.

## Daily use

Double-click **Video Browser.app** on the Desktop (starts the server if
needed, opens http://localhost:8765).

- Filter by trip / year / day / building / region / device (drone, camera,
  action cam, 360, phone) or search.
- The **building** filter lists climbed buildings from the Urbex rosters
  (`config/china_roofs.json` + `config/world_roofs.json`), matched against each
  clip's folder name exactly as `build_collections.facet_roofs` matches a
  photo's `building` field: longest roster token wins, and a "Day 12 …"
  itinerary folder only matches day-string tokens so a town driven through
  can't claim a same-named tower. The filter hides itself on trips with no
  climbs, and buildings become the group headers on those trips.
- Hover a card to scrub its filmstrip; click to play the proxy. Clips whose
  proxy is not built yet show a "no proxy" badge and stream the original from
  the NAS on click.
- "My Videos" panel: create a video, add clips with the + on each card (or
  press `a` in the player), drag to reorder, then **Create Resolve project**.
  That always writes an FCPXML to `local_videos/exports/` and, when Resolve is
  running with external scripting enabled (Preferences > System > General >
  External scripting using: Local), also builds the project + timeline live
  with the full-res NAS files.

## Onboarding a trip

Add an entry to `config/video_browse.json`:

```json
{
  "name": "<trip name, matching the map trip>",
  "path": "<trip root: the dir holding the day/building folders>",
  "gpx": "<a .gpx file, or a dir of them>",
  "tz_hours": 8,                     // local time, for display only
  "dates": { "start": "…", "end": "…" },   // optional, tightens the clock check
  "clock_offsets": { "<CameraModel>": -8 },
  "exclude": ["Transcripts", "GPX"]
}
```

then:

```bash
python3 video_browse/ingest.py               # metadata pass (fast, incremental)
python3 video_browse/ingest.py --proxies --workers 3   # proxies (slow, resumable)
```

Both are incremental: probes are cached by path+size+mtime, proxies are
written atomically and skipped when present. Interrupt and rerun freely.
Run the proxy pass on the LAN, not over Tailscale.

## Clock offsets (the important gotcha)

`clock_offsets` = hours ADDED to a file's claimed creation_time to get true
UTC. Keys are matched against the camera model first (substring,
case/space-insensitive), then the device bucket, so two bodies on one trip can
be corrected independently.

Cameras get this wrong in several ways, all of which have turned up here:
creation_time that is genuinely UTC; a local wall clock stamped as if it were
UTC (needs the negative of the trip's offset); and a device timezone set such
that a visibly wrong filename stamp still yields a correct creation_time. The
filename and the metadata can disagree, and the metadata is not automatically
the right one.

So do not guess: sweep candidate offsets against the trip's GPX and pick the
one that maximizes GPX matches and puts the clips in daylight hours. Both
signals agreeing is what makes an offset trustworthy.

## When a camera clock is unusable

`clock_offsets` fixes a clock that is wrong by a constant. Three other failures
need different handling, so each clip records where its time came from
(`time_src`), and ingest prints the tally:

- **`metadata`** — the file's own timestamp, believed because it lands inside
  the trip window.
- **`filename`** — no usable timestamp, but the name carries the date
  (`VID-20220428-WA0019.mp4`, WhatsApp exports).
- **`folder`** — a dead camera clock (a GoPro with a flat coin cell stamps
  1971, or resets to its firmware's default year). The clip takes the median
  DATE of its folder's dated siblings, since it sat in the same folder as the
  drone and phone clips from that climb. Time is left at local midday, so
  nothing pretends to be exact.
- **`rejected`** — out of window with no fallback. Deliberately left undated
  rather than polluting the year filter; these still sort by filename inside
  their folder.

The window defaults to 2000-01-01..now. Set `dates: {start, end}` on a trip to
tighten it, which is what catches a camera reset to a plausible-but-wrong year
(a dead clock can land on a real-looking date years off, which the default
window happily accepts).

## Pieces

- `ingest.py`   scan + ffprobe (parallel, cached) + device classification +
                clock correction/fallbacks + GPX/embedded location + day-folder
                parsing + Urbex-roster building matching
                -> `local_videos/index.json`; `--proxies` builds 480p-class
                h264 proxies (hw decode+encode, HDR tonemapped), posters, and
                10-frame filmstrips into `local_videos/media/`.
- `serve.py`    localhost server: UI, Range-capable media streaming (proxy
                and original), cuts + export + reveal APIs, proxy progress.
- `resolve_export.py`  FCPXML writer + live Resolve scripting-API import.
- `web/`        the UI (vanilla html/js/css).
- `launcher.sh` + `install_launcher.sh`  Desktop app wrapper.

Proxy budget: expect roughly 30 GB of proxies per TB of source at the default
1400k bitrate (about 17 MB per clip for 4K-sourced footage). Halve
`PROXY_VBITRATE` if that is too heavy; at 480p the cost is small.

Throughput is bounded by the link to the source media, not the Mac: hardware
decode/encode leaves the CPU mostly idle, and going beyond ~3 concurrent
workers does not increase aggregate read throughput. Run the first pass on the
LAN rather than over a VPN, and hold an idle-sleep assertion (`caffeinate -i -w
<pid>`) so an overnight run is not interrupted.
