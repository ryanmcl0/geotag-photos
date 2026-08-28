#!/usr/bin/env python3
"""Preview and tune who is hidden from the site (config/people.json).

Face clustering decides WHICH photos show a person; this picker is where you look
at those photos before any of them disappear, and carve out exceptions.

Per person you get one section:

  · a tier — Visible / Gated (See All only) / Blocked (hidden from every tier),
  · every photo that tier would hide, weakest face match first, so the wrong ones
    surface at the top instead of being buried,
  · click any photo to flip it between HIDE and KEEP. KEEP is the per-person
    escape hatch: that photo stays on the site even though the person is hidden.

Face matches are scored by cosine similarity to the person's centroid embedding,
which is what makes the false positives obvious: a distant figure or a bit of
machinery the detector mistook for a face lands near 0.05, while a real match sits
around 0.7-0.9. Anything under WEAK_SIM is badged, and one button keeps them all.

Nothing changes on disk until Apply, so switching someone to Blocked purely to see
the damage and switching back is free. Apply rewrites the changed people in
config/people.json and re-runs tools/people_index.py; the exclusion only reaches
the site on the next build/deploy.

    tools/people_privacy_picker.py [--person <key>]

Local-only: it reads thumbnails straight from hosted-photos/, so it works for
gated and private-trip photos the deployed site would refuse to serve.
"""
import html
import json
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import people_index  # noqa: E402  (path set above)

TRIPS = ROOT / 'web' / 'trips'
FACE_DB = ROOT / 'local_browse' / 'face_index.sqlite'
CLUSTERS = ROOT / 'local_browse' / 'clusters.json'
ROSTER = people_index.ROSTER

# Below this similarity to the person's centroid, treat the match as suspect.
# Real matches cluster around 0.7-0.9; detector false positives land under 0.15.
WEAK_SIM = 0.45

TIERS = [(False, 'Visible'), ('gated', 'Gated'), ('blocked', 'Blocked')]
TIER_HELP = {
    False: 'On the site as normal — nothing is hidden.',
    'gated': 'Off the public site; still visible to you with the See All password. '
             'Same treatment as a force_private photo.',
    'blocked': 'Hidden from every tier, See All included. Only the R2 object survives.',
}


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def face_scores(cluster_ids) -> dict:
    """(slug, photo_id) → (similarity, det_score) for one person's clusters.

    Similarity is cosine distance from each face to the mean embedding of every
    face attributed to the person (embeddings are already L2-normalised by
    face_index.py). Where a photo holds several of the person's faces, the best
    one wins — one good match is enough to say they are in the shot.

    Returns {} if numpy or the face DB is unavailable; the picker then simply
    shows no confidence badges rather than failing.
    """
    if not FACE_DB.exists():
        return {}
    try:
        import numpy as np
    except ImportError:
        return {}
    clusters = {c['id']: c for c in (_load(CLUSTERS) or {}).get('clusters', [])}
    fids = [f for cid in cluster_ids for f in clusters.get(cid, {}).get('face_ids', [])]
    if not fids:
        return {}
    con = sqlite3.connect(FACE_DB)
    out = {}
    try:
        rows = []
        # SQLite caps host parameters per statement; chunk the id list.
        for i in range(0, len(fids), 900):
            chunk = fids[i:i + 900]
            q = ','.join('?' * len(chunk))
            rows += con.execute(
                f"SELECT img, source, det, emb FROM faces WHERE id IN ({q})", tuple(chunk)
            ).fetchall()
    finally:
        con.close()
    if not rows:
        return {}
    emb = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
    centroid = emb.mean(0)
    norm = np.linalg.norm(centroid)
    if not norm:
        return {}
    sims = emb @ (centroid / norm)
    for (img, source, det, _), sim in zip(rows, sims):
        if source != 'camera':
            continue          # phone library is local-only; nothing there to hide
        slug = img.split('/')[0]
        pid = img.split('/')[-1].rsplit('.', 1)[0]
        key = (slug, pid)
        prev = out.get(key)
        if prev is None or sim > prev[0]:
            out[key] = (float(sim), float(det))
    return out


