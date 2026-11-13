#!/usr/bin/env python3
"""
Everything for working with post drafts. Always takes a subcommand:

  ./post.py serve     local site + phone photos, kept in sync with the remote
  ./post.py pull      pull the drafts from the remote and copy their files
                      into "IG <name>" or "XHS <name>" by platform, with
                      "Posted " in front once marked posted (every pull
                      renames all folders to match, even a one-post pull)
                      (plus a location card beside every photo marked with
                      the map button, NN_<photo>_map.jpg — see tools/map_card.py)
                      A trip with a photo marked 🛣 gets one route card
                      (the trip's whole GPS route at the post's shape),
                      NN_route_<trip>.jpg, or a message when it has no GPX.
                      Photos marked with the 📍 button get the place name
                      baked into a corner (tools/location_pin.py); each one
                      is listed, and a wrong name is fixed with
                      --location <photo number> "Name" (or "Name / Region",
                      "auto" to go back, "off" to drop the label).
                      A photo over 32 MB (Xiaohongshu's upload limit) is
                      shrunk to fit as it is copied: a lossless repack if
                      that is enough, else the best re-encode that fits.
                      The original in Edits is never touched.
                      Re-edits found in a post folder are listed (post file,
                      where it goes in Edits, an open command) and, once you
                      confirm, written back over their original in Edits
                      unblurred, optionally updating the live site too
                      (see reedits.py)
  ./post.py backfill  the same for re-edits not written back yet (earlier
                      pulls, or answered "not now")
  ./post.py ig N      post Xiaohongshu draft N (its #number, name or id) to
                      Instagram as @ryanmcl0 (the Instagram tab is the
                      urbex account, posted by hand): a fresh pull of that one post, then the
                      items in the draft's order, the caption exactly as it
                      goes out and any problems (which block it), then
                      stops: a dry run by default. With --post it asks
                      "Post this to @ryanmcl0? [y/N]" (--post --yes skips
                      the question). Once Instagram
                      confirms, the draft is marked posted (+ its permalink),
                      the same mark as the Posts page's Posted tick, and
                      synced to the live site. A single video with the
                      card's Trial reel set goes out as a trial reel
                      (non-followers first); more than 5 hashtags is a note.
                      Setup and limits: RUNBOOK §9, tools/instagram_post.py
  ./post.py mirror    sync a dev server started elsewhere (./serve.sh runs this)
  ./post.py sync      copy the remote state down to the local dev server
  ./post.py push      copy the local dev state up to the remote
  ./post.py status    show both sides' versions and post counts

Why sync exists: post drafts live in R2 behind /api/posts, but `wrangler pages
dev` serves its OWN local simulated R2 - a separate store. Editing posts
locally therefore never reaches the live site, and deploy.py does not carry
post state either (it only syncs web/, functions/ and the manifests). So the
two sides are copied explicitly, here.

`serve` and `mirror` (which ./serve.sh and the desktop app start) handle that
automatically, both ways: an edit made here goes up within seconds, an edit
made on the phone comes down within half a minute, and edits to different
posts on both sides combine (merge3, against the last agreed state kept in
local_browse/posts_sync_base.json). Only the same post changed on both sides
is a conflict: the live version wins and this laptop's copy is saved under
local_browse/posts_backups/. The localhost Posts page has a Sync button (and
syncs when it regains focus or closes); every desktop-app launch syncs too.
`status` shows whether the two sides match.

Local-only feature note: the phone-photo companion reads web/phone/, which is
never deployed, so phone selections can only be made through `serve`.

Environment (or parsed from .env.deploy): CF_POSTS_PASSWORD (required),
CF_SITE_PASSWORD (if the site gate is on), CF_PAGES_PROJECT (for the URL),
CF_ALL_PASSWORD (passed to the dev server so "See All" works locally).
"""

import argparse
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import reedits

ROOT = Path(__file__).parent
WEB_TRIPS = ROOT / 'web' / 'trips'
SOURCE_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp')
LOCAL_BASE = 'http://localhost:8788'
DEV_PORT = 8788
CURATE_PORT = 8799
# The mirror's control port (127.0.0.1 only): the localhost Posts page's Sync
# button and the desktop launcher ask it to sync now and read its status.
SYNC_PORT = 8790
# Last state both sides agreed on, per set: what a two-way merge diffs against.
# Kept on disk so a restart (or a mirror that died) still merges instead of
# letting one side overwrite the other.
SYNC_BASE = ROOT / 'local_browse' / 'posts_sync_base.json'
# Copies of anything a sync replaced: a laptop version that lost a same-post
# conflict, or the local document before a first-ever sync.
SYNC_BACKUPS = ROOT / 'local_browse' / 'posts_backups'


def load_env():
    """os.environ, falling back to .env.deploy (a shell file: export KEY="value")."""
    import os
    env = dict(os.environ)
    env_file = ROOT / '.env.deploy'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            line = re.sub(r'^export\s+', '', line)
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            val = val.strip().strip('"').strip("'")
            env.setdefault(key.strip(), val)
    return env


def token_for(env, tier, password):
    """A tier's cookie value, matching the server's tokenFor(): hex HMAC-SHA256
    keyed by CF_COOKIE_SECRET over "<tier>\\n<password>"."""
    key = env.get('CF_COOKIE_SECRET')
    if not key:
        sys.exit('CF_COOKIE_SECRET not set (env or .env.deploy)')
    return hmac.new(key.encode(), f'{tier}\n{password}'.encode(), hashlib.sha256).hexdigest()


def cookie_header(env):
    cookies = [f"posts_auth={token_for(env, 'posts', env['CF_POSTS_PASSWORD'])}"]
    if env.get('CF_SITE_PASSWORD'):
        cookies.insert(0, f"site_auth={token_for(env, 'site', env['CF_SITE_PASSWORD'])}")
    if env.get('CF_ALL_PASSWORD'):
        cookies.append(f"all_access={token_for(env, 'all', env['CF_ALL_PASSWORD'])}")
    return '; '.join(cookies)


def api_call(url, env, method='GET', body=None, timeout=30):
    """One /api/posts request. Returns the parsed JSON, or raises."""
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        # Cloudflare's bot rules 403 the default Python-urllib user agent
        headers={'Cookie': cookie_header(env), 'User-Agent': 'post-cli/1.0',
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_posts(url, env):
    """api_call with friendly errors, for the pull path."""
    try:
        return api_call(url, env)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit("❌ /api/posts returned 404 — wrong CF_POSTS_PASSWORD, "
                     "or the feature is not deployed/enabled.")
        sys.exit(f"❌ Fetch failed: HTTP {e.code}")
    except urllib.error.URLError as e:
        sys.exit(f"❌ Fetch failed: {e.reason}")


# ---------------------------------------------------------------- sync

SETS = ('main', 'auto')      # the two independent documents


def posts_url(base, which='main'):
    return f"{base}/api/posts" + ('?set=auto' if which == 'auto' else '')


def remote_base(env):
    if env.get('CF_SITE_URL'):
        return env['CF_SITE_URL'].rstrip('/')
    project = env.get('CF_PAGES_PROJECT')
    if not project:
        sys.exit('❌ CF_SITE_URL / CF_PAGES_PROJECT not set (needed to reach the live site).')
    return f'https://{project}.pages.dev'


def read_doc(base, env, which='main'):
    return api_call(posts_url(base, which), env)


# The Reels tab's own fields of the Posts document, beside the drafts: the post queue, the reels he rejected
# (each with his reason, for the video project's feedback loop) and his music pool for reels (the songs new reels are
# cut to). Each travels with the drafts; absent = kept.
REEL_FIELDS = ('reel_queue', 'reel_rejects', 'reel_music_pool')


def write_doc(base, env, which, posts, base_version, settings=None, reel_queue=None, reel_rejects=None,
              reel_music_pool=None):
    body = {'baseVersion': base_version, 'posts': posts}
    if settings is not None:
        body['settings'] = settings
    if reel_queue is not None:      # absent = the stored queue is kept (functions/api/posts.ts)
        body['reel_queue'] = reel_queue
    if reel_rejects is not None:    # the same for the reject list
        body['reel_rejects'] = reel_rejects
    if reel_music_pool is not None:     # and the music pool
        body['reel_music_pool'] = reel_music_pool
    return api_call(posts_url(base, which), env, method='PUT', body=body)


def merge_queue(base, local, remote):
    """The Reels tab's post queue (reel_queue: the reels he pasted for the video project's bot,
    video-editor docs/POST_QUEUE.md), both ways like the drafts: its lines by id through merge3 (a line
    added or removed on one side, or re-checked by tools/reelbot/queue_sync.py on the other, combine; the
    shortlist's lines the same way),
    every other field (span_days, span_set, schedule, ...) taken from the side that changed it, the
    live site's when both did. None when neither side has a queue."""
    # items: the queue proper; shortlist: reels he approved but isn't posting yet (the bot never reads it)
    return _merge_lists_doc(base, local, remote, ('items', 'shortlist'))


def merge_rejects(base, local, remote):
    """The Reels tab's reject list (reel_rejects: the reels he rejected, each with his reason; video-editor
    docs/POST_QUEUE.md "Reject list"), merged exactly like the queue: its lines by id through merge3 (a reject added
    here and the Mac's "noted" on another line there combine; an undo stays undone), other fields from the side that
    changed them. A side with no list never empties the other. None when neither side has one."""
    return _merge_lists_doc(base, local, remote, ('items',))


def merge_music_pool(base, local, remote):
    """The Reels tab's music pool (reel_music_pool: songs he added, songs he took out; video-editor docs/POST_QUEUE.md
    "Music pool"), merged like the queue: its lists (songs in the pool, songs taken out, and the Rejected music box's lines)
    line by line by id (a song added here and the Mac's "in" on
    another line there combine), other fields from the side that changed them. None when neither side has one."""
    return _merge_lists_doc(base, local, remote, ('items', 'removed', 'rejects'))


MERGE_FIELD = {'reel_queue': merge_queue, 'reel_rejects': merge_rejects, 'reel_music_pool': merge_music_pool}


def _merge_lists_doc(base, local, remote, lists):
    if local is None and remote is None:
        return None
    # A side with no such field at all has not deleted it: a live site still on an older
    # functions/api/posts.ts drops the field, and that must never empty the laptop's.
    b = base or {}
    l = local if local is not None else b
    r = remote if remote is not None else b
    out = {}
    for k in sorted((set(l) | set(r)) - set(lists)):
        lv, rv, bv = l.get(k), r.get(k), b.get(k)
        out[k] = lv if (same_posts(rv, bv) and not same_posts(lv, bv)) else rv
        if out[k] is None:
            del out[k]
    for k in lists:
        if k == 'items' or any(k in d for d in (b, l, r)):
            out[k], _conflicts = merge3(b.get(k) or [], l.get(k) or [], r.get(k) or [])
    return out


def same_posts(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def merge3(base, local, remote):
    """Two-way sync of post lists against the last copy both sides agreed on.

    Per post (by id): a post only one side changed takes that side's version,
    adds and deletes included. A post both sides changed differently keeps the
    live site's version and is returned in `conflicts` with the laptop's
    version, which the caller saves; a post deleted on one side but edited on
    the other is kept. Order: the live site's, unless only this laptop
    reordered; posts the chosen order lacks go after their predecessor in the
    other side's order (so a new draft at the top stays at the top).

    Returns (merged posts, [(local post, remote post), ...]).
    """
    B = {p.get('id'): p for p in base}
    L = {p.get('id'): p for p in local}
    R = {p.get('id'): p for p in remote}
    merged, conflicts = {}, []
    for i in list(R) + [i for i in L if i not in R] + [i for i in B if i not in R and i not in L]:
        b, l, r = B.get(i), L.get(i), R.get(i)
        if same_posts(l, r):
            pick = r
        elif same_posts(l, b):
            pick = r                       # only the live site changed it
        elif same_posts(r, b):
            pick = l                       # only this laptop changed it
        elif l is None or r is None:
            pick = l if r is None else r   # deleted on one side, edited on the other
        else:
            pick = r
            conflicts.append((l, r))
        if pick is not None:
            merged[i] = pick

    def reordered(order):
        kept = [i for i in order if i in B]
        return kept != [i for i in B if i in set(kept)]
    lo, ro = [p.get('id') for p in local], [p.get('id') for p in remote]
    first, other = (lo, ro) if reordered(lo) and not reordered(ro) else (ro, lo)
    out = [i for i in first if i in merged]
    for n, i in enumerate(other):
        if i in merged and i not in out:
            prev = next((other[j] for j in range(n - 1, -1, -1) if other[j] in out), None)
            out.insert(out.index(prev) + 1 if prev is not None else 0, i)
    out += [i for i in merged if i not in out]
    return [merged[i] for i in out], conflicts


def merge_down(remote_posts, local_posts):
    """Remote wins, except for drafts that exist only on this laptop.

    A copy-down used to replace the local document outright, which is harmless
    while the mirror runs (local edits have already travelled up). Started any
    other way the two sides drift, and a laptop-only draft — the phone-photo ones
    can only be made here — would be erased by the next sync. Those are appended
    instead, renumbered when their #N is already taken on the live site, and the
    mirror then pushes them up, so the two sides converge without losing work.

    Returns (merged posts, the local-only ones that were kept).
    """
    remote_ids = {p.get('id') for p in remote_posts}
    extra = [p for p in local_posts if p.get('id') not in remote_ids]
    merged = list(remote_posts)
    if not extra:
        return merged, []
    used = {p.get('num') for p in remote_posts if isinstance(p.get('num'), int)}
    nxt = max(used) if used else 0
    kept = []
    for post in extra:
        post = dict(post)
        if post.get('num') in used:
            nxt += 1
            post['num'] = nxt
        used.add(post.get('num'))
        merged.append(post)
        kept.append(post)
    return merged, kept


def copy_state(src_base, dst_base, env, label, quiet=False, keep_extra=False):
    """Copy both documents src -> dst. Returns the number of sets changed.

    keep_extra carries drafts the destination has and the source does not (see
    merge_down); used when copying DOWN, so a sync can never delete local work.
    """
    changed = 0
    for which in SETS:
        src = read_doc(src_base, env, which)
        dst = read_doc(dst_base, env, which)
        posts, kept = (merge_down(src.get('posts', []), dst.get('posts', []))
                       if keep_extra else (src.get('posts', []), []))
        # the Reels tab's post queue and reject list travel with the drafts (a source without one keeps the
        # destination's)
        reel = {f: src.get(f) if src.get(f) is not None else dst.get(f) for f in REEL_FIELDS}
        if same_posts(posts, dst.get('posts', [])) and all(same_posts(reel[f], dst.get(f)) for f in REEL_FIELDS):
            continue
        write_doc(dst_base, env, which, posts, dst.get('version', 0), **reel)
        changed += 1
        if not quiet:
            extra = (f" (+{len(kept)} kept from here: "
                     f"{', '.join(p.get('name', '?') for p in kept[:3])})" if kept else '')
            print(f"   ↳ {which}: {len(src.get('posts', []))} posts {label}{extra}")
    return changed


_manifest_cache = {}


def trip_manifest_photos(trip):
    """id → photo record for a trip, from manifest.json overlaid with
    manifest.all.json (private photos only exist in the latter). Also returns
    the trip's source photos_path."""
    if trip in _manifest_cache:
        return _manifest_cache[trip]
    photos, photos_path = {}, None
    for name in ('manifest.json', 'manifest.all.json'):
        p = WEB_TRIPS / trip / name
        if not p.exists():
            continue
        m = json.loads(p.read_text())
        photos_path = (m.get('source') or {}).get('photos_path') or photos_path
        for ph in m.get('photos', []):
            photos.setdefault(ph['id'], ph)
    _manifest_cache[trip] = (photos, photos_path)
    return photos, photos_path


def cached_capture_dates(plan):
    """dst -> 'YYYY-MM-DD' from each trip's exif_cache.json, keyed by the
    edited source path. Covers exports saved without metadata, whose copies
    have no DateTimeOriginal for the smart collection's date rule."""
    caches, out = {}, {}
    for _, ref, src, dst in plan:
        trip = ref['trip']
        if trip not in caches:
            p = WEB_TRIPS / trip / 'exif_cache.json'
            try:
                caches[trip] = json.loads(p.read_text()) if p.exists() else {}
            except ValueError:
                caches[trip] = {}
        dto = (caches[trip].get(str(src)) or {}).get('DateTimeOriginal') or ''
        if re.match(r'\d{4}:\d{2}:\d{2}', dto):
            out[dst] = dto[:10].replace(':', '-')
    return out


def normalize_stem(stem):
    # Same suffix handling as build_collections.build_id_index
    return re.split(r'-Enhanced|-NR|-SAI|-2$', stem)[0]


_scan_cache = {}   # photos_path → every source file under it (one walk per run)


def resolve_source(trip, photo_id, hint=None):
    """{trip, id} → absolute Path of the edited source file, or (None, why).
    hint is where the previous pull found it (the post folder's manifest.json):
    trips sorted into section subfolders (2024 China/Shanghai/...) aren't at
    photos_path/source_filename, and re-scanning the share for them on every
    pull is the slow part, so a still-valid hint short-circuits that."""
    photos, photos_path = trip_manifest_photos(trip)
    ph = photos.get(photo_id)
    if ph is None:
        return None, f'id not in {trip} manifests'
    if not photos_path:
        return None, f'{trip} manifest has no source.photos_path'
    base = Path(photos_path)
    if not base.exists():
        # Distinguish "drive not mounted" from "folder renamed"
        mount = Path(*base.parts[:3]) if str(base).startswith('/Volumes/') else base
        if not mount.exists():
            sys.exit(f"❌ {mount} is not available. Is the RYAN drive mounted?")
        return None, f'source dir missing: {base}'
    fname = ph.get('source_filename', f'{photo_id}.jpg')
    norm = normalize_stem(photo_id)
    quick = [base / fname]
    if hint:
        quick.append(Path(hint))
    if ph.get('section'):
        quick.append(base / ph['section'] / fname)
    for cand in quick:
        if normalize_stem(cand.stem) == norm and cand.exists():
            return cand, None
    # Fall back to a recursive stem search (re-edits can change the suffix)
    if base not in _scan_cache:
        print(f"   🔍 {photo_id}: not at its manifest filename, scanning {base} "
              "once (slow over the network)...")
        _scan_cache[base] = [f for f in base.rglob('*') if f.suffix.lower() in SOURCE_EXTS]
    candidates = [f for f in _scan_cache[base] if normalize_stem(f.stem) == norm]
    if candidates:
        # Prefer the longest stem (most-processed edit), then newest
        best = sorted(candidates, key=lambda f: (len(f.stem), f.stat().st_mtime))[-1]
        return best, None
    return None, f'no file matching {photo_id} under {base}'


MODELS_DIR = ROOT / 'tools' / 'models'


def _face_boxes_scored(img):
    """Face bboxes [(x, y, w, h, score)] via insightface if available (best
    recall, laptop venv) else OpenCV YuNet (tiny ONNX in tools/models — enough
    for the NAS docker container with just opencv-python-headless + numpy)."""
    try:
        from insightface.app import FaceAnalysis
        if not hasattr(_face_boxes_scored, '_app'):
            app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'],
                               allowed_modules=['detection'])
            app.prepare(ctx_id=-1, det_size=(1024, 1024), det_thresh=0.4)
            _face_boxes_scored._app = app
        faces = _face_boxes_scored._app.get(img)
        return [tuple(int(v) for v in (f.bbox[0], f.bbox[1],
                f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1])) + (float(f.det_score),)
                for f in faces]
    except ImportError:
        pass
    import cv2
    model = MODELS_DIR / 'face_detection_yunet_2023mar.onnx'
    h, w = img.shape[:2]
    scale = min(1.0, 1600 / max(w, h))
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
    det = cv2.FaceDetectorYN.create(str(model), '', (small.shape[1], small.shape[0]),
                                    score_threshold=0.5)
    _, faces = det.detect(small)
    out = []
    for f in (faces if faces is not None else []):
        x, y, fw, fh = [int(v / scale) for v in f[:4]]
        out.append((x, y, fw, fh, float(f[14]) if len(f) > 14 else 1.0))
    return out


