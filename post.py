#!/usr/bin/env python3
"""
Everything for working with post drafts. Always takes a subcommand:

  ./post.py serve     local site + phone photos, kept in sync with the remote
  ./post.py pull      pull the drafts from the remote and copy their files
                      (plus a location card in <post>/maps/ for every photo
                      marked with the map button — see tools/map_card.py)
                      Re-edits found in a post folder are listed (post file,
                      where it goes in Edits, an open command) and, once you
                      confirm, written back over their original in Edits
                      unblurred, optionally updating the live site too
                      (see reedits.py)
  ./post.py backfill  the same for re-edits not written back yet (earlier
                      pulls, or answered "not now")
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


def write_doc(base, env, which, posts, base_version, settings=None):
    body = {'baseVersion': base_version, 'posts': posts}
    if settings is not None:
        body['settings'] = settings
    return api_call(posts_url(base, which), env, method='PUT', body=body)


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
        if same_posts(posts, dst.get('posts', [])):
            continue
        write_doc(dst_base, env, which, posts, dst.get('version', 0))
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


def _all_face_boxes(img):
    """Whole-frame pass PLUS the tiled sweep, merged. The whole-frame pass has
    the last word on big faces; tiled-only additions (tiny distant figures the
    downscaled pass misses) must survive _confirm_face, which kills the
    sweep's texture false-positives while keeping real faces (verified: a real
    34px face confirms at 0.7, night-foliage hits at 0.0). Rotated passes add
    the faces that are not the right way up."""
    boxes = _face_boxes(img)
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


def blur_faces_file(src, dst, manual=(), detect=True):
    """Write a copy of src to dst with every detected face pixelated, plus the
    hand-marked `manual` boxes (fractions of the frame, see cmd_blur) the
    detector can't see: hooded or turned-away heads. Returns the face count
    (0 = wrote a plain copy; caller may warn)."""
    import cv2
    img = _read_image(src)
    if img is None:
        if Path(src) != Path(dst):   # in-place re-blur: leave the file alone
            shutil.copy2(src, dst)
        return -1
    H, W = img.shape[:2]
    boxes = _all_face_boxes(img) if detect else []
    boxes += [(int(fx * W), int(fy * H), int(fw * W), int(fh * H))
              for fx, fy, fw, fh in manual]
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
             and not re.match(r'^removed_', f.name)]
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
    BEFORE any face blur: a blur-flagged one keeps a clean copy in
    <post>/.reedits_staged/, since the post copy is about to be pixelated and
    Edits must get the clean photo."""
    if not _reedit_ok(ref, src, edited):
        return
    new = post_file
    if blur:
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