def build_candidates(only=None):
    """key → {label, hide, clusters, photos:[…]} over every roster person with photos."""
    roster = people_index.load_roster()
    if not roster:
        return {}, {}
    by_cluster, _ = people_index.cluster_photos()
    index = _load(TRIPS / 'index.json') or {}
    trip_name = {t['id']: (t.get('name') or t['id']) for t in index.get('trips', [])}
    public_trips = {t['id'] for t in index.get('trips', []) if t.get('public')}

    pub_cache = {}

    def is_public(slug, pid):
        if slug not in pub_cache:
            if slug in public_trips:
                man = _load(TRIPS / slug / 'manifest.json') or {}
                pub_cache[slug] = {p['id'] for p in man.get('photos', [])}
            else:
                pub_cache[slug] = set()
        return pid in pub_cache[slug]

    cands = {}
    for key, person in roster.items():
        if only and key != only:
            continue
        pairs = sorted({p for c in person['clusters'] for p in by_cluster.get(c, set())})
        if not pairs:
            continue
        scores = face_scores(person['clusters'])
        keep = person['keep_public']
        photos = [{
            'trip': slug, 'id': pid,
            'trip_name': trip_name.get(slug, slug),
            'thumb': f'hosted-photos/{slug}/thumbnails/{pid}.webp',
            'disp': f'hosted-photos/{slug}/display/{pid}.webp',
            'keep': pid in keep.get(slug, set()),
            'pub': is_public(slug, pid),
            'sim': scores.get((slug, pid), (None, None))[0],
            'det': scores.get((slug, pid), (None, None))[1],
        } for slug, pid in pairs]
        # Weakest match first: the whole point is that wrong attributions are the
        # ones you need to see. Unscored photos sort as if average.
        photos.sort(key=lambda p: (p['sim'] if p['sim'] is not None else 0.5, p['trip'], p['id']))
        cands[key] = {
            'label': person['label'],
            'hide': person['hide'],
            'clusters': person['clusters'],
            'photos': photos,
            'n_weak': sum(1 for p in photos if p['sim'] is not None and p['sim'] < WEAK_SIM),
        }
    return cands, roster


