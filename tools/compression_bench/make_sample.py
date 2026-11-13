"""Build a benchmark sample: N random photos across all trips.
For each: locate the master, record its properties, save the resized (pipeline-identical)
reference as lossless PNG, and re-encode at q85/q90 with the pipeline's exact settings to
check what quality the hosted file really is. Reads masters only; writes only to the out dir."""
import json, random, sys, io, os
from pathlib import Path
from PIL import Image, ImageCms

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tools'))
from process_trip import _fit_longest

out = Path(sys.argv[1]); n = int(sys.argv[2]); seed = int(sys.argv[3]) if len(sys.argv) > 3 else 7
(out / 'ref').mkdir(parents=True, exist_ok=True)

pool = []
for m in sorted((REPO / 'web/trips').glob('*/manifest.json')):
    try:
        d = json.loads(m.read_text())
    except Exception:
        continue
    ec = m.parent / 'exif_cache.json'
    idx = {}
    if ec.exists():
        for k in json.loads(ec.read_text()):
            kp = Path(k)
            if kp.suffix.lower() in ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp'):
                idx.setdefault(kp.name, k)
    full = m.parent / 'manifest.all.json'
    if full.exists():
        d = json.loads(full.read_text())
    for p in d.get('photos', []):
        pool.append((m.parent.name, idx.get(p.get('source_filename', '')), p))
random.Random(seed).shuffle(pool)
print('pool', len(pool), file=sys.stderr)

rows = []
for slug, root, p in pool:
    if len(rows) >= n:
        break
    hosted = REPO / 'hosted-photos' / slug / p['display']
    if not hosted.exists() or not root:
        continue
    src = Path(root)
    if not src.exists():
        continue
    try:
        with Image.open(src) as img:
            icc = img.info.get('icc_profile')
            desc = None
            if icc:
                try:
                    desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
                except Exception:
                    desc = '?'
            orient = (img.getexif() or {}).get(0x0112)
            fmt, mode, size = img.format, img.mode, img.size
            subs = None
            if fmt == 'JPEG':
                from PIL import JpegImagePlugin
                subs = JpegImagePlugin.get_sampling(img)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            r = _fit_longest(img, 2160)
            if r.mode != 'RGB':
                r = r.convert('RGB')
            key = f'{len(rows):03d}'
            r.save(out / 'ref' / f'{key}.png', compress_level=1)
            sz = {}
            for q in (80, 85, 90):
                b = io.BytesIO(); r.save(b, 'WEBP', quality=q, method=6); sz[q] = b.tell()
    except Exception as e:
        print('skip', src, e, file=sys.stderr); continue
    with Image.open(hosted) as h:
        hsize = h.size
    row = dict(key=key, slug=slug, id=p['id'], src_fmt=fmt, src_mode=mode, src_size=size,
               src_bytes=src.stat().st_size, icc=desc, orient=orient, subs=subs,
               ref_size=r.size, hosted=str(hosted), hosted_bytes=hosted.stat().st_size,
               hosted_size=hsize, q80=sz[80], q85=sz[85], q90=sz[90])
    rows.append(row)
    print(key, slug[:28], fmt, size, desc, orient, subs, row['hosted_bytes'], sz, file=sys.stderr)

(out / 'sample.json').write_text(json.dumps(rows, indent=1))
import collections
print('icc:', collections.Counter(r['icc'] for r in rows))
print('orient:', collections.Counter(r['orient'] for r in rows))
print('fmt:', collections.Counter(r['src_fmt'] for r in rows))
print('subs:', collections.Counter(r['subs'] for r in rows))
print('src longest<=2160:', sum(max(r['src_size']) <= 2160 for r in rows))
h = sum(r['hosted_bytes'] for r in rows)
for q in ('q80', 'q85', 'q90'):
    s = sum(r[q] for r in rows)
    exact = sum(r[q] == r['hosted_bytes'] for r in rows)
    print(f'{q}: re-encode total {s/1e6:.1f} MB vs hosted {h/1e6:.1f} MB  ratio {s/h:.3f}  byte-size-identical {exact}/{len(rows)}')
