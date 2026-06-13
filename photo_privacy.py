#!/usr/bin/env python3
"""
Per-photo privacy within public trips.

Some photos inside PUBLIC trips shouldn't be publicly listed (rooftop shots and
some bridge-visit photos). Trip-level privacy (the *-private split trips) can't
express that, so this module:

  1. computes the unlisted photo set per public trip:
       - any photo whose `building` matches a roofs roster entry
         (config/china_roofs.json + config/world_roofs.json),
       - non-drone photos at bridges (config/china_bridges.json geofences),
         labelled per visit session by config/bridge_visits.json (from
         analyze_bridge_visits.py); sessions/photos without a label stay
         unlisted (fail-closed). Drone aerials are always listed,
       - plus manual force_public / force_private overrides
         (config/photo_privacy.json, gitignored);
  2. splits each affected trip's manifest:
       manifest.json      → public photos only, marked  "filtered": true
       manifest.all.json  → everything (served only behind the See All gate);
     idempotent: when manifest.json carries "filtered", the canonical full
     manifest is manifest.all.json (a reprocessed trip rewrites manifest.json
     in full, which clears the marker and re-seeds the split);
  3. writes functions/photos/private_index.json (gitignored, bundled into the
     R2 image proxy) so protected images 404 without the See All cookie even
     when their URL is known.

Run standalone (./photo_privacy.py [--dry-run]) or via build_collections.py /
deploy.py, both of which call sync().
"""

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
WEB_TRIPS = ROOT / 'web' / 'trips'
OVERRIDES_PATH = ROOT / 'config' / 'photo_privacy.json'
ROOF_ROSTERS = [ROOT / 'config' / 'china_roofs.json', ROOT / 'config' / 'world_roofs.json']
BRIDGES_ROSTER = ROOT / 'config' / 'china_bridges.json'
BRIDGE_VISITS = ROOT / 'config' / 'bridge_visits.json'
PRIVATE_INDEX = ROOT / 'functions' / 'photos' / 'private_index.json'


def _dist_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    x = (lat2 - lat1) * p
    y = (lon2 - lon1) * p * math.cos((lat1 + lat2) / 2 * p)
    return 6371 * math.hypot(x, y)


def load_overrides() -> dict:
    if OVERRIDES_PATH.exists():
        try:
            return json.loads(OVERRIDES_PATH.read_text())
        except json.JSONDecodeError as e:
            print(f"⚠ {OVERRIDES_PATH} is invalid JSON ({e}) — ignoring overrides", file=sys.stderr)
    return {}


def load_trip_meta() -> dict:
    """slug → public flag from web/trips/index.json."""
    idx_path = WEB_TRIPS / 'index.json'
    if not idx_path.exists():
        return {}
    idx = json.loads(idx_path.read_text())
    return {t['id']: t.get('public', False) for t in idx.get('trips', [])}


