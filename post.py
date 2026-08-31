#!/usr/bin/env python3
"""
Everything for working with post drafts. Always takes a subcommand:

  ./post.py serve     local site + phone photos, kept in sync with the remote
  ./post.py pull      pull the drafts from the remote and copy their files
                      (plus a location card in <post>/maps/ for every photo
                      marked with the map button — see tools/map_card.py)
  ./post.py mirror    sync a dev server started elsewhere (./serve.sh runs this)
  ./post.py sync      copy the remote state down to the local dev server
  ./post.py push      copy the local dev state up to the remote
  ./post.py status    show both sides' versions and post counts

Why sync exists: post drafts live in R2 behind /api/posts, but `wrangler pages
dev` serves its OWN local simulated R2 - a separate store. Editing posts
locally therefore never reaches the live site, and deploy.py does not carry
post state either (it only syncs web/, functions/ and the manifests). So the
two sides are copied explicitly, here.

`serve` handles that automatically: it seeds the local server from the remote
on startup, then watches the local state and pushes every change up within a
few seconds, plus a final push when you Ctrl-C. If the remote changes while
you serve (an edit from your phone on the live site), the push is held back
and reported rather than silently overwriting it.

Local-only feature note: the phone-photo companion reads web/phone/, which is
never deployed, so phone selections can only be made through `serve`.

Environment (or parsed from .env.deploy): CF_POSTS_PASSWORD (required),
CF_SITE_PASSWORD (if the site gate is on), CF_PAGES_PROJECT (for the URL),
CF_ALL_PASSWORD (passed to the dev server so "See All" works locally).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
WEB_TRIPS = ROOT / 'web' / 'trips'
SOURCE_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp')
LOCAL_BASE = 'http://localhost:8788'
DEV_PORT = 8788
CURATE_PORT = 8799


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


def token_for(password):
    """Hex SHA-256, matching the server's tokenFor() cookie scheme."""
    return hashlib.sha256(password.encode()).hexdigest()


def cookie_header(env):
    cookies = [f"posts_auth={token_for(env['CF_POSTS_PASSWORD'])}"]
    if env.get('CF_SITE_PASSWORD'):
        cookies.insert(0, f"site_auth={token_for(env['CF_SITE_PASSWORD'])}")
    if env.get('CF_ALL_PASSWORD'):
        cookies.append(f"all_access={token_for(env['CF_ALL_PASSWORD'])}")
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


def fetch_posts(url, site_password, posts_password):
    """Back-compat wrapper used by the pull path."""
    env = {'CF_POSTS_PASSWORD': posts_password, 'CF_SITE_PASSWORD': site_password}
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
    project = env.get('CF_PAGES_PROJECT')
    if not project:
        sys.exit('❌ CF_PAGES_PROJECT not set (needed to reach the live site).')
    return f'https://{project}.pages.dev'


def read_doc(base, env, which='main'):
    return api_call(posts_url(base, which), env)


def write_doc(base, env, which, posts, base_version):
    return api_call(posts_url(base, which), env, method='PUT',
                    body={'baseVersion': base_version, 'posts': posts})


