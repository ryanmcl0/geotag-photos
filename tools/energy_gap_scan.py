#!/usr/bin/env python3
"""Find energy/industrial photos that are NOT in any Energy & Infrastructure tile.

The completeness net for config/china_energy_sites.json. Two detectors over every
geotagged China photo:

  1. CLIP zero-shot: each photo scored against industrial prompt classes (factory,
     power station, solar farm, open-pit mine, dam, refinery, port, coal yard, ...)
     vs landscape/city/people negatives. Scores cached — re-runs only score new
     photos.
  2. Session grouping: photos are clustered into visit sessions (>2 km or >90 min
     apart = new session); a session is flagged if it has industrial-scoring
     photos and is not already claimed by an energy / bridges / roofs tile.

Output: a ranked report of flagged sessions (location, day label, example photo
ids + which prompt matched) to review by eye — add real finds to
config/china_energy_sites.json and rebuild. Run after processing each new trip:

    ./build_collections.py --collection china     # refresh tiles first
    python3 tools/energy_gap_scan.py
"""
import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_privacy  # noqa: E402

WEB_TRIPS = ROOT / 'web' / 'trips'
COLLECTION = ROOT / 'web' / 'collections' / 'china.all.json'
CACHE = ROOT / 'config' / '.energy_scan_cache.json'   # gitignored with config/*.json

POS = [
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
]
NEG = [
    'a mountain landscape', 'a desert with sand dunes', 'a city skyline at night',
    'a portrait of a person', 'a road through countryside', 'a lake or reservoir shore',
    'food in a restaurant', 'a village street with shops', 'a suspension bridge over a canyon',
    'grazing animals', 'a hotel room', 'a snowy mountain pass road',
]
BBOX = (18.0, 73.0, 54.0, 135.0)     # China-ish: same clamp as road_scan


def load_photos():
    photos = []
    for mf in sorted(WEB_TRIPS.glob('*/manifest.json')):
        manifest = photo_privacy.load_full_manifest(mf.parent)
        if not manifest:
            continue
        for ph in manifest.get('photos', []):
            lat, lon = ph.get('lat'), ph.get('lon')
            if lat is None or lon is None:
                continue
            if not (BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]):
                continue
            photos.append({'trip': mf.parent.name, 'id': ph['id'], 'lat': lat, 'lon': lon,
                           'ts': ph.get('timestamp') or '',
                           'building': (ph.get('building') or '').strip()})
    return photos


