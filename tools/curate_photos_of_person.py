#!/usr/bin/env python3
"""Shortlist the strongest photos of a given person across the photo libraries.

Given a person defined in config/people.json (a set of face clusters), rank every
photo they appear in and pick a diverse, high-quality subset. Answers three
questions from three sources: the face index for "which photos is this person in",
CLIP for "is this a good photo", and the site's own publishing history for "is this
the kind of photo that gets chosen".

Local-only: reads local indexes, writes an HTML viewer and the JSON behind it.
Nothing is committed, uploaded, or changed on the deployed site.

    tools/curate_photos_of_person.py --person <key> [--n 100] [--since 2020]

Scoring, all z-scored over the candidate pool so the parts are comparable:

  aesthetic   zero-shot CLIP, mean(positive prompts) - mean(negative prompts).
              Each prompt is z-scored across the pool FIRST: CLIP cosines carry a
              large per-prompt bias, so comparing prompts within one image is
              meaningless while comparing one prompt across images is fine.
  taste       a logistic regression separating already-published photos (post
              drafts, gallery highlights) from the rest of the library. Weak
              labels — an unpublished photo isn't necessarily a bad one — so it
              carries a modest weight; 5-fold AUC lands around 0.67.
  presence    how prominently and how confidently the person appears: face box
              area relative to the 2160px long edge both libraries share, plus
              similarity to their face centroid. Keeps a distant figure in a wide
              landscape in play while preferring frames where they read clearly.
  recency     a small additive nudge only. The real recency bias is the band
              quotas below, which is what actually guarantees older years keep a
              foothold instead of being scored out entirely.

Then diversity, which turns out to matter more than the scoring: without it a
ranked list collapses onto whichever moment was photographed most. Three filters
run — CLIP near-duplicates, a same-trip burst window (frames seconds apart can
look different enough to pass a cosine check), and caps per trip AND per calendar
month (one trip shot on two cameras lands under two slugs, which silently doubles
the per-trip cap).

Tunables worth knowing about: WEIGHTS and the prompt lists set what "good" means,
BANDS sets the recency mix, and MAX_PER_TRIP / MAX_PER_MONTH / DUP_COSINE /
BURST_SECONDS control how varied the result is.
"""
import argparse
import html
import json
import os
import shutil
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
# Face index, clusters and CLIP caches are local-only artefacts and live under
# local_browse/ (git-ignored); this script is just the ranking on top of them.
LOCAL = ROOT / 'local_browse'
NAS_PHOTOS = Path('/Volumes/RYAN/phone_browse/photos')
# A phone's auto-backup folder holds thousands of photos that never became trips,
# so they are in no manifest and no library here. index_personal.py builds a
# face+CLIP index over one straight off its SMB share (nothing full-size is
# staged); this reads that index as a third source alongside camera and phone.
PERSONAL_DB_DEFAULT = LOCAL / 'personal_index.sqlite'
# Set from --personal-root (or the root recorded in the index) once args are known.
PERSONAL_ROOT = [None]
LONG_EDGE = 2160.0          # both libraries render display images to this

# Tuned for portrait curation: how clearly the subject reads matters most, then
# overall photo quality. `taste` is deliberately near-zero — it learns from what a
# landscape-led library publishes, which pulls towards scenery and would happily
# rank frames where the subject is a speck on the horizon. Raise it if you want
# results that look like the site's existing output rather than portraits.
WEIGHTS = {'aes': 0.34, 'taste': 0.06, 'presence': 0.42, 'recency': 0.18}
CURATED_BONUS = 0.35        # already picked for publication once before
MAX_PER_TRIP = 4
# A trip shot on both cameras appears under two different slugs (2026-china-cny vs
# 2026-02-03-26-china-cny), which quietly doubled the per-trip cap. Capping by
# calendar month as well catches that without needing to match slug names.
MAX_PER_MONTH = 5
DUP_COSINE = 0.90
BURST_SECONDS = 180         # same trip, near-simultaneous = one moment, one slot
MIN_IDENTITY = 0.55         # below this the face match is not reliable
# A face at 1.5% of the frame is fine for "they were there", useless as a portrait.
MIN_FACE_FRAC = 0.040
# Face size that reads best in a curation grid, as a fraction of the long edge.
# Below/above this the score tapers rather than cliffs, so full-body adventure
# shots still compete with head-and-shoulders ones.
FACE_SWEET = (0.10, 0.40)
# Photos full of other people are ambiguous in a portrait set: which one is the
# subject? Not a hard filter, just a preference for uncrowded frames.
MAX_COMFY_FACES = 2

# "Since 2020, biased to the last 3 years" — as quotas rather than a score nudge,
# which is what actually guarantees the older years keep a foothold. Bands are
# filled best-first; an underfilled band spills its slots into the others.
BANDS = [('last 18 months', 1.5, 0.40),
         ('18 months - 3 years', 3.0, 0.35),
         ('3+ years', 99.0, 0.25)]

