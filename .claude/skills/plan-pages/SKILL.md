---
name: plan-pages
description: Build or update a private trip-plan page from a Google My Maps KMZ plus an itinerary — parse pins/routes, research unpinned stops, curate a spec, build the page, register it on the plans hub, and mark plans done with a blog link. Never commits or deploys anything under private_planning/.
---

# Trip plan pages

Use when Ryan shares a My Maps KMZ and an itinerary for a trip (upcoming or past) and wants it
on the private plans hub, or wants an existing plan updated / marked done.

**PRIVACY (hard rule):** everything under `private_planning/` is git-excluded via
`.git/info/exclude` and must NEVER be committed, pushed, or deployed. The pages are served
locally only, through the `web/planning → ../private_planning/page` symlink (`wrangler pages
deploy` skips symlinked dirs). Do not put anything under `config/` (deploy.py rsyncs it).
Verify `git status --porcelain` is clean after building.

## Layout

```
private_planning/
  build_plan.py                    # generic builder — do not fork per trip
  plans/<plan-id>/
    spec.py                        # curated PLAN dict (see schema below)
    src.kml                        # doc.kml from the KMZ
    *.json                         # optional cached researched routes {"pts":[[lat,lon],...]}
  page/                            # served at /planning/ (index.html = hub, plan.html?id=<id>)
    data/<id>.json, <id>-route.gpx # built artifacts
    plans.json                     # built hub index
```

URLs (behind the site unlock): hub `http://localhost:8788/planning/`, plan
`http://localhost:8788/planning/plan.html?id=<plan-id>`. Preview with `./serve.sh` if asked —
default is to build only; Ryan serves/deploys himself.

## 1. Gather inputs — ask only for what's missing

- **KMZ path** (usually `~/Downloads/....kmz`) and the **itinerary** (dates / day numbers /
  titles / drive times, usually pasted as spreadsheet rows).
- **Status**: `pending` (upcoming) or `done` (past). For done trips check `web/blogs/` for a
  matching blog and set `blog='../blogs/<slug>.html'`; ask if ambiguous, `None` if none exists.
- **Exclusions**: leave out personal/off-topic itinerary details (shopping lists, prices,
  people's logistics) unless clearly wanted — when in doubt about a whole category, ask.

## 2. Ingest the KMZ

```bash
mkdir -p private_planning/plans/<plan-id>
unzip -p "<file>.kmz" doc.kml > private_planning/plans/<plan-id>/src.kml
```

`<plan-id>` = short slug like `qinghai-july-2026`. Inspect the KML (folders → Point/LineString
placemarks; `build_plan.py`'s `parse_kml` shows the namespace handling). My Maps exports have:
pin layers, and one folder per "Directions" leg containing one LineString + its via-point pins.

## 3. Curate the spec — the judgement calls

Write `plans/<plan-id>/spec.py` defining a `PLAN` dict. Copy an existing spec as the template;
the builder validates pin refs and route names and fails loudly. Fields:

- `title`/`sub` (h1 + thin suffix), `window`, `dates` (ISO start, hub sort key), `status`,
  `blog`, `region`, `standfirst`, `source` (map name + export date).
- `routes`: one entry per line — `kml_line=<LineString name>` or `file=<cached json>`;
  `role='main'` (solid highlighted) vs `'alt'` (dashed plan B).
- `map_pins`: `{KML name: (id, kind, note)}` — `kind='stop'` (on the itinerary) or `'alt'`
  (plan B / detour, dimmed layer).
- `research_pins`: stops with no pin on the map (see pin policy).
- `excluded`: source-map pins deliberately left off — leftovers from copied maps outside the
  trip region, and bad Google POIs.
- `nights` (pin ids that get the ☾ marker), `days`, `decision` (optional options block),
  `big_days` (highlighted day numbers), `stats_extra` (e.g. `[['5,231 m', 'high point']]`),
  `notes` (footer caveat).
- `days` entries: `n` (int or `None` for travel/untracked rows), `date` ('Sat 11 Jul'),
  `title`, `body`, `drive` (display string), `drive_h` (float — feeds the "driving (est.)"
  stat; omit when no drive), `pins` (list of pin ids, rendered as fly-to chips).

**Pin policy — accuracy over completeness:**
- Ryan's own My Maps pins are authoritative; use them wherever one exists.
- Itinerary stops with no pin: resolve via WebSearch → Wikipedia/Wikidata/GEM/Baidu (never a
  geocoding API), record the source in `src`, set `approx=True` when the source gives a range
  or you used a city centre. NEVER fabricate coordinates — if unresolvable, leave the stop
  unpinned (itinerary-only) and say so.
- Watch for mislabelled Google POIs (e.g. a "…Railway Station" POI pinned at the wrong town —
  cross-check against Ryan's other pins and the itinerary; exclude the bad one and note it).
- Directions via-points count as map pins (the parser collects every Point placemark).
- Sanity-check each stop's distance to the route lines; off-route stops are fine when they're
  genuine side-trips, but a routing waypoint far from its own line means Google snapped it —
  keep the pin, mention it.
- If the plan text names an alternate corridor that isn't drawn on the map (a "plan B" return
  etc.), route it once via OSRM (`router.project-osrm.org/route/v1/driving/lon,lat;...` with
  forcing via-points) and cache the normalised result as `plans/<id>/route_<name>.json`
  (`{"pts": [[lat,lon],...], "km": N}`) so builds stay offline. Cross-check the km/duration
  against the plan's own estimate.

**Copy rules:** no em dashes anywhere. Keep Ryan's wording, lightly cleaned; use → for legs.

## 4. Build and verify

```bash
python3 private_planning/build_plan.py <plan-id>    # or --all
```

Then verify: `node --check` the page js only if you touched it; check the built JSON (pin
count, route km vs the plan's own estimates, status/blog present); confirm the plan appears in
`page/plans.json`; `git status --porcelain` must be clean. Report route-km cross-checks and
any pin anomalies to Ryan.

## 5. Marking a plan done / linking a blog

Edit the spec: `status='done'`, `blog='../blogs/<slug>.html'` (check `web/blogs/` for the
slug), rebuild that plan. The hub filter chips (All / Pending / Done) and the blog links
render from this automatically.