def load_full_manifest(trip_dir: Path) -> dict | None:
    """The canonical FULL manifest for a trip. After a split, manifest.json is the
    filtered public view and the full data lives in manifest.all.json."""
    mj = trip_dir / 'manifest.json'
    if not mj.exists():
        return None
    try:
        manifest = json.loads(mj.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get('filtered'):
        mall = trip_dir / 'manifest.all.json'
        if mall.exists():
            try:
                return json.loads(mall.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        # Marker without the full file — best we have is the filtered view.
        manifest.pop('filtered', None)
    return manifest


def _roof_matcher():
    """Compiled word-boundary matcher over all roster match tokens. Tokens under
    3 chars are skipped (bare numbers like '22' substring-match road names such
    as 'G227'); the full building-name token still covers those entries."""
    tokens = set()
    for roster_path in ROOF_ROSTERS:
        if not roster_path.exists():
            continue
        roster = json.loads(roster_path.read_text())
        for b in roster.get('buildings', []):
            for t in b.get('match', [b.get('name', '')]):
                if t and len(t) >= 3:
                    tokens.add(t.lower())
    if not tokens:
        return None
    pattern = '|'.join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(r'(?<!\w)(?:' + pattern + r')(?!\w)')


def _bridge_fences() -> list:
    """(lat, lon, radius_km) for every roster bridge with coordinates."""
    fences = []
    if BRIDGES_ROSTER.exists():
        roster = json.loads(BRIDGES_ROSTER.read_text())
        for b in roster.get('bridges', []):
            if b.get('lat') is not None and b.get('lon') is not None:
                fences.append((b['lat'], b['lon'], b.get('radius_km', 3.0)))
    return fences


def load_bridge_labels() -> dict | None:
    """(trip, photo id) → unlisted? from the per-session visit labels
    (config/bridge_visits.json). None when the file hasn't been generated —
    callers then treat every fence photo as unlisted (fail-closed)."""
    if not BRIDGE_VISITS.exists():
        return None
    try:
        data = json.loads(BRIDGE_VISITS.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    labels = {}
    for s in data.get('sessions', []):
        default = s['override'] if s.get('override') is not None else s.get('private_auto', True)
        refined = s.get('refined') or {}
        for pid in s.get('photos', []):
            eff = default if s.get('override') is not None else refined.get(pid, default)
            key = (s['trip'], pid)
            labels[key] = bool(eff) or labels.get(key, False)   # any-session-unlisted wins
    return labels


def compute_private_map(echo=lambda *a: None) -> dict:
    """trip slug → set of private photo ids, for PUBLIC trips only.
    (Private trips are gated wholesale at the trip level.)"""
    trip_public = load_trip_meta()
    roof_match = _roof_matcher()
    fences = _bridge_fences()
    bridge_labels = load_bridge_labels()
    overrides = load_overrides()
    force_public = {k: set(v) for k, v in (overrides.get('force_public') or {}).items()}
    force_private = {k: set(v) for k, v in (overrides.get('force_private') or {}).items()}

    private_map = {}
    for trip_dir in sorted(WEB_TRIPS.iterdir()):
        slug = trip_dir.name
        if not trip_dir.is_dir() or not trip_public.get(slug, False):
            continue
        manifest = load_full_manifest(trip_dir)
        if not manifest:
            continue
        fp = force_public.get(slug, set())
        if '*' in fp:
            continue  # whole trip exempted (e.g. Mongolia UB rooftops stay public)
        roofs_n = bridges_n = 0
        priv = set()
        for ph in manifest.get('photos', []):
            pid = ph['id']
            if pid in fp:
                continue
            if pid in force_private.get(slug, ()):
                priv.add(pid)
                continue
            if pid.upper().startswith('DJI'):
                continue  # drone aerials stay listed
            building = (ph.get('building') or '').lower()
            if building and roof_match and roof_match.search(building):
                priv.add(pid)
                roofs_n += 1
                continue
            # bridges: per-session labels where generated (covers drifted-GPS photos
            # outside the fence too); unlabelled fence photos stay unlisted
            label = bridge_labels.get((slug, pid)) if bridge_labels is not None else None
            if label is True:
                priv.add(pid)
                bridges_n += 1
                continue
            if label is False:
                continue
            lat, lon = ph.get('lat'), ph.get('lon')
            if lat is not None and lon is not None:
                if any(_dist_km(blat, blon, lat, lon) <= r for blat, blon, r in fences):
                    priv.add(pid)
                    bridges_n += 1
        priv |= (force_private.get(slug, set()) & {p['id'] for p in manifest.get('photos', [])})
        if priv:
            private_map[slug] = priv
            echo(f"  {slug}: {len(priv)} private (roofs {roofs_n}, bridges {bridges_n})")
    return private_map


def split_manifests(private_map: dict, dry_run=False, echo=lambda *a: None) -> int:
    """Write the public (filtered) manifest.json + full manifest.all.json for every
    public trip with private photos; restore unaffected trips to a single manifest."""
    trip_public = load_trip_meta()
    changed = 0
    for trip_dir in sorted(WEB_TRIPS.iterdir()):
        if not trip_dir.is_dir() or not trip_public.get(trip_dir.name, False):
            continue
        full = load_full_manifest(trip_dir)
        if not full:
            continue
        mj = trip_dir / 'manifest.json'
        mall = trip_dir / 'manifest.all.json'
        priv = private_map.get(trip_dir.name, set()) & {p['id'] for p in full.get('photos', [])}
        if priv:
            public = dict(full)
            public['photos'] = [p for p in full.get('photos', []) if p['id'] not in priv]
            public['clusters'] = [
                c2 for c2 in (
                    {**c, 'photo_ids': [i for i in (c.get('photo_ids') or []) if i not in priv]}
                    for c in full.get('clusters', [])
                ) if c2['photo_ids']
            ]
            public['filtered'] = True
            if dry_run:
                echo(f"  [dry-run] {trip_dir.name}: would split "
                     f"({len(public['photos'])} public / {len(full.get('photos', []))} total)")
            else:
                mall.write_text(json.dumps(full, indent=2))
                mj.write_text(json.dumps(public, indent=2))
            changed += 1
        else:
            # No private photos (any more) — collapse back to a single manifest.
            current = json.loads(mj.read_text())
            if current.get('filtered') or mall.exists():
                if dry_run:
                    echo(f"  [dry-run] {trip_dir.name}: would restore single manifest")
                else:
                    mj.write_text(json.dumps(full, indent=2))
                    mall.unlink(missing_ok=True)
                changed += 1
    return changed


def _private_blogs():
    """Slugs of non-public blogs from config/blogs.json (empty if no blogs yet)."""
    path = ROOT / 'config' / 'blogs.json'
    if not path.exists():
        return []
    try:
        blogs = json.loads(path.read_text()).get('blogs', [])
    except (OSError, json.JSONDecodeError):
        return []
    return [b['slug'] for b in blogs if not b.get('public')]


def cover_serve_map() -> dict:
    """Photos referenced as tile covers in config/tile_covers.json, resolved to
    {trip: {ids}}. A locked tile (e.g. an all-private province) may still show a
    cover, so its cover photo must be SERVABLE by the image proxy — but it is a
    cover ONLY: it is NOT added to the public manifests, so it never appears on the
    map. (Unlike force_public, which makes a photo fully public.) tile_covers.json is
    thus the single place to set every cover, locked or not.

    Resolution mirrors build_collections' cover lookup: the filepath stem, with the
    owning trip disambiguated by each trip's source edits directory."""
    tc_path = ROOT / 'config' / 'tile_covers.json'
    if not tc_path.exists():
        return {}
    try:
        tc = json.loads(tc_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    specs = []
    def collect(v):
        if isinstance(v, dict):
            for k, val in v.items():
                if not k.startswith('_'):
                    collect(val)
        elif isinstance(v, str) and v and not v.startswith('previews/'):
            specs.append(v)
    collect(tc)

    stem_index, edits_map = {}, {}
    for mf in sorted(WEB_TRIPS.glob('*/manifest.json')):
        man = load_full_manifest(mf.parent)
        if not man:
            continue
        slug = mf.parent.name
        src = ((man.get('source') or {}).get('photos_path') or '').rstrip('/')
        if src:
            edits_map.setdefault(src, []).append(slug)
        for ph in man.get('photos', []):
            pid = ph['id']
            norm = re.split(r'-Enhanced|-NR|-SAI|-2$', pid)[0]
            stem_index.setdefault(pid, (slug, pid))
            stem_index.setdefault(norm, (slug, pid))
            stem_index[f"{slug}/{pid}"] = (slug, pid)
            stem_index.setdefault(f"{slug}/{norm}", (slug, pid))

    out = {}
    for spec in specs:
        stem = Path(spec).stem
        norm = re.split(r'-Enhanced|-NR|-SAI', stem)[0]
        trip_slugs = []
        if '/' in spec:
            best = None
            for path, slugs in edits_map.items():
                if (spec == path or spec.startswith(path + '/')) and (best is None or len(path) > len(best[0])):
                    best = (path, slugs)
            if best:
                trip_slugs = best[1]
        keys = [f'{ts}/{s}' for ts in trip_slugs for s in (stem, norm)] + [stem, norm]
        for k in keys:
            hit = stem_index.get(k)
            if hit:
                out.setdefault(hit[0], set()).add(hit[1])
                break
    return out


def write_private_index(private_map: dict, dry_run=False, echo=lambda *a: None):
    """The image proxy's access index: private trips + private photos within public
    trips + serve-exceptions + private blog pages/assets (a private blog's non-trip
    images live in the 'blog-<slug>-private' pseudo-trip). Bundled into functions/photos
    at deploy.

    'force_public' here is the proxy's allow-list (serve this image even if otherwise
    gated). It is the union of the manual force_public overrides AND every tile cover
    (cover_serve_map) — so a locked tile's cover loads while the photo stays off the
    map (the map is driven by the manifests, which are unaffected by this list)."""
    trip_public = load_trip_meta()
    overrides = load_overrides()
    private_blogs = _private_blogs()
    serve = {k: set(v) for k, v in (overrides.get('force_public') or {}).items()}
    for trip, ids in cover_serve_map().items():
        serve.setdefault(trip, set()).update(ids)
    index = {
        'private_trips': sorted({s for s, pub in trip_public.items() if not pub} |
                                {f'blog-{s}-private' for s in private_blogs}),
        'private_photos': {s: sorted(ids) for s, ids in sorted(private_map.items())},
        'force_public': {k: sorted(v) for k, v in sorted(serve.items())},
        'private_pages': sorted(f'/blogs/{s}' for s in private_blogs),
    }
    if dry_run:
        echo(f"  [dry-run] would write {PRIVATE_INDEX.relative_to(ROOT)} "
             f"({len(index['private_trips'])} private trips, "
             f"{sum(len(v) for v in index['private_photos'].values())} private photos)")
        return
    PRIVATE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_INDEX.write_text(json.dumps(index, indent=2))
    echo(f"  ✓ {PRIVATE_INDEX.relative_to(ROOT)}: {len(index['private_trips'])} private trips, "
         f"{sum(len(v) for v in index['private_photos'].values())} private photos in public trips")


def sync(dry_run=False, echo=print) -> dict:
    """Compute the private map, split manifests, refresh the proxy index.
    Returns the private map (used by build_collections for the public variants)."""
    private_map = compute_private_map(echo)
    n = split_manifests(private_map, dry_run, echo)
    echo(f"  {'would update' if dry_run else 'updated'} {n} trip manifest(s)" if n
         else "  manifests already in sync")
    write_private_index(private_map, dry_run, echo)
    return private_map


if __name__ == '__main__':
    sync(dry_run='--dry-run' in sys.argv)