def _face_boxes(img):
    """Back-compat: bboxes only."""
    return [b[:4] for b in _face_boxes_scored(img)]


def _confirm_face(img, box):
    """Second opinion on a low-confidence candidate: re-detect on a 4x
    upscaled crop around it. Genuine tiny faces re-detect; the texture noise
    the tiled sweep loves (night foliage, building windows) does not. Returns
    the best score found in the crop (0.0 = nothing)."""
    import cv2
    H, W = img.shape[:2]
    x, y, w, h = box
    m = int(2.0 * max(w, h))
    crop = img[max(0, y - m):min(H, y + h + m), max(0, x - m):min(W, x + w + m)]
    if crop.size == 0:
        return 0.0
    up = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    return max((s for *_, s in _face_boxes_scored(up)), default=0.0)


def _face_boxes_tiled(img, tile=1536, overlap=256):
    """Native-resolution tiled sweep for tiny faces (distant figures in wide
    shots)."""
    H, W = img.shape[:2]
    boxes = []
    step = tile - overlap
    for y0 in range(0, max(1, H - overlap), step):
        for x0 in range(0, max(1, W - overlap), step):
            roi = img[y0:min(H, y0 + tile), x0:min(W, x0 + tile)]
            if roi.shape[0] < 64 or roi.shape[1] < 64:
                continue
            for (x, y, w, h) in _face_boxes(roi):
                boxes.append((x + x0, y + y0, w, h))
    # de-dup overlapping tile hits
    out = []
    for b in boxes:
        if not any(abs(b[0] - o[0]) < 20 and abs(b[1] - o[1]) < 20 for o in out):
            out.append(b)
    return out


ROTATED_MIN_SCORE = 0.7   # rotated passes only: a real upside-down face scored
                          # 0.89, sideways city lights 0.59


def _rotated_face_boxes(img):
    """Faces that are sideways or upside down in the frame (a selfie
    shot looking down at someone under the camera), which the detector only
    finds the right way up: detect on the frame turned 90/180/270 degrees and
    map the boxes back. Held to ROTATED_MIN_SCORE, since three extra passes
    give the city three extra chances to look like a face."""
    import cv2
    H, W = img.shape[:2]
    out = []
    for rot in (cv2.ROTATE_180, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        for x, y, w, h, score in _face_boxes_scored(cv2.rotate(img, rot)):
            if score < ROTATED_MIN_SCORE:
                continue
            if rot == cv2.ROTATE_180:
                out.append((W - x - w, H - y - h, w, h))
            elif rot == cv2.ROTATE_90_CLOCKWISE:
                out.append((y, H - x - w, h, w))
            else:
                out.append((W - y - h, x, h, w))
    return out


PERSON_MIN_SCORE = 0.9   # real people scored >= 0.94; a tower, a CCTV camera
                         # and a block of flats 0.82-0.85
HEAD_KP_MIN = 2.0        # keypoint logit; tiny background figures sit below 0


def _person_head_boxes(img):
    """Heads of people whose face the face detector cannot see: turned away,
    backlit, hooded or masked (a backlit 3/4 rear view scored 0.23 against
    the 0.4 cut, while texture noise scored higher). A keypoint model finds
    nose/eyes/ears from any side, so the head box comes from those. Needs
    torchvision (laptop venv; the weights download to the torch cache on
    first use), else it adds nothing."""
    try:
        import torch
        from torchvision.models.detection import (keypointrcnn_resnet50_fpn,
                                                  KeypointRCNN_ResNet50_FPN_Weights)
    except ImportError:
        return []
    import cv2
    import numpy as np
    if not hasattr(_person_head_boxes, '_model'):
        _person_head_boxes._model = keypointrcnn_resnet50_fpn(
            weights=KeypointRCNN_ResNet50_FPN_Weights.DEFAULT).eval()
    H, W = img.shape[:2]
    scale = min(1.0, 1333 / max(H, W))
    small = cv2.resize(img, (int(W * scale), int(H * scale)),
                       interpolation=cv2.INTER_AREA) if scale < 1 else img
    t = torch.from_numpy(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)).permute(2, 0, 1).float() / 255
    with torch.no_grad():
        det = _person_head_boxes._model([t])[0]
    out = []
    for box, score, kps, kscores in zip(det['boxes'], det['scores'],
                                        det['keypoints'], det['keypoints_scores']):
        if score < PERSON_MIN_SCORE:
            continue
        head = kps[:5, :2].numpy() / scale   # nose, eyes, ears
        seen = head[kscores[:5].numpy() > HEAD_KP_MIN]
        # Both shoulders too: a rooftop that passed as a person had none.
        if len(seen) < 2 or not (kscores[5:7] > HEAD_KP_MIN).all():
            continue
        spread = max(np.ptp(seen[:, 0]), np.ptp(seen[:, 1]))
        if spread > 0.4 * float(box[2] - box[0]) / scale:
            continue   # a "person" stitched from two people in a group shot
        # A face-sized box like the detector's (blur_faces_file pads it out
        # to the whole face), centred on nose/eyes when seen, so the pixels
        # land on the face and not the back of the head.
        face = head[:3][kscores[:3].numpy() > HEAD_KP_MIN]
        cx, cy = np.mean(face if len(face) else seen, axis=0)
        shoulders = kps[5:7, :2].numpy() / scale
        side = int(max(spread, 0.45 * float(np.linalg.norm(shoulders[0] - shoulders[1]))))
        out.append((int(cx - side / 2), int(cy - side / 2), side, side))
    return out


def _all_face_boxes(img):
    """Whole-frame pass PLUS the tiled sweep, merged. The whole-frame pass has
    the last word on big faces; tiled-only additions (tiny distant figures the
    downscaled pass misses) must survive _confirm_face, which kills the
    sweep's texture false-positives while keeping real faces (verified: a real
    34px face confirms at 0.7, night-foliage hits at 0.0). Rotated passes add
    the faces that are not the right way up, and the person pass the heads
    whose face is not visible at all."""
    boxes = _face_boxes(img)
    for cand in _person_head_boxes(img):
        cx, cy = cand[0] + cand[2] // 2, cand[1] + cand[3] // 2
        if not any(b[0] - b[2] // 2 <= cx <= b[0] + 3 * b[2] // 2 and
                   b[1] - b[3] // 2 <= cy <= b[1] + 3 * b[3] // 2 for b in boxes):
            boxes.append(cand)
    for cand in _rotated_face_boxes(img):
        if not any(abs(cand[0] - b[0]) < max(30, b[2]) and abs(cand[1] - b[1]) < max(30, b[3])
                   for b in boxes):
            boxes.append(cand)
    for cand in _face_boxes_tiled(img):
        if any(abs(cand[0] - b[0]) < max(30, b[2]) and abs(cand[1] - b[1]) < max(30, b[3])
               for b in boxes):
            continue   # already covered by the whole-frame pass
        if _confirm_face(img, cand) >= 0.45:
            boxes.append(cand)
    return boxes


def _read_image(src):
    """src decoded for OpenCV, or None. OpenCV has no HEIC decoder (phone
    originals), so those go through sips to a temporary JPEG first."""
    import cv2
    import numpy as np
    data = np.fromfile(str(src), dtype=np.uint8)   # path-safe on SMB
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is not None or Path(src).suffix.lower() not in ('.heic', '.heif'):
        return img
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'frame.jpg'
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', 'best',
                        str(src), '--out', str(out)], capture_output=True)
        return cv2.imread(str(out), cv2.IMREAD_COLOR) if out.exists() else None