def sync_post_dir(dest_dir, plan, reblur=False, found=None):
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
    in Edits, see note_reedit."""
    found = [] if found is None else found
    planned = {dst.name for _, _, _, dst in plan}

    # Local NN_<file>s that are no longer at their planned name, keyed by base
    # filename. Two-phase (park under a temp name, then place) so swapped
    # order numbers can't collide mid-rename.
    parked, parked_n = {}, 0
    for f in sorted(dest_dir.iterdir()):
        m = re.match(r'^\d{2,3}_(.+)$', f.name)
        if not m or not f.is_file() or f.name in planned:
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
    manual = state.setdefault('manual_faces', {})   # hand-marked boxes (post.py blur), by source name

    def no_face_hint(dst):
        return f'no faces found, mark them by hand: ./post.py blur "{dst}"'

    # Re-edits exported under a Lightroom-renamed filename (X-Enhanced-NR, X-2, ...)
    adopted = _adopt_renamed_exports(dest_dir, plan, planned)

    copied = renamed = adopted_n = 0
    for i, ref, src, dst in plan:
        want_blur = bool(ref.get('blur'))
        have_blur = bool(blurred.get(src.name, False))
        if src.name in adopted:
            new = adopted.pop(src.name)
            note_reedit(found, ref, src, new, dest_dir, dst, want_blur)
            dst.unlink(missing_ok=True)
            new.rename(dst)
            print(f'   ⇢ {dst.name}: adopted re-edited export {new.name}')
            if want_blur:
                n = blur_faces_file(dst, dst, manual.get(src.name, ()))
                print(f'   🙂🚫 {dst.name}: ' + (
                    f'{n} face(s) pixelated' if n > 0 else (
                        no_face_hint(dst) if n == 0
                        else 'decode failed, check manually')))
            blurred[src.name] = want_blur
            hashes[src.name] = _file_md5(dst)
            adopted_n += 1
            continue
        if reblur and want_blur and dst.exists():
            dst.unlink(missing_ok=True)   # regenerate from source (missing_ok: SMB dir cache can be stale)
            have_blur = False
        if dst.exists() and have_blur == want_blur:
            rec = hashes.get(src.name)
            if rec is None:
                # pulled before fingerprinting existed: adopt what's there
                hashes[src.name] = _file_md5(dst)
                continue
            cur = _file_md5(dst)
            if cur == rec:
                continue   # right place, right content
            note_reedit(found, ref, src, dst, dest_dir, dst, want_blur)
            if not want_blur:
                hashes[src.name] = cur   # local re-edit kept, just remember it
                continue
            # blur-flagged copy was replaced (re-edit re-exported over it):
            # re-blur the NEW content in place so the re-edit survives.
            print(f'   ⟳ {dst.name}: replaced locally since last pull, re-blurring in place...')
            n = blur_faces_file(dst, dst, manual.get(src.name, ()))
            print(f'   🙂🚫 {dst.name}: ' + (
                f'{n} face(s) pixelated' if n > 0 else (
                    no_face_hint(dst) if n == 0
                    else 'decode failed, left as is, check manually')))
            hashes[src.name] = _file_md5(dst)
            copied += 1
            continue
        if parked.get(src.name):
            tmp = parked[src.name].pop(0)
            if have_blur == want_blur:
                tmp.rename(dst)
                renamed += 1
                print(f'   ↷ {dst.name}: reordered')
                continue
            tmp.unlink()   # blur state changed: regenerate below
        dst.unlink(missing_ok=True)
        try:
            mb = f' ({src.stat().st_size / 1e6:.1f} MB)'
        except OSError:
            mb = ''
        if want_blur:
            print(f'   ⇣ {dst.name}: copying + blurring faces{mb} — '
                  'the first blur loads the face model, give it ~30s...')
            n = blur_faces_file(src, dst, manual.get(src.name, ()))
            label = f'{n} face(s) pixelated' if n > 0 else (
                'copied unblurred, ' + no_face_hint(dst) if n == 0
                else 'decode failed — copied unblurred')
            print(f'   🙂🚫 {dst.name}: {label}')
        else:
            print(f'   ⇣ {dst.name}: copying{mb}...')
            shutil.copy2(src, dst)
        blurred[src.name] = want_blur
        hashes[src.name] = _file_md5(dst)
        copied += 1
    state_path.write_text(json.dumps(state, indent=1))

    # Whatever is still parked was removed from the post on the site.
    for tmps in parked.values():
        for tmp in tmps:
            orig = tmp.name.split('_', 2)[2]
            tmp.rename(dest_dir / f'removed_{orig}')
            print(f'   ⚠️  {orig} is no longer in this post, kept as removed_{orig}')
    return copied, renamed, adopted_n


def cmd_blur(args):
    """Pixelate faces the detector missed (hooded, turned away) by hand. The
    boxes go into the post folder's .pull_state.json as fractions of the frame
    and every later pull re-applies them alongside the detector, so a
    re-export of the photo comes back blurred too (re-mark it if the re-edit
    changes the crop). Only blur-flagged photos get them re-applied."""
    import cv2
    import numpy as np
    f = Path(args.file)
    m = re.match(r'^\d{2,3}_(.+)$', f.name)
    if not m or not f.is_file():
        sys.exit(f'❌ {f} is not a pulled post photo (NN_<name> in a post folder).')
    key = m.group(1)
    state_path = f.parent / '.pull_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    img = cv2.imdecode(np.fromfile(str(f), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f'❌ Could not decode {f}')
    H, W = img.shape[:2]
    if args.box:
        try:
            boxes = [tuple(int(v) for v in b.split(',')) for b in args.box]
        except ValueError:
            sys.exit('❌ --box takes x,y,w,h in pixels of the full-size photo.')
    else:
        scale = min(1.0, 1400 / max(H, W))
        view = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        print('Drag a box over each face, Enter after each one, Esc when done.')
        rois = cv2.selectROIs('Mark faces to blur', view, showCrosshair=False)
        cv2.destroyAllWindows()
        cv2.waitKey(1)
        boxes = [tuple(int(v / scale) for v in r) for r in rois]
    boxes = [b for b in boxes if len(b) == 4 and b[2] > 0 and b[3] > 0]
    if not boxes:
        sys.exit('Nothing marked, file left as is.')
    frac = [[round(x / W, 4), round(y / H, 4), round(w / W, 4), round(h / H, 4)]
            for x, y, w, h in boxes]
    manual = state.setdefault('manual_faces', {})
    manual[key] = frac if args.replace else manual.get(key, []) + frac
    blur_faces_file(f, f, frac, detect=False)
    # The pixelated file is now the expected content, not a local replacement
    state.setdefault('hashes', {})[key] = _file_md5(f)
    state_path.write_text(json.dumps(state, indent=1))
    print(f'🙂🚫 {f.name}: {len(boxes)} face(s) pixelated by hand, '
          f'{len(manual[key])} saved for future pulls')
    if not state.get('blurred', {}).get(key):
        print('   ⚠️  This photo is not blur-flagged on the site: flag it there, or '
              'later pulls will not re-apply the saved boxes.')


# ---------------------------------------------------------------- map cards

MAP_LONG_EDGE = 1350
# Shapes each platform allows, as width/height: Instagram caps portrait at 4:5,
# Xiaohongshu at 3:4.
PLATFORM_SHAPE = {'ig': (0.8, 1.91), 'xhs': (0.75, 4 / 3)}


def post_card_size(post, plan):
    """The one shape every map card in this post is rendered at.

    A carousel locks every slide to the shape of the first, so the cards follow
    the post's first photo, clamped to what the platform allows. A 9:16 frame
    therefore becomes a 4:5 card on Instagram, never a 9:16 one.
    """
    ar = 1.5
    for _, ref, _, _ in plan:
        photos, _ = trip_manifest_photos(ref['trip'])
        cand = (photos.get(ref['id']) or {}).get('ar')
        if cand:
            ar = float(cand)
            break
    lo, hi = PLATFORM_SHAPE['xhs' if post.get('platform') == 'xhs' else 'ig']
    r = min(hi, max(lo, ar))
    if r >= 1:
        return MAP_LONG_EDGE, round(MAP_LONG_EDGE / r)
    return round(MAP_LONG_EDGE * r), MAP_LONG_EDGE


def sync_map_dir(dest_dir, plan, size):
    """Render a location card for every photo carrying a map mark.

    Cards go in <post>/maps/, named after the photo they belong to so the pair
    is obvious in Lightroom. Which card was rendered is remembered next to the
    blur state, so an unchanged one is neither re-rendered nor re-downloaded and
    a reorder on the site is just a rename. Returns (rendered, renamed, failed).
    """
    sys.path.insert(0, str(ROOT / 'tools'))
    import map_card

    maps_dir = dest_dir / 'maps'
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
        if was.get('style') == style and was.get('size') == list(size) and old and old.exists():
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
        cards[src.name] = {'file': target.name, 'style': style, 'size': list(size)}
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
    wanted = {dst.name for _, _, _, dst in plan}
    removed = 0
    for f in sorted(phone_dir.iterdir()):
        if f.is_file() and re.match(r'\d+_', f.name) and f.name not in wanted:
            f.unlink()
            removed += 1
    # Same state file as a post folder, so ./post.py blur works on these too:
    # its hand-marked boxes are re-applied whenever the copy is regenerated.
    state_path = phone_dir / '.pull_state.json'
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    manual = state.get('manual_faces', {})
    copied = 0
    for _, ref, src, dst in plan:
        blur = dst.name.endswith('_blurred.jpg')
        if blur:
            state.setdefault('blurred', {})[dst.name.split('_', 1)[1]] = True
            if dst.exists():
                continue
        elif dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        try:
            mb = f' ({src.stat().st_size / 1e6:.1f} MB)'
        except OSError:
            mb = ''
        if blur:
            print(f'   ⇣ Phone/{dst.name}: copying + blurring faces{mb}...')
            n = blur_faces_file(src, dst, manual.get(dst.name.split('_', 1)[1], ()))
            if n == 0:
                print(f'      no faces found, mark them by hand: ./post.py blur "{dst}"')
            elif n < 0:
                print(f'      ⚠️  could not decode {src.name}, copied unblurred')
        else:
            print(f'   ⇣ Phone/{dst.name}: copying{mb}...')
            shutil.copy2(src, dst)
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
    if not posts:
        print('No post drafts yet.')
        return

    print(f"📥 {len(posts)} post draft(s) (state v{doc.get('version')})\n")
    unresolved = 0
    found = []   # re-edits to confirm for write-back to Edits
    for post in posts:
        name = sanitize(post['name'])
        dest_dir = Path(args.dest) / name
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
            src, why = resolve_source(ref['trip'], ref['id'], prev.get((ref['trip'], ref['id'])))
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

        if args.list or args.dry_run:
            size = post_card_size(post, plan)
            if cap_what:
                print(f"   📝 {cap_what}  →  {dest_dir / 'caption.txt'}")
            for i, ref, src, dst in plan:
                print(f"   {i:02d} {src}  →  {dst}")
                if ref.get('map'):
                    print(f"      🗺  {ref['map']} card {size[0]}x{size[1]}  →  "
                          f"{dest_dir / 'maps' / f'{i:02d}_{src.stem}_map.jpg'}")
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
        copied, renamed, adopted = sync_post_dir(dest_dir, plan,
                                                 reblur=getattr(args, 'reblur', False),
                                                 found=found)
        manifest = {
            'name': post['name'],
            'version': doc.get('version'),
            'photos': [{'order': i, 'trip': ref['trip'], 'id': ref['id'],
                        'source': str(src), 'copied_to': str(dst)}
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

        # Location cards for the photos marked with the map button -> <post>/maps/
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
                print(f"   ✓ maps/ ({size[0]}x{size[1]}): {', '.join(mparts)}")
                manifest['maps'] = [
                    {'order': i, 'style': ref['map'],
                     'file': str(dest_dir / 'maps' / f'{i:02d}_{src.stem}_map.jpg')}
                    for i, ref, src, _ in plan if ref.get('map')]
                (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        except Exception as e:
            if marked:
                print(f"   ⚠️  map cards not rendered: {e}")

        # Demoted photos -> <post>/Waitlist/. Still attached to the post but
        # out of the carousel, so they are pulled (blur marks honoured, same
        # re-edit/adoption rules) into their own subfolder rather than dropped.
        # skipwl leaves Waitlist/ exactly as the last pull left it, and keeps
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
                     'source': str(src), 'copied_to': str(dst)}
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
                 'source': str(src), 'copied_to': str(dst)}
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
            n_rules, undated = write_lrsmcol([dst for _, _, _, dst in plan], post['name'],
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
        self.status['versions'][which] = {'local': L.get('version', 0), 'live': R.get('version', 0)}
        if same_posts(lp, rp) and same_posts(ls, rs):
            if self.base.get(which) != {'posts': rp, 'settings': rs}:
                self.base[which] = {'posts': rp, 'settings': rs}
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
        if conflicts:
            f = self._backup(f'{which}_conflicts', [{'laptop': l, 'live': r} for l, r in conflicts])
            self.status['conflicts'] += len(conflicts)
            names = ', '.join(r.get('name', '?') for _l, r in conflicts[:3])
            print(f"\n⚠️  {which}: {len(conflicts)} post(s) changed on both sides ({names}). "
                  f"Kept the live site's; this laptop's copy is in {f}")
        down = not (same_posts(posts, lp) and same_posts(settings, ls))
        up = not (same_posts(posts, rp) and same_posts(settings, rs))
        # Local first: if the page saved meanwhile (409) nothing has been written
        # anywhere, and the next tick merges again from fresh copies. Only when
        # both writes landed does the agreed state move on.
        if down:
            write_doc(self.local, self.env, which, posts, L.get('version', 0), settings)
        if up:
            write_doc(self.remote, self.env, which, posts, R.get('version', 0), settings)
        self.base[which] = {'posts': posts, 'settings': settings}
        self._save_base()
        what = 'both' if down and up else 'down' if down else 'up'
        arrow = {'down': '⇣ from', 'up': '⇡ to', 'both': '⇅ with'}[what]
        print(f"   {arrow} the live site: {which} ({len(posts)} posts)")
        return what

    def sync(self, force_remote=False):
        """Sync both sets now (thread-safe). Returns the status dict."""
        with self.lock:
            local_changed = any(
                not same_posts(read_doc(self.local, self.env, w).get('posts', []),
                               (self.base.get(w) or {}).get('posts'))
                for w in SETS) if not force_remote else True
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
    p.add_argument('--skip-waitlist', action='store_true',
                   help='Leave the Waitlist photos alone (also: a bare "skipwl" word)')
    p.set_defaults(func=cmd_pull)

    b = sub.add_parser('blur', help='pixelate faces the detector missed, by hand')
    b.add_argument('file', help='The pulled photo, e.g. ".../Posts/<post>/05_RM107359.jpg"')
    b.add_argument('--box', action='append',
                   help='x,y,w,h in full-size pixels instead of drawing (repeatable)')
    b.add_argument('--replace', action='store_true',
                   help='Replace the saved boxes for this photo instead of adding to them')
    b.set_defaults(func=cmd_blur)

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

    argv = sys.argv[1:]
    # `post.py pull 24 skipwl`: a bare word is quicker to type than the flag.
    if argv[:1] == ['pull']:
        argv = ['--skip-waitlist' if a == 'skipwl' else a for a in argv]
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
