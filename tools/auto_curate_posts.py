#!/usr/bin/env python3
"""Auto-curate post drafts (IG / Xiaohongshu) from the whole photo library.

Builds themed candidate posts from every processed trip photographed 2019+,
using CLIP zero-shot classification over local thumbnails plus the existing
geo data, and uploads them to the Posts feature as a SEPARATE "auto" document
(_state/auto_posts.json via /api/posts?set=auto) shown in the "Auto curated"
tab of /posts. Re-running replaces the whole auto set; manual posts are never
touched.

Themes:
  story      per-trip behind-the-scenes sets: the best scenic shots from the
             sessions where photos of ME exist (face index), me-shots mixed in,
             plus a Phone bucket of phone photos of me from the same trip.
             The Xiaohongshu angle: foreigner solo road-tripping China.
  province   one post per China province (DataV admin-1 boundaries).
  place      one post per named location cluster (any country) with enough
             photos (manifest cluster reverse-geo names).
  industrial energy / infrastructure photos, global + top provinces.
  nature     landscapes, global + top provinces.
  wildlife   animals, global.

Every post is homogeneous in privacy: photos are partitioned into the
public pool and the private pool (trip privacy + per-photo privacy map) and a
post is emitted per pool that has enough candidates; private ones are
suffixed "(private)".

Run with the project venv (torch lives there):

    ./venv/bin/python tools/auto_curate_posts.py            # build + save JSON
    ./venv/bin/python tools/auto_curate_posts.py --list     # summary only
    ./venv/bin/python tools/auto_curate_posts.py --push local   # -> localhost:8788
    ./venv/bin/python tools/auto_curate_posts.py --push prod    # -> pages.dev

First run CLIP-embeds ~11k thumbnails (a few minutes on MPS); embeddings are
cached in local_browse/clip_embeddings.npz so later runs are instant.
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_privacy                              # noqa: E402
from build_collections import ProvinceIndex       # noqa: E402

WEB_TRIPS = ROOT / 'web' / 'trips'
PHONE_TRIPS = ROOT / 'web' / 'phone' / 'trips'
HOSTED = ROOT / 'hosted-photos'
EMB_CACHE = ROOT / 'local_browse' / 'clip_embeddings.npz'
OUT_JSON = ROOT / 'local_browse' / 'auto_posts.json'
PROVINCES_GEOJSON = ROOT / 'config' / 'geo' / 'china_provinces.geojson'
MIN_YEAR = 2019
CAROUSEL = 18          # XHS max; IG trims to 10 by hand
MIN_POST = 10          # do not emit groups with fewer candidates
PER_SESSION = 3        # diversity: max photos per visit session in one post
MAX_PHONE = 30         # phone bucket cap per story post

# ---------------------------------------------------------------- taxonomy

CATEGORIES = {
    'industrial': [
        'an aerial photo of a factory or industrial plant with chimneys and smoke',
        'a power station with cooling towers',
        'a large solar panel farm in the desert',
        'wind turbines in a field',
        'an open-pit mine with terraced walls',
        'a hydroelectric dam across a river',
        'an oil refinery or chemical plant with pipes and tanks',
        'a container port with cranes and stacked shipping containers',
        'electrical substation with pylons and power lines',
        'a coal yard with heavy mining trucks',
    ],
    'bridge': [
        'a giant suspension bridge spanning a deep canyon',
        'a tall highway viaduct on concrete piers above a valley',
    ],
    'road': [
        'a winding mountain road with hairpin turns seen from above',
        'a long straight desert highway vanishing to the horizon',
        'a car driving alone on a remote scenic road',
    ],
    'nature': [
        'a dramatic mountain landscape with snowy peaks',
        'sand dunes in a vast desert',
        'a deep river canyon with cliffs',
        'a turquoise alpine lake',
        'colourful rainbow rock formations',
        'green terraced rice fields',
        'a dense forest in mist',
        'vast open grassland steppe under a big sky',
        'a glacier or ice field',
        'a waterfall in a gorge',
        'sea cliffs on a rugged coastline',
    ],
    'wildlife': [
        'camels in the desert',
        'horses grazing on grassland',
        'yaks on a high mountain plateau',
        'a herd of sheep or goats on a road',
        'a wild monkey',
        'a bird in flight close up',
        'a wild animal in its natural habitat',
    ],
    'urban': [
        'a dense city skyline of skyscrapers at dusk',
        'a neon-lit city street at night',
        'an aerial view over a huge city',
        'a grand historic temple or pagoda',
        'an ancient walled old town',
    ],
    'village': [
        'a small rural village of traditional houses in the mountains',
        'a quiet village street with local shops and market stalls',
        'farmland and villages seen from a drone',
    ],
    'people': [
        'a portrait of a person looking at the camera',
        'a person standing small in a vast landscape',
    ],
    'misc': [
        'food on a table in a restaurant',
        'a hotel room interior',
        'a screenshot of a phone or computer screen',
        'a document or piece of paper',
        'the interior of a car',
    ],
}
AES_POS = 'an award-winning stunning professional photograph, dramatic light, perfect composition'
AES_NEG = 'a blurry dark poorly composed boring snapshot'

# ------------------------------------------------- free-text query geography
# Region / country / province words recognised inside a curation query
# ("highways in china mongolia and kyrgyzstan") become HARD geo filters;
# the full query text is still what CLIP ranks against.

REGION_ALIASES = {
    'central asia': {'KG', 'KZ', 'UZ', 'TJ', 'TM'},
    'southeast asia': {'TH', 'MY', 'VN', 'PH', 'ID', 'KH', 'LA', 'MM', 'SG'},
    'middle east': {'AE', 'IL', 'JO', 'OM', 'QA', 'SA', 'EG'},
    'balkans': {'HR', 'BA', 'RS', 'ME', 'AL', 'MK', 'SI'},
}
COUNTRY_ALIASES = {
    'china': 'CN', 'mongolia': 'MN', 'kyrgyzstan': 'KG', 'kazakhstan': 'KZ',
    'uzbekistan': 'UZ', 'tajikistan': 'TJ', 'croatia': 'HR', 'bosnia': 'BA',
    'slovenia': 'SI', 'italy': 'IT', 'sicily': 'IT', 'france': 'FR',
    'germany': 'DE', 'austria': 'AT', 'switzerland': 'CH', 'netherlands': 'NL',
    'belgium': 'BE', 'luxembourg': 'LU', 'uk': 'GB', 'scotland': 'GB',
    'england': 'GB', 'wales': 'GB', 'ireland': 'IE', 'romania': 'RO',
    'norway': 'NO', 'cyprus': 'CY', 'egypt': 'EG', 'mauritania': 'MR',
    'morocco': 'MA', 'dubai': 'AE', 'uae': 'AE', 'emirates': 'AE',
    'thailand': 'TH', 'malaysia': 'MY', 'korea': 'KR', 'south korea': 'KR',
    'vietnam': 'VN', 'philippines': 'PH', 'indonesia': 'ID', 'india': 'IN',
    'japan': 'JP', 'taiwan': 'TW', 'israel': 'IL', 'turkey': 'TR',
    'greece': 'GR', 'spain': 'ES', 'portugal': 'PT', 'poland': 'PL',
    'hungary': 'HU', 'usa': 'US', 'america': 'US', 'united states': 'US',
    'canada': 'CA', 'mexico': 'MX', 'kosovo': 'XK', 'albania': 'AL',
    'serbia': 'RS', 'montenegro': 'ME', 'georgia': 'GE', 'armenia': 'AM',
}


def parse_geo(query):
    """(countries, provinces) mentioned in the query. Longest phrases match
    first and are consumed, so 'inner mongolia' does not also match
    'mongolia'."""
    from build_collections import PROVINCE_ZH_EN
    text = ' ' + re.sub(r'[^a-z ]+', ' ', query.lower()) + ' '
    countries, provinces = set(), set()
    phrases = ([(k, ('region', v)) for k, v in REGION_ALIASES.items()]
               + [(p.lower(), ('province', p)) for p in PROVINCE_ZH_EN.values()]
               + [(k, ('country', v)) for k, v in COUNTRY_ALIASES.items()])
    for phrase, (kind, val) in sorted(phrases, key=lambda kv: -len(kv[0])):
        pat = ' ' + phrase + ' '
        if pat in text:
            text = text.replace(pat, ' ')
            if kind == 'region':
                countries |= val
            elif kind == 'country':
                countries.add(val)
            else:
                provinces.add(val)
    return countries, provinces

THEME_CATS = {
    'industrial': {'industrial'},
    'nature': {'nature'},
    'wildlife': {'wildlife'},
}
# categories that never carry a geographic post on their own
BORING = {'misc'}


# ---------------------------------------------------------------- pool

def load_pool():
    """Every 2019+ photo from full manifests, with privacy + place labels."""
    trip_meta = photo_privacy.load_trip_meta()
    pfp = photo_privacy.load_public_from_private()
    print('Computing per-photo privacy map...')
    private_map = photo_privacy.compute_private_map()
    idx = json.loads((WEB_TRIPS / 'index.json').read_text())
    public_trip = {t['id']: t.get('public', False) for t in idx.get('trips', [])}
    trip_name = {t['id']: t.get('name') or t['id'] for t in idx.get('trips', [])}
    trip_countries = {t['id']: t.get('countries') or [] for t in idx.get('trips', [])}

    pool = []
    for mf in sorted(WEB_TRIPS.glob('*/manifest.json')):
        slug = mf.parent.name
        m = photo_privacy.load_full_manifest(mf.parent)
        if not m:
            continue
        # cluster reverse-geo names: photo id -> location string / country code
        loc_of, country_of = {}, {}
        for c in m.get('clusters') or []:
            loc = (c.get('location') or '').strip()
            ctry = (c.get('country') or '').strip()
            for pid in c.get('photo_ids') or []:
                if loc:
                    loc_of[pid] = loc
                if ctry:
                    country_of[pid] = ctry
        tc = trip_countries.get(slug, [])
        fallback_country = tc[0] if len(tc) == 1 else None
        for ph in m.get('photos', []):
            ts = ph.get('timestamp') or ''
            if not ts or int(ts[:4]) < MIN_YEAR:
                continue
            is_pub = ((public_trip.get(slug, False) or slug in pfp)
                      and ph['id'] not in private_map.get(slug, set()))
            pool.append({
                'trip': slug, 'id': ph['id'],
                'lat': ph.get('lat'), 'lon': ph.get('lon'),
                'ts': ts, 'ar': ph.get('ar'),
                'loc': loc_of.get(ph['id'], ''),
                'country': country_of.get(ph['id'], fallback_country),
                'public': is_pub,
                'trip_name': trip_name.get(slug, slug),
            })
    print(f'Pool: {len(pool)} photos 2019+ from '
          f'{len({p["trip"] for p in pool})} trips '
          f'({sum(p["public"] for p in pool)} public)')
    return pool


# ---------------------------------------------------------------- CLIP

def key_of(p):
    return f"{p['trip']}/{p['id']}"


def thumb_path(p):
    return HOSTED / p['trip'] / 'thumbnails' / f"{p['id']}.webp"


def load_embeddings(pool):
    """Image embeddings for the pool, cached in EMB_CACHE. Returns key->row."""
    cached = {}
    if EMB_CACHE.exists():
        z = np.load(EMB_CACHE, allow_pickle=False)
        cached = dict(zip([k for k in z['keys']], z['embs']))
    todo = [p for p in pool if key_of(p) not in cached and thumb_path(p).exists()]
    if todo:
        import torch
        from PIL import Image
        from transformers import CLIPModel, CLIPProcessor
        dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
        print(f'CLIP-embedding {len(todo)} new thumbnails ({len(cached)} cached) on {dev}...')
        model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').to(dev).eval()
        proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
        for i in range(0, len(todo), 64):
            batch = todo[i:i + 64]
            imgs = [Image.open(thumb_path(p)).convert('RGB') for p in batch]
            with torch.no_grad():
                ii = proc(images=imgs, return_tensors='pt').to(dev)
                f = model.get_image_features(**ii)
                if hasattr(f, 'pooler_output'):
                    f = f.pooler_output
                f = f / f.norm(dim=-1, keepdim=True)
            for p, row in zip(batch, f.cpu().numpy()):
                cached[key_of(p)] = row.astype(np.float16)
            if (i // 64) % 10 == 0:
                print(f'  {i + len(batch)}/{len(todo)}', flush=True)
        keys = list(cached.keys())
        np.savez_compressed(EMB_CACHE, keys=np.array(keys),
                            embs=np.stack([cached[k] for k in keys]))
        print(f'  saved {len(keys)} embeddings -> {EMB_CACHE.name}')
    globals()['_EMB_INDEX'] = cached      # near-duplicate checks read this
    return cached


_text_model = None


def text_embed(prompts):
    global _text_model
    import torch
    if _text_model is None:
        from transformers import CLIPModel, CLIPProcessor
        dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
        model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').to(dev).eval()
        proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
        _text_model = (model, proc, dev)
    model, proc, dev = _text_model
    with torch.no_grad():
        ti = proc(text=prompts, return_tensors='pt', padding=True).to(dev)
        tf = model.get_text_features(**ti)
        if hasattr(tf, 'pooler_output'):
            tf = tf.pooler_output
        tf = tf / tf.norm(dim=-1, keepdim=True)
    return tf.cpu().numpy().astype(np.float32)


def classify(pool, embs):
    """Attach 'cat', 'catscore', 'aes', 'score' to each pooled photo."""
    prompts, owners = [], []
    for cat, plist in CATEGORIES.items():
        for pr in plist:
            prompts.append(pr)
            owners.append(cat)
    prompts += [AES_POS, AES_NEG]
    T = text_embed(prompts)

    # curated-favourite bonus from existing hand-picks
    favs = load_favourites()

    kept = []
    mat = []
    for p in pool:
        e = embs.get(key_of(p))
        if e is None:
            continue
        kept.append(p)
        mat.append(e)
    M = np.stack(mat).astype(np.float32)
    S = M @ T.T
    cats = list(CATEGORIES.keys())
    cat_cols = {c: [i for i, o in enumerate(owners) if o == c] for c in cats}
    for row, p in zip(S, kept):
        per_cat = {c: float(row[cols].max()) for c, cols in cat_cols.items()}
        best = max(per_cat, key=per_cat.get)
        p['cat'] = best
        p['catscore'] = per_cat[best]
        p['cats'] = per_cat
        p['aes'] = float(row[-2] - row[-1])
        p['score'] = p['aes'] + (0.06 if (p['trip'], p['id']) in favs else 0.0)
    print(f'Classified {len(kept)} photos '
          f'({len(pool) - len(kept)} without thumbnails skipped)')
    return kept


def load_favourites():
    """(trip, id) pairs from any existing hand-curated pick files."""
    favs = set()

    def walk(v):
        if isinstance(v, dict):
            if isinstance(v.get('trip'), str) and isinstance(v.get('id'), str):
                favs.add((v['trip'], v['id']))
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for name in ('gallery_highlights.json', 'tile_covers.json', 'bridge_photo_picks.json'):
        f = ROOT / 'config' / name
        if f.exists():
            try:
                walk(json.loads(f.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
    return favs


# ---------------------------------------------------------------- sessions

def parse_ts(s):
    try:
        return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None


def assign_sessions(pool):
    """Visit sessions per trip (<=2 km and <=90 min apart); sets p['session']."""
    def dkm(a, b):
        if a['lat'] is None or b['lat'] is None:
            return 0.0
        k = math.cos(math.radians((a['lat'] + b['lat']) / 2))
        return math.hypot((a['lat'] - b['lat']) * 111.32, (a['lon'] - b['lon']) * 111.32 * k)

    sid = 0
    prev = None
    for p in sorted(pool, key=lambda r: (r['trip'], r['ts'])):
        if prev is None or p['trip'] != prev['trip'] or dkm(p, prev) > 2.0 or (
                parse_ts(p['ts']) and parse_ts(prev['ts']) and
                abs((parse_ts(p['ts']) - parse_ts(prev['ts'])).total_seconds()) > 5400):
            sid += 1
        p['session'] = sid
        prev = p


# ---------------------------------------------------------------- selection

# Near-duplicate suppression. A carousel should not spend two slots on the
# same frame. Calibrated on real pairs: identical frames score ~0.97-1.0
# whatever the gap, while "same subject, seconds apart" (a bridge with and
# without a person, two frames of one camel) sits ~0.92-0.96 and is always
# within a few minutes. Genuinely different photos of the same subject taken
# at different stops stay below that or are far apart in time, so they live.
DUP_ALWAYS = 0.965     # visually the same image, drop regardless of time
DUP_BURST = 0.92       # same moment: only a duplicate when shot close together
DUP_BURST_MIN = 5      # minutes


def _embs_for_dup():
    return globals().get('_EMB_INDEX') or {}


def is_near_dup(cand, chosen):
    """Is cand a near-duplicate of anything already picked?"""
    embs = _embs_for_dup()
    e = embs.get(key_of(cand))
    if e is None:
        return False
    ct = parse_ts(cand.get('ts') or '')
    for p in chosen:
        o = embs.get(key_of(p))
        if o is None:
            continue
        sim = float(np.dot(e.astype(np.float32), o.astype(np.float32)))
        if sim >= DUP_ALWAYS:
            return True
        if sim >= DUP_BURST and ct:
            pt = parse_ts(p.get('ts') or '')
            if pt and abs((ct - pt).total_seconds()) <= DUP_BURST_MIN * 60:
                return True
    return False


def dominant_orientation(cands):
    """Carousels must not mix orientations: keep the majority side."""
    land = [p for p in cands if (p.get('ar') or 1.5) >= 1]
    port = [p for p in cands if (p.get('ar') or 1.5) < 1]
    return land if len(land) >= len(port) else port


def pick(cands, n=CAROUSEL, per_session=PER_SESSION):
    """Top-n by score with session-diversity; cover first, rest chronological.
    per_session=None disables the diversity cap (single-place groups)."""
    cands = dominant_orientation(cands)
    out, used = [], {}
    for p in sorted(cands, key=lambda r: -r['score']):
        if per_session is not None and used.get(p['session'], 0) >= per_session:
            continue
        if is_near_dup(p, out):
            continue
        out.append(p)
        used[p['session']] = used.get(p['session'], 0) + 1
        if len(out) >= n:
            break
    if not out:
        return out
    cover = max(out, key=lambda r: r['score'])
    rest = sorted([p for p in out if p is not cover], key=lambda r: r['ts'])
    return [cover] + rest


def ref(p):
    r = {'trip': p['trip'], 'id': p['id']}
    if isinstance(p.get('ar'), (int, float)):
        r['ar'] = p['ar']
    return r


def post_id(*parts):
    return 'a' + hashlib.sha1('|'.join(parts).encode()).hexdigest()[:10]


def emit(groups, theme, name, cands, note, per_session=PER_SESSION):
    """Split a candidate group into public/private pools and emit post(s)."""
    for pub, suffix in ((True, ''), (False, ' (private)')):
        subset = [p for p in cands if p['public'] == pub]
        if len(subset) < MIN_POST:
            continue
        photos = pick(subset, per_session=per_session)
        if len(photos) < 6:
            continue
        groups.append({
            'id': post_id(theme, name, 'pub' if pub else 'priv'),
            'name': name + suffix,
            'theme': theme,
            'note': f'{note}: {len(subset)} candidates, '
                    f'{"public" if pub else "private"} pool',
            'photos': [ref(p) for p in photos],
            '_n': len(subset),
        })


# ---------------------------------------------------------------- themes

def build_geo_posts(groups, pool):
    prov_index = ProvinceIndex(PROVINCES_GEOJSON)
    interesting = [p for p in pool if p['cat'] not in BORING]
    by_prov = {}
    for p in interesting:
        if p['lat'] is None:
            continue
        prov = prov_index.lookup(p['lat'], p['lon'])
        p['province'] = prov
        if prov:
            by_prov.setdefault(prov, []).append(p)
    for prov, cands in sorted(by_prov.items(), key=lambda kv: -len(kv[1])):
        emit(groups, 'province', prov, cands, 'China province')

    by_loc = {}
    for p in interesting:
        if p['loc']:
            by_loc.setdefault(p['loc'], []).append(p)
    for loc, cands in sorted(by_loc.items(), key=lambda kv: -len(kv[1])):
        if len(cands) < MIN_POST:
            continue
        # one place = one or two sessions, so the diversity cap has to go
        emit(groups, 'place', loc, cands, 'named place', per_session=None)
    return by_prov


def build_theme_posts(groups, pool, by_prov):
    for theme, cats in THEME_CATS.items():
        cands = [p for p in pool if p['cat'] in cats]
        title = {'industrial': 'Energy & industry',
                 'nature': 'Landscapes', 'wildlife': 'Wildlife & animals'}[theme]
        emit(groups, theme, title, cands, f'{theme} (all trips)')
        if theme == 'wildlife':
            continue
        # per-province variants for the biggest provinces in this theme
        per = {}
        for p in cands:
            prov = p.get('province')
            if prov:
                per.setdefault(prov, []).append(p)
        for prov, sub in sorted(per.items(), key=lambda kv: -len(kv[1]))[:3]:
            emit(groups, theme, f'{prov} {title.lower()}', sub, f'{theme} in {prov}')


# ---------------------------------------------------------------- story / me

def load_me_sets():
    """(camera_set, phone_set) of (trip, id) photos containing me, from the
    face index + the person labels picked in the people UI."""
    people = json.loads((ROOT / 'local_browse' / 'people.json').read_text())
    clusters = json.loads((ROOT / 'local_browse' / 'clusters.json').read_text())['clusters']
    me = set(people.get('me') or [])
    fids = set()
    for c in clusters:
        if c['id'] in me:
            fids.update(c['face_ids'])
    cam, pho = set(), set()
    if not fids:
        return cam, pho
    con = sqlite3.connect(ROOT / 'local_browse' / 'face_index.sqlite')
    q = f"SELECT source, img FROM faces WHERE id IN ({','.join(map(str, fids))})"
    for src, img in con.execute(q):
        parts = img.split('/')
        t, pid = parts[0], parts[-1].rsplit('.', 1)[0]
        (cam if src == 'camera' else pho).add((t, pid))
    return cam, pho


def load_phone_photos():
    """All local phone-library photos: [{trip(phone-...), id, ts, ar}]."""
    out = []
    if not PHONE_TRIPS.exists():
        return out
    for mf in sorted(PHONE_TRIPS.glob('phone-*/manifest.json')):
        try:
            m = json.loads(mf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for ph in m.get('photos', []):
            out.append({'trip': mf.parent.name, 'id': ph['id'],
                        'ts': ph.get('timestamp') or '', 'ar': ph.get('ar')})
    return out


def build_story_posts(groups, pool):
    me_cam, me_pho = load_me_sets()
    if not me_cam and not me_pho:
        print('No face labels found (local_browse/people.json), skipping story posts')
        return
    # phone dirs on the NAS lack the site's phone- prefix
    me_phone_keys = {('phone-' + t, i) for t, i in me_pho}
    phone_lib = load_phone_photos()
    phone_me = [p for p in phone_lib if (p['trip'], p['id']) in me_phone_keys]
    print(f'Story: {len(me_cam)} camera / {len(me_pho)} phone photos of me '
          f'({len(phone_me)} phone matched in local library)')

    by_trip = {}
    for p in pool:
        by_trip.setdefault(p['trip'], []).append(p)

    for trip, photos in sorted(by_trip.items()):
        me_here = [p for p in photos if (p['trip'], p['id']) in me_cam]
        if not me_here:
            continue
        me_sessions = {p['session'] for p in me_here}
        if len(me_sessions) < 2:
            continue
        # carousel: per me-session, the best me shot + the best scenery shot
        for pub in (True, False):
            sel = []
            for s in sorted(me_sessions):
                sess_me = [p for p in me_here if p['session'] == s and p['public'] == pub]
                sess_all = [p for p in photos
                            if p['session'] == s and p['public'] == pub
                            and (p['trip'], p['id']) not in me_cam
                            and p['cat'] not in BORING]
                if sess_me:
                    sel.append(max(sess_me, key=lambda r: r['score']))
                if sess_all:
                    sel.append(max(sess_all, key=lambda r: r['score']))
            sel = dominant_orientation(sel)
            deduped = []
            for p in sorted(sel, key=lambda r: -r['score']):
                if not is_near_dup(p, deduped):
                    deduped.append(p)
            sel = deduped
            if len(sel) < 6:      # stories are the point; lower bar than MIN_POST
                continue
            sel = sorted(sel, key=lambda r: -r['score'])[:CAROUSEL]
            sel = sorted(sel, key=lambda r: r['ts'])
            name = f"Story: {photos[0]['trip_name']}" + ('' if pub else ' (private)')
            post = {
                'id': post_id('story', trip, 'pub' if pub else 'priv'),
                'name': name,
                'theme': 'story',
                'note': f'behind-the-scenes, {len(me_here)} shots of you across '
                        f'{len(me_sessions)} stops',
                'photos': [ref(p) for p in sel],
            }
            # phone bucket: phone shots of me taken during this trip
            t0 = min(p['ts'] for p in photos)
            t1 = max(p['ts'] for p in photos)
            near = [p for p in phone_me if t0[:10] <= p['ts'][:10] <= t1[:10]]
            near = sorted(near, key=lambda r: r['ts'])
            if len(near) > MAX_PHONE:
                step = len(near) / MAX_PHONE
                near = [near[int(i * step)] for i in range(MAX_PHONE)]
            if near:
                post['phone'] = [{'trip': p['trip'], 'id': p['id'],
                                  **({'ar': p['ar']} if isinstance(p.get('ar'), (int, float)) else {})}
                                 for p in near]
            groups.append(post)


# ---------------------------------------------------------------- free-text query

_prov_index_singleton = None


def _prov_index():
    global _prov_index_singleton
    if _prov_index_singleton is None:
        _prov_index_singleton = ProvinceIndex(PROVINCES_GEOJSON)
    return _prov_index_singleton


def prep_query_pool(pool, embs):
    """Light prep for query mode (no taxonomy pass): aesthetic score for
    tie-breaking + visit sessions for pick() diversity."""
    T = text_embed([AES_POS, AES_NEG])
    favs = load_favourites()
    kept = [p for p in pool if key_of(p) in embs]
    M = np.stack([embs[key_of(p)] for p in kept]).astype(np.float32)
    A = M @ T.T
    for p, row in zip(kept, A):
        p['aes'] = float(row[0] - row[1])
        p['score'] = p['aes'] + (0.06 if (p['trip'], p['id']) in favs else 0.0)
    assign_sessions(kept)
    return kept


def build_query_posts(pool, embs, query, n=CAROUSEL):
    """Curate post(s) for a free-text query like 'truck stops in china'.
    Recognised place names become hard geo filters; CLIP similarity to the
    full query ranks the rest. Returns ([posts], meta) - up to one public and
    one private post (never mixed)."""
    countries, provinces = parse_geo(query)
    cands = []
    for p in pool:
        if countries or provinces:
            ok = bool(countries) and p.get('country') in countries
            if not ok and provinces and p['lat'] is not None:
                if 'province' not in p:
                    p['province'] = _prov_index().lookup(p['lat'], p['lon'])
                ok = p.get('province') in provinces
            if not ok:
                continue
        cands.append(p)
    meta = {'countries': sorted(countries), 'provinces': sorted(provinces),
            'geo_pool': len(cands), 'matches': 0}
    if not cands:
        return [], meta
    T = text_embed([query])[0]
    M = np.stack([embs[key_of(p)] for p in cands]).astype(np.float32)
    sims = M @ T
    best = float(sims.max())
    floor = max(0.18, best - 0.10)
    scored = []
    for p, s in zip(cands, sims):
        if s < floor:
            continue
        q = dict(p)          # copy: don't clobber the shared pool's 'score'
        q['score'] = float(s) + 0.05 * p.get('aes', 0.0)
        scored.append(q)
    meta['matches'] = len(scored)
    meta['best_sim'] = round(best, 3)
    label = query.strip()
    label = (label[0].upper() + label[1:]) if label else 'Custom'
    posts = []
    for pub, suffix in ((True, ''), (False, ' (private)')):
        subset = [p for p in scored if p['public'] == pub]
        if len(subset) < 4:
            continue
        photos = pick(subset, n=n)
        posts.append({
            'id': post_id('query', query.lower(), 'pub' if pub else 'priv'),
            'name': label + suffix,
            'theme': 'custom',
            'note': f'query "{query}": {len(subset)} matches'
                    + (f", geo {'/'.join(sorted(countries) + sorted(provinces))}"
                       if countries or provinces else '')
                    + f", {'public' if pub else 'private'} pool",
            'photos': [ref(p) for p in photos],
        })
    return posts, meta


# ---------------------------------------------------------------- push

def load_env():
    import os
    env = dict(os.environ)
    f = ROOT / '.env.deploy'
    if f.exists():
        for line in f.read_text().splitlines():
            line = re.sub(r'^export\s+', '', line.strip())
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def push(doc_posts, target, mode='replace', which='auto'):
    env = load_env()
    pw = env.get('CF_POSTS_PASSWORD')
    if not pw:
        sys.exit('CF_POSTS_PASSWORD not set (env or .env.deploy)')
    if target == 'local':
        base = 'http://localhost:8788'
    elif target == 'prod':
        proj = env.get('CF_PAGES_PROJECT')
        if not proj:
            sys.exit('CF_PAGES_PROJECT not set for --push prod')
        base = f'https://{proj}.pages.dev'
    else:
        base = target.rstrip('/')
    url = f'{base}/api/posts' + ('?set=auto' if which == 'auto' else '')
    tok = hashlib.sha256(pw.encode()).hexdigest()
    cookies = [f'posts_auth={tok}']
    site_pw = env.get('CF_SITE_PASSWORD')
    if site_pw:
        cookies.insert(0, f'site_auth={hashlib.sha256(site_pw.encode()).hexdigest()}')
    hdrs = {'Cookie': '; '.join(cookies), 'User-Agent': 'auto-curate/1.0',
            'Content-Type': 'application/json'}

    def call(method, body=None, u=None):
        req = urllib.request.Request(u or url, headers=hdrs, method=method,
                                     data=body.encode() if body else None)
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    # Safety: a server running the OLD posts API ignores ?set=auto, so a PUT
    # would clobber the MANUAL drafts. If the two endpoints return the same
    # doc, this server does not know about the auto set -- refuse to push.
    current = call('GET')
    if which == 'auto':
        # Safety: a server running the OLD posts API ignores ?set=auto, so a
        # PUT would clobber the MANUAL drafts. Identical docs mean it doesn't
        # know about the auto set -- refuse to push.
        main_doc = call('GET', u=f'{base}/api/posts')
        if json.dumps(main_doc, sort_keys=True) == json.dumps(current, sort_keys=True):
            sys.exit(f'✗ {base} does not support ?set=auto yet (old API deployed) - '
                     'pushing would overwrite your manual drafts. Deploy the new '
                     'functions/api/posts.ts first (python3 deploy.py --skip-images).')
    if mode == 'append':
        new_ids = {g['id'] for g in doc_posts}
        keep = [p for p in current.get('posts', []) if p.get('id') not in new_ids]
        doc_posts = (doc_posts + keep)[:200]
    body = json.dumps({'baseVersion': current.get('version', 0), 'posts': doc_posts})
    res = call('PUT', body)
    label = 'auto posts' if which == 'auto' else 'posts'
    print(f'Pushed {len(doc_posts)} {label} -> {url} (version {res.get("version")})')


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--list', action='store_true', help='print the plan, save nothing')
    ap.add_argument('--push', metavar='TARGET',
                    help="upload to the site: 'local' (wrangler dev), 'prod', or a base URL")
    ap.add_argument('--query', metavar='TEXT',
                    help="curate ONE post from a free-text prompt (e.g. 'truck "
                         "stops in china') and append it to the auto set")
    ap.add_argument('--themes', help='comma list to keep (story,province,place,industrial,nature,wildlife)')
    ap.add_argument('--max-posts', type=int, default=120)
    ap.add_argument('--max-places', type=int, default=30,
                    help='cap on place posts (they dwarf every other theme)')
    args = ap.parse_args()

    pool = load_pool()
    embs = load_embeddings(pool)

    if args.query:
        kept = prep_query_pool(pool, embs)
        posts, meta = build_query_posts(kept, embs, args.query)
        print(f"Query '{args.query}': {meta['matches']} matches "
              f"(geo pool {meta['geo_pool']}, "
              f"countries {meta['countries'] or '-'}, provinces {meta['provinces'] or '-'})")
        for g in posts:
            print(f"  {g['name']:<50} {len(g['photos'])} photos")
        if not posts:
            sys.exit('No post emitted (too few matches).')
        if args.push:
            push(posts, args.push, mode='append')
        return

    pool = classify(pool, embs)
    assign_sessions(pool)

    groups = []
    build_story_posts(groups, pool)
    by_prov = build_geo_posts(groups, pool)
    build_theme_posts(groups, pool, by_prov)

    if args.themes:
        keep = {t.strip() for t in args.themes.split(',')}
        groups = [g for g in groups if g['theme'] in keep]

    # stable order: stories first, then by theme, biggest candidate pools first
    order = {'story': 0, 'province': 1, 'place': 2, 'industrial': 3, 'nature': 4, 'wildlife': 5}
    groups.sort(key=lambda g: (order.get(g['theme'], 9), -g.get('_n', 0), g['name']))
    places = [g for g in groups if g['theme'] == 'place']
    if len(places) > args.max_places:
        print(f'Trimming place posts {len(places)} -> {args.max_places} (--max-places)')
        drop = {g['id'] for g in places[args.max_places:]}
        groups = [g for g in groups if g['id'] not in drop]
    if len(groups) > args.max_posts:
        print(f'Trimming {len(groups)} -> {args.max_posts} posts (--max-posts)')
        # trim the tail themes' smallest pools first, keep stories always
        keep = [g for g in groups if g['theme'] == 'story']
        rest = sorted([g for g in groups if g['theme'] != 'story'],
                      key=lambda g: -g.get('_n', 0))[:args.max_posts - len(keep)]
        kept_ids = {g['id'] for g in keep} | {g['id'] for g in rest}
        groups = [g for g in groups if g['id'] in kept_ids]
    for g in groups:
        g.pop('_n', None)

    print(f'\n{len(groups)} auto posts:')
    for g in groups:
        extra = f" +{len(g['phone'])} phone" if g.get('phone') else ''
        print(f"  [{g['theme']:<10}] {g['name']:<44} {len(g['photos'])} photos{extra}")

    if args.list:
        return
    OUT_JSON.write_text(json.dumps({'posts': groups}, indent=1))
    print(f'\nSaved -> {OUT_JSON}')
    if args.push:
        push(groups, args.push)


if __name__ == '__main__':
    main()