PAGE_CSS = """
:root{--bg:#111;--panel:#1b1b1d;--fg:#eee;--muted:#9a9a9f;--line:#2c2c30;--ok:#5ad17e;
  --warn:#d9a441;--bad:#e06b6b;--blue:#6b8cff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header.top{position:sticky;top:0;z-index:20;background:var(--panel);
  border-bottom:1px solid var(--line);padding:11px 18px;display:flex;align-items:center;
  gap:14px;flex-wrap:wrap}
header.top h1{font-size:16px;margin:0;font-weight:600}
header.top .sub{color:var(--muted);font-size:12px;flex-basis:100%;margin:0}
header.top .sub b{color:#ccc;font-weight:600}
.pill{background:#26262a;color:var(--muted);border:1px solid var(--line);border-radius:12px;
  padding:4px 12px;font-size:12px;cursor:pointer}
.pill:hover{color:#ddd}
.pill.on{color:var(--fg);border-color:var(--blue)}
.grp{display:flex;gap:6px;align-items:center}
.grp .lbl{color:#666;font-size:11px;text-transform:uppercase;letter-spacing:.06em}

.sec{padding:18px}
.sec+.sec{border-top:1px solid var(--line)}
.sec h2{font-size:16px;margin:0 0 4px;font-weight:600}
.sec .meta{color:var(--muted);font-size:12px;margin-bottom:12px}
.sec .meta .clusters{color:#555;font-family:ui-monospace,Menlo,monospace;font-size:11px}
.sec .willhide{color:var(--warn)}
.sec[data-tier="false"] .willhide{color:var(--ok)}

.tiers{display:flex;gap:7px;align-items:center;margin:0 0 10px;flex-wrap:wrap}
.tiers button.on[data-tier="false"]{color:var(--ok);border-color:var(--ok)}
.tiers button.on[data-tier="gated"]{color:var(--warn);border-color:var(--warn)}
.tiers button.on[data-tier="blocked"]{color:var(--bad);border-color:var(--bad)}
.tiers .help{color:var(--muted);font-size:12px}
.bulk{display:flex;gap:7px;align-items:center;margin:0 0 12px;flex-wrap:wrap}
.bulk .lbl{color:#666;font-size:11px;text-transform:uppercase;letter-spacing:.06em}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:9px}
.cell{position:relative;background:#000;border-radius:5px;overflow:hidden;aspect-ratio:3/2;
  cursor:pointer;border:2px solid transparent}
.cell img{width:100%;height:100%;object-fit:cover;display:block;background:#222}
.cell .cap{position:absolute;left:0;right:0;bottom:0;font-size:10px;padding:12px 5px 3px;
  background:linear-gradient(transparent,rgba(0,0,0,.9));color:#ccc;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.cell .sim{position:absolute;top:4px;left:4px;font-size:10px;font-family:ui-monospace,Menlo,monospace;
  background:rgba(0,0,0,.7);border-radius:3px;padding:1px 5px;color:#9a9a9f}
.cell.weak .sim{color:var(--bad);font-weight:600}
.cell .gate{position:absolute;top:4px;right:4px;font-size:10px;background:rgba(0,0,0,.7);
  border-radius:3px;padding:1px 4px;color:var(--warn)}
/* State badge: the cell says what WILL happen, rather than making you infer it. */
.cell .state{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;
  font-size:11px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;
  padding:4px 10px;border-radius:999px;display:none}
.cell .open{position:absolute;bottom:4px;right:4px;z-index:4;width:22px;height:22px;
  border-radius:50%;background:rgba(0,0,0,.65);color:#fff;text-decoration:none;display:none;
  align-items:center;justify-content:center;font-size:12px}
.cell:hover .open{display:flex}
.cell .open:hover{background:var(--blue)}
/* Visible tier: nothing is happening to any of these, so leave them plain. */
.sec[data-tier="gated"] .cell:not(.keep) img,
.sec[data-tier="blocked"] .cell:not(.keep) img{opacity:.3;filter:grayscale(.75)}
.sec[data-tier="gated"] .cell:not(.keep),
.sec[data-tier="blocked"] .cell:not(.keep){border-color:#3a2020}
.sec[data-tier="gated"] .cell:not(.keep) .state,
.sec[data-tier="blocked"] .cell:not(.keep) .state{display:block;background:rgba(90,20,20,.9);color:#ffb4b4}
.sec[data-tier="gated"] .cell.keep,.sec[data-tier="blocked"] .cell.keep{border-color:var(--ok)}
.sec[data-tier="gated"] .cell.keep .state,.sec[data-tier="blocked"] .cell.keep .state{
  display:block;background:rgba(20,70,35,.9);color:#a8ecbd}
.cell:hover{outline:2px solid var(--blue)}

.applybar{position:fixed;right:16px;bottom:16px;z-index:50;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;padding:13px 15px;display:flex;
  flex-direction:column;gap:9px;box-shadow:0 6px 24px rgba(0,0,0,.55);min-width:250px}
.applybar .n{font-size:13px}
.applybar .n b{color:var(--bad);font-size:15px}
.applybar button.apply{background:#2c6b3f;border:1px solid #2c6b3f;color:#fff;font-weight:600;
  border-radius:7px;padding:8px 12px;font-size:13px;cursor:pointer}
.applybar button.apply:hover{background:#357d49}
.applybar button.apply:disabled{opacity:.35;cursor:default}
.applybar .msg{font-size:12px;color:var(--muted);max-width:235px;line-height:1.45}
body.f-public .cell[data-pub="0"],body.f-gated .cell[data-pub="1"]{display:none}
"""

