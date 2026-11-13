import json, sys, re, math, collections
from pathlib import Path
import numpy as np

B = Path(sys.argv[1])
rows = {r['key']: r for r in json.loads((B / 'sample.json').read_text())}
res = collections.defaultdict(dict)
for l in (B / 'results.jsonl').read_text().splitlines():
    r = json.loads(l)
    if 'error' in r:
        print('ERR', r); continue
    res[r['variant']][r['key']] = r
keys = sorted(k for k in rows if k in res['hosted'])
keys = [k for k in keys if all(k in res[v] for v in res)]
print('images with complete results:', len(keys))
H = res['hosted']
hb = sum(H[k]['bytes'] for k in keys)

print(f"\n{'variant':26} {'MB':>7} {'vs now':>7} {'ssimu2':>7} {'p10':>6} {'min':>6} {'d_mean':>7} {'worse':>6} {'PSNR Y':>7} {'Cb':>6} {'Cr':>6} {'enc s':>6}")
for v in res:
    R = res[v]
    b = sum(R[k]['bytes'] for k in keys)
    s = np.array([R[k]['ssimu2'] for k in keys]); h = np.array([H[k]['ssimu2'] for k in keys])
    worse = int(np.sum(s < h - 0.25))
    print(f"{v:26} {b/1e6:7.1f} {b/hb-1:+7.1%} {s.mean():7.2f} {np.percentile(s,10):6.2f} {s.min():6.2f} {(s-h).mean():+7.2f} {worse:6d} "
          f"{np.mean([R[k]['psnr_y'] for k in keys]):7.2f} {np.mean([R[k]['psnr_cb'] for k in keys]):6.2f} {np.mean([R[k]['psnr_cr'] for k in keys]):6.2f} "
          f"{np.mean([R[k].get('enc_s',0) for k in keys]):6.1f}")

fam = collections.defaultdict(list)
for v in res:
    m = re.match(r'(.+)_q(\d+)$', v)
    if m:
        fam[m.group(1)].append((int(m.group(2)), v))


def bytes_at(family, k, target):
    """smallest interpolated size on this family's curve reaching `target` score; None if unreachable"""
    pts = sorted((res[v][k]['ssimu2'], res[v][k]['bytes'], q) for q, v in fam[family])
    if target <= pts[0][0]:
        return pts[0][1], 'below-grid'
    for (s0, b0, _), (s1, b1, _) in zip(pts, pts[1:]):
        if s0 <= target <= s1:
            t = (target - s0) / (s1 - s0) if s1 > s0 else 0
            return math.exp(math.log(b0) + t * (math.log(b1) - math.log(b0))), 'ok'
    return None, 'unreachable'


for margin in (0.0, 1.0, 2.0):
    print(f"\nPer-photo matched quality: smallest file scoring >= (current hosted score + {margin})")
    for f in fam:
        tot = 0; un = 0; below = 0
        for k in keys:
            b, st = bytes_at(f, k, H[k]['ssimu2'] + margin)
            if b is None:
                un += 1; b = max(res[v][k]['bytes'] for _, v in fam[f])
            if st == 'below-grid':
                below += 1
            tot += b
        print(f"  {f:24} {tot/1e6:7.1f} MB  {tot/hb-1:+7.1%}   (unreachable {un}, floor-clamped {below})")

print('\nSingle fixed setting: lowest q where NO sampled photo scores below its current file (tolerance 0.25) / <=5% do')
for f in fam:
    for q, v in sorted(fam[f]):
        s = np.array([res[v][k]['ssimu2'] for k in keys]); h = np.array([H[k]['ssimu2'] for k in keys])
        b = sum(res[v][k]['bytes'] for k in keys)
        nw = int(np.sum(s < h - 0.25))
        if nw <= 0.05 * len(keys):
            print(f"  {f:24} q{q}: {b/hb-1:+7.1%} size, mean score {(s-h).mean():+.2f}, photos worse: {nw}/{len(keys)}")
            if nw == 0:
                break

# split by current hosted quality level
q90 = [k for k in keys if rows[k]['hosted_bytes'] == rows[k]['q90']]
q85 = [k for k in keys if rows[k]['hosted_bytes'] == rows[k]['q85']]
print(f"\nhosted at q90: {len(q90)} photos, at q85: {len(q85)}, other: {len(keys)-len(q90)-len(q85)}")


def need(family, k, metric, target):
    pts = sorted((res[v][k]['bytes'], res[v][k][metric]) for q, v in fam[family])
    if pts[0][1] >= target:
        return pts[0][0]
    for (b0, s0), (b1, s1) in zip(pts, pts[1:]):
        if s0 < target <= s1:
            t = (target - s0) / (s1 - s0)
            return math.exp(math.log(b0) + t * (math.log(b1) - math.log(b0)))
    return None


print('\nPer-photo matched on EVERY measure (perceptual score, luma PSNR and both chroma PSNRs all >= current file):')
for f in fam:
    tot = 0; un = 0; gain = collections.defaultdict(list)
    for k in keys:
        reqs = [need(f, k, m, H[k][m]) for m in ('ssimu2', 'psnr_y', 'psnr_cb', 'psnr_cr')]
        if any(r is None for r in reqs):
            un += 1; b = max(res[v][k]['bytes'] for _, v in fam[f])
        else:
            b = max(reqs)
        tot += b
    print(f"  {f:24} {tot/1e6:7.1f} MB  {tot/hb-1:+7.1%}   (unreachable {un})")

print('\nPer-photo matched on perceptual score + luma PSNR:')
for f in fam:
    tot = 0; un = 0
    for k in keys:
        reqs = [need(f, k, m, H[k][m]) for m in ('ssimu2', 'psnr_y')]
        if any(r is None for r in reqs):
            un += 1; b = max(res[v][k]['bytes'] for _, v in fam[f])
        else:
            b = max(reqs)
        tot += b
    print(f"  {f:24} {tot/1e6:7.1f} MB  {tot/hb-1:+7.1%}   (unreachable {un})")