def same_posts(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


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


def normalize_stem(stem):
    # Same suffix handling as build_collections.build_id_index
    return re.split(r'-Enhanced|-NR|-SAI|-2$', stem)[0]


def resolve_source(trip, photo_id):
    """{trip, id} → absolute Path of the edited source file, or (None, why)."""
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
    exact = base / ph.get('source_filename', f'{photo_id}.jpg')
    if exact.exists():
        return exact, None
    # Fall back to a recursive stem search (re-edits can change the suffix)
    print(f"   🔍 {photo_id}: not at its manifest filename, scanning {base} "
          "(slow over the network)...")
    norm = normalize_stem(photo_id)
    candidates = [f for f in base.rglob('*')
                  if f.suffix.lower() in SOURCE_EXTS
                  and normalize_stem(f.stem) == norm]
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


def _all_face_boxes(img):
    """Whole-frame pass PLUS the tiled sweep, merged. The whole-frame pass has
    the last word on big faces; tiled-only additions (tiny distant figures the
    downscaled pass misses) must survive _confirm_face, which kills the
    sweep's texture false-positives while keeping real faces (verified: a real
    34px face confirms at 0.7, night-foliage hits at 0.0)."""
    boxes = _face_boxes(img)
    for cand in _face_boxes_tiled(img):
        if any(abs(cand[0] - b[0]) < max(30, b[2]) and abs(cand[1] - b[1]) < max(30, b[3])
               for b in boxes):
            continue   # already covered by the whole-frame pass
        if _confirm_face(img, cand) >= 0.45:
            boxes.append(cand)
    return boxes


def blur_faces_file(src, dst):
    """Write a copy of src to dst with every detected face pixelated.
    Returns the face count (0 = wrote a plain copy; caller may warn)."""
    import cv2
    import numpy as np
    data = np.fromfile(str(src), dtype=np.uint8)   # path-safe on SMB
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        if Path(src) != Path(dst):   # in-place re-blur: leave the file alone
            shutil.copy2(src, dst)
        return -1
    boxes = _all_face_boxes(img)
    H, W = img.shape[:2]
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


def _file_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
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


def sync_post_dir(dest_dir, plan, reblur=False):
    """Bring dest_dir in line with the plan. Reorders done on the site are
    applied by renaming the existing local NN_ files (in-place edits kept);
    only genuinely new photos are copied from the drive. Returns (copied,
    renamed). reblur=True regenerates every blur-flagged copy from its source
    (use after the blur algorithm changes).

    Every copy we write is fingerprinted (md5 in .pull_state.json). A local
    NN_ file whose content no longer matches was replaced by hand - typically
    a Lightroom re-export after re-editing the original. Those local edits are
    kept, but if the photo is blur-flagged the fresh export has an unblurred
    face, so it is re-blurred IN PLACE (the re-edit survives; only the face
    pixelation is reapplied)."""
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

    # Re-edits exported under a Lightroom-renamed filename (X-Enhanced-NR, X-2, ...)
    adopted = _adopt_renamed_exports(dest_dir, plan, planned)

    copied = renamed = 0
    for i, ref, src, dst in plan:
        want_blur = bool(ref.get('blur'))
        have_blur = bool(blurred.get(src.name, False))
        if src.name in adopted:
            new = adopted.pop(src.name)
            dst.unlink(missing_ok=True)
            new.rename(dst)
            print(f'   ⇢ {dst.name}: adopted re-edited export {new.name}')
            if want_blur:
                n = blur_faces_file(dst, dst)
                print(f'   🙂🚫 {dst.name}: ' + (
                    f'{n} face(s) pixelated' if n > 0 else (
                        'no faces found, check manually' if n == 0
                        else 'decode failed, check manually')))
            blurred[src.name] = want_blur
            hashes[src.name] = _file_md5(dst)
            copied += 1
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
            if not want_blur:
                hashes[src.name] = cur   # local re-edit kept, just remember it
                continue
            # blur-flagged copy was replaced (re-edit re-exported over it):
            # re-blur the NEW content in place so the re-edit survives.
            print(f'   ⟳ {dst.name}: replaced locally since last pull, re-blurring in place...')
            n = blur_faces_file(dst, dst)
            print(f'   🙂🚫 {dst.name}: ' + (
                f'{n} face(s) pixelated' if n > 0 else (
                    'no faces found, left as is, check manually' if n == 0
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
            n = blur_faces_file(src, dst)
            label = f'{n} face(s) pixelated' if n > 0 else (
                'no faces found — copied unblurred, check manually' if n == 0
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
    return copied, renamed


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


def resolve_phone(ref):
    """Original NAS file for a {trip, id} phone photo or {trip, file} video."""
    slug = ref['trip'].removeprefix('phone-')
    names, phone_root = phone_sources(slug)
    if ref.get('id'):
        src = names.get(ref['id'])
        return (src, None) if src and src.exists() else (None, 'not in phone manifest')
    if phone_root is None:
        return None, 'phone trip manifest missing'
    src = phone_root / ref['file']
    return (src, None) if src.exists() else (None, f'missing: {src}')


def sync_phone_dir(phone_dir, plan):
    """Mirror the plan into <post>/Phone/: copy new, drop unselected copies."""
    phone_dir.mkdir(parents=True, exist_ok=True)
    wanted = {dst.name for _, _, _, dst in plan}
    removed = 0
    for f in sorted(phone_dir.iterdir()):
        if f.is_file() and re.match(r'\d+_', f.name) and f.name not in wanted:
            f.unlink()
            removed += 1
    copied = 0
    for _, _, src, dst in plan:
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        try:
            mb = f' ({src.stat().st_size / 1e6:.1f} MB)'
        except OSError:
            mb = ''
        print(f'   ⇣ Phone/{dst.name}: copying{mb}...')
        shutil.copy2(src, dst)
        copied += 1
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
    doc = fetch_posts(url, env.get('CF_SITE_PASSWORD'), posts_password)
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
    for post in posts:
        name = sanitize(post['name'])
        dest_dir = Path(args.dest) / name
        tag = f"#{post['num']} " if post.get('num') else ''
        acct = f" @{post['account']}" if post.get('account') else ''
        print(f"── {tag}{post['name']}{acct} ({len(post['photos'])} photos) → {dest_dir}")
        plan = []
        for i, ref in enumerate(post['photos'], 1):
            src, why = resolve_source(ref['trip'], ref['id'])
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
            for i, ref in enumerate(post.get('phone') or [], 1):
                src, why = resolve_phone(ref)
                print(f"   Phone {i:02d} {src or why}  →  {dest_dir / 'Phone'}")
            print()
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        copied, renamed = sync_post_dir(dest_dir, plan, reblur=getattr(args, 'reblur', False))
        manifest = {
            'name': post['name'],
            'version': doc.get('version'),
            'photos': [{'order': i, 'trip': ref['trip'], 'id': ref['id'],
                        'source': str(src), 'copied_to': str(dst)}
                       for i, ref, src, dst in plan],
        }
        (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        parts = [f'{copied} copied']
        if renamed:
            parts.append(f'{renamed} reordered')
        parts.append(f'{len(plan) - copied - renamed} already up to date')
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
                phone_plan.append((i, ref, src, dest_dir / 'Phone' / f'{i:02d}_{src.name}'))
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
            write_lrsmcol([dst for _, _, _, dst in plan], post['name'], lr_out)
            print(f"   ✓ Lightroom smart collection → {lr_out}\n")
        except Exception as e:
            print(f"   ⚠️  Lightroom smart collection not written: {e}\n")

    if unresolved:
        sys.exit(f'⚠️  {unresolved} photo(s) could not be resolved (see above).')


# ---------------------------------------------------------------- serve

def server_up(base, env):
    try:
        api_call(posts_url(base), env, timeout=3)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
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
    """Keeps the remote in step with the local dev server.

    Pushes local edits up, but never overwrites a remote change it did not
    make: the version the remote had after our last write is remembered, and
    if the remote has moved past it (an edit on the live site from a phone)
    the push is held and reported instead.
    """

    def __init__(self, env, remote, local=None):
        self.env = env
        self.remote = remote
        self.local = local or LOCAL_BASE
        self.expect = {}        # set -> remote version we last observed/wrote
        self.warned = set()

    def seed_local(self):
        print('⇣ Seeding the local dev server from the live site...')
        for which in SETS:
            src = read_doc(self.remote, self.env, which)
            dst = read_doc(self.local, self.env, which)
            posts, kept = merge_down(src.get('posts', []), dst.get('posts', []))
            if not same_posts(posts, dst.get('posts', [])):
                write_doc(self.local, self.env, which, posts, dst.get('version', 0))
            self.expect[which] = src.get('version', 0)
            extra = (f", kept {len(kept)} made here: "
                     f"{', '.join(p.get('name', '?') for p in kept[:3])}" if kept else '')
            print(f"   ↳ {which}: {len(src.get('posts', []))} posts from the live site{extra}")

    def push(self, quiet=False):
        """Push any local change up. Returns the number of sets written."""
        written = 0
        for which in SETS:
            try:
                local = read_doc(self.local, self.env, which)
                remote = read_doc(self.remote, self.env, which)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError):
                continue
            if same_posts(local.get('posts', []), remote.get('posts', [])):
                self.expect[which] = remote.get('version', 0)
                self.warned.discard(which)
                continue
            if which in self.expect and remote.get('version') != self.expect[which]:
                if which not in self.warned:
                    print(f"\n⚠️  The live site's {which} posts changed while serving "
                          f"(v{self.expect[which]} → v{remote.get('version')}).\n"
                          "    Not overwriting it. Finish here, then choose:\n"
                          "      ./post.py push --force   keep what is on this laptop\n"
                          "      ./post.py sync           take the live version instead")
                    self.warned.add(which)
                continue
            res = write_doc(self.remote, self.env, which,
                            local.get('posts', []), remote.get('version', 0))
            self.expect[which] = res.get('version', remote.get('version', 0) + 1)
            self.warned.discard(which)
            written += 1
            if not quiet:
                print(f"   ⇡ synced {which} to the live site "
                      f"({len(local.get('posts', []))} posts, v{self.expect[which]})")
        return written


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
                mirror.push()
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
                if mirror.push(quiet=False) == 0:
                    print('   ↳ already up to date')
            except Exception as e:                      # noqa: BLE001
                print(f'   ⚠️  Final sync failed: {e}\n'
                      '      Your edits are safe locally; run ./post.py push to retry.')
        for proc, name in ((curate, 'curation server'), (dev, 'dev server')):
            if proc and proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                print(f'   ↳ stopped the {name}')
        print('Done.')


def cmd_mirror(args):
    """Sync a dev server this script did not start, then keep pushing edits up.

    ./serve.sh (and the desktop app that wraps it) runs this in the background,
    so the posts on localhost are the posts on the live site however the server
    was started — the drafts used to arrive only when the server happened to be
    started through ./post.py serve, and a post made on the phone would simply
    never show up here.
    """
    env = require_env()
    remote = remote_base(env)
    local = f'http://localhost:{args.port}'
    if not wait_for_server(local, env, args.wait):
        print('⚠️  posts: no local dev server answered; not syncing drafts.')
        return
    if not server_up(remote, env):
        print('⚠️  posts: the live site did not answer; leaving local drafts alone.')
        return
    mirror = RemoteMirror(env, remote, local)
    mirror.seed_local()

    def _stop(signum, _frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, AttributeError):
            pass
    # Also stop on our own when the dev server goes away. The shell that started
    # this is supposed to kill it, but it can be killed itself (or crash), and a
    # mirror left running against a dead server would keep pushing an empty
    # document at the live site.
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
            mirror.push()
    except KeyboardInterrupt:
        pass
    finally:
        # The dev server is usually on its way out too, so a failed last push is
        # normal: everything up to the previous tick is already on the live site.
        try:
            if server_up(local, env):
                mirror.push(quiet=True)
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
    p.set_defaults(func=cmd_pull)

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

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
