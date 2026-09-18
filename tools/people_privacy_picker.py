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
        pairs = sorted({p for c in person['clusters'] for p in by_cluster.get(c, set())}
                       | {(slug, pid) for slug, ids in person['additional_photos'].items()
                          for pid in ids})
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
            'n_existing_keep': sum(1 for p in photos if p['keep']),
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
.trip-filter{display:flex;align-items:center;gap:7px;color:#aaa;font-size:12px}
.trip-filter select{background:#26262a;color:#eee;border:1px solid var(--line);border-radius:6px;
  padding:5px 8px;font:inherit;max-width:min(360px,70vw)}
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
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
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
.cell .state{position:absolute;top:5px;right:30px;z-index:3;font-size:9px;letter-spacing:.07em;
  text-transform:uppercase;font-weight:700;padding:3px 6px;border-radius:4px;display:none}
.cell .open{position:absolute;bottom:4px;right:4px;z-index:4;width:26px;height:26px;
  border:0;border-radius:50%;background:rgba(0,0,0,.7);color:#fff;text-decoration:none;display:none;
  align-items:center;justify-content:center;font-size:12px}
.cell:hover .open{display:flex}
.cell .open:hover{background:var(--blue)}
.cell:not(.keep) img{opacity:.72;filter:grayscale(.35)}
.cell:not(.keep){border-color:#3a2020}
.cell:not(.keep) .state{display:block;background:rgba(90,20,20,.9);color:#ffb4b4}
.cell.keep{border-color:var(--ok)}
.cell.keep .state{display:block;background:rgba(20,70,35,.94);color:#a8ecbd}
.cell:hover{outline:2px solid var(--blue)}
.cell.filter-hidden{display:none}
@media(max-width:600px){.grid{grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}}

.viewer{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.94);display:none;
  align-items:center;justify-content:center;padding:28px;cursor:zoom-out}
.viewer.show{display:flex}
.viewer img{display:block;max-width:100%;max-height:100%;object-fit:contain;cursor:default}
.viewer button{position:fixed;top:14px;right:16px;background:rgba(20,20,20,.9);color:#fff;
  border:1px solid #555;border-radius:50%;width:36px;height:36px;font-size:22px;cursor:pointer}
.viewer .label{position:fixed;bottom:14px;left:16px;color:#ddd;font-size:12px;
  background:rgba(20,20,20,.8);border-radius:4px;padding:5px 8px}

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
const state = {};
let viewerCells = [];
let viewerIndex = -1;
document.querySelectorAll('.sec').forEach(sec => {
  state[sec.dataset.key] = {
    tier: sec.dataset.tier,
    keep: new Set(JSON.parse(sec.dataset.existingKeeps || '[]')),
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

function refresh() {
  let restoring = 0;
  document.querySelectorAll('.sec').forEach(sec => {
    const s = state[sec.dataset.key];
    const cells = [...sec.querySelectorAll('.cell')];
    const selected = cells.filter(c => s.keep.has(c.dataset.ref));
    cells.forEach(c => {
      const keep = s.keep.has(c.dataset.ref);
      c.classList.toggle('keep', keep);
      c.querySelector('.state').textContent = keep ? 'Will restore' : 'Hidden';
    });
    restoring += selected.length;
    sec.querySelector('.selection').textContent = selected.length
      ? selected.length + ' selected to restore' : 'nothing selected yet';
  });
  document.getElementById('count').innerHTML =
    '<b>' + restoring + '</b> photo' + (restoring === 1 ? '' : 's') + ' selected to restore';
  document.getElementById('apply').disabled = JSON.stringify(serialise()) === orig;
}

const tripFilter = document.getElementById('trip-filter');
tripFilter.addEventListener('change', () => {
  const trip = tripFilter.value;
  document.querySelectorAll('.cell').forEach(cell =>
    cell.classList.toggle('filter-hidden', Boolean(trip) && cell.dataset.trip !== trip));
});

document.querySelectorAll('.grid').forEach(grid => grid.addEventListener('click', e => {
  const open = e.target.closest('.open');
  if (open) {
    e.preventDefault();
    const cell = open.closest('.cell');
    viewerCells = [...document.querySelectorAll('.cell')].filter(c => c.offsetParent !== null);
    viewerIndex = viewerCells.indexOf(cell);
    showViewer();
    return;
  }
  const cell = e.target.closest('.cell');
  if (!cell) return;
  e.preventDefault();
  const s = state[cell.closest('.sec').dataset.key];
  const ref = cell.dataset.ref;
  if (s.keep.has(ref)) s.keep.delete(ref); else s.keep.add(ref);
  refresh();
}));

function showViewer() {
  const cell = viewerCells[viewerIndex];
  if (!cell) return;
  const viewer = document.getElementById('viewer');
  document.getElementById('viewer-image').src = cell.dataset.display;
  document.getElementById('viewer-label').textContent =
    cell.dataset.ref + ' · ' + (viewerIndex + 1) + ' / ' + viewerCells.length;
  viewer.classList.add('show');
}

document.getElementById('viewer').addEventListener('click', e => {
  if (e.target.id === 'viewer' || e.target.closest('.close')) e.currentTarget.classList.remove('show');
});
document.addEventListener('keydown', e => {
  const viewer = document.getElementById('viewer');
  if (e.key === 'Escape') viewer.classList.remove('show');
  if (!viewer.classList.contains('show')) return;
  if (e.key === 'ArrowLeft' && viewerIndex > 0) {
    e.preventDefault(); viewerIndex -= 1; showViewer();
  }
  if (e.key === 'ArrowRight' && viewerIndex < viewerCells.length - 1) {
    e.preventDefault(); viewerIndex += 1; showViewer();
  }
});

document.getElementById('apply').addEventListener('click', async () => {
  const msg = document.getElementById('msg');
  msg.textContent = 'applying…';
  try {
    const r = await fetch('/apply', {method: 'POST', body: JSON.stringify({changes: serialise()})});
    const j = await r.json();
    msg.textContent = j.ok ? (j.message || 'saved') + ' — rebuild locally before deploying'
                           : 'error: ' + j.error;
    if (j.ok) setTimeout(() => location.reload(), 1400);
  } catch (err) { msg.textContent = 'error: ' + err; }
});

refresh();
"""


def render(cands):
    total = sum(len(c['photos']) - c['n_existing_keep'] for c in cands.values())
    P = ['<!doctype html><html lang=en><head><meta charset=utf-8>',
         '<meta name=viewport content="width=device-width,initial-scale=1">',
         '<title>people privacy picker</title>',
         '<link rel=icon href="data:,">',   # else every load logs a favicon 404
         f'<style>{PAGE_CSS}</style></head><body>']

    P.append('<header class=top><h1>restore hidden people photos</h1>')
    trips = sorted({(p['trip'], p['trip_name'])
                    for info in cands.values() for p in info['photos'] if not p['keep']},
                   key=lambda x: x[1])
    P.append('<label class=trip-filter>Trip <select id=trip-filter><option value="">All trips</option>'
             + ''.join(f'<option value="{html.escape(slug)}">{html.escape(name)}</option>'
                       for slug, name in trips)
             + '</select></label>')
    P.append(f'<p class=sub><b>Every photo below is currently hidden.</b> Click a photo to '
             f'choose it for restoration; selected photos turn green. Use ⤢ to open the image '
             f'in the full-screen viewer. Nothing changes until you press Apply. '
             f'{total} currently hidden photos shown.</p>')
    P.append('</header>')

    for key, info in cands.items():
        tier = 'false' if info['hide'] is False else info['hide']
        photos = [p for p in info['photos'] if not p['keep']]
        n = len(photos)
        existing = [f'{p["trip"]}/{p["id"]}' for p in info['photos'] if p['keep']]
        P.append(f'<section class=sec data-key="{html.escape(key)}" data-tier="{tier}" '
                 f'data-existing-keeps="{html.escape(json.dumps(existing), quote=True)}">')
        P.append(f'<h2>{html.escape(info["label"])}</h2>')
        P.append(f'<div class=meta>{n} currently hidden · <span class=selection></span> · '
                 f'{len(existing)} already restored (not shown)</div>')

        P.append('<div class=grid>')
        for ph in photos:
            ref = f'{ph["trip"]}/{ph["id"]}'
            weak_cls = ' weak' if ph['sim'] is not None and ph['sim'] < WEAK_SIM else ''
            cls = 'cell' + (' keep' if ph['keep'] else '') + weak_cls
            sim = (f'<span class=sim title="similarity to this person&rsquo;s average face '
                   f'· detector score {ph["det"]:.2f}">{ph["sim"]:.2f}</span>'
                   if ph['sim'] is not None else '')
            gate = '' if ph['pub'] else '<span class=gate title="already gated or in a private trip">🔒</span>'
            P.append(
                f'<div class="{cls}" data-ref="{html.escape(ref)}" '
                f'data-trip="{html.escape(ph["trip"])}" data-display="{html.escape(ph["disp"])}" '
                f'title="{html.escape(ref)}">'
                f'{sim}{gate}'
                f'<span class=state></span>'
                f'<button class=open type=button title="open full-screen viewer">⤢</button>'
                f'<img loading=lazy src="{html.escape(ph["thumb"])}" alt="{html.escape(ph["id"])}">'
                f'<span class=cap>{html.escape(ph["trip_name"])}</span></div>')
        P.append('</div></section>')

    P.append('<div class=applybar><span class=n id=count></span>'
             '<button class=apply id=apply disabled>Apply</button>'
             '<span class=msg id=msg>Tier and keep-picks are saved to config/people.json.</span>'
             '</div>')
    P.append('<div class=viewer id=viewer><button class=close type=button aria-label="Close">×</button>'
             '<img id=viewer-image alt=""><span class=label id=viewer-label></span></div>')
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