PAGE_JS = """
const WEAK = __WEAK__;
const state = {};
document.querySelectorAll('.sec').forEach(sec => {
  state[sec.dataset.key] = {
    tier: sec.dataset.tier,
    keep: new Set([...sec.querySelectorAll('.cell.keep')].map(c => c.dataset.ref)),
  };
});

function serialise() {
  const out = {};
  Object.keys(state).sort().forEach(k => {
    out[k] = {tier: state[k].tier, keep: [...state[k].keep].sort()};
  });
  return out;
}
const orig = JSON.stringify(serialise());

/* Counts are always over what the filter is actually showing, so the numbers on
   screen and the numbers in the buttons can never disagree. */
function shownCells(sec) {
  return [...sec.querySelectorAll('.cell')].filter(c => c.offsetParent !== null);
}

function refresh() {
  let total = 0;
  document.querySelectorAll('.sec').forEach(sec => {
    const s = state[sec.dataset.key];
    sec.dataset.tier = s.tier;
    sec.querySelectorAll('.tiers .pill').forEach(b =>
      b.classList.toggle('on', b.dataset.tier === s.tier));
    sec.querySelectorAll('.cell').forEach(c =>
      c.classList.toggle('keep', s.keep.has(c.dataset.ref)));
    const shown = shownCells(sec);
    const hiding = s.tier === 'false' ? 0
      : shown.filter(c => !s.keep.has(c.dataset.ref)).length;
    total += hiding;
    sec.querySelector('.willhide').textContent = s.tier === 'false'
      ? 'nothing hidden'
      : hiding + ' of the ' + shown.length + ' shown will be hidden';
    sec.querySelector('.shown').textContent = shown.length;
  });
  document.getElementById('count').innerHTML =
    '<b>' + total + '</b> photo' + (total === 1 ? '' : 's') + ' would be hidden';
  document.getElementById('apply').disabled = JSON.stringify(serialise()) === orig;
}

document.querySelectorAll('.tiers .pill').forEach(b => b.addEventListener('click', () => {
  const sec = b.closest('.sec');
  state[sec.dataset.key].tier = b.dataset.tier;
  sec.querySelector('.tierhelp').textContent = b.dataset.help;
  refresh();
}));

document.querySelectorAll('.grid').forEach(grid => grid.addEventListener('click', e => {
  if (e.target.closest('.open')) return;      // the ⤢ link opens the full size
  const cell = e.target.closest('.cell');
  if (!cell) return;
  e.preventDefault();
  const s = state[cell.closest('.sec').dataset.key];
  const ref = cell.dataset.ref;
  if (s.keep.has(ref)) s.keep.delete(ref); else s.keep.add(ref);
  refresh();
}));

/* Bulk actions operate on what is visible, matching the counts. */
document.querySelectorAll('.bulk .pill').forEach(b => b.addEventListener('click', () => {
  const sec = b.closest('.sec');
  const s = state[sec.dataset.key];
  shownCells(sec).forEach(c => {
    const weak = c.classList.contains('weak');
    if (b.dataset.act === 'weak' && weak) s.keep.add(c.dataset.ref);
    if (b.dataset.act === 'all') s.keep.add(c.dataset.ref);
    if (b.dataset.act === 'none') s.keep.delete(c.dataset.ref);
  });
  refresh();
}));

document.querySelectorAll('#filters .pill').forEach(b => b.addEventListener('click', () => {
  document.body.classList.remove('f-public', 'f-gated');
  if (b.dataset.filter !== 'all') document.body.classList.add('f-' + b.dataset.filter);
  document.querySelectorAll('#filters .pill').forEach(x => x.classList.toggle('on', x === b));
  refresh();
}));

document.getElementById('apply').addEventListener('click', async () => {
  const msg = document.getElementById('msg');
  msg.textContent = 'applying…';
  try {
    const r = await fetch('/apply', {method: 'POST', body: JSON.stringify({changes: serialise()})});
    const j = await r.json();
    msg.textContent = j.ok ? (j.message || 'saved') + ' — rebuild and deploy to publish'
                           : 'error: ' + j.error;
    if (j.ok) setTimeout(() => location.reload(), 1400);
  } catch (err) { msg.textContent = 'error: ' + err; }
});

refresh();
"""


