"""Assemble the standalone A/B folder from the benchmark sample + per-photo picks.
Usage: build_ab.py <work_dir> <out_dir>"""
import json, shutil, sys
from pathlib import Path
from PIL import Image

S = Path(__file__).parent
B = Path(sys.argv[1])   # work dir used by make_sample/bench/search
OUT = Path(sys.argv[2])
(OUT / 'img').mkdir(parents=True, exist_ok=True)
(OUT / 'thumbs').mkdir(exist_ok=True)
(OUT / 'bench').mkdir(exist_ok=True)

rows = json.loads((B / 'sample.json').read_text())
picks = json.loads((B / 'picks.json').read_text())
FAM = {'avif444_strict': 'avif444_iq_s4', 'avif444_score': 'avif444_iq_s4', 'avif420_strict': 'avif420_iq_s4'}
VARIANTS = [
    ('current', 'Current WebP', 'What the site serves today'),
    ('avif444_strict', 'AVIF 4:4:4 strict', 'Full colour resolution; no measure below Current'),
    ('avif420_strict', 'AVIF 4:2:0 strict', 'Half colour resolution like WebP; no measure below Current'),
    ('avif444_score', 'AVIF 4:4:4 score match', 'Full colour resolution; perceptual score matched only'),
]
photos = []
for row in rows:
    k = row['key']
    d = OUT / 'img' / k
    d.mkdir(exist_ok=True)
    ref = Image.open(B / 'ref' / f'{k}.png')
    if not (d / 'master.webp').exists():
        ref.save(d / 'master.webp', 'WEBP', lossless=True, quality=70, method=4)
    if not (OUT / 'thumbs' / f'{k}.jpg').exists():
        t = ref.copy(); t.thumbnail((480, 480)); t.save(OUT / 'thumbs' / f'{k}.jpg', quality=82)
    shutil.copyfile(row['hosted'], d / 'current.webp')
    h = picks[k]['hosted']
    vs = [dict(id='current', file=f'img/{k}/current.webp', bytes=h['bytes'], q=90 if row['hosted_bytes'] == row['q90'] else 85,
               ssimu2=h['ssimu2'], psnr_y=h['psnr_y'], psnr_c=(h['psnr_cb'] + h['psnr_cr']) / 2)]
    for vid, _, _ in VARIANTS[1:]:
        fam = FAM[vid]
        p = picks[k][vid]
        if p is None:
            raise SystemExit(f'{k} {vid}: no passing quality found')
        src = B / 'enc' / f"{k}__{fam}_q{p['q']}.avif"
        shutil.copyfile(src, d / f'{vid}.avif')
        vs.append(dict(id=vid, file=f'img/{k}/{vid}.avif', bytes=p['bytes'], q=p['q'], ssimu2=p['ssimu2'],
                       psnr_y=p['psnr_y'], psnr_c=(p['psnr_cb'] + p['psnr_cr']) / 2))
    photos.append(dict(key=k, trip=row['slug'], id=row['id'], w=ref.size[0], h=ref.size[1],
                       master=f'img/{k}/master.webp', thumb=f'thumbs/{k}.jpg', variants=vs))

data = dict(variants=[dict(id=i, label=l, note=n) for i, l, n in VARIANTS], photos=photos)
(OUT / 'data.js').write_text('window.AB = ' + json.dumps(data) + ';\n')
shutil.copyfile(S / 'ab_index.html', OUT / 'index.html')
for f in ('bench.py', 'search.py', 'analyze.py', 'make_sample.py', 'build_ab.py', 'ab_index.html'):
    shutil.copyfile(S / f, OUT / 'bench' / f)
for f in ('sample.json', 'results.jsonl', 'picks.json'):
    shutil.copyfile(B / f, OUT / 'bench' / f)

cur = sum(p['variants'][0]['bytes'] for p in photos)
print(f'photos {len(photos)}')
for i, (vid, label, _) in enumerate(VARIANTS):
    b = sum(p['variants'][i]['bytes'] for p in photos)
    ds = sum(p['variants'][i]['ssimu2'] - p['variants'][0]['ssimu2'] for p in photos) / len(photos)
    dy = sum(p['variants'][i]['psnr_y'] - p['variants'][0]['psnr_y'] for p in photos) / len(photos)
    dc = sum(p['variants'][i]['psnr_c'] - p['variants'][0]['psnr_c'] for p in photos) / len(photos)
    qs = sorted(p['variants'][i]['q'] for p in photos)
    print(f'  {label:24} {b/1e6:6.1f} MB {b/cur-1:+6.1%}  score {ds:+.2f}  luma {dy:+.2f} dB  colour {dc:+.2f} dB  q {qs[0]}..{qs[len(qs)//2]}..{qs[-1]}')
