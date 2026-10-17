#!/usr/bin/env python3
"""Find candidate photos for the phone library's Categories tab.

The categories (tools/build_phone_categories.py) are hand-curated, but nobody
can eyeball 13,000 phone photos. This tool narrows them down; the final picks
are still a human (or Claude) looking at the shortlist and writing them into
local_browse/phone_categories.json.

Two passes over the phone library, both cached and resumable:

  ocr    Apple Vision OCR (Chinese + English) of every display WebP, via the
         Swift helper in tools/phone_ocr/ (compiled on first use).
  clip   CLIP image embeddings of every thumbnail, for things text can't find
         (a car wearing a fake badge in a font OCR misreads).

Then:

  list translations   translation-app screenshots (Google Translate / Lens,
                      WeChat "Translated by Weixin"...), with their text
  list english        ordinary photos with English text in them: signs,
                      menus, packaging. Chinglish lives here.
  list match --pattern REGEX
                      photos whose OCR text (either language) matches REGEX
  list signs          photos CLIP thinks are signs, shopfronts, menus or labels
                      but where OCR found no English, for the ones it misread
  list cars           photos ranked by CLIP "a car" score, plus any whose text
                      names a car brand
  sheet OUT KEY...    contact sheet (JPEG) of the given photos, for review

`list` skips photos already placed in a category unless --all is given, so a
rerun after new trips are added shows only what is new.

Keys are "<phone trip id>/<photo id>", exactly the {trip, id} refs
phone_categories.json uses.

    python3 tools/phone_text_scan.py ocr                 # default: China trips
    venv/bin/python tools/phone_text_scan.py clip         # needs torch + transformers
    python3 tools/phone_text_scan.py list translations > /tmp/tr.txt
    venv/bin/python tools/phone_text_scan.py sheet /tmp/s.jpg phone-2026-.../IMG_4965 ...

Caches live in local_browse/phone_text_scan/ (git-ignored, like the rest of
local_browse). The NAS is an SMB mount and wedges under heavy parallel reads,
so keep --workers low (3 is stable; 5 OCR workers plus a CLIP loader was not).
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / 'web' / 'phone' / 'trips'
CACHE = ROOT / 'local_browse' / 'phone_text_scan'
SWIFT_SRC = ROOT / 'tools' / 'phone_ocr' / 'ocr.swift'
OCR_BIN = CACHE / 'ocr'
CATEGORIES = ROOT / 'local_browse' / 'phone_categories.json'

# Phone trips that were (at least partly) in China. Asia 23 / Asia 24 pt2 have
# no country tags in the index, so this is an explicit list, not a filter.
CHINA_TRIPS = [
    'phone-2026-07-26-china-summer-2026',
    'phone-2026-02-03-26-china-cny',
    'phone-2025-01-25-china',
    'phone-2024-china-zhejiang-shanghai-jiangsu-jiangxi-xinjiang',
    'phone-2024-asia-24-pt2',
    'phone-2024-asia-24',
    'phone-2023-asia-23',
]

# Translation apps leave their UI chrome in the screenshot; any of these marks it.
TRANSLATE_UI = re.compile(
    r'translat|翻译|Detect language|Chinese ?[（(]|Pleco|DeepL|有道|Google Lens|Weixin', re.I)
CAR_WORDS = re.compile(
    r'\b(auto|motors?|rover|range|benz|bmw|audi|porsche|ferrari|lambo\w*|toyota|honda|'
    r'nissan|byd|haval|geely|chery|wuling|baojun|hongqi|jeep|tesla|xiaomi|tank|suv)\b', re.I)
CAR_PROMPTS = ['a photo of a car', 'a close-up of a car badge or logo', 'the front of an SUV',
               'the back of a car with its model name']
NOT_CAR_PROMPTS = ['a photo of a street', 'a photo of a landscape', 'a screenshot of a phone',
                   'a photo of food', 'a car dashboard seen from the driver seat']
SIGN_PROMPTS = ['a sign with text on it', 'a shop front with a sign', 'a warning sign',
                'a restaurant menu', 'a product label with writing']
NOT_SIGN_PROMPTS = ['a photo of a landscape', 'a photo of a person', 'a screenshot of a phone',
                    'a photo of a car', 'a photo of a building']


def photos(trip_ids):
    """[(key, display_path, source_filename, ar)] for the given phone trips."""
    out = []
    for t in trip_ids:
        m = json.loads((TRIPS / t / 'manifest.json').read_text())
        disp = os.path.realpath(TRIPS / t / 'display')
        for p in m['photos']:
            out.append((f"{t}/{p['id']}", f"{disp}/{p['id']}.webp",
                        p.get('source_filename', ''), p.get('ar') or 1))
    return out


def load_ocr():
    rows = {}
    for f in glob.glob(str(CACHE / 'ocr-*.jsonl')):
        for line in open(f):
            try:
                o = json.loads(line)
            except ValueError:
                continue  # a worker killed mid-line
            rows[o['k']] = o['r']
    return rows


def ensure_ocr_binary():
    if OCR_BIN.exists() and OCR_BIN.stat().st_mtime >= SWIFT_SRC.stat().st_mtime:
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    print('compiling', SWIFT_SRC.relative_to(ROOT))
    subprocess.run(['swiftc', '-O', str(SWIFT_SRC), '-o', str(OCR_BIN)], check=True)


def cmd_ocr(args):
    ensure_ocr_binary()
    done = load_ocr()
    todo = [p for p in photos(args.trips) if p[0] not in done]
    print(f'{len(done)} cached, {len(todo)} to OCR with {args.workers} workers')
    if not todo:
        return
    # Each worker appends to its own file; a rerun picks up where it stopped.
    run = len(glob.glob(str(CACHE / 'ocr-*.jsonl')))
    procs = []
    for w in range(args.workers):
        lst = CACHE / f'todo-{w}.txt'
        lst.write_text(''.join(f'{k}\t{path}\n' for k, path, *_ in todo[w::args.workers]))
        out = open(CACHE / f'ocr-{run + w:03d}.jsonl', 'w')
        procs.append(subprocess.Popen([str(OCR_BIN), str(lst)], stdout=out, stderr=subprocess.DEVNULL))
    for p in procs:
        p.wait()
    print(f'done: {len(load_ocr())} photos have OCR')


def cmd_clip(args):
    import numpy as np
    import torch
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    path = CACHE / 'clip.npz'
    keys, embs = [], []
    if path.exists():
        z = np.load(path)
        keys, embs = list(z['keys']), [z['embs']]
    have = set(keys)
    todo = [p for p in photos(args.trips) if p[0] not in have]
    print(f'{len(have)} cached, {len(todo)} to embed')
    if not todo:
        return
    dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').to(dev).eval()
    proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')

    def load(p):
        try:
            return Image.open(p[1].replace('/display/', '/thumbs/')).convert('RGB')
        except OSError:
            return None

    pool = ThreadPoolExecutor(args.workers)
    for i in range(0, len(todo), 64):
        chunk = todo[i:i + 64]
        ok = [(p[0], im) for p, im in zip(chunk, pool.map(load, chunk)) if im is not None]
        if not ok:
            continue
        with torch.no_grad():
            f = model.get_image_features(**proc(images=[im for _, im in ok], return_tensors='pt').to(dev))
            if hasattr(f, 'pooler_output'):
                f = f.pooler_output
            f = f / f.norm(dim=-1, keepdim=True)
        keys += [k for k, _ in ok]
        embs.append(f.cpu().numpy().astype(np.float16))
        if i % 1280 == 0:
            print(f'  {i}/{len(todo)}', flush=True)
            np.savez(path, keys=np.array(keys), embs=np.concatenate(embs))  # checkpoint
    np.savez(path, keys=np.array(keys), embs=np.concatenate(embs))
    print(f'done: {len(keys)} embedded')


def placed():
    if not CATEGORIES.exists():
        return set()
    cats = json.loads(CATEGORIES.read_text()).get('categories', [])
    return {f"{r['trip']}/{r['id']}" for c in cats for r in c.get('photos', [])}


def compact(rows, latin_only=False, limit=700):
    lines = [x['t'] for x in rows if x['c'] >= 0.5]
    if latin_only:
        lines = [l for l in lines if re.search(r'[A-Za-z]{3,}', l)]
    return ' | '.join(lines)[:limit]


def cmd_list(args):
    meta = {p[0]: p for p in photos(args.trips)}
    ocr = load_ocr()
    skip = set() if args.all else placed()
    keys = [k for k in meta if k not in skip]

    if args.kind == 'translations':
        for k in keys:
            text = compact(ocr.get(k, []))
            if TRANSLATE_UI.search(text):
                print(f'## {k}\n{text}\n')

    elif args.kind == 'english':
        for k in keys:
            src, ar = meta[k][2], meta[k][3]
            shot = src.lower().endswith('.png') or src.lower().startswith('screenshot') or 0.40 <= ar <= 0.50
            rows = ocr.get(k, [])
            if shot or TRANSLATE_UI.search(compact(rows)):
                continue
            text = compact(rows, latin_only=True, limit=260)
            if len(re.findall(r'[A-Za-z]{3,}', text)) >= args.min_words:
                print(f'{k}: {text}')

    elif args.kind == 'match':
        # Free-text search over the OCR (Chinese and English), for theme
        # categories that are easiest to find by what the photos say.
        if not args.pattern:
            sys.exit('list match needs --pattern REGEX')
        pat = re.compile(args.pattern, re.I)
        for k in keys:
            text = compact(ocr.get(k, []), limit=args.limit)
            if pat.search(text):
                print(f'## {k}\n{text}\n')

    elif args.kind == 'signs':
        # Signs whose English OCR couldn't read (small, stylised, at an angle):
        # anything OCR found English in is already covered by `list english`.
        for k, s in clip_ranked(SIGN_PROMPTS, NOT_SIGN_PROMPTS):
            if k in skip or k not in meta or s < args.min_score:
                continue
            if not re.search(r'[A-Za-z]{3,}', compact(ocr.get(k, []))):
                print(f'{s:+.3f} {k}')

    elif args.kind == 'cars':
        shown = set()
        for k, s in clip_ranked(CAR_PROMPTS, NOT_CAR_PROMPTS):
            if k in skip or k not in meta or s < args.min_score:
                continue
            shown.add(k)
            print(f'{s:+.3f} {k}  {compact(ocr.get(k, []), latin_only=True, limit=120)}')
        for k in keys:  # text-only hits CLIP didn't rank as cars
            text = compact(ocr.get(k, []), latin_only=True, limit=120)
            if k not in shown and CAR_WORDS.search(text):
                print(f'  text {k}  {text}')


def clip_ranked(want, against):
    """[(key, score)] best first: how much closer each photo is to the best
    `want` prompt than to the best `against` prompt."""
    import numpy as np
    import torch
    from transformers import CLIPModel, CLIPProcessor
    z = np.load(CACHE / 'clip.npz')
    model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32').eval()
    proc = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
    with torch.no_grad():
        t = model.get_text_features(**proc(text=want + against, return_tensors='pt', padding=True))
        if hasattr(t, 'pooler_output'):
            t = t.pooler_output
        t = (t / t.norm(dim=-1, keepdim=True)).numpy()
    sims = z['embs'].astype(np.float32) @ t.T
    score = sims[:, :len(want)].max(1) - sims[:, len(want):].max(1)
    return sorted(zip(z['keys'], score), key=lambda x: -x[1])


def cmd_sheet(args):
    from PIL import Image, ImageDraw, ImageFont
    meta = {p[0]: p for p in photos(sorted({k.split('/')[0] for k in args.keys}))}
    cell, cols = args.cell, min(4, len(args.keys))
    rows = (len(args.keys) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * cell, rows * (cell + 22)), 'white')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=16)
    for i, k in enumerate(args.keys):
        path = meta[k][1] if args.display else meta[k][1].replace('/display/', '/thumbs/')
        try:
            im = Image.open(path).convert('RGB')
            im.thumbnail((cell, cell))
        except OSError:
            im = Image.new('RGB', (cell, cell), 'gray')
        x, y = (i % cols) * cell, (i // cols) * (cell + 22)
        sheet.paste(im, (x + (cell - im.width) // 2, y + 22))
        draw.text((x + 4, y + 2), f'{i}: {k.split("/")[-1]}', fill='black', font=font)
    sheet.save(args.out, quality=85)
    print(args.out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--trips', nargs='+', default=CHINA_TRIPS, help='phone trip ids (default: China)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('ocr')
    p.add_argument('--workers', type=int, default=3)
    p = sub.add_parser('clip')
    p.add_argument('--workers', type=int, default=3, help='thumbnail loader threads')
    p = sub.add_parser('list')
    p.add_argument('kind', choices=['translations', 'english', 'match', 'signs', 'cars'])
    p.add_argument('--all', action='store_true', help='include photos already in a category')
    p.add_argument('--min-words', type=int, default=2, help='english: Latin words needed')
    p.add_argument('--min-score', type=float, default=0.0, help='signs/cars: CLIP margin cut-off')
    p.add_argument('--pattern', help='match: regex searched in each photo\'s OCR text')
    p.add_argument('--limit', type=int, default=500, help='match: characters of text to print')
    p = sub.add_parser('sheet')
    p.add_argument('out')
    p.add_argument('keys', nargs='+')
    p.add_argument('--cell', type=int, default=600)
    p.add_argument('--display', action='store_true', help='use display WebPs (sharper, slower)')
    args = ap.parse_args()
    {'ocr': cmd_ocr, 'clip': cmd_clip, 'list': cmd_list, 'sheet': cmd_sheet}[args.cmd](args)


if __name__ == '__main__':
    main()