def render(cands):
    total = sum(len(c['photos']) for c in cands.values())
    weak = sum(c['n_weak'] for c in cands.values())
    P = ['<!doctype html><html lang=en><head><meta charset=utf-8>',
         '<meta name=viewport content="width=device-width,initial-scale=1">',
         '<title>people privacy picker</title>',
         '<link rel=icon href="data:,">',   # else every load logs a favicon 404
         f'<style>{PAGE_CSS}</style></head><body>']

    P.append('<header class=top><h1>people privacy</h1>')
    P.append('<span class=grp><span class=lbl>show</span><span id=filters>'
             + ''.join(f'<button class="pill{" on" if v == "all" else ""}" data-filter="{v}">{t}</button>'
                       for v, t in (('all', 'All photos'), ('public', 'Public only'),
                                    ('gated', 'Already gated')))
             + '</span></span>')
    P.append(f'<p class=sub>Sorted <b>weakest face match first</b>, so wrong attributions come '
             f'up top. The number on each photo is its similarity to that person&rsquo;s average '
             f'face &mdash; a real match sits around <b>0.70&ndash;0.90</b>, a detector false '
             f'positive lands under <b>0.15</b>. Pick a tier, then click any photo to flip it '
             f'between <b>HIDE</b> and <b>KEEP</b>. Nothing is written until you press Apply. '
             f'{total} photos, {weak} flagged as weak matches.</p>')
    P.append('</header>')

    for key, info in cands.items():
        tier = 'false' if info['hide'] is False else info['hide']
        n = len(info['photos'])
        P.append(f'<section class=sec data-key="{html.escape(key)}" data-tier="{tier}">')
        P.append(f'<h2>{html.escape(info["label"])}</h2>')
        P.append(f'<div class=meta><span class=shown>{n}</span> of {n} photos shown · '
                 f'<span class=willhide></span> · {info["n_weak"]} weak · '
                 f'<span class=clusters>{html.escape(" ".join(info["clusters"]))}</span></div>')

        P.append('<div class=tiers><span class=lbl style="color:#666;font-size:11px;'
                 'text-transform:uppercase;letter-spacing:.06em">tier</span>')
        for value, label in TIERS:
            v = 'false' if value is False else value
            on = ' on' if v == tier else ''
            P.append(f'<button class="pill{on}" data-tier="{v}" '
                     f'data-help="{html.escape(TIER_HELP[value])}">{label}</button>')
        P.append(f'<span class="help tierhelp">{html.escape(TIER_HELP[info["hide"]])}</span></div>')

        P.append('<div class=bulk><span class=lbl>keep</span>'
                 f'<button class=pill data-act="weak">Keep all {info["n_weak"]} weak matches</button>'
                 '<button class=pill data-act="all">Keep everything shown</button>'
                 '<button class=pill data-act="none">Clear keeps</button></div>')

        P.append('<div class=grid>')
        for ph in info['photos']:
            ref = f'{ph["trip"]}/{ph["id"]}'
            weak_cls = ' weak' if ph['sim'] is not None and ph['sim'] < WEAK_SIM else ''
            cls = 'cell' + (' keep' if ph['keep'] else '') + weak_cls
            sim = (f'<span class=sim title="similarity to this person&rsquo;s average face '
                   f'· detector score {ph["det"]:.2f}">{ph["sim"]:.2f}</span>'
                   if ph['sim'] is not None else '')
            gate = '' if ph['pub'] else '<span class=gate title="already gated or in a private trip">🔒</span>'
            P.append(
                f'<div class="{cls}" data-ref="{html.escape(ref)}" '
                f'data-pub="{1 if ph["pub"] else 0}" title="{html.escape(ref)}">'
                f'{sim}{gate}'
                f'<span class=state></span>'
                f'<a class=open href="{html.escape(ph["disp"])}" target=_blank rel=noopener '
                f'title="open full size">⤢</a>'
                f'<img loading=lazy src="{html.escape(ph["thumb"])}" alt="{html.escape(ph["id"])}">'
                f'<span class=cap>{html.escape(ph["trip_name"])}</span></div>')
        P.append('</div></section>')

    P.append('<div class=applybar><span class=n id=count></span>'
             '<button class=apply id=apply disabled>Apply</button>'
             '<span class=msg id=msg>Tier and keep-picks are saved to config/people.json.</span>'
             '</div>')
    P.append(f'<script>{PAGE_JS.replace("__WEAK__", str(WEAK_SIM))}</script></body></html>')
    return '\n'.join(P)


