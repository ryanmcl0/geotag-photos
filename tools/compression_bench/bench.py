"""Rate/quality benchmark. Reference = the resized master (lossless PNG), i.e. exactly what
the pipeline hands its encoder. Every candidate is decoded back to PNG and scored against
that reference with SSIMULACRA2 (higher = closer to the master; 100 = identical).
Results are cached per (image, variant) in results.jsonl so the run is resumable."""
import json, sys, subprocess, os, io, tempfile, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from PIL import Image
import numpy as np
from ssimulacra2 import compute_ssimulacra2
import pillow_jxl

B = Path(sys.argv[1])
ENC = B / 'enc'; ENC.mkdir(exist_ok=True)
RES = B / 'results.jsonl'


def variants():
    v = [('hosted', None)]
    for q in (75, 80, 85, 90, 95):
        v.append((f'webp_m6_q{q}', ('webp', dict(quality=q, method=6))))
    for q in (75, 80, 85, 90):
        v.append((f'webp_sharp_q{q}', ('webp', dict(quality=q, method=6, use_sharp_yuv=True))))
    for q in (63, 80, 90):
        v.append((f'pilavif_s6_q{q}', ('pilavif', dict(quality=q, speed=6))))
    for q in (50, 55, 60, 65, 70, 75, 80, 85, 90):
        v.append((f'avif444_iq_s4_q{q}', ('avifenc', ['-q', str(q), '-s', '4', '-y', '444', '-d', '8', '-a', 'tune=iq'])))
    for q in (50, 60, 70, 80, 90):
        v.append((f'avif444_10b_iq_s4_q{q}', ('avifenc', ['-q', str(q), '-s', '4', '-y', '444', '-d', '10', '-a', 'tune=iq'])))
    for q in (60, 70, 80, 90):
        v.append((f'avif420_iq_s4_q{q}', ('avifenc', ['-q', str(q), '-s', '4', '-y', '420', '-d', '8', '-a', 'tune=iq'])))
    for q in (60, 70, 80, 90):
        v.append((f'avif444_ssim_s4_q{q}', ('avifenc', ['-q', str(q), '-s', '4', '-y', '444', '-d', '8', '-a', 'tune=ssim'])))
    for d in (0.5, 0.65, 0.8, 1.0, 1.25, 1.5, 2.0):
        v.append((f'jxl_e7_q{int(round(100 - d * 10))}', ('jxl', dict(distance=d, effort=7))))
    for q in (80, 85, 90):
        v.append((f'cwebp_sharpyuv_q{q}', ('cwebp', ['-q', str(q), '-m', '6', '-sharp_yuv'])))
    for q in (80, 85, 90):
        v.append((f'cwebp_tuned_q{q}', ('cwebp', ['-q', str(q), '-m', '6', '-sharp_yuv', '-af', '-pass', '10', '-sns', '70'])))
    return v


def psnr(a, b):
    a = np.asarray(Image.open(a).convert('RGB'), dtype=np.float64)
    b = np.asarray(Image.open(b).convert('RGB'), dtype=np.float64)
    def ycc(x):
        r, g, bl = x[..., 0], x[..., 1], x[..., 2]
        y = .299 * r + .587 * g + .114 * bl
        return y, (bl - y) * .564, (r - y) * .713
    out = []
    for p, q in zip(ycc(a), ycc(b)):
        mse = np.mean((p - q) ** 2)
        out.append(99.0 if mse == 0 else float(10 * np.log10(255 ** 2 / mse)))
    return out  # Y, Cb, Cr


def run(row, name, spec):
    key = row['key']
    ref = B / 'ref' / f'{key}.png'
    t0 = time.time()
    if name == 'hosted':
        encp = Path(row['hosted'])
    else:
        kind, opt = spec
        ext = {'webp': 'webp', 'cwebp': 'webp', 'jxl': 'jxl'}.get(kind, 'avif')
        encp = ENC / f'{key}__{name}.{ext}'
        if not encp.exists():
            if kind == 'webp':
                Image.open(ref).save(encp, 'WEBP', **opt)
            elif kind == 'jxl':
                import imagecodecs
                encp.write_bytes(imagecodecs.jpegxl_encode(np.asarray(Image.open(ref).convert('RGB')), **opt))
            elif kind == 'cwebp':
                subprocess.run(['cwebp', '-quiet', *opt, str(ref), '-o', str(encp)], check=True, capture_output=True)
            elif kind == 'pilavif':
                Image.open(ref).save(encp, 'AVIF', **opt)
            else:
                subprocess.run(['avifenc', '-j', '2', *opt, str(ref), str(encp)],
                               check=True, capture_output=True)
    tenc = time.time() - t0
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tf:
        dec = tf.name
    try:
        im = Image.open(encp).convert('RGB')
        refsize = Image.open(ref).size
        if im.size != refsize:
            return dict(key=key, variant=name, error=f'size {im.size} != {refsize}')
        im.save(dec, compress_level=1)
        s = float(compute_ssimulacra2(str(ref), dec))
        py, pcb, pcr = psnr(ref, dec)
    finally:
        os.unlink(dec)
    return dict(key=key, variant=name, bytes=encp.stat().st_size, ssimu2=s,
                psnr_y=py, psnr_cb=pcb, psnr_cr=pcr, enc_s=round(tenc, 2))


if __name__ == '__main__':
    rows = json.loads((B / 'sample.json').read_text())
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else len(rows)
    only = sys.argv[3].split(',') if len(sys.argv) > 3 else None
    rows = rows[:limit]
    done = set()
    if RES.exists():
        for l in RES.read_text().splitlines():
            r = json.loads(l); done.add((r['key'], r['variant']))
    jobs = [(r, n, s) for r in rows for n, s in variants()
            if (r['key'], n) not in done and (not only or any(n.startswith(o) for o in only))]
    print('jobs', len(jobs), flush=True)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=8) as ex, open(RES, 'a') as f:
        futs = [ex.submit(run, *j) for j in jobs]
        for i, fu in enumerate(as_completed(futs)):
            try:
                r = fu.result()
            except Exception as e:
                r = dict(key='?', variant='?', error=repr(e)[:300])
            f.write(json.dumps(r) + '\n'); f.flush()
            if i % 50 == 0:
                print(i, len(jobs), round(time.time() - t0), r.get('variant'), r.get('error', ''), flush=True)
    print('done', round(time.time() - t0), flush=True)
