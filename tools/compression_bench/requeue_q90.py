"""Re-encode hosted display files that are still WebP q90 (Base Q 12) at q85 from their masters.
Usage: requeue_q90.py [--apply] slug [slug...]
Without --apply: only reports what it would do. Writes only into hosted-photos/<slug>/display.
Each master match is verified: the resized master must closely match the hosted file
(same size, PSNR >= 25 dB) or the photo is skipped."""
import json, sys, subprocess, re, io, os
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from process_trip import generate_display_image, _fit_longest

apply = '--apply' in sys.argv
slugs = [a for a in sys.argv[1:] if not a.startswith('--')]


def base_q(path):
    out = subprocess.run(['webpinfo', '-bitstream_info', str(path)], capture_output=True, text=True).stdout
    m = re.search(r'Base Q:\s*(\d+)', out)
    return int(m.group(1)) if m else None


def jobs_for(slug):
    t = REPO / 'web/trips' / slug
    idx = {}
    ec = t / 'exif_cache.json'
    for k in json.loads(ec.read_text()):
        idx.setdefault(Path(k).name, []).append(k)
    disp2src = {}
    for mf in ('manifest.full.json', 'manifest.all.json', 'manifest.json'):
        f = t / mf
        if f.exists():
            for p in json.loads(f.read_text()).get('photos', []):
                if p.get('display') and p.get('source_filename'):
                    disp2src.setdefault(p['display'], p['source_filename'])
    out = []
    for disp, srcname in sorted(disp2src.items()):
        hosted = REPO / 'hosted-photos' / slug / disp
        if hosted.exists():
            out.append((slug, str(hosted), idx.get(srcname, [])))
    return out


def work(job):
    slug, hosted, cands = job
    hosted = Path(hosted)
    q = base_q(hosted)
    if q is None or q > 14:  # already q85-ish (Base Q ~20) or unreadable
        return slug, 'skip_not_q90', hosted.name, 0, 0
    before = hosted.stat().st_size
    with Image.open(hosted) as h:
        ha = np.asarray(h.convert('RGB'), dtype=np.float32); hsize = h.size
    for c in cands:
        src = Path(c)
        if not src.exists():
            continue
        try:
            with Image.open(src) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                r = _fit_longest(img, 2160).convert('RGB')
        except Exception:
            continue
        if r.size != hsize:
            continue
        mse = float(np.mean((np.asarray(r, dtype=np.float32) - ha) ** 2))
        psnr = 99 if mse == 0 else 10 * np.log10(255 ** 2 / mse)
        if psnr < 25:
            continue
        if not apply:
            b = io.BytesIO(); r.save(b, 'WEBP', quality=85, method=6)
            return slug, 'would_encode', hosted.name, before, b.tell()
        tmp = hosted.with_suffix('.tmp.webp')
        if not generate_display_image(src, tmp, 2160, 'webp', 85):
            return slug, 'encode_failed', hosted.name, before, before
        os.replace(tmp, hosted)
        return slug, 'encoded', hosted.name, before, hosted.stat().st_size
    return slug, 'no_verified_master', hosted.name, before, before


if __name__ == '__main__':
    jobs = [j for s in slugs for j in jobs_for(s)]
    print('candidates', len(jobs), flush=True)
    stats = {}
    log = open(Path.cwd() / 'requeue_q90.log', 'a')
    with ProcessPoolExecutor(max_workers=8) as ex:
        for i, (slug, st, name, b0, b1) in enumerate(ex.map(work, jobs, chunksize=4)):
            k = (slug, st); a = stats.setdefault(k, [0, 0, 0]); a[0] += 1; a[1] += b0; a[2] += b1
            log.write(f'{slug}\t{st}\t{name}\t{b0}\t{b1}\n')
            if i % 200 == 0:
                print(i, len(jobs), flush=True)
    tb0 = tb1 = 0
    for (slug, st), (n, b0, b1) in sorted(stats.items()):
        print(f'{slug:32} {st:20} {n:5}  {b0/1e6:8.1f} MB -> {b1/1e6:8.1f} MB')
        if st in ('encoded', 'would_encode'):
            tb0 += b0; tb1 += b1
    print(f'TOTAL {tb0/1e9:.3f} GB -> {tb1/1e9:.3f} GB, saves {(tb0-tb1)/1e9:.3f} GB')