POS_PROMPTS = [
    "a flattering photo of a handsome young man with a genuine smile, sharp focus",
    "a natural candid portrait of an attractive man, clear face, good lighting",
    "a stylish photo of a well dressed young man, clean background",
    "a fit young man in the middle of an outdoor adventure, action shot",
    "a warm approachable photo of a man laughing, relaxed confident posture",
]
# The novelty negatives earn their place. A smiling face next to an animal, or
# holding an object up to the lens, scores well on "clear face, genuine smile"
# while being no use as a portrait — without these the top of the list fills up
# with them.
NEG_PROMPTS = [
    "a blurry out of focus snapshot",
    "a dark underexposed grainy photo",
    "an awkward unflattering photo, mid-blink, bad angle, double chin",
    "a photo of a crowd of strangers where no one stands out",
    "a screenshot, document, or picture of a screen",
    "a photo where the face is hidden by a helmet, mask, or sunglasses",
    "a distant photo where the person is a tiny unrecognisable speck",
    "a photo dominated by a cow, farm animal or livestock",
    "a person holding up a book, sign, poster or product to the camera",
    "a silly novelty joke photo, thumbs up, peace sign to the camera",
    "a photo mainly of a plate of food on a restaurant table",
]


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def parse_ts(t):
    """Trip manifests write '...Z'; EXIF from a phone backup has no zone at all.
    Naive values are read as UTC — an hour either way changes nothing here."""
    d = datetime.fromisoformat(str(t).replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def zscore(x):
    x = np.asarray(x, dtype=np.float32)
    s = x.std()
    return (x - x.mean()) / (s if s > 1e-6 else 1.0)


# ------------------------------------------------------------------ candidates

def personal_source(db: Path, root: Path, centroid, min_sim):
    """Extra candidates from a phone auto-backup index: faces, times, CLIP vectors.

    Returned in the same shapes the trip libraries use, under source 'personal',
    so the scoring and diversity passes need no special cases. Empty when the
    index hasn't been built.
    """
    if not db.exists():
        return {}, {}, {}
    con = sqlite3.connect(db)
    meta = {r[0]: (r[1], r[2], r[3], r[4]) for r in
            con.execute("SELECT path, ts, w, h, n_faces FROM img WHERE n_faces > 0")}
    faces, ts, clip = {}, {}, {}
    for path, x, y, fw, fh, det, emb in con.execute(
            "SELECT path, x, y, fw, fh, det, emb FROM face"):
        if path not in meta:
            continue
        v = np.frombuffer(emb, np.float32)
        sim = float(v @ centroid)
        if sim < min_sim:
            continue
        # Bucket by month, not one flat 'phone-backup' slug: the per-trip cap is
        # meant to stop one trip dominating, and with a single slug it throttled
        # the entire backup — hundreds of candidates — to a handful of slots.
        month = (meta[path][0] or '0000-00')[:7]
        key = ('personal', f'phone-backup-{month}', path)
        cand = {'sim': sim, 'det': float(det),
                'face_frac': float(np.sqrt(fw * fh) / LONG_EDGE),
                'n_faces': int(meta[path][3]), 'box': (x, y, fw, fh)}
        if key not in faces or sim > faces[key]['sim']:
            faces[key] = cand
    for src_key in list(faces):
        path = src_key[2]
        t, w, h, _ = meta[path]
        ts[src_key] = (t, (w / h) if h else None)
    by_path = {k[2]: k for k in faces}
    for path, emb in con.execute("SELECT path, emb FROM clip"):
        if path in by_path:
            v = np.frombuffer(emb, np.float16).astype(np.float32)
            clip[by_path[path]] = v / np.linalg.norm(v)
    con.close()
    return faces, ts, clip


def person_label(person='me'):
    """Display name from config/people.json, falling back to the key itself."""
    roster = (load(ROOT / 'config' / 'people.json') or {}).get('people', {}).get(person) or {}
    return roster.get('label') or person


def person_centroid(person='me'):
    """Unit mean of the person's face embeddings, for matching other libraries."""
    roster = (load(ROOT / 'config' / 'people.json') or {}).get('people', {}).get(person)
    clusters = {c['id']: c for c in load(LOCAL / 'clusters.json')['clusters']}
    fids = [f for cid in roster['clusters'] for f in clusters.get(cid, {}).get('face_ids', [])]
    con = sqlite3.connect(LOCAL / 'face_index.sqlite')
    rows = []
    for i in range(0, len(fids), 900):
        chunk = fids[i:i + 900]
        rows += con.execute(
            f"SELECT emb FROM faces WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk)
        ).fetchall()
    con.close()
    e = np.stack([np.frombuffer(r[0], np.float32) for r in rows]).mean(0)
    return e / np.linalg.norm(e)


def person_photos(person='me'):
    """(source, slug, photo_id) -> face metrics for every photo the person is in."""
    roster = (load(ROOT / 'config' / 'people.json') or {}).get('people', {}).get(person)
    if not roster:
        raise SystemExit(f"✗ config/people.json has no '{person}' entry — "
                         "run tools/people_index.py --seed")
    clusters = {c['id']: c for c in load(LOCAL / 'clusters.json')['clusters']}
    fids = [f for cid in roster['clusters'] for f in clusters.get(cid, {}).get('face_ids', [])]
    con = sqlite3.connect(LOCAL / 'face_index.sqlite')
    rows = []
    for i in range(0, len(fids), 900):
        chunk = fids[i:i + 900]
        q = ','.join('?' * len(chunk))
        rows += con.execute(
            f"SELECT img, source, w, h, det, emb, x, y FROM faces WHERE id IN ({q})", tuple(chunk)
        ).fetchall()
    # How many faces the detector found in each photo at all, not just the subject's.
    n_faces = {(src, img): n for img, src, n in
               con.execute("SELECT img, source, n_faces FROM images")}
    con.close()

    emb = np.stack([np.frombuffer(r[5], np.float32) for r in rows])
    centroid = emb.mean(0)
    centroid /= np.linalg.norm(centroid)
    sims = emb @ centroid

    best = {}
    for r, sim in zip(rows, sims):
        slug = r[0].split('/')[0]
        pid = r[0].split('/')[-1].rsplit('.', 1)[0]
        key = (r[1], slug, pid)
        cand = {'sim': float(sim), 'det': float(r[4]),
                'face_frac': float(np.sqrt(r[2] * r[3]) / LONG_EDGE),
                'n_faces': int(n_faces.get((r[1], r[0]), 1)),
                'box': (int(r[6]), int(r[7]), int(r[2]), int(r[3]))}
        # One good face is enough; where a photo yields several matches (mirrors,
        # reflections, a face detected twice) judge it on the clearest.
        if key not in best or cand['sim'] > best[key]['sim']:
            best[key] = cand
    return best


def face_clipped(box, ar):
    """Is the face box running off the edge of the frame?

    Both libraries render to a 2160px long edge, so the manifest aspect ratio is
    enough to recover the display dimensions without opening the file. A box that
    touches an edge means the head continues out of shot — fine for a holiday
    snap, wrong for a profile photo, and the detector is happy to report a
    confident face for a forehead-cropped one.
    """
    if not ar or ar <= 0:
        return False
    w, h = (LONG_EDGE, LONG_EDGE / ar) if ar >= 1 else (LONG_EDGE * ar, LONG_EDGE)
    x, y, bw, bh = box
    m = 0.012 * LONG_EDGE          # ~26px: detector boxes sit a little inside the edge
    return x <= m or y <= m or (x + bw) >= (w - m) or (y + bh) >= (h - m)


def timestamps():
    out = {}
    for d in (ROOT / 'web' / 'trips').iterdir():
        if not d.is_dir():
            continue
        m = (load(d / 'manifest.full.json') or load(d / 'manifest.all.json')
             or load(d / 'manifest.json'))
        for ph in (m or {}).get('photos', []):
            out[('camera', d.name, ph['id'])] = (ph.get('timestamp'), ph.get('ar'))
    phone_dir = ROOT / 'web' / 'phone' / 'trips'
    if phone_dir.is_dir():
        for d in phone_dir.iterdir():
            if not d.is_dir():
                continue
            for ph in (load(d / 'manifest.json') or {}).get('photos', []):
                out[('phone', d.name[len('phone-'):], ph['id'])] = (ph.get('timestamp'), ph.get('ar'))
    return out


def curated_keys():
    """'<slug>/<id>' for every photo already chosen for publication: post drafts
    plus hand-picked gallery highlights. Used both as a small score bonus and as
    the positive class for the taste model."""
    picks = {}
    state = load(LOCAL / 'posts_state.json') or {}
    for p in state.get('posts', []):
        for ref in p.get('photos', []):
            picks[f"{ref['trip']}/{ref['id']}"] = 'posted' if p.get('posted') else 'draft'
    hl = (load(ROOT / 'config' / 'gallery_highlights.json') or {}).get('highlights', {})
    for slug, ids in hl.items():
        for i in ids:
            picks.setdefault(f'{slug}/{i}', 'highlight')
    return picks


# ------------------------------------------------------------------ embeddings

def clip_index(phone_npz):
    """('camera'|'phone', slug, id) -> unit CLIP vector."""
    idx = {}
    cam = np.load(ROOT / 'local_browse' / 'clip_embeddings.npz', allow_pickle=True)
    E = cam['embs'].astype(np.float32)
    E /= np.linalg.norm(E, axis=1, keepdims=True)
    for k, v in zip(cam['keys'], E):
        slug, _, pid = str(k).partition('/')
        idx[('camera', slug, pid)] = v
    if phone_npz and Path(phone_npz).exists():
        ph = np.load(phone_npz, allow_pickle=True)
        P = ph['embs'].astype(np.float32)
        P /= np.linalg.norm(P, axis=1, keepdims=True)
        for k, v in zip(ph['keys'], P):
            slug, _, pid = str(k).partition('/')
            idx[('phone', slug, pid)] = v
    return idx


def text_embed(prompts):
    """Projected CLIP text features. In transformers 5.x get_text_features returns
    an output object whose pooler_output already IS the projected 512-d vector —
    verified against the cached image embeddings (cosine 1.0). Projecting it again
    silently produces garbage."""
    import torch
    from transformers import CLIPModel, CLIPProcessor
    dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').to(dev).eval()
    proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
    with torch.no_grad():
        t = proc(text=prompts, return_tensors='pt', padding=True).to(dev)
        f = model.get_text_features(**t)
        if hasattr(f, 'pooler_output'):
            f = f.pooler_output
        f = f / f.norm(dim=-1, keepdim=True)
    return f.detach().cpu().numpy().astype(np.float32)


def taste_direction(clip_idx, curated):
    """Logistic regression: already-published photos vs the rest of the library.

    Returns (w, b) or None when there are too few picks to learn anything.
    """
    import torch
    cam = {k: v for k, v in clip_idx.items() if k[0] == 'camera'}
    keys = list(cam)
    E = np.stack([cam[k] for k in keys])
    pos = [i for i, k in enumerate(keys) if f'{k[1]}/{k[2]}' in curated]
    if len(pos) < 40:
        return None
    rng = np.random.default_rng(0)
    negpool = np.array(sorted(set(range(len(keys))) - set(pos)))
    neg = rng.choice(negpool, min(len(negpool), 4 * len(pos)), replace=False)
    X = torch.tensor(np.concatenate([E[pos], E[neg]]))
    y = torch.tensor(np.concatenate([np.ones(len(pos), 'float32'),
                                     np.zeros(len(neg), 'float32')]))
    w = torch.zeros(X.shape[1], requires_grad=True)
    b = torch.zeros(1, requires_grad=True)
    opt = torch.optim.Adam([w, b], lr=0.05)
    pw = torch.tensor(len(neg) / max(len(pos), 1), dtype=torch.float32)
    for _ in range(400):
        opt.zero_grad()
        loss = torch.nn.functional.binary_cross_entropy_with_logits(
            X @ w + b, y, pos_weight=pw) + 3e-3 * (w ** 2).sum()
        loss.backward()
        opt.step()
    return w.detach().numpy(), float(b.detach())


# Prescription glasses only — sunglasses are deliberately NOT treated as glasses
# here, so sunglasses shots stay eligible for the no-glasses section.
GLASSES_PROMPTS = ["a close up photo of a face wearing eyeglasses",
                   "a close up photo of a face wearing spectacles with frames"]
BARE_PROMPTS = ["a close up photo of a face with no glasses, bare eyes",
                "a close up photo of a clean face without eyewear"]


def glasses_scores(cands, workdir: Path):
    """Higher = more likely wearing glasses, over the FACE CROP not the whole photo.

    Whole-image prompts cannot see a detail this small — a face is often 10% of the
    frame. Cropping to the face with margin first makes the signal clean.

    The score is only meaningful RELATIVELY: CLIP text prompts carry an arbitrary
    offset, so on a validation set the glasses shots came out at -0.018/-0.001 and
    the bare ones at -0.042/-0.046. Correctly ordered, but nowhere near zero — so
    callers must rank, never threshold at 0.
    """
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()   # backup originals are HEIC
    except ImportError:
        pass

    workdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    by_phone_trip = defaultdict(list)
    for c in cands:
        key = (c['src'], c['slug'], c['id'])
        if c['src'] == 'camera':
            f = ROOT / 'hosted-photos' / c['slug'] / 'display' / f"{c['id']}.webp"
            if f.exists():
                paths[key] = f
        elif c['src'] == 'personal':
            f = personal_path(PERSONAL_ROOT[0], c['id'])
            if f and f.exists():
                paths[key] = f
        else:
            by_phone_trip[c['slug']].append(c['id'])
    for slug, ids in by_phone_trip.items():      # one bulk fetch per trip, not per file
        src = NAS_PHOTOS / slug / 'display'
        if not src.is_dir():
            continue
        dst = workdir / slug
        dst.mkdir(exist_ok=True)
        subprocess.run(['rsync', '-a', '--files-from=-', str(src) + '/', str(dst) + '/'],
                       input='\n'.join(f'{i}.webp' for i in ids), text=True, capture_output=True)
        for i in ids:
            f = dst / f'{i}.webp'
            if f.exists():
                paths[('phone', slug, i)] = f

    dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').to(dev).eval()
    proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')

    def embed_text(prompts):
        with torch.no_grad():
            t = proc(text=prompts, return_tensors='pt', padding=True).to(dev)
            f = model.get_text_features(**t)
            f = f.pooler_output if hasattr(f, 'pooler_output') else f
            return (f / f.norm(dim=-1, keepdim=True)).detach().cpu().numpy()

    G, B = embed_text(GLASSES_PROMPTS), embed_text(BARE_PROMPTS)
    out, batch, keys = {}, [], []

    def flush():
        if not batch:
            return
        with torch.no_grad():
            ii = proc(images=batch, return_tensors='pt').to(dev)
            f = model.get_image_features(**ii)
            f = f.pooler_output if hasattr(f, 'pooler_output') else f
            f = (f / f.norm(dim=-1, keepdim=True)).detach().cpu().numpy()
        for k, v in zip(keys, f):
            out[k] = float((v @ G.T).mean() - (v @ B.T).mean())
        batch.clear()
        keys.clear()

    for c in cands:
        key = (c['src'], c['slug'], c['id'])
        f = paths.get(key)
        if not f:
            continue
        try:
            img = Image.open(f).convert('RGB')
        except Exception:
            continue
        x, y, w, h = c['box']
        mx, my = int(w * 0.55), int(h * 0.55)     # margin so frames/temples are in shot
        batch.append(img.crop((max(0, x - mx), max(0, y - my),
                               min(img.width, x + w + mx), min(img.height, y + h + my))))
        keys.append(key)
        if len(batch) >= 64:
            flush()
    flush()
    return out


# ------------------------------------------------------------------ scoring

def recency_bonus(year, now_year):
    """Additive, in z units. Deliberately not a multiplier: the ask was to bias
    the last three years, not to bury everything older."""
    age = now_year - year
    if age <= 1:
        return 1.0
    if age <= 2:
        return 0.75
    if age <= 3:
        return 0.45
    return 0.0


def build(args):
    faces = person_photos(args.person)
    ts = timestamps()
    curated = curated_keys()
    clip_idx = clip_index(args.phone_npz)

    pf, pts, pclip = personal_source(Path(args.personal_db), Path(args.personal_root),
                                     person_centroid(args.person), MIN_IDENTITY)
    if pf:
        faces.update(pf)
        ts.update(pts)
        clip_idx.update(pclip)
        print(f"  + {len(pf)} candidates from the phone-backup index")

    pool = []
    dropped = defaultdict(int)
    for (src, slug, pid), f in faces.items():
        t, ar = ts.get((src, slug, pid), (None, None))
        if not t:
            dropped['no timestamp'] += 1
            continue
        if t[:4] < str(args.since):
            dropped[f'older than {args.since}'] += 1
            continue
        if f['sim'] < MIN_IDENTITY:
            dropped['weak face match (likely someone else)'] += 1
            continue
        if f['face_frac'] < MIN_FACE_FRAC:
            dropped['face too small to attribute'] += 1
            continue
        if (src, slug, pid) not in clip_idx:
            dropped['no CLIP embedding'] += 1
            continue
        pool.append({'src': src, 'slug': slug, 'id': pid, 'ts': t,
                     'clipped': face_clipped(f['box'], ar), **f})

    if not pool:
        raise SystemExit('no candidates — nothing to rank')
    E = np.stack([clip_idx[(p['src'], p['slug'], p['id'])] for p in pool])

    # Aesthetic: z-score EACH prompt across the pool before combining, so a prompt
    # that happens to score high on everything can't dominate.
    tp, tn = text_embed(POS_PROMPTS), text_embed(NEG_PROMPTS)
    aes = (np.stack([zscore(E @ v) for v in tp]).mean(0)
           - np.stack([zscore(E @ v) for v in tn]).mean(0))

    tw = taste_direction(clip_idx, curated)
    taste = (E @ tw[0] + tw[1]) if tw else np.zeros(len(pool), np.float32)

    def size_score(f):
        """1.0 across the sweet spot, tapering outside it — a big face and a
        full-body shot are both fine, a distant speck is not."""
        lo, hi = FACE_SWEET
        if f < lo:
            return max(0.0, f / lo) ** 0.7
        if f > hi:
            return max(0.35, 1.0 - (f - hi))
        return 1.0

    crowd = np.array([min(0, MAX_COMFY_FACES - p['n_faces']) for p in pool], np.float32)
    clip_pen = np.array([-1.0 if p['clipped'] else 0.0 for p in pool], np.float32)
    presence = (1.2 * zscore([size_score(p['face_frac']) for p in pool])
                + 0.8 * zscore([p['sim'] for p in pool])
                + 0.5 * zscore(crowd)
                + 0.7 * zscore(clip_pen))
    now_year = datetime.now(timezone.utc).year
    rec = np.array([recency_bonus(int(p['ts'][:4]), now_year) for p in pool], np.float32)

    score = (WEIGHTS['aes'] * zscore(aes)
             + WEIGHTS['taste'] * zscore(taste)
             + WEIGHTS['presence'] * zscore(presence)
             + WEIGHTS['recency'] * zscore(rec))
    for i, p in enumerate(pool):
        p['aes'] = float(aes[i])
        p['taste'] = float(taste[i])
        p['recency'] = float(rec[i])
        p['curated'] = curated.get(f"{p['slug']}/{p['id']}")
        p['score'] = float(score[i]) + (CURATED_BONUS if p['curated'] else 0.0)

    # ---- diversity + recency quotas
    now = datetime.now(timezone.utc)

    def age_years(p):
        return (now - parse_ts(p['ts'])).days / 365.25

    def band_of(p):
        a = age_years(p)
        for n, (name, upper, _) in enumerate(BANDS):
            if a <= upper:
                return n
        return len(BANDS) - 1

    for i, p in enumerate(pool):
        p['_i'] = i
        p['band'] = band_of(p)

    order = sorted(range(len(pool)), key=lambda i: -pool[i]['score'])
    quota = [max(1, round(args.n * frac)) for _, _, frac in BANDS]
    chosen, per_trip, per_month, kept = [], defaultdict(int), defaultdict(int), []
    stats = defaultdict(int)

    def admit(i, enforce_quota):
        p = pool[i]
        if enforce_quota and quota[p['band']] <= 0:
            return False
        if per_trip[p['slug']] >= MAX_PER_TRIP:
            stats['over trip cap'] += 1
            return False
        if per_month[p['ts'][:7]] >= MAX_PER_MONTH:
            stats['over month cap'] += 1
            return False
        for q, qv in kept:
            # Same trip within a few minutes is one moment however different the
            # frames look — bursts of the same pose were slipping past the cosine.
            if (q['src'], q['slug']) == (p['src'], p['slug']) and \
                    abs((parse_ts(p['ts']) - parse_ts(q['ts'])).total_seconds()) < BURST_SECONDS:
                stats['same burst'] += 1
                return False
            if float(E[i] @ qv) > DUP_COSINE:
                stats['near-duplicate'] += 1
                return False
        chosen.append(p)
        kept.append((p, E[i]))
        per_trip[p['slug']] += 1
        per_month[p['ts'][:7]] += 1
        quota[p['band']] -= 1
        return True

    for i in order:                       # pass 1: respect the band quotas
        if len(chosen) >= args.n:
            break
        admit(i, True)
    taken = {id(p) for p in chosen}
    for i in order:                       # pass 2: fill any slots a thin band left
        if len(chosen) >= args.n:
            break
        if id(pool[i]) not in taken:
            admit(i, False)

    chosen.sort(key=lambda p: -p['score'])

    # ---- optional second section: more picks, without glasses
    extra = []
    if args.no_glasses:
        taken_ids = {(p['src'], p['slug'], p['id']) for p in chosen}
        rest = [pool[i] for i in order if (pool[i]['src'], pool[i]['slug'], pool[i]['id'])
                not in taken_ids]
        # Generous probe: the glasses filter halves it, then burst/duplicate
        # suppression against the main list and the per-trip caps take most of the
        # rest. At 6x the ask this returned 17 of a requested 50.
        probe = rest[:max(args.no_glasses * 20, 600)]
        gs = glasses_scores(probe, Path(args.out) / '_crops')
        scored = [(gs[(c['src'], c['slug'], c['id'])], c) for c in probe
                  if (c['src'], c['slug'], c['id']) in gs]
        scored.sort(key=lambda kv: kv[0])          # relative ranking: lowest = barest
        # Keep the clearly-bare half, then order those by photo quality again, so
        # this section is "best photos without glasses" not "least glassy photos".
        bare = [c for _, c in scored[:max(len(scored) // 2, args.no_glasses * 3)]]
        for c in bare:
            c['glasses'] = gs[(c['src'], c['slug'], c['id'])]
        bare.sort(key=lambda c: -c['score'])
        # Fresh trip/month counters: this is a SECOND list, so it must not inherit
        # the first section's spent quota (that let only 2 through). Duplicate and
        # burst suppression still run against everything already picked.
        per_trip.clear()
        per_month.clear()
        for c in bare:
            if len(extra) >= args.no_glasses:
                break
            if admit(c['_i'], False):
                extra.append(chosen.pop())         # admit() appends to chosen
        extra.sort(key=lambda p: -p['score'])

    return {'pool': pool, 'chosen': chosen, 'extra': extra, 'dropped': dict(dropped),
            'filtered': dict(stats), 'taste_trained': tw is not None,
            'bands': [(BANDS[b][0], sum(1 for p in chosen if p['band'] == b))
                      for b in range(len(BANDS))]}


# ------------------------------------------------------------------ output

def personal_path(root: Path, rel: str):
    return (root / rel) if root else None


def render_personal(src: Path, dst: Path, long_edge=LONG_EDGE):
    """HEIC off the backup share -> webp. Chrome will not display HEIC, and the
    originals are ~2MB each, so the picked ones are converted once on the way out."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    from PIL import Image
    try:
        im = Image.open(src)
        im.load()
        im = im.convert('RGB')
        sc = long_edge / max(im.size)
        if sc < 1:
            im = im.resize((round(im.width * sc), round(im.height * sc)), Image.LANCZOS)
        im.save(dst, 'WEBP', quality=88)
        return True
    except Exception:
        return False


def stage_images(chosen, out: Path):
    """Resolve each pick to something the viewer can load.

    Camera photos are just linked in place — hosted-photos/ is on local disk, so
    copying them would duplicate gigabytes for no benefit.

    Phone photos are the exception and do get copied: they live on the NAS behind
    Tailscale SMB, where a single file read takes 10-20s and a page of 100 links
    would never finish rendering. A bulk rsync moves them at ~0.1s each, so they
    are fetched once, in one batched call per trip.
    """
    imgs = out / 'img'
    imgs.mkdir(parents=True, exist_ok=True)
    rel_root = os.path.relpath(ROOT, out)
    by_phone_trip = defaultdict(list)
    for p in chosen:
        name = f"{p['src']}_{p['slug']}_{p['id']}.webp".replace('/', '_')
        if p['src'] == 'camera':
            src = ROOT / 'hosted-photos' / p['slug'] / 'display' / f"{p['id']}.webp"
            p['file'] = f"{rel_root}/hosted-photos/{p['slug']}/display/{p['id']}.webp"
            p['linked'] = True
            if not src.exists():
                p['file'] = None
        elif p['src'] == 'personal':
            p['file'] = f'img/{name}'
            p['linked'] = False
            src = personal_path(PERSONAL_ROOT[0], p['id'])
            if not (src and src.exists() and render_personal(src, imgs / name)):
                p['file'] = None
        else:
            p['file'] = f'img/{name}'
            p['linked'] = False
            by_phone_trip[p['slug']].append((p['id'], name))
    for slug, items in by_phone_trip.items():
        src = NAS_PHOTOS / slug / 'display'
        if not src.is_dir():
            continue
        listing = '\n'.join(f'{pid}.webp' for pid, _ in items)
        tmp = imgs / f'_stage_{slug}'
        tmp.mkdir(exist_ok=True)
        subprocess.run(['rsync', '-a', '--files-from=-', str(src) + '/', str(tmp) + '/'],
                       input=listing, text=True, capture_output=True)
        for pid, name in items:
            f = tmp / f'{pid}.webp'
            if f.exists():
                shutil.move(str(f), imgs / name)
        shutil.rmtree(tmp, ignore_errors=True)
    return sum(1 for p in chosen if p['file'] and (out / p['file']).exists())


def render(res, out: Path, args):
    chosen = res['chosen'] + res.get('extra', [])
    years = defaultdict(int)
    srcs = defaultdict(int)
    for p in chosen:
        years[p['ts'][:4]] += 1
        srcs[p['src']] += 1
    recent = sum(v for y, v in years.items() if int(y) >= datetime.now(timezone.utc).year - 3)

    n_main = len(res['chosen'])
    cards = []
    for i, p in enumerate(chosen, 1):
        if res.get('extra') and i == n_main + 1:
            cards.append(f'</div><h2 class=sec>Without glasses — {len(res["extra"])} more</h2>'
                         f'<div class=grid>')
        badge = ''
        if p['curated']:
            badge = f"<span class=b title='already published'>{p['curated']}</span>"
        missing = '' if (p['file'] and (out / p['file']).exists()) else " data-missing=1"
        key = f"{p['src']}|{p['slug']}|{p['id']}"
        cards.append(f"""
<figure class=c{missing} data-key="{html.escape(key)}">
  <img loading=lazy src="{p['file']}" alt="" data-full="{p['file']}">
  <figcaption>
    <span class=n>{i}</span>
    <span class=t>{p['ts'][:10]}</span>
    <span class=s data-src="{p['src']}">{p['src']}</span>
    {badge}
    <span class=meta>{p['slug']}/{p['id']}</span>
    <span class=sc>score {p['score']:.2f} · aes {p['aes']:+.2f} · face {p['face_frac']:.2f} · id {p['sim']:.2f}</span>
  </figcaption>
</figure>""")

    page = f"""<!doctype html><meta charset=utf-8>
<title>{args.title} — {len(chosen)} picks</title>
<link rel=icon href="data:,">
<style>
:root{{color-scheme:dark}}
body{{margin:0;background:#0d0d0f;color:#e8e8ea;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
header{{padding:26px 30px 18px;border-bottom:1px solid #232327}}
h1{{margin:0 0 8px;font-size:22px;letter-spacing:.02em}}
.sub{{color:#8b8b93;max-width:80ch}}
.stats{{margin-top:12px;display:flex;gap:20px;flex-wrap:wrap;color:#8b8b93;font-size:13px}}
.stats b{{color:#e8e8ea}}
.filters{{margin-top:14px;display:flex;gap:8px;flex-wrap:wrap}}
.filters button{{background:#1c1c20;color:#9a9aa2;border:1px solid #2c2c32;border-radius:999px;
  padding:5px 14px;font-size:12px;cursor:pointer}}
.filters button.on{{color:#fff;border-color:#6b8cff}}
/* Letterboxed rather than centre-cropped: a cropped 3:2 card cut the heads off
   portrait shots, which is the exact thing being reviewed. A masonry/columns layout
   fixed that but flowed rank order down each column, so #1 and #35 sat side by
   side — a ranked list has to read left-to-right. Fixed-height grid keeps both. */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;padding:22px 30px 60px}}
.c{{margin:0;background:#151518;border:1px solid #232327;border-radius:10px;overflow:hidden}}
.c img{{width:100%;height:330px;object-fit:contain;display:block;background:#000}}
.c[data-missing] img{{min-height:180px}}
.c[data-missing]::after{{content:'image not staged';display:block;padding:8px;color:#c66;font-size:12px}}
figcaption{{padding:9px 11px;display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}}
.n{{color:#6b8cff;font-weight:700}}
.t{{color:#e8e8ea}}
.s{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8b8b93;
  border:1px solid #2c2c32;border-radius:999px;padding:1px 8px}}
.s[data-src=phone]{{color:#7fb6ff;border-color:#2b3f5c}}
.b{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#5ad17e;
  border:1px solid #2c4a33;border-radius:999px;padding:1px 8px}}
.meta{{flex-basis:100%;color:#5c5c64;font-size:11px;font-family:ui-monospace,Menlo,monospace}}
h2.sec{{margin:34px 30px 0;padding-top:22px;border-top:1px solid #232327;font-size:18px;letter-spacing:.02em}}
.sc{{flex-basis:100%;color:#4a4a52;font-size:11px;font-family:ui-monospace,Menlo,monospace}}
body.f-camera .c:has(.s[data-src=phone]),body.f-phone .c:has(.s[data-src=camera]){{display:none}}
.c img{{cursor:zoom-in}}
/* In-page viewer. A tab per photo makes stepping through a set unusable. */
#lb{{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.94);display:none;
  align-items:center;justify-content:center}}
#lb.on{{display:flex}}
#lb img{{max-width:94vw;max-height:88vh;object-fit:contain}}
#lb .nav{{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.08);
  color:#fff;border:0;font-size:26px;width:52px;height:76px;cursor:pointer}}
#lb .nav:hover{{background:rgba(255,255,255,.18)}}
#lb .prev{{left:10px}} #lb .next{{right:10px}}
#lb .bar{{position:absolute;left:0;right:0;bottom:0;padding:14px;text-align:center;color:#ccc;
  font-size:13px;background:linear-gradient(transparent,rgba(0,0,0,.8))}}
#lb .x{{position:absolute;top:12px;right:16px;background:none;border:0;color:#fff;font-size:26px;cursor:pointer}}
</style>
<header>
<h1>{args.title} — {len(chosen)} picks</h1>
<p class=sub>Ranked from every photo the face index attributes to this person since
{args.since}, across every library configured below. Scored on CLIP image quality, a model of
which photos have been published before, how prominently the subject appears, and recency.
Bursts, near-duplicates and more than {MAX_PER_TRIP} per trip / {MAX_PER_MONTH} per month are
filtered out so one moment can't take several slots. The year mix is set by quota rather than
left to the score.</p>
<div class=stats>
  <span>pool <b>{len(res['pool'])}</b> candidate photos</span>
  <span>camera <b>{srcs['camera']}</b> · phone <b>{srcs['phone']}</b></span>
  <span>last 3 years <b>{recent}</b> of {len(chosen)}</span>
  {f'<span>without glasses <b>{len(res["extra"])}</b></span>' if res.get('extra') else ''}
  <span>{' · '.join(f'{y} <b>{n}</b>' for y, n in sorted(years.items()))}</span>
</div>
<div class=filters>
  <button class=on data-f=all>All</button>
  <button data-f=camera>Camera only</button>
  <button data-f=phone>Phone only</button>
</div>
</header>
<div class=grid>{''.join(cards)}</div>
<div id=lb><button class=x>&times;</button><button class="nav prev">&#8249;</button>
  <img alt=""><button class="nav next">&#8250;</button><div class=bar></div></div>
<script>
/* In-page viewer. Opening a new tab per photo makes stepping through a set of
   150 unusable, so clicking a photo opens it here: arrows or click to move,
   Esc or a click outside to close. */
const figs = [...document.querySelectorAll('.c')];
const lb = document.getElementById('lb');
const lbImg = lb.querySelector('img');
const lbBar = lb.querySelector('.bar');
let at = -1;
const shownFigs = () => figs.filter(f => f.offsetParent !== null);
function show(i) {{
  const vis = shownFigs();
  if (!vis.length) return;
  at = (i + vis.length) % vis.length;
  const f = vis[at];
  lbImg.src = f.querySelector('img').dataset.full;
  lbBar.textContent = (at + 1) + ' / ' + vis.length + '  \u00b7  '
    + (f.querySelector('.meta') ? f.querySelector('.meta').textContent : '');
  lb.classList.add('on');
}}
figs.forEach(f => f.querySelector('img').addEventListener('click', () => show(shownFigs().indexOf(f))));
lb.querySelector('.x').addEventListener('click', () => lb.classList.remove('on'));
lb.querySelector('.prev').addEventListener('click', e => {{ e.stopPropagation(); show(at - 1); }});
lb.querySelector('.next').addEventListener('click', e => {{ e.stopPropagation(); show(at + 1); }});
lb.addEventListener('click', e => {{ if (e.target === lb) lb.classList.remove('on'); }});
document.addEventListener('keydown', e => {{
  if (!lb.classList.contains('on')) return;
  if (e.key === 'Escape') lb.classList.remove('on');
  if (e.key === 'ArrowLeft') show(at - 1);
  if (e.key === 'ArrowRight') show(at + 1);
}});

document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{{
  document.body.className = b.dataset.f==='all' ? '' : 'f-'+b.dataset.f;
  document.querySelectorAll('.filters button').forEach(x=>x.classList.toggle('on',x===b));
}}));
</script>"""
    (out / 'index.html').write_text(page)



CURATED_SETS = ROOT / 'config' / 'curated_sets.json'
# Backup photos exist nowhere the site can serve from, so the chosen ones are
# materialised into a pseudo phone trip. Gallery.photoUrl already routes any
# 'phone-' trip to /phone/trips, and posts mode already understands phone refs.
CURATED_TRIP = 'phone-curated'


def emit_curated_set(picks, name):
    """Publish the picks as a named set the local People page can show.

    Written to config/curated_sets.json, which people_index.py merges into the
    LOCALHOST people document only — never the one uploaded to R2.
    """
    trip_dir = ROOT / 'web' / 'phone' / 'trips' / CURATED_TRIP
    entries, manifest = [], []
    for pk in picks:
        if pk['src'] == 'camera':
            entries.append({'t': pk['slug'], 'i': pk['id'], 'g': 1})
        elif pk['src'] == 'phone':
            entries.append({'t': f"phone-{pk['slug']}", 'i': pk['id'], 'g': 2})
        else:
            # Backup original -> a webp pair under the pseudo trip, so it loads
            # like any other phone photo instead of being unservable HEIC.
            src = PERSONAL_ROOT[0] / pk['id'] if PERSONAL_ROOT[0] else None
            if not (src and src.exists()):
                continue
            pid = Path(pk['id']).stem
            ok = True
            for kind, edge in (('display', LONG_EDGE), ('thumbnails', 800)):
                d = trip_dir / kind
                d.mkdir(parents=True, exist_ok=True)
                ok = render_personal(src, d / f'{pid}.webp', edge) and ok
            if not ok:
                continue
            # Keep a copy of the backup original alongside the renders: the
            # backup share is mounted ad hoc, and post.py pull needs a stable
            # original to hand to Lightroom long after the mount is gone.
            odir = trip_dir / 'originals'
            odir.mkdir(parents=True, exist_ok=True)
            if not (odir / src.name).exists():
                # copyfile, not copy2: chflags on SMB-sourced files fails.
                shutil.copyfile(src, odir / src.name)
                st = src.stat()
                os.utime(odir / src.name, (st.st_atime, st.st_mtime))
            entries.append({'t': CURATED_TRIP, 'i': pid, 'g': 2})
            manifest.append({'id': pid, 'timestamp': pk['ts'],
                             'thumbnail': f'thumbnails/{pid}.webp',
                             'display': f'display/{pid}.webp',
                             'original': f'originals/{src.name}',
                             'src': str(src)})
    if manifest:
        # Merge over previous runs — a re-emit must not orphan photos that
        # older post drafts still reference by id.
        mpath = trip_dir / 'manifest.json'
        photos = {p['id']: p for p in (load(mpath) or {}).get('photos', [])}
        photos.update((p['id'], p) for p in manifest)
        mpath.write_text(json.dumps(
            {'trip': CURATED_TRIP, 'photos': list(photos.values())}, indent=2))
    data = load(CURATED_SETS) or {}
    data[name] = entries
    CURATED_SETS.parent.mkdir(parents=True, exist_ok=True)
    CURATED_SETS.write_text(json.dumps(data, indent=2) + '\n')
    return len(entries)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=100)
    ap.add_argument('--since', type=int, default=2020)
    # Output goes under web/phone/, NOT local_browse/: deploy.py rsyncs the whole
    # of local_browse/ into the private config-backup repo (it already excludes
    # face_index.sqlite for the same reason), and the staged full-size images would
    # add ~30MB per run to that repo's history. web/phone/ is git-ignored AND
    # excluded from the Pages deploy, and serve.sh serves it at /phone/best-of-me/.
    ap.add_argument('--out', default=str(ROOT / 'web' / 'phone' / 'best-of-me'))
    ap.add_argument('--person', default='me',
                    help="key of the person in config/people.json's `people` map")
    ap.add_argument('--title', default='Curated photos',
                    help='heading for the generated viewer')
    ap.add_argument('--no-glasses', dest='no_glasses', type=int, default=0,
                    help='append N extra picks where the subject is not wearing glasses')
    ap.add_argument('--personal-db', dest='personal_db', default=str(PERSONAL_DB_DEFAULT),
                    help='face+CLIP index over a phone auto-backup (index_personal.py)')
    ap.add_argument('--set-name', dest='set_name', default='',
                    help='publish the result as a named set on the local People '
                         'page (default: "<person> curated")')
    ap.add_argument('--personal-root', dest='personal_root', default='',
                    help='mount path the personal index paths are relative to')
    ap.add_argument('--phone-npz', dest='phone_npz', default=str(LOCAL / 'phone_clip.npz'))
    args = ap.parse_args()
    # 22 trips at 4 each cannot fill a 100-slot list, so the caps scale with n
    # rather than silently letting the list come up short.
    global MAX_PER_TRIP, MAX_PER_MONTH
    MAX_PER_TRIP = max(MAX_PER_TRIP, -(-args.n // 12))
    MAX_PER_MONTH = max(MAX_PER_MONTH, -(-args.n // 10))

    root = args.personal_root
    if not root and Path(args.personal_db).exists():
        try:
            con = sqlite3.connect(args.personal_db)
            row = con.execute("SELECT v FROM meta WHERE k='root'").fetchone()
            con.close()
            root = row[0] if row else ''
        except sqlite3.Error:
            root = ''
    PERSONAL_ROOT[0] = Path(root) if root else None

    if not args.set_name:
        # The person's display name, not the config key: 'me' is a key, the label
        # is what belongs on a page heading.
        args.set_name = f'{person_label(args.person)} curated'

    res = build(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    staged = stage_images(res['chosen'] + res.get('extra', []), out)
    render(res, out, args)
    (out / 'picks.json').write_text(
        json.dumps({'picks': res['chosen'], 'no_glasses': res.get('extra', [])}, indent=2))
    shutil.rmtree(out / '_crops', ignore_errors=True)   # NAS fetches for the glasses probe

    print(f"pool: {len(res['pool'])} photos of '{args.person}' since {args.since}")
    for reason, n in sorted(res['dropped'].items(), key=lambda kv: -kv[1]):
        print(f"   dropped {n}: {reason}")
    print('filtered: ' + ', '.join(f'{n} {k}' for k, n in
          sorted(res['filtered'].items(), key=lambda kv: -kv[1])))
    print('bands:    ' + ' · '.join(f'{name} {n}' for name, n in res['bands']))
    print('taste model: ' + ('trained on previously published photos'
                             if res['taste_trained'] else 'skipped, too few published photos'))
    if res.get('extra'):
        print(f"plus {len(res['extra'])} more without glasses")
    print(f"chose {len(res['chosen']) + len(res.get('extra', []))}, staged {staged} images")
    print(f"→ {out / 'index.html'}")

    n = emit_curated_set(res['chosen'] + res.get('extra', []), args.set_name)
    print(f"published '{args.set_name}' as a People-page set: {n} photos")
    print("  → shown only on localhost; browse it in posts mode to build a post")


if __name__ == '__main__':
    main()