def sessions_of(photos):
    """Cluster into visit sessions: same trip, <=2 km and <=90 min apart."""
    def t(r):
        try:
            return datetime.strptime(r['ts'][:19], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return None

    def dkm(a, b):
        k = math.cos(math.radians((a['lat'] + b['lat']) / 2))
        return math.hypot((a['lat'] - b['lat']) * 111.32, (a['lon'] - b['lon']) * 111.32 * k)

    out, cur = [], []
    for r in sorted(photos, key=lambda r: (r['trip'], r['ts'])):
        if cur:
            p = cur[-1]
            gap = None
            if t(r) and t(p):
                gap = abs((t(r) - t(p)).total_seconds()) / 60
            if r['trip'] != p['trip'] or dkm(r, p) > 2.0 or (gap is not None and gap > 90):
                out.append(cur)
                cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return out


def claimed_keys():
    """Photos already in a place tile (energy / bridges / roofs)."""
    data = json.loads(COLLECTION.read_text())
    claimed = set()
    for tile in data.get('tiles', []):
        if tile.get('id') not in ('energy', 'bridges', 'roofs'):
            continue
        subs = (tile.get('subtiles') or []) + \
               [s for sec in (tile.get('sections') or []) for s in sec.get('subtiles', [])]
        for s in subs:
            for p in s.get('photos') or []:
                claimed.add((p['trip'], p['id']))
    return claimed


def clip_scores(photos, threshold):
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = [p for p in photos if f"{p['trip']}/{p['id']}" not in cache
            and (ROOT / 'hosted-photos' / p['trip'] / 'thumbnails' / f"{p['id']}.webp").exists()]
    if todo:
        print(f'CLIP-scoring {len(todo)} new photos ({len(cache)} cached)...')
        dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
        model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').to(dev).eval()
        proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
        with torch.no_grad():
            ti = proc(text=POS + NEG, return_tensors='pt', padding=True).to(dev)
            tf = model.get_text_features(**ti)
            if hasattr(tf, 'pooler_output'):
                tf = tf.pooler_output
            tf = tf / tf.norm(dim=-1, keepdim=True)
        for i in range(0, len(todo), 64):
            batch = todo[i:i + 64]
            imgs = [Image.open(ROOT / 'hosted-photos' / p['trip'] / 'thumbnails' / f"{p['id']}.webp").convert('RGB')
                    for p in batch]
            with torch.no_grad():
                ii = proc(images=imgs, return_tensors='pt').to(dev)
                f = model.get_image_features(**ii)
                if hasattr(f, 'pooler_output'):
                    f = f.pooler_output
                f = f / f.norm(dim=-1, keepdim=True)
                sims = (f @ tf.T).cpu()
            for p, row in zip(batch, sims):
                cache[f"{p['trip']}/{p['id']}"] = [round(row[:len(POS)].max().item(), 4),
                                                  round(row[len(POS):].max().item(), 4),
                                                  int(row[:len(POS)].argmax())]
            if i % 640 == 0:
                CACHE.write_text(json.dumps(cache))
                print(f'  {i + len(batch)}/{len(todo)}', flush=True)
        CACHE.write_text(json.dumps(cache))
    hits = {}
    for p in photos:
        v = cache.get(f"{p['trip']}/{p['id']}")
        if v and v[0] > v[1] and v[0] > threshold:
            hits[(p['trip'], p['id'])] = v
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--threshold', type=float, default=0.26, help='CLIP positive-score floor (default 0.26)')
    ap.add_argument('--top', type=int, default=40, help='sessions to report (default 40)')
    args = ap.parse_args()

    photos = load_photos()
    claimed = claimed_keys()
    hits = clip_scores(photos, args.threshold)
    print(f'{len(photos)} photos, {len(claimed)} already in place tiles, {len(hits)} CLIP-industrial')

    flagged = []
    for sess in sessions_of(photos):
        keys = [(r['trip'], r['id']) for r in sess]
        if sum(1 for k in keys if k in claimed) / len(keys) >= 0.5:
            continue                       # session already substantially claimed
        sess_hits = [(r, hits[k]) for r, k in zip(sess, keys) if k in hits]
        if not sess_hits:
            continue
        best = max(sess_hits, key=lambda x: x[1][0])
        flagged.append({'n_hits': len(sess_hits), 'n': len(sess),
                        'lat': round(sum(r['lat'] for r in sess) / len(sess), 4),
                        'lon': round(sum(r['lon'] for r in sess) / len(sess), 4),
                        'trip': sess[0]['trip'], 'building': sess[0]['building'][:44],
                        'example': best[0]['id'], 'score': best[1][0],
                        'prompt': POS[best[1][2]]})
    flagged.sort(key=lambda f: (-f['n_hits'], -f['score']))
    if not flagged:
        print('\nNo unclaimed industrial-looking sessions - the energy facet is complete.')
        return
    print(f'\n{len(flagged)} unclaimed sessions look industrial - review these '
          f'(add real finds to config/china_energy_sites.json):\n')
    for f in flagged[:args.top]:
        print(f"  {f['n_hits']:2d}/{f['n']:3d} hits  {f['lat']},{f['lon']}  {f['trip']}"
              f"  | {f['building']}\n         e.g. {f['example']}  {f['score']:.3f}  [{f['prompt'][:52]}]")


if __name__ == '__main__':
    main()