def write_changes(changes: dict) -> str:
    """Merge tiers + keep_public back into config/people.json, then re-resolve."""
    config = json.loads(ROSTER.read_text())
    people = config.setdefault('people', {})
    touched = []
    for key, ch in changes.items():
        if key not in people:
            continue
        tier = ch.get('tier')
        hide = False if tier in (False, 'false', None) else tier
        if hide not in people_index.HIDE_TIERS:
            raise ValueError(f"bad tier {tier!r} for {key}")
        keep = {}
        for ref in ch.get('keep') or []:
            slug, _, pid = ref.partition('/')
            if slug and pid:
                keep.setdefault(slug, []).append(pid)
        entry = people[key]
        before = (entry.get('hide', False), entry.get('keep_public') or {})
        entry['hide'] = hide
        entry['keep_public'] = {s: sorted(v) for s, v in sorted(keep.items())}
        if before != (entry['hide'], entry['keep_public']):
            touched.append(key)
    ROSTER.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')

    # Re-resolve immediately so config/people_private.json can never sit stale
    # behind the roster (photo_privacy aborts the build if it does).
    out = subprocess.run([sys.executable, str(ROOT / 'tools' / 'people_index.py')],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or 'people_index.py failed')
    return f"{len(touched)} people updated" if touched else "no changes"


def make_handler(page_html):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(ROOT), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ('/', '/index.html', '/picker'):
                body = page_html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def do_POST(self):
            if self.path != '/apply':
                self.send_error(404)
                return
            try:
                n = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(n) or b'{}')
                message = write_changes(data.get('changes', {}))
                payload = json.dumps({'ok': True, 'message': message}).encode()
            except Exception as e:                 # noqa: BLE001 — report back to the page
                payload = json.dumps({'ok': False, 'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return Handler


def main():
    if not ROSTER.exists():
        print(f"no roster at {ROSTER.relative_to(ROOT)} — run tools/people_index.py --seed first")
        sys.exit(1)
    only = None
    if '--person' in sys.argv:
        only = sys.argv[sys.argv.index('--person') + 1]
    cands, _ = build_candidates(only)
    if not cands:
        if only:
            print(f"'{only}' is not in the roster, or has no photos in the face index")
        else:
            print('no roster people have photos in the face index — nothing to preview')
        sys.exit(1)
    page = render(cands)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(page))
    url = f'http://127.0.0.1:{httpd.server_address[1]}/'
    npics = sum(len(c['photos']) for c in cands.values())
    nweak = sum(c['n_weak'] for c in cands.values())
    print(f'people privacy picker · {len(cands)} people · {npics} photos · '
          f'{nweak} weak matches flagged')
    print(f'serving {url}  (Ctrl-C to stop)')
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped.')


if __name__ == '__main__':
    main()
