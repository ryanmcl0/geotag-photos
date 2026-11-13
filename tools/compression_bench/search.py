"""Per-photo quality search: for each photo find the lowest AVIF q whose decode is no worse
than the currently hosted file, under a given criterion. Writes picks.json."""
import json, sys, collections
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import bench

B = bench.B
FAMS = {
    'avif444_iq_s4': lambda q: ['-q', str(q), '-s', '4', '-y', '444', '-d', '8', '-a', 'tune=iq'],
    'avif420_iq_s4': lambda q: ['-q', str(q), '-s', '4', '-y', '420', '-d', '8', '-a', 'tune=iq'],
}
STRICT = ('ssimu2', 'psnr_y', 'psnr_cb', 'psnr_cr')
TARGETS = {
    'avif444_strict': ('avif444_iq_s4', STRICT),
    'avif444_score': ('avif444_iq_s4', ('ssimu2',)),
    'avif420_strict': ('avif420_iq_s4', STRICT),
}


def search(row, target, known, hosted):
    fam, crit = TARGETS[target]
    new = []

    def ev(q):
        if q not in known:
            r = bench.run(row, f'{fam}_q{q}', ('avifenc', FAMS[fam](q)))
            known[q] = r; new.append(r)
        return known[q]

    def ok(q):
        r = ev(q)
        return all(r[m] >= hosted[m] for m in crit)

    passing = sorted(q for q in known if all(known[q][m] >= hosted[m] for m in crit))
    hi = passing[0] if passing else None
    if hi is None:
        for q in (95, 98, 100):
            if ok(q):
                hi = q; break
        if hi is None:
            return row['key'], target, None, new
    failing = [q for q in known if q < hi and not all(known[q][m] >= hosted[m] for m in crit)]
    lo = max(failing) if failing else None
    if lo is None:
        lo = 30
        for q in (45, 40, 35):
            if q < hi:
                if ok(q):
                    hi = q
                else:
                    lo = q; break
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return row['key'], target, known[hi] | {'q': hi}, new


if __name__ == '__main__':
    rows = json.loads((B / 'sample.json').read_text())
    res = collections.defaultdict(dict)
    for l in bench.RES.read_text().splitlines():
        r = json.loads(l)
        if 'error' not in r:
            res[r['key']][r['variant']] = r
    picks = collections.defaultdict(dict)
    with ProcessPoolExecutor(max_workers=8) as ex, open(bench.RES, 'a') as f:
        futs = []
        for row in rows:
            k = row['key']
            for t, (fam, _) in TARGETS.items():
                known = {int(v.rsplit('_q', 1)[1]): r for v, r in res[k].items() if v.startswith(fam + '_q')}
                futs.append(ex.submit(search, row, t, known, res[k]['hosted']))
        for i, fu in enumerate(as_completed(futs)):
            k, t, pick, new = fu.result()
            for r in new:
                f.write(json.dumps(r) + '\n')
            f.flush()
            picks[k][t] = pick
            print(i, len(futs), k, t, pick and pick['q'], flush=True)
    for row in rows:
        picks[row['key']]['hosted'] = res[row['key']]['hosted']
    (B / 'picks.json').write_text(json.dumps(picks, indent=1))
    print('done')