def blur_faces_file(src, dst):
    """Write a copy of src to dst with every detected face pixelated. Returns
    the face count (0 = wrote a plain copy, -1 = could not decode; the caller
    flags the file name with NEEDS_BLUR either way)."""
    import cv2
    import numpy as np
    img = _read_image(src)
    if img is None:
        if Path(src) != Path(dst):   # in-place re-blur: leave the file alone
            shutil.copy2(src, dst)
        return -1
    H, W = img.shape[:2]
    boxes = _all_face_boxes(img)
    for (x, y, w, h) in boxes:
        # Generous pad so the ellipse inscribed below swallows chin, forehead
        # and the top of the neck (identifying tattoos live there); the
        # pixelation itself is confined to a feathered face-shaped oval
        # instead of stamping the whole padded rectangle.
        pad = int(0.3 * max(w, h))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
        roi = img[y0:y1, x0:x1]
        if roi.size == 0:
            continue
        rh, rw = roi.shape[:2]
        blocks = 9
        small = cv2.resize(roi, (blocks, max(1, round(blocks * rh / rw))),
                           interpolation=cv2.INTER_LINEAR)
        pix = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
        mask = np.zeros((rh, rw), np.float32)
        cv2.ellipse(mask, (rw // 2, rh // 2), (max(1, rw // 2), max(1, rh // 2)),
                    0, 0, 360, 1.0, -1)
        k = max(3, (min(rh, rw) // 8) | 1)   # odd Gaussian kernel = soft edge
        mask = cv2.GaussianBlur(mask, (k, k), 0)[..., None]
        img[y0:y1, x0:x1] = (pix * mask + roi * (1.0 - mask)).astype(img.dtype)
    ok, buf = cv2.imencode(src.suffix if src.suffix.lower() in ('.jpg', '.jpeg', '.png') else '.jpg',
                           img, [cv2.IMWRITE_JPEG_QUALITY, 96])
    if not ok:
        if Path(src) != Path(dst):
            shutil.copy2(src, dst)
        return -1
    buf.tofile(str(dst))
    return len(boxes)


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip() or 'untitled'



def post_dir(root, post, all_posts, move=True, quiet=False):
    """<root>/IG <name> or <root>/XHS <name>, with "Posted " in front once the
    draft is marked posted on the site. The platform prefix keeps an
    Instagram draft and its Xiaohongshu copy (same name) out of each other's
    folder. An existing folder under another of the draft's names is renamed
    to fit, so its re-edits and pull state carry over: the same draft
    before/after being marked posted, or one pulled before prefixes (plain
    <name>), which goes to the IG draft of that name, or the XHS one when
    there is no IG draft; any other draft starts fresh."""
    name = sanitize(post['name'])
    plat = f"{'XHS' if post.get('platform') == 'xhs' else 'IG'} {name}"
    new = root / (f'Posted {plat}' if post.get('posted') else plat)
    if new.exists():
        return new
    olds = [root / plat, root / f'Posted {plat}']
    same = [q for q in all_posts if sanitize(q['name']) == name]
    heir = next((q for q in same if q.get('platform') != 'xhs'), same[0] if same else None)
    if heir is post:
        olds.append(root / name)
    for old in olds:
        if old == new or not (old / 'manifest.json').exists():
            continue
        if quiet:
            break
        if move:
            old.rename(new)
            if old.name == name:
                (new / f'{name}.lrsmcol').unlink(missing_ok=True)   # rewritten under the new name
            print(f'   ↷ {old.name}/ renamed to {new.name}/')
        else:
            print(f'   ↷ {old.name}/ will be renamed to {new.name}/')
        break
    return new


@contextlib.contextmanager
def _read(path):
    """open(path, 'rb') for the RYAN share, whose SMB mount sometimes fails
    the CLOSE with EBADF after a perfectly good read. A read-only handle has
    nothing to flush, so that error is safe to drop; open/read errors are not."""
    f = open(path, 'rb')
    try:
        yield f
    finally:
        try:
            f.close()
        except OSError:
            pass


def _copy(src, dst):
    """shutil.copyfile through _read, checked by size (dst gets a fresh mtime)."""
    with _read(src) as fi, open(dst, 'wb') as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    if Path(dst).stat().st_size != Path(src).stat().st_size:
        raise OSError(f'incomplete copy of {src}')


def _file_md5(path):
    h = hashlib.md5()
    with _read(path) as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# Xiaohongshu refuses any photo over 32 MB (Instagram takes the same files
# fine), so every photo a pull writes is brought under this, whatever the account.
UPLOAD_CAP = 32_000_000
FIT_MIN_QUALITY = 85   # lowest JPEG quality kept at full size; below it, scale down instead


def _too_big(path):
    """A JPEG over UPLOAD_CAP, i.e. one fit_upload_cap would shrink."""
    try:
        return Path(path).stat().st_size > UPLOAD_CAP and _magic(path) == 'jpg'
    except OSError:
        return False


def _repack(data):
    """data losslessly repacked by jpegtran (optimized Huffman tables,
    progressive, every marker kept), or None if that failed."""
    try:
        r = subprocess.run(['jpegtran', '-copy', 'all', '-optimize', '-progressive'],
                           input=data, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b'\xff\xd8' else None


def _reencode_to_fit(data):
    """(jpeg bytes, how) for the best re-encode of data under UPLOAD_CAP: the
    highest quality that fits at full size, else scaled down just enough to
    fit at high quality. EXIF, XMP, ICC profile and chroma sampling are kept.
    (None, None) if nothing fits."""
    from PIL import Image, JpegImagePlugin
    Image.MAX_IMAGE_PIXELS = None   # panoramas
    img = Image.open(io.BytesIO(data))
    img.load()
    sampling = JpegImagePlugin.get_sampling(img)
    keep = {k: img.info[k] for k in ('exif', 'icc_profile', 'xmp') if img.info.get(k)}

    def enc(im, q):
        buf = io.BytesIO()
        # baseline here, then jpegtran's lossless optimize: Pillow's own
        # optimize/progressive fails ("Suspension not allowed") on busy frames
        im.save(buf, 'JPEG', quality=q, subsampling=sampling if sampling >= 0 else 0, **keep)
        return _repack(buf.getvalue()) or buf.getvalue()

    def best(im):
        lo, hi, hit = FIT_MIN_QUALITY, 100, None
        while lo <= hi:
            q = (lo + hi) // 2
            out = enc(im, q)
            if len(out) <= UPLOAD_CAP:
                hit, lo = (q, out), q + 1
            else:
                hi = q - 1
        return hit

    hit = best(img)
    if hit:
        return hit[1], f're-encoded at quality {hit[0]}, full size'
    im, w, h = img, *img.size
    for _ in range(4):
        scale = (UPLOAD_CAP / len(enc(im, 95))) ** 0.5 * 0.97
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
        im = img.resize((w, h), Image.LANCZOS)
        hit = best(im)
        if hit:
            return hit[1], f'scaled to {w}x{h}, quality {hit[0]}'
    return None, None


def fit_upload_cap(path):
    """Shrink a photo over UPLOAD_CAP in place, losing as little as possible:
    first a lossless repack (_repack), and only if that is not enough a re-encode
    (_reencode_to_fit). The file keeps its dates. Returns (shrunk, note):
    note is a line for the log, None when the file was already small enough."""
    path = Path(path)
    size = path.stat().st_size
    if size <= UPLOAD_CAP or path.suffix.lower() not in SOURCE_EXTS:
        return False, None
    head = f'{size / 1e6:.1f} MB, over the {UPLOAD_CAP // 10**6} MB upload limit'
    if _magic(path) != 'jpg':
        return False, f'{head}, not a JPEG so left as is: export it as a JPEG'
    with _read(path) as f:
        data = f.read()
    out = how = None
    packed = _repack(data)
    if packed and len(packed) <= UPLOAD_CAP:
        out, how = packed, 'repacked losslessly'
    if out is None:
        try:
            out, how = _reencode_to_fit(data)
        except Exception as e:
            return False, f'{head}, could not shrink it ({e}), left as is'
    if out is None:
        return False, f'{head}, could not shrink it, left as is'
    st = path.stat()
    tmp = path.with_name(f'.{path.name}.fit_tmp')
    tmp.write_bytes(out)
    os.utime(tmp, (st.st_atime, st.st_mtime))
    os.replace(tmp, path)
    return True, f'{head}: {how}, now {len(out) / 1e6:.1f} MB'


# Lightroom rename suffixes (AI denoise, edits, virtual copies). Same shape as
# tools/sync_post_edits.py: stripping them yields the "core id" both names share.
_LR_SUFFIX_RE = re.compile(
    r'(-(Enhanced-NR|Enhanced-SR|Enhanced|Edit|NR|SR|HDR|Pano)|-\d+)+$', re.I)


def _core_id(filename):
    stem = os.path.splitext(os.path.basename(str(filename)))[0]
    stem = re.sub(r'^\d{2,3}_', '', stem)          # NN_ post prefix, if any
    return _LR_SUFFIX_RE.sub('', stem).lower()


def _capture_id(path):
    """EXIF capture identity (DateTimeOriginal, SubSec, body serial) — what
    truly pins a shot when Lightroom renamed the file. None if unreadable."""
    try:
        out = subprocess.run(
            ['exiftool', '-j', '-DateTimeOriginal', '-SubSecTimeOriginal', '-SerialNumber',
             str(path)], capture_output=True, text=True, timeout=60)
        d = json.loads(out.stdout or '[]')[0]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
    if not d.get('DateTimeOriginal'):
        return None
    return (d['DateTimeOriginal'], str(d.get('SubSecTimeOriginal', '')),
            str(d.get('SerialNumber', '')))


def _adopt_renamed_exports(dest_dir, plan, planned_names):
    """Loose image files in the post folder that are a planned photo under a
    different name — a re-edit exported as e.g. X-Enhanced-NR.jpg or X-2.jpg.
    Matched by core id (LR suffixes stripped), confirmed by EXIF capture
    identity against the drive source. Returns {source_name: loose_path}."""
    loose = [f for f in dest_dir.iterdir()
             if f.is_file() and f.suffix.lower() in SOURCE_EXTS
             and f.name not in planned_names and not f.name.startswith('.')
             and not re.match(r'^removed_', f.name) and not is_card(f.name)]
    if not loose:
        return {}
    by_core = {}
    for _, _, src, _ in plan:
        by_core.setdefault(_core_id(src.name), []).append(src)
    adopted = {}
    for f in loose:
        cands = by_core.get(_core_id(f.name), [])
        if len(cands) != 1:
            if len(cands) > 1:
                print(f'   ⚠️  {f.name}: matches several photos in this post, leaving it alone')
            continue
        src = cands[0]
        a, b = _capture_id(f), _capture_id(src)
        if a is None or b is None or a[0] != b[0] or (a[1] and b[1] and a[1] != b[1]) \
                or (a[2] and b[2] and a[2] != b[2]):
            print(f'   ⚠️  {f.name}: name matches {src.name} but the EXIF capture '
                  'identity does not, leaving it alone')
            continue
        adopted[src.name] = f
    return adopted


def _exif_brief(path):
    try:
        out = subprocess.run(['exiftool', '-j', '-DateTimeOriginal', '-GPSLatitude', str(path)],
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout or '[]')[0]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {}


def _carry_metadata(old, new):
    """An export saved without metadata would cost the photo its capture time
    or GPS on the next reprocess (placement depends on both). Copy over
    whichever of the two the re-edit lacks and the old edit had."""
    o, n = _exif_brief(old), _exif_brief(new)
    tags = []
    if o.get('DateTimeOriginal') and not n.get('DateTimeOriginal'):
        tags += ['-EXIF:DateTimeOriginal', '-EXIF:SubSecTimeOriginal',
                 '-EXIF:OffsetTimeOriginal', '-EXIF:CreateDate',
                 '-EXIF:Make', '-EXIF:Model', '-EXIF:SerialNumber']
    if o.get('GPSLatitude') and not n.get('GPSLatitude'):
        tags.append('-GPS:all')
    if tags:
        subprocess.run(['exiftool', '-q', '-overwrite_original', '-tagsFromFile', str(old),
                        *tags, str(new)], capture_output=True, timeout=120)


def _magic(path):
    try:
        with _read(path) as f:
            head = f.read(4)
    except OSError:
        return None
    return 'jpg' if head[:2] == b'\xff\xd8' else head


EDITS_ROOT = Path('/Volumes/RYAN/Edits')
STAGED = '.reedits_staged'   # clean exports of blur-flagged re-edits, awaiting write-back


def _reedit_ok(ref, src, edited):
    """Whether a re-edit exported into a post folder may go back over its
    original in Edits. Warns about the refusals worth knowing about."""
    if ref['trip'].startswith('phone-'):
        return False   # phone originals are backups, never edits
    if _magic(edited) != _magic(src):
        print(f'   ⚠️  {edited.name}: not the same file type as {src.name}, '
              'Edits left alone')
        return False
    if src.stat().st_size == edited.stat().st_size and _file_md5(src) == _file_md5(edited):
        return False
    a, b = _capture_id(edited), _capture_id(src)
    if a and b and a[0] != b[0]:
        # Sony reuses RM numbers: a same-named export of a different shot
        print(f'   ⚠️  {edited.name}: taken {a[0]}, but {src.name} in Edits was taken '
              f'{b[0]}, Edits left alone')
        return False
    return True


def note_reedit(found, ref, src, edited, dest_dir, post_file, blur):
    """Queue a re-edit for write-back (confirm_reedits asks first). Called
    BEFORE any face blur or shrink: a blur-flagged one, or one over
    UPLOAD_CAP, keeps a clean copy in <post>/.reedits_staged/, since the post
    copy is about to be pixelated or recompressed and Edits must get the
    clean photo."""
    if not _reedit_ok(ref, src, edited):
        return
    new = post_file
    if blur or _too_big(edited):
        new = dest_dir / STAGED / f'reedit_{src.name}'
        new.parent.mkdir(exist_ok=True)
        shutil.copyfile(edited, new)
    found.append({'ref': ref, 'src': src, 'new': new, 'post_file': post_file,
                  'dest_dir': dest_dir})


def write_back(rec):
    """Copy a confirmed re-edit over its original in Edits. The replaced
    original is kept once in <post>/.previous_edits/ (the first one only:
    that is the true original if a photo is re-edited several times)."""
    src, new = rec['src'], rec['new']
    keep = rec['dest_dir'] / '.previous_edits' / src.name
    if not keep.exists():
        keep.parent.mkdir(exist_ok=True)
        part = keep.with_name(f'.{keep.name}.part')   # never a half backup under the real name
        _copy(src, part)
        st = src.stat()
        os.utime(part, (st.st_atime, st.st_mtime))   # the original's date, like copy2
        os.replace(part, keep)
    tmp = src.with_name(f'.{src.name}.reedit_tmp')
    _copy(new, tmp)   # a fresh mtime marks it changed for the pipeline
    _carry_metadata(src, tmp)
    os.replace(tmp, src)
    reedits.add_pending(rec['ref']['trip'], src)
    if new.parent.name == STAGED:
        new.unlink()
        try:
            new.parent.rmdir()
        except OSError:
            pass
    print(f'   ✎ {src.name}: written back to {src.parent}')


# A blur-flagged photo the detector found no face in (hooded, turned away,
# too small) is left unblurred under NN_<stem>_NEEDS-BLUR.<ext>, to blur by
# hand. Once that file's content changes (the hand-blurred version saved over
# it), the next pull takes it as done and drops the flag from the name.
NEEDS_BLUR = '_NEEDS-BLUR'


def flagged(path):
    return path.with_name(path.stem + NEEDS_BLUR + path.suffix)


def unflagged_name(name):
    stem, dot, ext = name.rpartition('.')
    return stem[:-len(NEEDS_BLUR)] + dot + ext if stem.endswith(NEEDS_BLUR) else name


def on_disk(dst):
    """dst, or its NEEDS_BLUR name when that is how it sits in the folder."""
    return dst if dst.exists() or not flagged(dst).exists() else flagged(dst)


def blur_note(n):
    return (f'{n} face(s) pixelated' if n > 0 else
            f'no face found, left unblurred as *{NEEDS_BLUR}, blur it by hand' if n == 0 else
            f'could not decode, left unblurred as *{NEEDS_BLUR}, blur it by hand')


def sync_post_dir(dest_dir, plan, reblur=False, found=None, locs=None, shape=None):
    """Bring dest_dir in line with the plan. Reorders done on the site are
    applied by renaming the existing local NN_ files (in-place edits kept);
    only genuinely new photos are copied from the drive. Returns (copied,
    renamed, adopted). reblur=True regenerates every blur-flagged copy from its source
    (use after the blur algorithm changes).

    Every copy we write is fingerprinted (md5 in .pull_state.json). A local
    NN_ file whose content no longer matches was replaced by hand - typically
    a Lightroom re-export after re-editing the original. Those local edits are
    kept, but if the photo is blur-flagged the fresh export has an unblurred
    face, so it is re-blurred IN PLACE (the re-edit survives; only the face
    pixelation is reapplied).

    Both kinds of re-edit (an adopted renamed export, or an NN_ file replaced
    in place) are also queued in `found` for writing back over the original
    in Edits, see note_reedit.

    Blur-flagged copies with no detectable face end up named *NEEDS_BLUR (see
    flagged); they are moved to their plain name while this runs and back at
    the end.

    locs ({source name: "style|name|region"}, from location_sigs) are the
    location labels to bake in, after any blur. Like the blur, a label that
    changed (or was turned on or off) regenerates that copy from its source,
    and a re-edit exported over a labelled copy is labelled again in place.

    shape (width / height, from post_ratio) crops every copy to the post's one
    shape, centred, before its label goes on; a changed shape regenerates the
    copies the same way, and a re-edit exported over a copy is cropped again."""
    found = [] if found is None else found
    planned = {dst.name for _, _, _, dst in plan}

    # Local NN_<file>s that are no longer at their planned name, keyed by base
    # filename. Two-phase (park under a temp name, then place) so swapped
    # order numbers can't collide mid-rename.
    parked, parked_n = {}, 0
    for f in sorted(dest_dir.iterdir()):
        m = re.match(r'^\d{2,3}_(.+)$', unflagged_name(f.name))
        if not m or not f.is_file() or unflagged_name(f.name) in planned or is_card(f.name):
            continue
        tmp = dest_dir / f'.reorder_{parked_n}_{m.group(1)}'
        parked_n += 1
        f.rename(tmp)
        parked.setdefault(m.group(1), []).append(tmp)

    # Face-blur state: which copies were written blurred, keyed by source
    # filename (stable across reorders). A blur toggle on the site regenerates
    # the copy — the one case where an existing local file is replaced.
    state_path = dest_dir / '.pull_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    blurred = state.setdefault('blurred', {})
    hashes = state.setdefault('hashes', {})   # md5 of each copy as we wrote it, by source name
    needs = state.setdefault('needs_blur', {})   # source name -> True: no face found, flagged
    shrunk = state.setdefault('compressed', {})   # md5 of copies shrunk to UPLOAD_CAP, by source name
    located = state.setdefault('located', {})   # source name -> the label baked into its copy
    shaped = state.setdefault('shaped', {})     # source name -> the post shape its copy was cropped to
    locs = locs or {}
    want_shape = f'{shape:.4f}' if shape else None

    def label(ref, src, dst):
        """Crop dst to the post's shape and bake the wanted location label into it (after any blur);
        remember what went in."""
        sig = locs.get(src.name)
        if want_shape:
            if sig:
                style, head, sub = sig.split('|', 2)
                # cropped first (in the same step, so the face boxes line up) and the label lands inside
                cut = shape_change(dst, shape)
                corner = stamp_location(dst, ref, style, head, sub, shape)
            else:
                cut = crop_to_shape(dst, shape)
            if cut:
                print(f'   ✂️  {dst.name}: cropped {cut[0]} → {cut[1]} (the post\'s shape)')
            shaped[src.name] = want_shape
        else:
            shaped.pop(src.name, None)
            if sig:
                style, head, sub = sig.split('|', 2)
                corner = stamp_location(dst, ref, style, head, sub)
        if sig:
            print(f'   📍 {dst.name}: {head}' + (f' / {sub}' if sub else '') + f' ({style}, {corner})')
            located[src.name] = sig
        else:
            located.pop(src.name, None)
    state.pop('manual_faces', None)   # retired hand-marking tool

    def fit(src, dst):
        """Bring a finished copy under UPLOAD_CAP and fingerprint it."""
        done, note = fit_upload_cap(dst)
        if note:
            print(f'   🗜  {dst.name}: {note}')
        hashes[src.name] = _file_md5(dst)
        if done:
            shrunk[src.name] = hashes[src.name]
        elif hashes[src.name] != shrunk.get(src.name):
            shrunk.pop(src.name, None)

    # Flagged files go to their plain name for the run (reflagged at the end).
    # One whose content changed since we flagged it was blurred by hand: done.
    for _, _, src, dst in plan:
        fl = flagged(dst)
        if not fl.exists() or dst.exists():
            continue
        if needs.get(src.name) and hashes.get(src.name) not in (None, _file_md5(fl)):
            needs.pop(src.name)
            hashes[src.name] = _file_md5(fl)
            print(f'   ✓ {dst.name}: blurred by hand, flag dropped from the name')
        fl.rename(dst)

    # Re-edits exported under a Lightroom-renamed filename (X-Enhanced-NR, X-2, ...)
    adopted = _adopt_renamed_exports(dest_dir, plan, planned)

    copied = renamed = adopted_n = 0
    for i, ref, src, dst in plan:
        want_blur = bool(ref.get('blur'))
        have_blur = bool(blurred.get(src.name, False))
        if src.name in adopted:
            new = adopted.pop(src.name)
            note_reedit(found, ref, src, new, dest_dir, dst,
                        want_blur or bool(locs.get(src.name)) or bool(want_shape))
            dst.unlink(missing_ok=True)
            new.rename(dst)
            print(f'   ⇢ {dst.name}: adopted re-edited export {new.name}')
            needs.pop(src.name, None)
            if want_blur:
                n = blur_faces_file(dst, dst)
                print(f'   🙂🚫 {dst.name}: {blur_note(n)}')
                if n <= 0:
                    needs[src.name] = True
            blurred[src.name] = want_blur
            label(ref, src, dst)
            fit(src, dst)
            adopted_n += 1
            continue
        if reblur and want_blur and dst.exists():
            dst.unlink(missing_ok=True)   # regenerate from source (missing_ok: SMB dir cache can be stale)
            have_blur = False
        want_loc, have_loc = locs.get(src.name), located.get(src.name)
        have_shape = shaped.get(src.name)
        # Only the post's shape changed (e.g. a folder pulled before pulls cropped): the copy is reworked
        # without losing what is in it, see below. A reordered one is brought into place first.
        shape_only = have_blur == want_blur and have_loc == want_loc and have_shape != want_shape
        if shape_only and not dst.exists() and parked.get(src.name):
            parked[src.name].pop(0).rename(dst)
            renamed += 1
            print(f'   ↷ {dst.name}: reordered')
        # A re-edit exported over the copy since the last pull goes through the re-edit path even then.
        fresh = (shape_only and dst.exists() and hashes.get(src.name) is not None
                 and _file_md5(dst) != hashes[src.name])
        if dst.exists() and have_blur == want_blur and have_loc == want_loc and (have_shape == want_shape or fresh):
            rec = hashes.get(src.name)
            cur = _file_md5(dst)
            if rec is None or cur == rec:
                # right place, right content (no fingerprint: pulled before
                # fingerprinting existed, adopt what's there). An over-limit
                # copy from before UPLOAD_CAP is shrunk now; a kept re-edit
                # first leaves its clean file in .reedits_staged/ for backfill.
                hashes[src.name] = cur
                if _too_big(dst):
                    if not want_blur and dst.stat().st_size != src.stat().st_size:
                        keep = dest_dir / STAGED / f'reedit_{src.name}'
                        keep.parent.mkdir(exist_ok=True)
                        shutil.copy2(dst, keep)   # its own date: backfill checks it is newer
                    fit(src, dst)
                continue
            if want_blur and needs.get(src.name):
                # a flagged copy blurred by hand (flag already dropped from the
                # name): post-only, never a re-edit for Edits
                needs.pop(src.name)
                hashes[src.name] = cur
                print(f'   ✓ {dst.name}: blurred by hand')
                continue
            # A re-edit exported over the copy: queued for Edits clean (staged before it is changed here),
            # then cropped and labelled again.
            note_reedit(found, ref, src, dst, dest_dir, dst, want_blur or bool(want_loc) or bool(want_shape))
            if not want_blur:
                if want_loc or want_shape:
                    label(ref, src, dst)
                fit(src, dst)   # local re-edit kept (shrunk if over the limit), just remember it
                continue
            # blur-flagged copy was replaced (re-edit re-exported over it):
            # re-blur the NEW content in place so the re-edit survives.
            print(f'   ⟳ {dst.name}: replaced locally since last pull, re-blurring in place...')
            n = blur_faces_file(dst, dst)
            print(f'   🙂🚫 {dst.name}: {blur_note(n)}')
            if n <= 0:
                needs[src.name] = True
            else:
                needs.pop(src.name, None)
            if want_loc or want_shape:
                label(ref, src, dst)
            fit(src, dst)
            copied += 1
            continue
        if shape_only and dst.exists() and not want_loc and not want_blur and have_shape is None and want_shape:
            # never cropped and nothing baked in: crop it where it is, so a re-edit in it survives
            cut = crop_to_shape(dst, shape)
            if cut:
                print(f'   ✂️  {dst.name}: cropped {cut[0]} → {cut[1]} (the post\'s shape)')
            shaped[src.name] = want_shape
            fit(src, dst)
            continue
        if parked.get(src.name):
            tmp = parked[src.name].pop(0)
            if have_blur == want_blur and have_loc == want_loc and have_shape == want_shape:
                tmp.rename(dst)
                renamed += 1
                print(f'   ↷ {dst.name}: reordered')
                continue
            tmp.unlink()   # blur or label changed: regenerate below
        dst.unlink(missing_ok=True)
        needs.pop(src.name, None)
        # A clean re-edit still waiting to be written back to Edits is newer than the original: build from it.
        base = src
        staged = dest_dir / STAGED / f'reedit_{src.name}'
        try:
            if staged.exists() and staged.stat().st_mtime > src.stat().st_mtime:
                base = staged
        except OSError:
            pass
        try:
            mb = f' ({base.stat().st_size / 1e6:.1f} MB)'
        except OSError:
            mb = ''
        if want_blur:
            print(f'   ⇣ {dst.name}: copying + blurring faces{mb} (the first blur '
                  'loads the face model, give it ~30s)...')
            n = blur_faces_file(base, dst)
            print(f'   🙂🚫 {dst.name}: {blur_note(n)}')
            if n <= 0:
                needs[src.name] = True
        else:
            print(f'   ⇣ {dst.name}: copying{mb}' + (' from the re-edit waiting for Edits' if base != src else '') + '...')
            shutil.copy2(base, dst)
        blurred[src.name] = want_blur
        label(ref, src, dst)
        fit(src, dst)
        copied += 1
    for _, ref, src, dst in plan:
        if not ref.get('blur'):
            needs.pop(src.name, None)
        elif needs.get(src.name) and dst.exists():
            dst.rename(flagged(dst))
    state_path.write_text(json.dumps(state, indent=1))

    # Whatever is still parked was removed from the post on the site.
    for tmps in parked.values():
        for tmp in tmps:
            orig = tmp.name.split('_', 2)[2]
            tmp.rename(dest_dir / f'removed_{orig}')
            print(f'   ⚠️  {orig} is no longer in this post, kept as removed_{orig}')
    return copied, renamed, adopted_n


# ---------------------------------------------------------------- cards in the post folder

# Map and route cards sit in the post folder beside the photos they belong to (they used to live in maps/).
# Anything that sorts the post's own NN_ photos (reorders, re-export adoption) must leave them alone.
CARD_RE = re.compile(r'^\d{2,3}_(.+_map|route_.+)\.jpg$')


def is_card(name):
    return bool(CARD_RE.match(name))


def lift_old_cards(dest_dir):
    """Cards rendered into <post>/maps/ by earlier pulls move up into the post folder (the folder goes once
    empty), so the next render finds them where it now looks."""
    old = dest_dir / 'maps'
    if not old.is_dir():
        return
    for f in sorted(old.iterdir()):
        if f.is_file() and is_card(f.name) and not (dest_dir / f.name).exists():
            f.rename(dest_dir / f.name)
    try:
        old.rmdir()
    except OSError:
        pass   # something else in it: leave it


# ---------------------------------------------------------------- trip route cards

def route_jobs(plan):
    """[(photo number, trip, [refs])] for the trips with photos marked 🛣, in post order: one route card per
    trip, numbered after its first marked photo so it sorts beside it."""
    jobs = {}
    for i, ref, _, _ in plan:
        if ref.get('route'):
            jobs.setdefault(ref['trip'], (i, []))[1].append(ref)
    return [(i, trip, refs) for trip, (i, refs) in jobs.items()]


def sync_route_cards(dest_dir, plan, size):
    """The trip route card for every trip with a photo marked 🛣 (tools/map_card.render_trip_route): the
    trip's whole GPS route at the post's shape, a dot on each marked photo, one card per trip however many
    photos asked for it, in the post folder as NN_route_<trip>.jpg. Re-rendered only when the marked photos, the
    shape or the route itself changed. A trip with no GPX is skipped and said so. → (rendered, skipped)."""
    sys.path.insert(0, str(ROOT / 'tools'))
    import map_card

    maps_dir = dest_dir
    lift_old_cards(dest_dir)
    ours = re.compile(r'^\d{2,3}_route_.+\.jpg$')
    state_path = dest_dir / '.pull_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    cards = state.setdefault('routes', {})
    rendered = skipped = 0
    keep = set()
    jobs = route_jobs(plan)
    for i, trip, refs in jobs:
        target = maps_dir / f'{i:02d}_route_{trip}.jpg'
        route_file = ROOT / 'web' / 'trips' / trip / 'route.geojson'
        if not map_card.route_segments(trip):     # no file, or one with no line in it
            print(f"   🛣  {trip}: no GPX route on record for this trip, so no route card "
                  f"({len(refs)} photo{'s' if len(refs) != 1 else ''} marked)")
            skipped += 1
            continue
        keep.add(target.name)
        sig = {'file': target.name, 'size': list(size), 'marks': sorted(r['id'] for r in refs),
               'route': route_file.stat().st_mtime, 'v': map_card.CARD_VERSION}
        if cards.get(trip) == sig and target.exists():
            continue
        was = cards.get(trip) or {}
        old = maps_dir / was['file'] if was.get('file') else None
        if old and old.exists() and {**was, 'file': None} == {**sig, 'file': None}:
            old.replace(target)            # only its number changed (the post was reordered)
            cards[trip] = sig
            continue
        photos, _ = trip_manifest_photos(trip)
        marks = [(ph['lat'], ph['lon']) for ph in (photos.get(r['id']) for r in refs)
                 if ph and ph.get('lat') is not None]
        print(f"   🛣  {target.name}: rendering the {trip} route...")
        card, why = map_card.render_trip_route(trip, size, marks)
        if card is None:
            print(f"   ⚠️  no route card for {trip}: {why}")
            skipped += 1
            continue
        maps_dir.mkdir(parents=True, exist_ok=True)
        card.save(target, quality=93)
        cards[trip] = sig
        rendered += 1
    for trip in [t for t in cards if t not in {t for _, t, _ in jobs}]:
        del cards[trip]
    if dest_dir.exists():
        state_path.write_text(json.dumps(state, indent=1))
    # cards for trips that lost all their marks (or whose first marked photo moved)
    if maps_dir.exists():
        for f in sorted(maps_dir.iterdir()):
            if f.is_file() and ours.match(f.name) and f.name not in keep:
                f.unlink()
    return rendered, skipped


# ---------------------------------------------------------------- location pins

def stamp_location(path, ref, style, head, sub, ratio=None):
    """Bake the place name into the copy at path (tools/location_pin.py), after cropping it to `ratio` when
    given → the corner it went in."""
    sys.path.insert(0, str(ROOT / 'tools'))
    import location_pin
    return location_pin.stamp(path, path, head, sub, style, ref['trip'], ref['id'], ratio)


def shape_change(path, ratio):
    """(old shape, new shape) labels when the image at path needs cropping to `ratio`, else None. Reads only
    the header."""
    from PIL import Image, ImageOps
    sys.path.insert(0, str(ROOT / 'tools'))
    import location_pin
    if Path(path).suffix.lower() in ('.heic', '.heif'):
        from pillow_heif import register_heif_opener
        register_heif_opener()
    with Image.open(path) as im:
        w, h = im.size[::-1] if im.getexif().get(0x0112) in (5, 6, 7, 8) else im.size
    return (ratio_label(w / h), ratio_label(ratio)) if location_pin.crop_box(w, h, ratio) else None


def crop_to_shape(path, ratio):
    """Centre-crop the copy at path to `ratio` (width / height) in place, keeping its EXIF and colour
    profile → (old shape, new shape) as labels, or None when it already has that shape."""
    from PIL import Image, ImageOps
    sys.path.insert(0, str(ROOT / 'tools'))
    import location_pin
    heic = Path(path).suffix.lower() in ('.heic', '.heif')
    if heic:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    with Image.open(path) as im:
        exif, icc = im.info.get('exif'), im.info.get('icc_profile')
        img = ImageOps.exif_transpose(im)
        img.load()
    box = location_pin.crop_box(*img.size, ratio)
    if not box:
        return None
    before = ratio_label(img.size[0] / img.size[1])
    out = img.crop(box).convert('RGB')
    kw = {'quality': 95}
    if not heic:
        kw['subsampling'] = 0
    if exif:
        try:   # the pixels are upright now: drop the orientation tag so nothing rotates them again
            ex = Image.Exif()
            ex.load(exif)
            ex[0x0112] = 1
            kw['exif'] = ex.tobytes()
        except Exception:
            pass
    if icc:
        kw['icc_profile'] = icc
    out.save(path, 'HEIF' if heic else 'JPEG', **kw)
    return before, ratio_label(ratio)


def location_sigs(dest_dir, plan, overrides=()):
    """{source name: "style|name|region"} for every photo to label: marked 📍 on the site (ref.loc, style
    A or C), or named with --location at pull. This pull's --location overrides are remembered in the
    post folder's .pull_state.json (by source name, so they survive reorders): "Name" replaces the name
    and keeps the worked-out region line, "Name / Region" sets both, "auto" forgets the override, "off"
    keeps that photo unlabelled. → (sigs, notes to print)."""
    sys.path.insert(0, str(ROOT / 'tools'))
    import location_pin
    state_path = dest_dir / '.pull_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    texts = state.setdefault('loc_text', {})
    by_num = {i: src for i, _, src, _ in plan}
    notes = []
    for num, text in overrides:
        num = int(num) if str(num).isdigit() else None
        src = by_num.get(num)
        if src is None:
            notes.append(f'⚠️  --location: no photo with that number in this post (its NN_ prefix)')
            continue
        if text.strip().lower() == 'auto':
            texts.pop(src.name, None)
        else:
            texts[src.name] = text.strip()
    if overrides and dest_dir.exists():
        state_path.write_text(json.dumps(state, indent=1))
    sigs = {}
    for i, ref, src, _ in plan:
        text = texts.get(src.name)
        if text and text.lower() == 'off':
            continue
        style = ref.get('loc')
        if not style and not text:
            continue
        style = style if style in location_pin.STYLES else 'A'
        head, sub = location_pin.label_for(ref['trip'], ref['id'])
        if text:
            given, _, region = text.partition(' / ')
            head, sub = given.strip(), (region.strip() if region else (sub or ''))
        if not head:
            notes.append(f'⚠️  📍 {i:02d}: no position on record, so no name: '
                         f'give one with --location {i} "Name"')
            continue
        sigs[src.name] = f'{style}|{head}|{sub or ""}'
    return sigs, notes


# ---------------------------------------------------------------- map cards

MAP_LONG_EDGE = 1350
# Shapes each platform allows, as width/height: Instagram caps portrait at 4:5,
# Xiaohongshu at 3:4.
PLATFORM_SHAPE = {'ig': (0.8, 1.91), 'xhs': (0.75, 4 / 3)}


def post_ratio(post, plan):
    """The one shape (width / height) of every slide in this post.

    A carousel shows every slide at the shape of the first, so the post follows
    its first photo, clamped to what Instagram allows (4:5 to 1.91:1; the same
    folder goes to Xiaohongshu): a 9:16 frame becomes 4:5. Pull crops every carousel
    photo to it (crop_box) and renders the map cards at it, so the post folder
    is exactly what goes out."""
    ar = 1.5
    for _, ref, _, _ in plan:
        cand = ref.get('ar')
        if not cand:
            photos, _ = trip_manifest_photos(ref['trip'])
            cand = (photos.get(ref['id']) or {}).get('ar')
        if cand:
            ar = float(cand)
            break
    lo, hi = PLATFORM_SHAPE['ig']
    return min(hi, max(lo, ar))


def ratio_label(r):
    for name, v in (('1:1', 1), ('4:5', 4 / 5), ('3:4', 3 / 4), ('4:3', 4 / 3), ('3:2', 1.5), ('16:9', 16 / 9),
                    ('1.91:1', 1.91), ('2:3', 2 / 3), ('9:16', 9 / 16)):
        if abs(r - v) < 0.01:
            return name
    return f'{r:.2f}:1'


def post_card_size(post, plan):
    """The size every map card in this post is rendered at: the post's shape (post_ratio)."""
    r = post_ratio(post, plan)
    if r >= 1:
        return MAP_LONG_EDGE, round(MAP_LONG_EDGE / r)
    return round(MAP_LONG_EDGE * r), MAP_LONG_EDGE


def sync_map_dir(dest_dir, plan, size):
    """Render a location card for every photo carrying a map mark.

    Cards go in the post folder beside their photo (NN_<photo>_map.jpg), so the pair
    is obvious in Lightroom. Which card was rendered is remembered next to the
    blur state, so an unchanged one is neither re-rendered nor re-downloaded and
    a reorder on the site is just a rename. Returns (rendered, renamed, failed).
    """
    sys.path.insert(0, str(ROOT / 'tools'))
    import map_card

    maps_dir = dest_dir
    lift_old_cards(dest_dir)
    jobs = [(i, ref, src, ref.get('map')) for i, ref, src, _ in plan if ref.get('map')]
    ours = re.compile(r'^\d{2,3}_.+_map\.jpg$')
    if not jobs:
        if maps_dir.exists():
            for f in sorted(maps_dir.iterdir()):
                if f.is_file() and ours.match(f.name):
                    f.unlink()
        return 0, 0, 0

    maps_dir.mkdir(parents=True, exist_ok=True)
    state_path = dest_dir / '.pull_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    cards = state.setdefault('maps', {})

    rendered = renamed = failed = 0
    keep = set()
    for i, ref, src, style in jobs:
        target = maps_dir / f'{i:02d}_{src.stem}_map.jpg'
        keep.add(target.name)
        if style not in map_card.STYLES:
            print(f"   ⚠️  {src.name}: unknown map style {style!r}")
            failed += 1
            continue
        was = cards.get(src.name) or {}
        old = maps_dir / was['file'] if was.get('file') else None
        if was.get('style') == style and was.get('size') == list(size) and old and old.exists() \
                and was.get('v') == map_card.CARD_VERSION:
            if old != target:
                old.replace(target)
                cards[src.name] = {**was, 'file': target.name}
                renamed += 1
            continue
        print(f"   🗺  {target.name}: rendering {style} card...")
        card, why = map_card.card_for_photo(style, ref['trip'], ref['id'], size)
        if card is None:
            print(f"   ⚠️  no {style} card for {src.name}: {why}")
            failed += 1
            continue
        if old and old.exists() and old != target:
            old.unlink()
        card.save(target, quality=93)
        cards[src.name] = {'file': target.name, 'style': style, 'size': list(size), 'v': map_card.CARD_VERSION}
        rendered += 1
    for name in [n for n in cards if n not in {s.name for _, _, s, _ in jobs}]:
        del cards[name]
    state_path.write_text(json.dumps(state, indent=1))

    # Cards for photos that lost their mark, or left the post entirely.
    for f in sorted(maps_dir.iterdir()):
        if f.is_file() and ours.match(f.name) and f.name not in keep:
            f.unlink()
    return rendered, renamed, failed


PHONE_MANIFESTS = Path('/Volumes/RYAN/phone_browse/manifests')
_phone_cache = {}


def phone_sources(slug):
    """(name -> original src Path, Phone dir root) for one phone trip slug."""
    if slug in _phone_cache:
        return _phone_cache[slug]
    mpath = PHONE_MANIFESTS / f'{slug}.jsonl'
    names, phone_root = {}, None
    if mpath.exists():
        for line in mpath.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            names[row['name']] = Path(row['src'])
            if phone_root is None:
                d = Path(row['src']).parent
                while d.name.lower() != 'phone' and d != d.parent:
                    d = d.parent
                phone_root = d
    _phone_cache[slug] = (names, phone_root)
    return _phone_cache[slug]


def pseudo_source(trip, pid):
    """Original for a curated pseudo trip (web/phone/trips/<trip>/) photo.

    Those trips are materialised from the personal backup share by
    curate_photos_of_person.py, so they are in no phone_browse manifest; their
    own manifest.json records a local copy of the original ('original') and
    the backup path it came from ('src').
    """
    mpath = ROOT / 'web' / 'phone' / 'trips' / trip / 'manifest.json'
    if not mpath.exists():
        return None
    for row in json.loads(mpath.read_text()).get('photos', []):
        if row.get('id') != pid:
            continue
        for key in ('original', 'src'):
            if row.get(key):
                p = Path(row[key])
                p = p if p.is_absolute() else mpath.parent / p
                if p.exists():
                    return p
    return None


def resolve_phone(ref):
    """Original NAS file for a {trip, id} phone photo or {trip, file} video."""
    slug = ref['trip'].removeprefix('phone-')
    names, phone_root = phone_sources(slug)
    if ref.get('id'):
        src = names.get(ref['id']) or pseudo_source(ref['trip'], ref['id'])
        return (src, None) if src and src.exists() else (None, 'not in phone manifest')
    if phone_root is None:
        return None, 'phone trip manifest missing'
    src = phone_root / ref['file']
    return (src, None) if src.exists() else (None, f'missing: {src}')


def resolve_wait(ref, hint=None):
    """Waitlist refs are carousel or phone photos; pick the right resolver."""
    if ref['trip'].startswith('phone-'):
        return resolve_phone(ref)
    return resolve_source(ref['trip'], ref['id'], hint)


def previous_sources(dest_dir):
    """(trip, id) → source path recorded by the last pull into dest_dir."""
    try:
        m = json.loads((dest_dir / 'manifest.json').read_text())
    except (OSError, ValueError):
        return {}
    return {(r['trip'], r['id']): r['source']
            for r in (m.get('photos') or []) + (m.get('waitlist') or [])
            if r.get('source') and r.get('id')}


def phone_dest(phone_dir, i, ref, src):
    """Where a phone pick lands in <post>/Phone/. A blur-flagged photo becomes
    a face-pixelated JPEG under its own name, so toggling the mark swaps one
    file for the other and the folder alone shows which is which."""
    if ref.get('blur') and ref.get('id'):
        return phone_dir / f'{i:02d}_{src.stem}_blurred.jpg'
    return phone_dir / f'{i:02d}_{src.name}'


def sync_phone_dir(phone_dir, plan):
    """Mirror the plan into <post>/Phone/: copy new, drop unselected copies,
    write a blurred JPEG for each blur-flagged photo."""
    phone_dir.mkdir(parents=True, exist_ok=True)
    wanted = {n for _, _, _, dst in plan for n in (dst.name, flagged(dst).name)}
    removed = 0
    for f in sorted(phone_dir.iterdir()):
        if f.is_file() and re.match(r'\d+_', f.name) and f.name not in wanted:
            f.unlink()
            removed += 1
    state_path = phone_dir / '.pull_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    state.pop('manual_faces', None)   # retired hand-marking tool

    def fit(src, dst):
        """Bring a copy under UPLOAD_CAP; a shrunk one is remembered by its
        source's size, which its own size no longer matches."""
        done, note = fit_upload_cap(dst)
        if note:
            print(f'   🗜  Phone/{dst.name}: {note}')
        if done:
            state.setdefault('compressed', {})[dst.name] = src.stat().st_size

    copied = 0
    for _, ref, src, dst in plan:
        blur = dst.name.endswith('_blurred.jpg')
        if blur:
            state.setdefault('blurred', {})[dst.name.split('_', 1)[1]] = True
            if dst.exists() or flagged(dst).exists():
                if _too_big(dst):
                    fit(src, dst)   # blurred before UPLOAD_CAP existed
                continue   # a flagged one is kept for blurring by hand
        elif dst.exists() and state.get('compressed', {}).get(dst.name) == src.stat().st_size:
            continue
        elif dst.exists() and dst.stat().st_size == src.stat().st_size:
            if _too_big(dst):
                fit(src, dst)   # copied before UPLOAD_CAP existed
            continue
        try:
            mb = f' ({src.stat().st_size / 1e6:.1f} MB)'
        except OSError:
            mb = ''
        if blur:
            print(f'   ⇣ Phone/{dst.name}: copying + blurring faces{mb}...')
            n = blur_faces_file(src, dst)
            fit(src, dst)
            if n <= 0:
                dst.rename(flagged(dst))
                print(f'      ⚠️  {blur_note(n)}')
        else:
            print(f'   ⇣ Phone/{dst.name}: copying{mb}...')
            shutil.copy2(src, dst)
            fit(src, dst)
        copied += 1
    if state:
        state_path.write_text(json.dumps(state, indent=1))
    return copied, removed


def cmd_pull(args):
    # The dest (and the source files) live on the network drive: fail fast
    # with a clear message if it isn't mounted.
    dest_root = Path(args.dest)
    if str(dest_root).startswith('/Volumes/'):
        mount = Path(*dest_root.parts[:3])
        if not mount.exists():
            sys.exit(f"⚠️  {mount} is not mounted. Connect the RYAN drive first — "
                     "the post folders and their source files both live on it.")

    env = load_env()
    posts_password = env.get('CF_POSTS_PASSWORD')
    if not posts_password:
        sys.exit('❌ CF_POSTS_PASSWORD not set (environment or .env.deploy).')
    url = args.url
    if not url and args.local:
        url = posts_url(LOCAL_BASE)
    if not url:
        url = posts_url(remote_base(env))

    # `pull` means "whatever the live site says": if the dev server is running
    # with edits that never made it up, pulling the remote would quietly use
    # stale drafts. Say so rather than copying the wrong photos.
    if not args.local and server_up(LOCAL_BASE, env):
        try:
            if any(not same_posts(read_doc(LOCAL_BASE, env, w).get('posts', []),
                                  read_doc(remote_base(env), env, w).get('posts', []))
                   for w in SETS):
                print('⚠️  The local dev server has drafts that differ from the live '
                      'site. Run  ./post.py push  first, or pull with --local.\n')
        except (urllib.error.URLError, urllib.error.HTTPError):
            pass

    if getattr(args, 'location', None) and args.num is None and not args.post:
        sys.exit('❌ --location needs the post: ./post.py pull <#> --location <photo> "Name"')

    print(f'⇣ Fetching drafts from {url} ...')
    doc = fetch_posts(url, env)
    posts = doc.get('posts', [])
    if args.num is not None:
        posts = [p for p in posts if p.get('num') == args.num]
        if not posts:
            avail = ', '.join(f"#{p['num']}" if p.get('num') else f'"{p["name"]}" (no number)'
                              for p in doc.get('posts', []))
            sys.exit(f'❌ No post numbered #{args.num}. Available: {avail or "none"}\n'
                     '   (Numbers are assigned when the /posts page is opened.)')
    if args.post:
        posts = [p for p in posts if p['name'] == args.post]
        if not posts:
            sys.exit(f'❌ No post named "{args.post}". '
                     f'Available: {", ".join(p["name"] for p in doc.get("posts", []))or "none"}')
    if getattr(args, 'id', None):            # ./post.py ig: exactly one draft, by its id
        posts = [p for p in posts if p.get('id') == args.id]
        if not posts:
            sys.exit(f'❌ The draft {args.id} is no longer on {url}.')
    if not posts:
        print('No post drafts yet.')
        return

    print(f"📥 {len(posts)} post draft(s) (state v{doc.get('version')})\n")
    # Every draft's folder follows its name, platform and posted mark, even on
    # a pull of one post: renames only, the photos sync below as usual.
    for post in doc.get('posts', []):
        post_dir(Path(args.dest), post, doc.get('posts', []),
                 move=not (args.list or args.dry_run))
    unresolved = 0
    found = []   # re-edits to confirm for write-back to Edits
    for post in posts:
        dest_dir = post_dir(Path(args.dest), post, doc.get('posts', []), move=False, quiet=True)
        name = dest_dir.name
        tag = f"#{post['num']} " if post.get('num') else ''
        acct = f" @{post['account']}" if post.get('account') else ''
        n_wait = len(post.get('waitlist') or [])
        wait_txt = f", {n_wait} waitlisted" if n_wait else ''
        if n_wait and args.skip_waitlist:
            wait_txt += ' (skipped)'
        print(f"── {tag}{post['name']}{acct} ({len(post['photos'])} photos{wait_txt}) → {dest_dir}")
        prev = previous_sources(dest_dir)
        plan = []
        for i, ref in enumerate(post['photos'], 1):
            # A phone photo moved into the carousel resolves to its phone original.
            src, why = resolve_wait(ref, prev.get((ref['trip'], ref['id'])))
            if src is None:
                print(f"   ⚠️  {i:02d} {ref['trip']}/{ref['id']}: {why}")
                unresolved += 1
                continue
            plan.append((i, ref, src, dest_dir / f'{i:02d}_{src.name}'))

        caption = (post.get('caption') or '').strip()
        song = (post.get('song') or '').strip()
        account = (post.get('account') or '').strip()
        cap_what = ' + '.join(w for w, v in (('account', account), ('song', song),
                                             ('caption', caption)) if v)

        shape = post_ratio(post, plan) if plan else None
        if args.list or args.dry_run:
            size = post_card_size(post, plan)
            if shape:
                print(f"   ✂️  every photo cropped to {ratio_label(shape)} (the post's shape, from its first photo)")
            if cap_what:
                print(f"   📝 {cap_what}  →  {dest_dir / 'caption.txt'}")
            sigs, notes = location_sigs(dest_dir, plan)
            for n in notes:
                print(f"   {n}")
            for i, ref, src, dst in plan:
                big = '  (over 32 MB, shrunk to fit)' if _too_big(src) else ''
                cut = shape_change(src, shape) if shape else None
                print(f"   {i:02d} {src}  →  {dst}{big}" + (f"  (cropped {cut[0]} → {cut[1]})" if cut else ''))
                if src.name in sigs:
                    style, head, sub = sigs[src.name].split('|', 2)
                    print(f"      📍 {head}" + (f" / {sub}" if sub else '') + f"  ({style})")
                if ref.get('map'):
                    print(f"      🗺  {ref['map']} card {size[0]}x{size[1]}  →  "
                          f"{dest_dir / f'{i:02d}_{src.stem}_map.jpg'}")
            sys.path.insert(0, str(ROOT / 'tools'))
            import map_card
            for i, trip, refs in route_jobs(plan):
                has = bool(map_card.route_segments(trip))
                print(f"   🛣  {trip} route card ({len(refs)} marked)  →  "
                      + (f"{dest_dir / f'{i:02d}_route_{trip}.jpg'}" if has
                         else 'skipped: no GPX route on record for this trip'))
            for i, ref in enumerate([] if args.skip_waitlist else post.get('waitlist') or [], 1):
                src, why = resolve_wait(ref, prev.get((ref['trip'], ref.get('id'))))
                print(f"   Waitlist {i:02d} {src or why}  →  {dest_dir / 'Waitlist'}")
            for i, ref in enumerate(post.get('phone') or [], 1):
                src, why = resolve_phone(ref)
                print(f"   Phone {i:02d} {src or why}  →  "
                      f"{phone_dest(dest_dir / 'Phone', i, ref, src) if src else dest_dir / 'Phone'}")
            print()
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            old_manifest = json.loads((dest_dir / 'manifest.json').read_text())
        except (OSError, ValueError):
            old_manifest = {}
        sigs, notes = location_sigs(dest_dir, plan, getattr(args, 'location', None) or [])
        for n in notes:
            print(f"   {n}")
        copied, renamed, adopted = sync_post_dir(dest_dir, plan,
                                                 reblur=getattr(args, 'reblur', False),
                                                 found=found, locs=sigs, shape=shape)
        if sigs:
            # every label on this post, with the fix for a wrong one
            num = post.get('num')
            print(f"   📍 {len(sigs)} labelled:")
            for i, ref, src, dst in plan:
                if src.name in sigs:
                    style, head, sub = sigs[src.name].split('|', 2)
                    print(f"      {i:02d} {head}" + (f" / {sub}" if sub else ''))
            print(f"      wrong? ./post.py pull {num if num else '<#>'} --location <photo number> \"Name\"")
        manifest = {
            'name': post['name'],
            'version': doc.get('version'),
            'photos': [{'order': i, 'trip': ref['trip'], 'id': ref['id'],
                        'source': str(src), 'copied_to': str(on_disk(dst))}
                       for i, ref, src, dst in plan],
        }
        (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        parts = [f'{copied} copied']
        if adopted:
            parts.append(f'{adopted} local re-exports adopted')
        if renamed:
            parts.append(f'{renamed} reordered')
        parts.append(f'{len(plan) - copied - adopted - renamed} already up to date')
        print(f"   ✓ {', '.join(parts)}")

        # Account, caption and song noted on the site -> one caption.txt in
        # the post folder. Rewritten only when the content actually changed.
        if cap_what:
            lines = []
            if account:
                lines.append(f'Account: @{account}')
            if song:
                lines.append(f'Song: {song}')
            if caption:
                if lines:
                    lines.append('')
                lines.append(caption)
            txt = '\n'.join(lines) + '\n'
            cap_path = dest_dir / 'caption.txt'
            if not cap_path.exists() or cap_path.read_text() != txt:
                cap_path.write_text(txt)
                print(f"   ✓ caption.txt ({cap_what})")

        # Location cards for the photos marked with the map button -> beside them in the post folder
        marked = [ref for _, ref, _, _ in plan if ref.get('map')]
        try:
            size = post_card_size(post, plan)
            mrendered, mrenamed, mfailed = sync_map_dir(dest_dir, plan, size)
            if marked:
                mparts = [f'{mrendered} rendered']
                if mrenamed:
                    mparts.append(f'{mrenamed} reordered')
                if mfailed:
                    mparts.append(f'{mfailed} failed')
                mparts.append(f'{len(marked) - mrendered - mrenamed - mfailed} already up to date')
                print(f"   ✓ map cards ({size[0]}x{size[1]}): {', '.join(mparts)}")
                manifest['maps'] = [
                    {'order': i, 'style': ref['map'],
                     'file': str(dest_dir / f'{i:02d}_{src.stem}_map.jpg')}
                    for i, ref, src, _ in plan if ref.get('map')]
                (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        except Exception as e:
            if marked:
                print(f"   ⚠️  map cards not rendered: {e}")

        # Trip route cards: one per trip with a photo marked 🛣 -> NN_route_<trip>.jpg in the post folder
        try:
            size = post_card_size(post, plan)
            rrendered, rskipped = sync_route_cards(dest_dir, plan, size)
            if rrendered:
                print(f"   ✓ route cards ({size[0]}x{size[1]}): {rrendered} rendered")
        except Exception as e:
            if route_jobs(plan):
                print(f"   ⚠️  route cards not rendered: {e}")

        # Demoted photos -> <post>/Waitlist/. Still attached to the post but
        # out of the carousel, so they are pulled (blur marks honoured, same
        # re-edit/adoption rules) into their own subfolder rather than dropped.
        # Without wl, Waitlist/ stays exactly as the last pull left it, keeping
        # its manifest rows so sync_post_edits and the next pull still see it.
        wait_refs = post.get('waitlist') or []
        if args.skip_waitlist:
            if old_manifest.get('waitlist'):
                manifest['waitlist'] = old_manifest['waitlist']
                (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            wait_refs = []
        if wait_refs:
            wait_dir = dest_dir / 'Waitlist'
            wait_plan = []
            for i, ref in enumerate(wait_refs, 1):
                src, why = resolve_wait(ref, prev.get((ref['trip'], ref.get('id'))))
                if src is None:
                    print(f"   ⚠️  Waitlist {i:02d} {ref['trip']}/{ref['id']}: {why}")
                    unresolved += 1
                    continue
                wait_plan.append((i, ref, src, wait_dir / f'{i:02d}_{src.name}'))
            if wait_plan:
                wait_dir.mkdir(parents=True, exist_ok=True)
                wcopied, wrenamed, wadopted = sync_post_dir(
                    wait_dir, wait_plan, reblur=getattr(args, 'reblur', False),
                    found=found)
                manifest['waitlist'] = [
                    {'order': i, 'trip': ref['trip'], 'id': ref['id'],
                     'source': str(src), 'copied_to': str(on_disk(dst))}
                    for i, ref, src, dst in wait_plan]
                (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
                wparts = [f'{wcopied} copied']
                if wadopted:
                    wparts.append(f'{wadopted} local re-exports adopted')
                if wrenamed:
                    wparts.append(f'{wrenamed} reordered')
                wparts.append(f'{len(wait_plan) - wcopied - wadopted - wrenamed} already up to date')
                print(f"   ✓ Waitlist/ ({len(wait_plan)} photos): {', '.join(wparts)}")

        # Behind-the-scenes phone selections -> <post>/Phone/ (originals,
        # photos and videos alike, resolved via the phone_browse manifests).
        phone_refs = post.get('phone') or []
        if phone_refs:
            phone_plan = []
            for i, ref in enumerate(phone_refs, 1):
                src, why = resolve_phone(ref)
                label = ref.get('id') or ref.get('file')
                if src is None:
                    print(f"   ⚠️  Phone {i:02d} {ref['trip']}/{label}: {why}")
                    unresolved += 1
                    continue
                phone_plan.append((i, ref, src, phone_dest(dest_dir / 'Phone', i, ref, src)))
            pcopied, premoved = sync_phone_dir(dest_dir / 'Phone', phone_plan)
            manifest['phone'] = [
                {'order': i, 'trip': ref['trip'],
                 'id': ref.get('id'), 'file': ref.get('file'),
                 'source': str(src), 'copied_to': str(on_disk(dst))}
                for i, ref, src, dst in phone_plan]
            (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            pparts = [f'{pcopied} copied']
            if premoved:
                pparts.append(f'{premoved} removed')
            pparts.append(f'{len(phone_plan) - pcopied} already up to date')
            print(f"   ✓ Phone/ ({len(phone_plan)} items): {', '.join(pparts)}")

        # Lightroom smart collection matching these photos (filename + capture
        # date), so the post is one import away in the LR Collections panel.
        try:
            sys.path.insert(0, str(ROOT / 'tools'))
            from lr_smart_collection import write_lrsmcol
            lr_out = dest_dir / f'{name}.lrsmcol'
            notes = []
            n_rules, undated = write_lrsmcol([dst for _, _, _, dst in plan], name,
                                             lr_out, cached_capture_dates(plan), notes)
            print(f"   ✓ Lightroom smart collection → {lr_out}")
            for f, why in notes:
                print(f"     ⚠️  {f.name}: {why}")
            for f in undated:
                print(f"     ⚠️  {f.name}: no capture date anywhere, matched by filename "
                      "only (may pull in other photos with the same number)")
            print()
        except Exception as e:
            print(f"   ⚠️  Lightroom smart collection not written: {e}\n")

    if not (args.list or args.dry_run):
        confirm_reedits(found, Path(args.dest))
    if unresolved:
        sys.exit(f'⚠️  {unresolved} photo(s) could not be resolved (see above).')


def _sample_differs(a, b, chunk=1 << 18):
    """Cheap content check for two files on the network drive: size, then the
    first and last 256 KB. A re-export never matches the copy byte for byte."""
    if a.stat().st_size != b.stat().st_size:
        return True
    with _read(a) as fa, _read(b) as fb:
        if fa.read(chunk) != fb.read(chunk):
            return True
        fa.seek(-min(chunk, a.stat().st_size), 2)
        fb.seek(-min(chunk, b.stat().st_size), 2)
        return fa.read() != fb.read()


def _visual_diff(a, b):
    """Mean per-pixel difference at 1/8 scale (inf if the frames differ in
    shape). A face blur moves this well under 1; a re-grade or crop does not."""
    import cv2
    import numpy as np
    ia, ib = (cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_REDUCED_COLOR_8)
              for p in (a, b))
    if ia is None or ib is None or ia.shape != ib.shape:
        return float('inf')
    return float(np.mean(np.abs(ia.astype(np.int16) - ib.astype(np.int16))))


BLURRED_REEDIT_DIFF = 4.0


def cmd_backfill(args):
    """Catch-up for re-edits a pull did not write back: pulls made before
    write-back existed, or ones answered "not now".

    Every photo in every pulled post folder (its manifest.json says where it
    came from) is compared with its source in Edits. A post copy that differs
    and is newer than the source is a re-edit, offered for write-back with the
    same prompt as a pull. Blur-flagged copies have pixelated faces, so only
    a clean copy a pull set aside (.reedits_staged/) can go back; the others
    that look re-edited are listed for a clean export from Lightroom into the
    post folder, which the next pull adopts and offers unblurred."""
    root = Path(args.dest)
    if not root.exists():
        sys.exit(f'⚠️  {root} is not available. Is the RYAN drive mounted?')
    cands, blurred_reedits, stamped, found = {}, [], 0, []
    for man in sorted(root.glob('*/manifest.json')):
        folder = man.parent
        try:
            m = json.loads(man.read_text())
        except ValueError:
            continue
        print(f'── {folder.name}')
        for d, rows in ((folder, m.get('photos')), (folder / 'Waitlist', m.get('waitlist'))):
            state_path = d / '.pull_state.json'
            try:
                state = json.loads(state_path.read_text())
            except (OSError, ValueError):
                state = {}
            hashes = state.setdefault('hashes', {})
            dirty = False
            for r in rows or []:
                if r['trip'].startswith('phone-') or not r.get('source'):
                    continue
                src = Path(r['source'])
                # by name inside THIS folder: a copied post folder keeps the
                # original's absolute copied_to paths
                dst = d / Path(r['copied_to']).name
                if not (src.exists() and dst.exists()):
                    continue
                if state.get('blurred', {}).get(src.name):
                    if src.name not in hashes and not args.dry_run:
                        # pulled before fingerprints: without one, a re-export
                        # over this file would go unnoticed by the next pull
                        hashes[src.name] = _file_md5(dst)
                        dirty = stamped = stamped + 1
                    staged = d / STAGED / f'reedit_{src.name}'
                    if staged.exists():
                        if _reedit_ok(r, src, staged):
                            found.append({'ref': r, 'src': src, 'new': staged,
                                          'post_file': dst, 'dest_dir': d})
                        continue
                    diff = _visual_diff(src, dst)
                    if diff > BLURRED_REEDIT_DIFF:
                        blurred_reedits.append((dst, diff))
                    continue
                staged = d / STAGED / f'reedit_{src.name}'
                if staged.exists():
                    # a re-edit set aside clean before its post copy was shrunk
                    if _sample_differs(src, staged):
                        cands.setdefault(src, []).append((staged.stat().st_mtime, staged, r, d))
                    continue
                fit_md5 = state.get('compressed', {}).get(src.name)
                if fit_md5 and fit_md5 == _file_md5(dst):
                    continue   # shrunk to UPLOAD_CAP by a pull: not a re-edit
                if _sample_differs(src, dst):
                    cands.setdefault(src, []).append((dst.stat().st_mtime, dst, r, d))
            if dirty:
                state_path.write_text(json.dumps(state, indent=1))

    print()
    for src, versions in sorted(cands.items()):
        versions.sort(key=lambda c: c[0])
        mtime, dst, ref, d = versions[-1]
        if mtime <= src.stat().st_mtime:
            print(f'   · {src.name}: the Edits copy is newer than {dst.parent.name}/{dst.name}, '
                  'left alone')
            continue
        if len(versions) > 1 and len({_file_md5(c[1]) for c in versions}) > 1:
            print(f'   {src.name}: {len(versions)} different versions in post folders, '
                  f'offering the newest ({dst.parent.name}/{dst.name})')
        if _reedit_ok(ref, src, dst):
            found.append({'ref': ref, 'src': src, 'new': dst, 'post_file': dst, 'dest_dir': d})

    found.sort(key=lambda r: str(r['post_file']))
    if blurred_reedits:
        print(f'\n🙂🚫 {len(blurred_reedits)} blur-flagged photo(s) look re-edited, but the '
              'post copy has pixelated faces, so it cannot go back to Edits.\n'
              '   Export each from Lightroom into its post folder (any name Lightroom gives\n'
              '   it), then run ./post.py pull: it adopts the export and offers the clean\n'
              '   version for Edits while re-blurring the post copy.')
        for dst, diff in blurred_reedits:
            print(f'     {dst}  (diff {"crop" if diff == float("inf") else f"{diff:.1f}"})')
    if stamped:
        print(f'\n   (fingerprinted {stamped} older blurred copies so pulls notice re-exports)')
    if args.dry_run:
        if found:
            print_reedits(found, root, 'would be offered for write-back')
        else:
            print('\nNo re-edits to write back.')
        return
    if not found:
        print('\nNo re-edits to write back.')
    confirm_reedits(found, root)


def _rel(path, root, label=''):
    """path shown under root's name when it lives there, else in full."""
    try:
        return label + str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def print_reedits(found, posts_root, what):
    print(f'\n🔁 {len(found)} re-edit(s) {what}:')
    for k, r in enumerate(found, 1):
        print(f"  {k:>2}  {_rel(r['post_file'], posts_root)}  →  "
              f"{_rel(r['src'].parent, EDITS_ROOT, 'Edits/')}/")


def preview_pairs(found):
    """Open re-edits in Preview, each followed by the Edits version it would
    replace. A blur-flagged one shows as its clean reedit_ copy, which is
    what Edits gets. Opens the existing files only, nothing is copied."""
    files = [str(f) for r in found for f in (r['new'], r['src'])]
    subprocess.run(['open', '-a', 'Preview', *files])


def confirm_reedits(found, posts_root):
    """List the re-edits found, then ask before anything goes into Edits.
    y = write back and update the live site, e = write back only (the site
    follows with the next full deploy), n = leave everything as it is
    (./post.py backfill offers them again). All of them open in Preview
    first, each next to the Edits version it replaces; a number reopens one."""
    if not found:
        offer_site_update()
        return
    print_reedits(found, posts_root, 'found, nothing written to Edits yet')
    if not sys.stdin.isatty():
        print('   No terminal to confirm in, so nothing was written. '
              'Run ./post.py backfill to review them.')
        return
    preview_pairs(found)
    print('   Preview has each re-edit followed by the Edits version it replaces '
          '(type a number to reopen one pair).')
    while True:
        try:
            ans = input('   Write them back to Edits?  y = and update the live site now, '
                        'e = Edits only, n = not now [y/e/N] ').strip().lower()
        except EOFError:
            ans = 'n'
        if ans.isdigit() and 1 <= int(ans) <= len(found):
            preview_pairs([found[int(ans) - 1]])
            continue
        if ans in ('y', 'yes', 'e'):
            break
        if ans in ('', 'n', 'no'):
            print('   Nothing written. ./post.py backfill offers them again when you are ready.')
            return
        print(f'   Type y, e, n or a number from 1 to {len(found)}.')
    failed = 0
    for r in found:
        try:
            write_back(r)
        except OSError as e:
            failed += 1
            print(f"   ⚠️  {r['src'].name}: not written ({e}); "
                  './post.py backfill offers it again')
    if failed == len(found):
        return
    if ans == 'e':
        print('   The live site picks them up with the next full deploy (python3 deploy.py).')
        return
    run_deploy()


def run_deploy():
    """deploy.py reprocesses the trips with pending re-edits first (reedits.py).
    Only the trips with pending re-edits get their images synced to R2, and
    the config backup and Expedition Tours rebuild are skipped: a re-edit
    changes neither. The privacy, security and manifest steps always run."""
    py = ROOT / 'venv' / 'bin' / 'python'
    cmd = [str(py if py.exists() else sys.executable), 'deploy.py']
    slugs = list(reedits.load_pending())
    if slugs:
        cmd += [a for slug in slugs for a in ('--trip', slug)]
        cmd += ['--skip-config-backup', '--skip-expeditions']
    rc = subprocess.run(cmd, cwd=str(ROOT), env=load_env()).returncode
    if rc:
        print(f'⚠️  deploy.py exited with {rc}; the re-edits stay pending.')


def offer_site_update():
    """Re-edits written to Edits earlier ("Edits only") but not live yet."""
    pending = reedits.load_pending()
    if not pending:
        return
    n = sum(len(e.get('sources', [])) for e in pending.values())
    names = ', '.join(e.get('name') or slug for slug, e in pending.items())
    print(f'🔁 {n} re-edit(s) already in Edits are not live yet ({names}).')
    try:
        answer = input('   Update the live site now? [y/N] ') if sys.stdin.isatty() else ''
    except EOFError:
        answer = ''
    if answer.strip().lower() in ('y', 'yes'):
        run_deploy()
    else:
        print('   Left for later: the next full deploy (python3 deploy.py) reprocesses '
              'and uploads them.')


# ---------------------------------------------------------------- serve

def server_up(base, env):
    try:
        api_call(posts_url(base), env, timeout=3)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        # ValueError: an answer that isn't JSON (a login or error page). The mirror
        # used to crash on that at startup and the whole session ran unsynced.
        return False


def wait_for_server(base, env, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if server_up(base, env):
            return True
        time.sleep(1)
    return False


def local_ip():
    for iface in ('en0', 'en1'):
        try:
            out = subprocess.run(['ipconfig', 'getifaddr', iface],
                                 capture_output=True, text=True).stdout.strip()
            if out:
                return out
        except OSError:
            pass
    return None


def start_dev_server(env, port):
    """Start the dev server through ./serve.sh — the one place that knows how.

    It also seeds the local R2 with the People document, which a bare `wrangler
    pages dev` does not, so starting the server two different ways no longer
    gives you two different sites. POSTS_MIRROR=0 because `serve` runs its own
    mirror around this call.
    """
    child = dict(os.environ, POSTS_MIRROR='0', PORT=str(port))
    # Own process group: Ctrl-C reaches this script first, so the final sync
    # runs while the dev server (and its API) is still alive.
    return subprocess.Popen([str(ROOT / 'serve.sh')], cwd=str(ROOT), env=child,
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_curate_server(port):
    py = ROOT / 'venv' / 'bin' / 'python'
    script = ROOT / 'tools' / 'curate_server.py'
    if not py.exists() or not script.exists():
        return None
    return subprocess.Popen([str(py), str(script), str(port)], cwd=str(ROOT),
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RemoteMirror:
    """Keeps the local dev server and the live site in step, both ways.

    Every few seconds it compares the two sides with the last state they
    agreed on (SYNC_BASE, kept on disk) and merges (merge3): an edit made on
    the phone comes down, an edit made here goes up, edits to different posts
    on both sides combine. Only the same post changed on both sides is a
    conflict: the live version is kept and the laptop's is saved to
    SYNC_BACKUPS, so nothing is lost. The live site is read at most every
    REMOTE_EVERY seconds unless something changed here (each read is a
    Functions request on the free plan).

    Before the first sync on this laptop there is no agreed state: the local
    document is backed up and merge_down applies (live wins, laptop-only
    drafts kept).
    """

    REMOTE_EVERY = 30

    def __init__(self, env, remote, local=None):
        self.env = env
        self.remote = remote
        self.local = local or LOCAL_BASE
        self.lock = threading.Lock()
        try:
            self.base = json.loads(SYNC_BASE.read_text())
        except (OSError, ValueError):
            self.base = {}
        self.last_remote = 0.0
        self.status = {'state': 'starting', 'message': 'Starting', 'last_sync': None,
                       'conflicts': 0, 'versions': {}}

    def _save_base(self):
        SYNC_BASE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SYNC_BASE.with_name(SYNC_BASE.name + f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(self.base, ensure_ascii=False))
        os.replace(tmp, SYNC_BASE)

    def _backup(self, label, data):
        SYNC_BACKUPS.mkdir(parents=True, exist_ok=True)
        f = SYNC_BACKUPS / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}.json"
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        return f

    def _sync_set(self, which):
        """One set, both ways. Returns 'same', 'down', 'up', 'both' or 'merged'."""
        L = read_doc(self.local, self.env, which)
        R = read_doc(self.remote, self.env, which)
        lp, rp = L.get('posts', []), R.get('posts', [])
        ls, rs = L.get('settings') or {}, R.get('settings') or {}
        lr = {f: L.get(f) for f in REEL_FIELDS}       # the Reels tab's queue and reject list
        rr = {f: R.get(f) for f in REEL_FIELDS}
        self.status['versions'][which] = {'local': L.get('version', 0), 'live': R.get('version', 0)}

        def agreed_doc(posts, settings, reel):
            return {'posts': posts, 'settings': settings, **{f: v for f, v in reel.items() if v is not None}}
        if same_posts(lp, rp) and same_posts(ls, rs) and same_posts(lr, rr):
            agreed = agreed_doc(rp, rs, rr)
            if self.base.get(which) != agreed:
                self.base[which] = agreed
                self._save_base()
            return 'same'
        base = self.base.get(which)
        conflicts = []
        if base is None:
            self._backup(f'{which}_local_before_first_sync', L)
            posts, _kept = merge_down(rp, lp)
            settings = rs or ls
        else:
            posts, conflicts = merge3(base.get('posts', []), lp, rp)
            bs = base.get('settings') or {}
            settings = rs if (same_posts(ls, rs) or same_posts(ls, bs)) else ls if same_posts(rs, bs) else rs
        reel = {f: MERGE_FIELD[f]((base or {}).get(f), lr[f], rr[f]) if base is not None else (rr[f] or lr[f])
                for f in REEL_FIELDS}
        if conflicts:
            f = self._backup(f'{which}_conflicts', [{'laptop': l, 'live': r} for l, r in conflicts])
            self.status['conflicts'] += len(conflicts)
            names = ', '.join(r.get('name', '?') for _l, r in conflicts[:3])
            print(f"\n⚠️  {which}: {len(conflicts)} post(s) changed on both sides ({names}). "
                  f"Kept the live site's; this laptop's copy is in {f}")
        down = not (same_posts(posts, lp) and same_posts(settings, ls) and same_posts(reel, lr))
        # A live site still on an older functions/api/posts.ts drops reel_queue / reel_rejects: once one has been
        # sent, don't send it again every tick for that alone.
        reel_up = any(not same_posts(reel[f], rr[f]) and not (
            rr[f] is None and base is not None and same_posts(reel[f], base.get(f))) for f in REEL_FIELDS)
        up = not (same_posts(posts, rp) and same_posts(settings, rs)) or reel_up
        if not down and not up:
            self.base[which] = agreed_doc(posts, settings, reel)
            self._save_base()
            return 'same'
        # Local first: if the page saved meanwhile (409) nothing has been written
        # anywhere, and the next tick merges again from fresh copies. Only when
        # both writes landed does the agreed state move on.
        if down:
            write_doc(self.local, self.env, which, posts, L.get('version', 0), settings, **reel)
        if up:
            write_doc(self.remote, self.env, which, posts, R.get('version', 0), settings, **reel)
        self.base[which] = agreed_doc(posts, settings, reel)
        self._save_base()
        what = 'both' if down and up else 'down' if down else 'up'
        arrow = {'down': '⇣ from', 'up': '⇡ to', 'both': '⇅ with'}[what]
        print(f"   {arrow} the live site: {which} ({len(posts)} posts)")
        return what

    def sync(self, force_remote=False):
        """Sync both sets now (thread-safe). Returns the status dict."""
        with self.lock:
            def changed_here(w):
                d = read_doc(self.local, self.env, w)
                b = self.base.get(w) or {}
                return not (same_posts(d.get('posts', []), b.get('posts')) and
                            all(same_posts(d.get(f), b.get(f)) for f in REEL_FIELDS))
            local_changed = any(changed_here(w) for w in SETS) if not force_remote else True
            if not (force_remote or local_changed or
                    time.time() - self.last_remote >= self.REMOTE_EVERY):
                return self.status
            try:
                results = {w: self._sync_set(w) for w in SETS}
                self.last_remote = time.time()
                self.status.update(state='in_sync', last_sync=time.strftime('%Y-%m-%dT%H:%M:%S'),
                                   message='In sync with the live site', results=results)
            except urllib.error.HTTPError as e:
                self.status.update(state='retry', message=f'Live site busy (HTTP {e.code}); retrying')
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.status.update(state='offline', message=f'Live site not reachable ({e}); retrying')
            return self.status

    def seed_local(self):
        """Startup: one full sync (kept for the callers that seed first)."""
        print('⇅ Syncing posts with the live site...')
        st = self.sync(force_remote=True)
        print(f"   ↳ {st['message']}")

    def serve_control(self, port=SYNC_PORT):
        """127.0.0.1:<port>: GET /status, POST /sync, for the localhost Posts page
        and the launcher. Returns the server, or None if the port is taken (another
        mirror already runs: the caller should not start a second one)."""
        mirror = self
        allowed = {f'http://localhost:{DEV_PORT}', f'http://127.0.0.1:{DEV_PORT}'}

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                origin = self.headers.get('Origin', '')
                if origin in allowed:
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Vary', 'Origin')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                self.send_response(204)
                origin = self.headers.get('Origin', '')
                if origin in allowed:
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Access-Control-Allow-Methods', 'GET, POST')
                self.end_headers()

            def do_GET(self):
                if self.path.startswith('/status'):
                    self._send(200, mirror.status)
                else:
                    self._send(404, {'error': 'not found'})

            def do_POST(self):
                if self.path.startswith('/sync'):
                    self._send(200, mirror.sync(force_remote=True))
                else:
                    self._send(404, {'error': 'not found'})

            def log_message(self, *a):      # quiet: the dev server log is busy enough
                pass

        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        except OSError:
            return None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


def cmd_serve(args):
    env = require_env()
    remote = remote_base(env)
    if not server_up(remote, env):
        print('⚠️  The live site did not answer; serving without the initial sync.')
        remote = None

    already = server_up(LOCAL_BASE, env)
    dev = curate = None
    if already:
        print(f'▶ Reusing the dev server already on {LOCAL_BASE}')
    else:
        print(f'▶ Starting the dev server on port {args.port}...')
        dev = start_dev_server(env, args.port)
        if not wait_for_server(LOCAL_BASE, env):
            dev.terminate()
            sys.exit('❌ The dev server did not come up. Run ./serve.sh to see why.')

    mirror = RemoteMirror(env, remote, f'http://localhost:{args.port}') if remote else None
    if mirror:
        mirror.seed_local()
        if not mirror.serve_control():
            print(f'⚠️  Another sync is already running (port {SYNC_PORT}); not syncing from here.')
            mirror = None

    if not args.no_curate:
        curate = start_curate_server(args.curate_port)
        if curate:
            print(f'▶ Curation server on port {args.curate_port} '
                  '(the prompt box appears once it finishes loading)')

    # Ctrl-C is not the only way this ends: closing the terminal (SIGHUP) or a
    # plain `kill` (SIGTERM) would otherwise skip the final sync AND leave the
    # child servers running, since they sit in their own sessions. Route both
    # through the same shutdown path.
    def _stop(signum, _frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, AttributeError):
            pass

    ip = local_ip()
    print(f'\n🌐 {LOCAL_BASE}/posts' + (f'   📱 http://{ip}:{args.port}/posts' if ip else ''))
    print('   Phone photos work here (web/phone/ is local-only and never deployed).')
    print('   Edits sync to the live site automatically; Ctrl-C to stop.\n')

    try:
        while True:
            time.sleep(args.interval)
            if mirror:
                mirror.sync()
            if dev and dev.poll() is not None:
                print('⚠️  The dev server exited.')
                break
    except KeyboardInterrupt:
        print('\n⇡ Final sync to the live site...')
    except Exception as e:                              # noqa: BLE001
        print(f'\n⚠️  Stopping after an unexpected error: {e}')
    finally:
        if mirror:
            try:
                print(f"   ↳ {mirror.sync(force_remote=True)['message']}")
            except Exception as e:                      # noqa: BLE001
                print(f'   ⚠️  Final sync failed: {e}\n'
                      '      Your edits are safe locally; run ./post.py push to retry.')
        for proc, name in ((curate, 'curation server'), (dev, 'dev server')):
            if proc and proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                print(f'   ↳ stopped the {name}')
        print('Done.')


def cmd_mirror(args):
    """Keep a dev server this script did not start in sync with the live site.

    ./serve.sh and the desktop app run this in the background. It syncs both
    ways (RemoteMirror), answers the Posts page's Sync button on 127.0.0.1:
    SYNC_PORT, keeps retrying while the live site is unreachable (it used to
    crash on a bad answer at startup and leave the session unsynced), and stops
    when the dev server goes away. Only one runs: a second exits at once.
    """
    env = require_env()
    remote = remote_base(env)
    local = f'http://localhost:{args.port}'
    if not wait_for_server(local, env, args.wait):
        print('⚠️  posts: no local dev server answered; not syncing drafts.')
        return
    mirror = RemoteMirror(env, remote, local)
    if not mirror.serve_control():
        print(f'posts: a sync is already running (port {SYNC_PORT}); leaving it to that one.')
        return

    def _stop(signum, _frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, AttributeError):
            pass
    print(f"⇅ posts: syncing with {remote} (Sync button / status on 127.0.0.1:{SYNC_PORT})")
    st = mirror.sync(force_remote=True)
    print(f"   ↳ {st['message']}")
    # Stop when the dev server goes away: a mirror left running against a dead
    # server would read an empty document.
    gone = 0
    try:
        while True:
            time.sleep(args.interval)
            if not server_up(local, env):
                gone += 1
                if gone >= 3:
                    break
                continue
            gone = 0
            before = mirror.status.get('state')
            st = mirror.sync()
            if st.get('state') != before and st.get('state') != 'in_sync':
                print(f"   ⚠️  {st['message']}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if server_up(local, env):
                mirror.sync(force_remote=True)
        except Exception:                               # noqa: BLE001
            pass

def cmd_sync(args):
    env = require_env()
    remote = remote_base(env)
    if not server_up(LOCAL_BASE, env):
        sys.exit(f'❌ No dev server on {LOCAL_BASE}. Start one with ./post.py serve.')
    print('⇣ Live site → local dev server')
    if copy_state(remote, LOCAL_BASE, env, 'copied down', keep_extra=True) == 0:
        print('   ↳ already identical')


def cmd_push(args):
    env = require_env()
    remote = remote_base(env)
    if not server_up(LOCAL_BASE, env):
        sys.exit(f'❌ No dev server on {LOCAL_BASE}. Start one with ./post.py serve.')
    for which in SETS:
        local = read_doc(LOCAL_BASE, env, which)
        rem = read_doc(remote, env, which)
        if same_posts(local.get('posts', []), rem.get('posts', [])):
            continue
        if not args.force:
            print(f"   {which}: local {len(local.get('posts', []))} posts → "
                  f"live {len(rem.get('posts', []))} posts")
        write_doc(remote, env, which, local.get('posts', []), rem.get('version', 0))
        print(f"   ⇡ pushed {which} to the live site")
    print('Done.')


def cmd_status(args):
    env = require_env()
    remote = remote_base(env)
    rows = []
    for label, base in (('live site', remote), ('local dev', LOCAL_BASE)):
        if not server_up(base, env):
            rows.append((label, base, None, None))
            continue
        for which in SETS:
            d = read_doc(base, env, which)
            rows.append((label, which, d.get('version'), len(d.get('posts', []))))
    for label, which, ver, n in rows:
        if ver is None:
            print(f'{label:<10} {which}  not reachable')
        else:
            print(f'{label:<10} {which:<5} v{ver:<4} {n} posts')
    if server_up(LOCAL_BASE, env) and server_up(remote, env):
        diff = [w for w in SETS
                if not same_posts(read_doc(LOCAL_BASE, env, w).get('posts', []),
                                  read_doc(remote, env, w).get('posts', []))]
        print('\n' + ('in sync' if not diff else
                      f"out of sync: {', '.join(diff)} — ./post.py push or ./post.py sync"))


# ---------------------------------------------------------------- ig

# One line per Instagram post made by `ig` (ids, name, permalink; no secrets).
IG_LOG = ROOT / 'local_browse' / 'instagram_posts.jsonl'
POSTS_DEST = '/Volumes/RYAN/Edits/Posts'


def _ask_mirror():
    """POST /sync to the running mirror (./serve.sh or ./post.py serve started it) → its status, or None
    when none runs."""
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{SYNC_PORT}/sync', data=b'', method='POST')
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def sync_with_live(env):
    """One two-way sync of the local dev server with the live site, the way an edit made on the localhost
    Posts page travels: the running mirror does it when there is one, else one RemoteMirror pass from here
    (merge3 against SYNC_BASE, conflicts kept in SYNC_BACKUPS). Returns the status dict."""
    st = _ask_mirror()
    if st is not None:
        return st
    return RemoteMirror(env, remote_base(env), LOCAL_BASE).sync(force_remote=True)


def pick_ig_post(posts, which):
    """The Xiaohongshu draft `which` names (its #number, its id, or its name, any case): the Xiaohongshu tab's
    drafts go to @ryanmcl0 as well as Xiaohongshu; the Instagram tab is the urbex account, posted by hand."""
    w = str(which).strip().lstrip('#')
    xhs = [p for p in posts if p.get('platform') == 'xhs']
    urbex = [p for p in posts if p.get('platform') != 'xhs']

    def named(group):
        if w.isdigit():
            return [p for p in group if p.get('num') == int(w)]
        return ([p for p in group if p.get('id') == w]
                or [p for p in group if p.get('name', '').strip().lower() == w.lower()])
    hits = named(xhs)
    if len(hits) == 1:
        return hits[0]
    if hits:
        sys.exit(f'❌ {len(hits)} Xiaohongshu drafts are called "{which}"; use the number: '
                 + ', '.join(f"#{p.get('num')}" for p in hits))
    if named(urbex):
        sys.exit(f'❌ "{which}" is on the Instagram tab: that is the urbex account, posted by hand. '
                 'Only Xiaohongshu-tab drafts go to @ryanmcl0.')
    open_ = [f"#{p.get('num')} {p['name']}" for p in xhs if not p.get('posted')]
    sys.exit(f'❌ No Xiaohongshu draft "{which}". Not yet posted: {", ".join(open_) or "none"}')


def mark_posted(base, env, post_id, permalink):
    """The Posts page's own Posted tick, plus the permalink, on one draft at `base`. Written the way the page
    writes: against the version just read, re-applied to a fresh copy if another edit landed first (409).
    True once saved, False if the draft is gone."""
    for _ in range(5):
        d = read_doc(base, env)
        posts = d.get('posts', [])
        p = next((x for x in posts if x.get('id') == post_id), None)
        if p is None:
            return False
        p['posted'] = True
        p['permalink'] = permalink
        try:
            write_doc(base, env, 'main', posts, d.get('version', 0))
            return True
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
    raise OSError('the draft kept changing while saving the Posted mark')


def _find(doc, post_id):
    return next((p for p in doc.get('posts', []) if p.get('id') == post_id), None)


def print_ig_plan(IG, post, folder, plan, setup):
    shape = {'IMAGE': 'one photo', 'REELS': 'a Reel',
             'CAROUSEL': f"a carousel of {len(plan['items'])}"}[plan['type']]
    tag = f"#{post['num']} " if post.get('num') else ''
    print(f"\n📸 {tag}{post['name']} → @{plan['account']} ({shape})")
    print(f'   {folder}')
    trial = {'auto': 'shared to followers automatically if it performs well',
             'manual': 'you share it to followers yourself in the app'}.get(plan.get('trial'))
    if trial:
        print(f'   Trial reel: shown to non-followers first; {trial}')
    for it in plan['items']:
        size = f"{it['bytes'] / 1e6:.1f} MB" if it.get('bytes') else '-'
        if it['kind'] == 'image' and it.get('staged_bytes'):
            size += f" → {it['staged_bytes'] / 1e6:.1f} MB JPEG"
        dims = (f"  {it['width']}×{it['height']} ({IG.ratio_name(it['width'], it['height'])})"
                if it.get('width') and it.get('height') else '')
        dur = f"  {it['duration']:.1f} s" if it.get('duration') else ''
        what = '' if it['what'] == 'photo' else f"  [{it['what']}]"
        print(f"   {it['n']:>2}  {it['label']}  {size}{dims}{dur}{what}")
        for p in it.get('problems', []):
            print(f'        ⛔ {p}')
    print('\n   Caption, exactly as it goes out:')
    print('   ' + '─' * 50)
    print(plan['caption'] or '(no caption)')
    print('   ' + '─' * 50)
    for n in plan['notes'] + ['no location tag (the API needs a Facebook Page permission for that)']:
        print(f'   ℹ️  {n}')
    if setup.missing:
        print(f"   {'⛔' if setup.enabled else 'ℹ️ '} not set up: {', '.join(setup.missing)}")
    for p in plan['problems']:
        print(f'   ⛔ {p}')


def cmd_ig(args):
    """Post one Xiaohongshu-tab draft to Instagram (@ryanmcl0). See the module docstring and RUNBOOK §9.

    Where the draft is read from: with the local dev server up, the localhost Posts page's copy, after one sync
    with the live site (so the page's latest edits and the phone's are both in); without it, the live site.
    The Posted mark goes back the same way: onto the local copy and up through the sync, or straight onto the
    live copy when no dev server runs."""
    sys.path.insert(0, str(ROOT / 'tools'))
    import instagram_post as IG
    env = require_env()
    remote = remote_base(env)
    local_up = server_up(LOCAL_BASE, env)
    if local_up:
        print('⇅ Syncing the localhost Posts page with the live site first...')
        print(f"   ↳ {sync_with_live(env).get('message', '')}")
    base = LOCAL_BASE if local_up else remote
    url = posts_url(base)
    post = pick_ig_post(fetch_posts(url, env).get('posts', []), args.post)
    if post.get('posted'):
        sys.exit(f"❌ \"{post['name']}\" is already marked posted"
                 + (f" ({post['permalink']})" if post.get('permalink') else '') + '.')

    # 1. A fresh pull of this one post: his latest order and re-edits.
    try:
        cmd_pull(argparse.Namespace(
            dest=args.dest, url=url, local=True, num=None, post=None, id=post['id'], list=False,
            dry_run=False, reblur=False, waitlist=False, skip_waitlist=True))
    except SystemExit as e:
        if 'could not be resolved' not in str(e.code):
            raise
        print(e.code)                       # the folder check below names what is missing
    doc = fetch_posts(url, env)
    post = _find(doc, post['id'])
    if post is None:
        sys.exit('❌ The draft was deleted during the pull.')
    folder = post_dir(Path(args.dest), post, doc.get('posts', []), move=False, quiet=True)
    try:
        manifest = json.loads((folder / 'manifest.json').read_text())
    except (OSError, ValueError):
        sys.exit(f'❌ No pulled folder at {folder}.')
    seen = json.dumps(post, sort_keys=True)

    # 2. What goes out, checked before anything is uploaded.
    staging = Path(tempfile.mkdtemp(prefix='ig-post-'))
    try:
        plan = IG.prepare(post, manifest, folder, staging,
                          progress=lambda n, total: print(f'\r⚙️  Preparing {n}/{total} for Instagram...',
                                                          end='', flush=True))
        print('\r\033[K', end='')                  # the plan below replaces the progress line
        setup = IG.Setup()
        problems = IG.blocking(plan) + ([f"not set up: {', '.join(setup.missing)}"]
                                        if setup.enabled and setup.missing else [])
        print_ig_plan(IG, post, folder, plan, setup)
        if args.dry or not args.real:
            print(f"\n{'⛔ ' + str(len(problems)) + ' problem(s). ' if problems else ''}Dry run: nothing posted.")
            ref = post.get('num') or args.post
            ref = ref if isinstance(ref, int) or ' ' not in str(ref) else f'"{ref}"'
            print(('Once they are fixed, post' if problems else 'To post it for real') +
                  f': ./post.py ig {ref} --post')
            return
        if problems:
            sys.exit(f'\n⛔ Not posted: {len(problems)} problem(s) above. Fix them, then run ./post.py ig {post.get("num") or args.post} --post again.')

        # 3. The question.
        if not args.yes:
            try:
                ans = input(f"\nPost this to @{plan['account']}? [y/N] ") if sys.stdin.isatty() else ''
            except EOFError:
                ans = ''
            if ans.strip().lower() not in ('y', 'yes'):
                print('Not posted.')
                return
        if not setup.enabled:
            print('Posting is off (publish_enabled): nothing was posted. '
                  f'Turn it on in {setup.folder / "config.json"}.')
            return
        fresh = _find(fetch_posts(url, env), post['id'])
        if fresh is None or json.dumps(fresh, sort_keys=True) != seen:
            sys.exit(f'⛔ Not posted: the draft changed since the pull. Run ./post.py ig {post.get("num") or args.post} --post again.')

        # 4. Publish.
        print('⇡ Posting to Instagram...')
        try:
            res = IG.publish_items(plan['items'], plan['caption'], setup.publisher(), setup.r2(), setup.prefix,
                                   trial=plan['trial'],
                                   progress=lambda text: print(f'\r\033[K   {text}...', end='', flush=True))
        except IG.LimitReached as e:
            sys.exit(f'⛔ Not posted: {e}.')
        except Exception as e:                                  # noqa: BLE001 — nothing went out
            sys.exit(f'⛔ Not posted: {IG.clean(e)}\n   Nothing was marked posted.')
        finally:
            print('\r\033[K', end='', flush=True)      # the outcome below replaces the progress line
        for w in res['warnings']:
            print(f'   ⚠️  {w}')
        IG.log_post({'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'post_id': post['id'], 'num': post.get('num'),
                     'name': post['name'], 'account': plan['account'], 'type': plan['type'], 'trial': plan['trial'],
                     'items': [it['label'] for it in plan['items']], 'media_id': res['media_id'],
                     'permalink': res['permalink']}, IG_LOG)
        if not res['permalink']:
            sys.exit(f"⚠️  Posted (Instagram media id {res['media_id']}), but Instagram did not return its link, "
                     'so the draft is NOT marked posted: tick Posted on the Posts page by hand.')
        print(f"✅ Posted: {res['permalink']}  ({res['left']} posts left in Instagram's 24 h limit)")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 5. The Posted mark (+ permalink), where the Posts pages read it.
    try:
        if not mark_posted(base, env, post['id'], res['permalink']):
            print('⚠️  The draft is gone from the Posts page, so nothing was marked.')
            return
        if base == LOCAL_BASE:
            st = sync_with_live(env)
            live = _find(read_doc(remote, env), post['id']) or {}
            print('✓ Marked posted on the localhost Posts page'
                  + (' and the live site.' if live.get('posted') else
                     f"; the live site gets it with the next sync ({st.get('message', '')})."))
        else:
            print('✓ Marked posted on the live site; the localhost Posts page picks it up when it next syncs.')
        print('   The next ./post.py pull renames the folder "Posted …".')
    except Exception as e:                                      # noqa: BLE001 — the post itself is out
        print(f'⚠️  Posted, but the Posted mark was not saved ({e}): tick Posted on the Posts page.')


def require_env():
    env = load_env()
    if not env.get('CF_POSTS_PASSWORD'):
        sys.exit('❌ CF_POSTS_PASSWORD not set (environment or .env.deploy).')
    return env


def main():
    # `serve` is long-running: keep progress visible instead of buffering it
    # until exit (matters when the output is piped or run under a wrapper).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(
        description='Post drafts: serve locally, pull to the drive, keep both sides in sync.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('serve', help='local site + phone photos, synced with the live site')
    s.add_argument('--port', type=int, default=DEV_PORT)
    s.add_argument('--curate-port', type=int, default=CURATE_PORT)
    s.add_argument('--no-curate', action='store_true', help='skip the curation server')
    s.add_argument('--interval', type=int, default=10,
                   help='seconds between sync checks (default 10)')
    s.set_defaults(func=cmd_serve)

    p = sub.add_parser('pull', help='pull drafts from the live site and copy their files')
    p.add_argument('num', nargs='?', type=int,
                   help='Only the post with this number (the #N on its card)')
    p.add_argument('--dest', default='/Volumes/RYAN/Edits/Posts',
                   help='Destination root (default: /Volumes/RYAN/Edits/Posts)')
    p.add_argument('--post', help='Only this post name')
    p.add_argument('--url', help='Posts API URL')
    p.add_argument('--local', action='store_true',
                   help='Pull from the local dev server instead of the live site')
    p.add_argument('--dry-run', action='store_true', help='Print the copy plan only')
    p.add_argument('--reblur', action='store_true',
                   help='Regenerate every blur-flagged copy from its source (after blur changes)')
    p.add_argument('--list', action='store_true', help='List drafts and resolved paths only')
    p.add_argument('photo', nargs='?', type=int,
                   help='A photo of that post (its NN_ number), for --location')
    p.add_argument('--location', nargs='+', action='append', metavar='[PHOTO] NAME',
                   help='Correct the place name baked onto a photo: pull 32 08 --location "Name" '
                        '(or --location 08 "Name"). "Name / Region" sets both lines, "auto" goes '
                        'back to the worked-out name, "off" drops the label. Remembered; repeat '
                        'for more photos.')
    p.add_argument('--waitlist', action='store_true',
                   help='Also pull the Waitlist photos (also: a bare "wl" word); '
                        'skipped by default')
    p.set_defaults(func=cmd_pull)

    f = sub.add_parser('backfill', help='write re-edits from earlier pulls back to Edits')
    f.add_argument('--dest', default='/Volumes/RYAN/Edits/Posts',
                   help='Folder the posts were pulled into')
    f.add_argument('--dry-run', action='store_true', help='Report only, change nothing')
    f.set_defaults(func=cmd_backfill)

    m = sub.add_parser('mirror', help='sync an already-running dev server with the live site')
    m.add_argument('--interval', type=int, default=5,
                   help='seconds between push checks (default 5)')
    m.add_argument('--port', type=int, default=DEV_PORT,
                   help=f'port the dev server is on (default {DEV_PORT})')
    m.add_argument('--wait', type=int, default=120,
                   help='seconds to wait for the dev server to come up (default 120)')
    m.set_defaults(func=cmd_mirror)

    y = sub.add_parser('sync', help='copy the live state down to the local dev server')
    y.set_defaults(func=cmd_sync)

    u = sub.add_parser('push', help='copy the local dev state up to the live site')
    u.add_argument('--force', action='store_true', help='overwrite the live state')
    u.set_defaults(func=cmd_push)

    t = sub.add_parser('status', help='show both sides')
    t.set_defaults(func=cmd_status)

    g = sub.add_parser('ig', help='post one Xiaohongshu draft to Instagram (@ryanmcl0)')
    g.add_argument('post', help='the draft: its number (the #N on its card), name or id')
    g.add_argument('--post', dest='real', action='store_true',
                   help='post for real (without it: a dry run that pulls, shows what would go out and stops)')
    g.add_argument('--yes', action='store_true', help='with --post: post without asking')
    g.add_argument('--dry', action='store_true', help=argparse.SUPPRESS)   # the default now; kept for old habits
    g.add_argument('--dest', default=POSTS_DEST, help=f'Post folders root (default: {POSTS_DEST})')
    g.set_defaults(func=cmd_ig)

    argv = sys.argv[1:]
    # `post.py pull 24 wl`: a bare word is quicker to type than the flag.
    # `skipwl` is the old opt-out, now the default, so it is just dropped.
    if argv[:1] == ['pull']:
        argv = ['--waitlist' if a == 'wl' else a for a in argv if a != 'skipwl']
    args = ap.parse_args(argv)
    if argv[:1] == ['pull']:
        args.skip_waitlist = not args.waitlist
        # --location NAME (the photo given after the post number) or --location PHOTO NAME
        pairs = []
        for vals in args.location or []:
            if len(vals) == 2 and vals[0].isdigit():
                pairs.append((int(vals[0]), vals[1]))
            elif len(vals) == 1 and args.photo is not None:
                pairs.append((args.photo, vals[0]))
            else:
                ap.error('--location: say which photo: pull 32 08 --location "Name" '
                         '(or --location 08 "Name"); quote a name with spaces')
        args.location = pairs
        if args.photo is not None and not pairs:
            ap.error(f'photo {args.photo:02d} given but nothing to do with it: add --location "Name"')
    args.func(args)


if __name__ == '__main__':
    main()
