"""
Re-edits made while posting, carried through to the live site.

`./post.py pull` writes a photo you re-edited for a post back over its original
in the Edits folder once you confirm (see post.py write_back). This module
remembers which trips that touched and brings them up to date on the site, so
the next deploy uploads the new images.

A re-edit is the same shot in the same place with new pixels, so the usual
route is refresh_photo: re-encode that one photo's web images and update its
aspect ratio, seconds per photo. Only a photo that can't be pinned to one
manifest entry falls back to reprocessing its whole trip.

Why the extra bookkeeping for that fallback instead of a plain
`process_all --update`:
  - --update adopts the current files as its baseline on a trip that has no
    source_state.json yet, which would treat the re-edit as unchanged. Such a
    trip gets a --reindex first, then the re-edited files are marked stale.
  - deploy.py never reprocesses on its own, so the pending list is what lets
    "the next deploy" pick a re-edit up when you decline the pull's prompt.

The pending list lives in .posts_reedits_pending.json (git-ignored):
  {slug: {"name": <trips.json name>, "sources": [<edits path>, ...]}}
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PENDING = ROOT / '.posts_reedits_pending.json'
TRIPS_CONFIG = ROOT / 'config' / 'trips.json'
WEB_TRIPS = ROOT / 'web' / 'trips'


def _slugify(name):
    # Same as process_all.slugify (not imported: that pulls in process_trip)
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def trip_name(slug):
    """trips.json name for a web/trips slug, or None."""
    try:
        config = json.loads(TRIPS_CONFIG.read_text())
    except (OSError, ValueError):
        return None
    for t in config.get('public', []) + config.get('private', []):
        if _slugify(t.get('name', '')) == slug:
            return t['name']
    return None


def load_pending():
    try:
        return json.loads(PENDING.read_text())
    except (OSError, ValueError):
        return {}


def _save(pending):
    if pending:
        PENDING.write_text(json.dumps(pending, indent=1) + '\n')
    else:
        PENDING.unlink(missing_ok=True)


def add_pending(slug, source):
    pending = load_pending()
    entry = pending.setdefault(slug, {'name': trip_name(slug), 'sources': []})
    if str(source) not in entry['sources']:
        entry['sources'].append(str(source))
    _save(pending)


def _python():
    venv = ROOT / 'venv' / 'bin' / 'python'
    return str(venv) if venv.exists() else sys.executable


def _process(name, flag):
    return subprocess.run([_python(), 'process_all.py', '--trip', name, flag],
                          cwd=str(ROOT)).returncode == 0


def _mark_stale(slug, sources):
    """Make --update see these files as changed, whatever their mtime/size."""
    state_path = WEB_TRIPS / slug / 'source_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return
    for src in sources:
        state[src] = [0, 0]
        try:
            os.utime(src)   # newer than the manifest, so process_all calls the trip dirty
        except OSError:
            pass
    state_path.write_text(json.dumps(state))


DIMS_CACHE = ROOT / 'config' / '.dims_cache.json'
COLLECTIONS = ROOT / 'web' / 'collections'


def _manifests(trip_dir):
    """Every manifest file of a trip that lists photos (the public view and,
    once split, the full manifest.all.json)."""
    return [p for p in (trip_dir / 'manifest.json', trip_dir / 'manifest.all.json')
            if p.exists()]


def refresh_photo(slug, source):
    """Fast path for a re-edit: same shot, same place, new pixels. Re-encode
    just this photo's thumbnail + display image with the trip's recorded
    settings and update its aspect ratio, instead of a trip-wide reprocess
    (which walks the whole trip on the drive and re-places every photo).
    Returns (photo id, new ar), or None when the photo can't be pinned down
    (not in the manifest, or the name is ambiguous) so the caller falls back
    to the full reprocess."""
    from PIL import Image
    trip_dir = WEB_TRIPS / slug
    src = Path(source)
    full = None
    for mpath in _manifests(trip_dir):
        m = json.loads(mpath.read_text())
        if full is None or len(m.get('photos', [])) > len(full.get('photos', [])):
            full = m
    if not full:
        return None
    matches = [p for p in full.get('photos', []) if p.get('source_filename') == src.name]
    if len(matches) != 1:
        return None
    rec = matches[0]
    comp = full.get('compression') or {}
    fmt = comp.get('format', 'webp')
    pil_fmt = {'webp': 'WEBP', 'jpeg': 'JPEG', 'avif': 'AVIF'}[fmt]
    opts = {'webp': {'method': 6}, 'jpeg': {'optimize': True, 'progressive': True},
            'avif': {'speed': 6}}[fmt]
    quality = comp.get('quality', 85)
    # Same steps as process_trip.generate_thumbnail / generate_display_image
    with Image.open(src) as img:
        img.load()
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        for key, longest in (('display', comp.get('display_longest', 2160)),
                             ('thumbnail', comp.get('thumbnail_longest', 400))):
            out = trip_dir / rec[key]   # symlinked into hosted-photos/<slug>/
            fit = img
            if max(img.size) > longest:
                r = longest / max(img.size)
                fit = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))),
                                 Image.Resampling.LANCZOS)
            fit.save(out, pil_fmt, quality=quality, **opts)
            if key == 'display':
                w, h = fit.size
    ar = round(w / h, 3)
    for mpath in _manifests(trip_dir):
        m = json.loads(mpath.read_text())
        hit = False
        for p in m.get('photos', []):
            if p.get('id') == rec['id']:
                p['ar'] = ar
                hit = True
        if hit:
            mpath.write_text(json.dumps(m, indent=2))
    _set_dims(slug, rec['id'], w, h, ar)
    # Record the new version, so a later --update does not redo it
    state_path = trip_dir / 'source_state.json'
    try:
        state = json.loads(state_path.read_text())
        st = src.stat()
        state[str(src)] = [int(st.st_mtime), st.st_size]
        state_path.write_text(json.dumps(state))
    except (OSError, ValueError):
        pass
    return rec['id'], ar


def _set_dims(slug, pid, w, h, ar):
    """A crop changes the shape: refresh build_collections' size cache and the
    aspect ratio baked into the collection files (China hub, rooftopping...)."""
    try:
        dims = json.loads(DIMS_CACHE.read_text())
    except (OSError, ValueError):
        dims = None
    if dims is not None and dims.get(f'{slug}/{pid}') != [w, h]:
        dims[f'{slug}/{pid}'] = [w, h]
        DIMS_CACHE.write_text(json.dumps(dims))

    def patch(node):
        n = 0
        if isinstance(node, dict):
            if node.get('trip') == slug and node.get('id') == pid and 'ar' in node \
                    and node['ar'] != ar:
                node['ar'] = ar
                n += 1
            for v in node.values():
                n += patch(v)
        elif isinstance(node, list):
            for v in node:
                n += patch(v)
        return n

    for f in COLLECTIONS.glob('*.json') if COLLECTIONS.exists() else []:
        try:
            data = json.loads(f.read_text())
        except ValueError:
            continue
        if patch(data):
            f.write_text(json.dumps(data))


def apply_pending(dry_run=False):
    """Bring every trip with re-edits waiting up to date. Returns the slugs
    done. Each re-edit takes the fast path (refresh_photo: re-encode that one
    photo); only a trip with a photo it can't pin down gets the full
    reprocess. Trips that fail (or can't be found) stay pending."""
    pending = load_pending()
    if not pending:
        return []
    done = []
    for slug, entry in list(pending.items()):
        name = entry.get('name') or trip_name(slug)
        n = len(entry.get('sources', []))
        if not name:
            print(f"   ⚠️  {slug}: not in config/trips.json, left pending")
            continue
        print(f"   🔁 {name}: {n} re-edited photo(s)")
        if dry_run:
            continue
        missing = [s for s in entry['sources'] if not Path(s).exists()]
        if missing:
            print(f"   ⚠️  {name}: {missing[0]} is not reachable (drive mounted?), left pending")
            continue
        slow = []
        for s in entry['sources']:
            try:
                res = refresh_photo(slug, s)
            except Exception as e:   # unreadable file, odd format: let the pipeline try
                print(f"      {Path(s).name}: fast path failed ({e})")
                res = None
            if res:
                print(f"      ✓ {res[0]} re-encoded")
            else:
                slow.append(s)
        if not slow:
            _done(slug, entry)
            done.append(slug)
            continue
        print(f"      {len(slow)} photo(s) need the full reprocess of {name}")
        if not (WEB_TRIPS / slug / 'source_state.json').exists():
            if not _process(name, '--reindex'):
                print(f"   ⚠️  {name}: baseline (--reindex) failed, left pending")
                continue
        _mark_stale(slug, slow)
        if not _process(name, '--update'):
            print(f"   ⚠️  {name}: reprocess failed, left pending")
            continue
        _done(slug, entry)
        done.append(slug)
    return done


def _done(slug, entry):
    """Drop what was just handled from the pending list. Re-read first: a
    pull may have queued more while this ran, and only the sources handled
    here are done."""
    latest = load_pending()
    left = [s for s in latest.get(slug, {}).get('sources', [])
            if s not in entry['sources']]
    if left:
        latest[slug]['sources'] = left
    else:
        latest.pop(slug, None)
    _save(latest)
