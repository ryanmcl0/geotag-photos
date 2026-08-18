#!/usr/bin/env python3
"""Hand-pick the photos that make up a bridge's gallery (config/bridge_photo_picks.json).

For bridges whose gallery is curated by hand instead of geofenced — flagged
`manual_photos` in config/china_bridges.json (Aizhai, Wumengshan, and the
under-construction Yalong Liangshan) — this picker shows every processed photo
from the candidate edits folders listed in bridge_photo_picks.json `sources`,
one section per bridge, and lets you multi-select the ones that belong.

    tools/bridge_photo_picker.py

Candidates come from the PROCESSED trips (local hosted-photos webp thumbnails),
never the raw drive: a source folder is resolved to the trips whose manifest
`photos_path` contains it. When a trip's manifest predates per-photo `section`
paths (e.g. 2025-china-cny) the folder membership falls back to a one-off
filename listing of the folder on the mount — metadata only, no content reads.

Cells are sorted by distance to the bridge (roster lat/lon) when known, else by
time, and carry folder + distance labels. Click to toggle; Apply rewrites the
`picks` block for the changed bridges only. Then run ./build_collections.py.
"""
import html
import json
import os
import re
import sys
import threading
import webbrowser
from collections import OrderedDict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / 'web' / 'trips'
CONFIG = ROOT / 'config' / 'bridge_photo_picks.json'
ROSTER = ROOT / 'config' / 'china_bridges.json'

INITIAL_SHOWN = 200       # cells rendered visible per section; the rest reveal on demand


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def dist_km(lat1, lon1, lat2, lon2):
    import math
    p = math.pi / 180
    x = (lat2 - lat1) * p
    y = (lon2 - lon1) * p * math.cos((lat1 + lat2) / 2 * p)
    return 6371 * math.hypot(x, y)


# ---------------------------------------------------------------- candidates

def load_manifests():
    """slug → (edits_root, manifest photos, has_sections)."""
    out = {}
    for mf in sorted(TRIPS.glob('*/manifest.json')):
        slug = mf.parent.name
        man = _load(mf.parent / 'manifest.all.json') or _load(mf)
        if not man:
            continue
        src = ((man.get('source') or {}).get('photos_path') or '').rstrip('/')
        photos = man.get('photos', [])
        has_sections = any(p.get('section') for p in photos)
        out[slug] = (src, photos, has_sections)
    return out


_FOLDER_STEMS = {}   # folder → set of lowercased image stems under it (mount listing)


def folder_stems(folder: str, top_only=False):
    """Filename stems under `folder` on the mount — metadata-only walk, cached.
    `top_only` lists just the folder itself, ignoring subfolders.
    Returns None when the folder isn't reachable (mount not up)."""
    key = (folder, top_only)
    if key not in _FOLDER_STEMS:
        p = Path(folder)
        if not p.is_dir():
            _FOLDER_STEMS[key] = None
        elif top_only:
            _FOLDER_STEMS[key] = {Path(f).stem.lower() for f in os.listdir(p)
                                  if (p / f).is_file()}
        else:
            stems = set()
            for base, _dirs, files in os.walk(p):
                for f in files:
                    stems.add(Path(f).stem.lower())
            _FOLDER_STEMS[key] = stems
    return _FOLDER_STEMS[key]


def photos_in_folder(slug, src, photos, has_sections, folder, top_only=False):
    """The manifest photos of `slug` that live under `folder` (directly in it when
    `top_only`), with a note on how membership was decided ('' = fine, else a warning)."""
    if not src:
        return [], 'manifest has no source path'
    if src.startswith(folder + '/'):
        # trip rooted in a subfolder of `folder`: all of it counts unless top_only
        return ([], '') if top_only else (photos, '')
    if src == folder:
        if not top_only:
            return photos, ''
        # top-level photos of a trip rooted at the folder = those with no section
        if has_sections:
            return [p for p in photos if not p.get('section')], ''
    elif not folder.startswith(src + '/'):
        return [], ''                            # unrelated trip
    else:
        rel = folder[len(src) + 1:]
        if has_sections:
            return [p for p in photos
                    if (s := p.get('section') or '') == rel
                    or (not top_only and s.startswith(rel + '/'))], ''
    # manifest predates `section` — fall back to a filename listing on the mount
    stems = folder_stems(folder, top_only)
    if stems is None:
        return [], f'{folder} not reachable (mount down?) — candidates from {slug} missing'
    return [p for p in photos
            if Path(p.get('source_filename') or p['id']).stem.lower() in stems], ''


def build_candidates():
    config = _load(CONFIG) or {}
    sources = config.get('sources') or {}
    picks = config.get('picks') or {}
    roster = {b['name']: b for b in (_load(ROSTER) or {}).get('bridges', [])}
    manifests = load_manifests()

    cands = OrderedDict()
    for bridge, folders in sources.items():
        b = roster.get(bridge) or {}
        blat, blon = b.get('lat'), b.get('lon')
        picked = {(p['trip'], p['id']) for p in picks.get(bridge, [])}
        seen, entries, notes = set(), [], []
        # a source is a folder path string, or {"folder": ..., "top_level_only": true}
        # to offer only the photos sitting directly in the folder (no subfolders)
        specs = [(f, False) if isinstance(f, str) else (f['folder'], f.get('top_level_only', False))
                 for f in folders]
        for folder, top_only in specs:
            short = Path(folder).parent.name + '/' + Path(folder).name + (' (top level)' if top_only else '')
            for slug, (src, photos, has_sections) in manifests.items():
                sel, note = photos_in_folder(slug, src, photos, has_sections, folder, top_only)
                if note:
                    notes.append(note)
                for p in sel:
                    key = (slug, p['id'])
                    if key in seen:
                        continue
                    seen.add(key)
                    d = None
                    if blat is not None and p.get('lat') is not None:
                        d = dist_km(blat, blon, p['lat'], p['lon'])
                    entries.append({
                        'trip': slug, 'id': p['id'], 'ar': p.get('ar'),
                        'thumb': f'hosted-photos/{slug}/thumbnails/{p["id"]}.webp',
                        'disp': f'hosted-photos/{slug}/display/{p["id"]}.webp',
                        'label': p.get('section') or p.get('building') or '',
                        'folder': short, 'ts': p.get('timestamp') or '',
                        'dist': None if d is None else round(d, 2),
                        'sel': key in picked,
                    })
        if blat is not None:
            entries.sort(key=lambda e: (e['dist'] is None, e['dist'] if e['dist'] is not None else 0, e['ts']))
        else:
            entries.sort(key=lambda e: e['ts'])
        # never bury an existing pick behind "Show more"
        entries.sort(key=lambda e: not e['sel'])
        cands[bridge] = {
            'photos': entries, 'total': len(entries), 'picked': sorted(picked),
            'folders': [Path(f).parent.name + '/' + Path(f).name + (' (top level)' if t else '')
                        for f, t in specs],
            'sorted_by': 'distance to bridge' if blat is not None else 'time',
            'note': ' · '.join(sorted(set(notes))),
        }
    return cands


# ---------------------------------------------------------------- HTML

PAGE_CSS = """
:root{--bg:#111;--panel:#1b1b1d;--fg:#eee;--muted:#9a9a9f;--line:#2c2c30;--pin:#ffd27a;--ok:#5ad17e;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header.top{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);
  padding:11px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header.top h1{font-size:16px;margin:0;font-weight:600}
header.top .sub{color:var(--muted)}
header.top nav{display:flex;gap:6px;flex-wrap:wrap}
header.top nav a{color:var(--fg);text-decoration:none;background:#26262a;padding:3px 9px;border-radius:12px;font-size:12px;white-space:nowrap}
header.top nav a:hover{background:#34343a}
.sec{padding:16px 18px;border-bottom:1px solid var(--line)}
.sec h2{font-size:15px;margin:0 0 2px;font-weight:600}
.sec .meta{color:var(--muted);font-size:12px;margin-bottom:9px}
.sec .meta .picked{color:var(--ok)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:9px}
.filters button{background:#26262a;color:var(--muted);border:1px solid var(--line);border-radius:12px;
  padding:2px 10px;font-size:11px;cursor:pointer}
.filters button.on{color:var(--fg);border-color:#4a9eff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.cell{position:relative;background:#000;border-radius:4px;overflow:hidden;aspect-ratio:3/2;cursor:pointer}
.cell img{width:100%;height:100%;object-fit:cover;display:block;background:#222}
.cell .id{position:absolute;left:0;right:0;bottom:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(transparent,rgba(0,0,0,.8));color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell .info{position:absolute;top:0;left:0;right:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(rgba(0,0,0,.75),transparent);color:var(--pin);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell:hover{outline:2px solid #4a9eff}
.cell.sel{outline:3px solid var(--ok)}
.cell.sel img{opacity:.55}
.cell .check{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;
  font-size:30px;color:var(--ok);text-shadow:0 0 6px #000;display:none}
.cell.sel .check{display:block}
.cell .open{position:absolute;top:3px;right:3px;z-index:3;width:22px;height:22px;border-radius:50%;
  background:rgba(0,0,0,.6);color:#fff;text-decoration:none;display:none;align-items:center;justify-content:center;font-size:13px}
.cell:hover .open{display:flex}
.cell .open:hover{background:#4a9eff}
.cell.hidden{display:none}
.more{margin-top:10px;display:flex;gap:8px;align-items:center}
.more button{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:6px 12px;font-size:12px;cursor:pointer}
.more button:hover{background:#34343a}
.more .left{color:var(--muted);font-size:12px}
.empty{color:var(--muted);font-size:12px;font-style:italic;padding:4px 0}
.warn{color:#e0a24a;font-size:12px;margin-bottom:8px}
.applybar{position:fixed;right:16px;bottom:16px;z-index:50;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:8px;box-shadow:0 6px 24px rgba(0,0,0,.5);min-width:220px}
.applybar .n b{color:var(--ok)}
.applybar button{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:7px 11px;font-size:13px;cursor:pointer}
.applybar button.apply{background:#2c6b3f;border-color:#2c6b3f;color:#fff;font-weight:600}
.applybar button.apply:hover{background:#357d49}
.applybar button:disabled{opacity:.4;cursor:default}
.applybar .msg{font-size:12px;color:var(--muted)}
.lightbox{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.88);display:none;
  align-items:center;justify-content:center;cursor:zoom-out}
.lightbox.open{display:flex}
.lightbox img{max-width:94vw;max-height:94vh;object-fit:contain;box-shadow:0 8px 40px rgba(0,0,0,.8);cursor:default}
"""


def render(cands):
    P = []
    P.append('<!doctype html><html lang=en><head><meta charset=utf-8>')
    P.append('<meta name=viewport content="width=device-width,initial-scale=1">')
    P.append('<title>bridge photo picker</title>')
    P.append(f'<style>{PAGE_CSS}</style></head><body>')
    P.append('<header class=top>')
    P.append('<h1>bridge photo picker</h1>')
    P.append('<span class=sub>click to toggle — pick every photo that belongs to the bridge · '
             '<span style="color:var(--ok)">▦</span> picked</span>')
    P.append('<nav>')
    for i, key in enumerate(cands):
        P.append(f'<a href="#s{i}">{html.escape(key)}</a>')
    P.append('</nav></header>')

    for i, (key, info) in enumerate(cands.items()):
        photos, total = info['photos'], info['total']
        P.append(f'<section class="sec" id="s{i}" data-key="{html.escape(key)}">')
        P.append(f'<h2>{html.escape(key)}</h2>')
        npicked = sum(1 for p in photos if p['sel'])
        P.append(f'<div class=meta>{total} candidate photos · sorted by {info["sorted_by"]} · '
                 f'<span class=picked id="m{i}">{npicked} picked</span></div>')
        if info['note']:
            P.append(f'<div class=warn>⚠ {html.escape(info["note"])}</div>')
        if len(info['folders']) > 1:
            P.append('<div class=filters><button class="on" data-folder="">all folders</button>')
            for f in info['folders']:
                P.append(f'<button data-folder="{html.escape(f)}">{html.escape(f)}</button>')
            P.append('</div>')
        P.append('<div class=grid>')
        for n, ph in enumerate(photos):
            hidden = ' hidden' if n >= INITIAL_SHOWN and not ph['sel'] else ''
            cls = 'cell' + (' sel' if ph['sel'] else '') + hidden
            bits = [ph['folder']]
            if ph['dist'] is not None:
                bits.append(f'{ph["dist"]} km')
            if ph['label']:
                bits.append(ph['label'])
            top = ' · '.join(bits)
            P.append(
                f'<div class="{cls}" data-trip="{html.escape(ph["trip"])}" data-id="{html.escape(ph["id"])}" '
                f'data-folder="{html.escape(ph["folder"])}" title="{html.escape(ph["trip"] + "/" + ph["id"])}">'
                f'<span class=info>{html.escape(top)}</span>'
                f'<a class=open href="{html.escape(ph["disp"])}" target=_blank rel=noopener title="open full size">⤢</a>'
                f'<span class=check>✓</span>'
                f'<img loading=lazy src="{html.escape(ph["thumb"])}" alt="{html.escape(ph["id"])}">'
                f'<span class=id>{html.escape(ph["id"])}</span></div>')
        if not photos:
            P.append('<div class=empty>no candidate photos — check the sources folders in '
                     'config/bridge_photo_picks.json</div>')
        P.append('</div>')
        if total > INITIAL_SHOWN:
            P.append(f'<div class=more data-step="{INITIAL_SHOWN}">'
                     f'<button class=showmore>Show more</button>'
                     f'<button class=showall>Show all {total}</button>'
                     f'<span class=left>{total - INITIAL_SHOWN} more hidden</span></div>')
        P.append('</section>')

    P.append(r"""
<div class=applybar>
  <div class=n><b id=nchg>0</b> bridge(s) changed</div>
  <button class=apply id=apply disabled>Apply to config</button>
  <button id=reset>Reset picks</button>
  <div class=msg id=msg></div>
</div>
<script>
// per-section "Show more / Show all"
document.querySelectorAll('.more').forEach(bar=>{
  const grid=bar.previousElementSibling, step=+bar.dataset.step;
  const left=bar.querySelector('.left');
  const reveal=n=>{
    const hid=grid.querySelectorAll('.cell.hidden');
    const k=n===Infinity?hid.length:Math.min(n,hid.length);
    for(let i=0;i<k;i++)hid[i].classList.remove('hidden');
    const rem=grid.querySelectorAll('.cell.hidden').length;
    if(rem===0){bar.remove();}else{left.textContent=rem+' more hidden';}
  };
  bar.querySelector('.showmore').onclick=()=>reveal(step);
  bar.querySelector('.showall').onclick=()=>reveal(Infinity);
});
// folder filter chips (visual filter only — selections persist regardless)
document.querySelectorAll('.filters').forEach(bar=>{
  const sec=bar.closest('.sec');
  bar.addEventListener('click',e=>{
    const b=e.target.closest('button'); if(!b)return;
    bar.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    const f=b.dataset.folder;
    sec.querySelectorAll('.cell').forEach(c=>{
      c.style.display=(!f||c.dataset.folder===f)?'':'none';
    });
  });
});
// selection state: per bridge, the set of "trip/id" keys
const secs=[...document.querySelectorAll('.sec')];
const orig=new Map(), cur=new Map();
for(const s of secs){
  const set=new Set([...s.querySelectorAll('.cell.sel')].map(c=>c.dataset.trip+'|'+c.dataset.id));
  orig.set(s.dataset.key,new Set(set));
  cur.set(s.dataset.key,set);
}
const nchg=document.getElementById('nchg'), applyBtn=document.getElementById('apply'), msg=document.getElementById('msg');
const sameSet=(a,b)=>a.size===b.size&&[...a].every(x=>b.has(x));
function refresh(){
  let n=0;
  secs.forEach((s,i)=>{
    const k=s.dataset.key;
    if(!sameSet(cur.get(k),orig.get(k)))n++;
    document.getElementById('m'+i).textContent=cur.get(k).size+' picked';
  });
  nchg.textContent=n;
  applyBtn.disabled=n===0;
}
document.addEventListener('click',e=>{
  if(e.target.closest('.open')||e.target.closest('.filters'))return;
  const cell=e.target.closest('.cell'); if(!cell)return;
  const k=cell.closest('.sec').dataset.key;
  const key=cell.dataset.trip+'|'+cell.dataset.id;
  const set=cur.get(k);
  if(set.has(key)){set.delete(key);cell.classList.remove('sel');}
  else{set.add(key);cell.classList.add('sel');}
  refresh();
});
document.getElementById('reset').onclick=()=>{
  for(const s of secs){
    const k=s.dataset.key, o=orig.get(k);
    cur.set(k,new Set(o));
    s.querySelectorAll('.cell').forEach(c=>{
      c.classList.toggle('sel',o.has(c.dataset.trip+'|'+c.dataset.id));
    });
  }
  msg.textContent=''; refresh();
};
applyBtn.onclick=async()=>{
  const changes={};
  for(const[k,set]of cur){
    if(sameSet(set,orig.get(k)))continue;
    changes[k]=[...set].map(x=>{const[trip,id]=x.split('|');return{trip,id};});
  }
  applyBtn.disabled=true; msg.textContent='saving…';
  try{
    const r=await fetch('/apply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({changes})});
    const j=await r.json();
    if(j.ok){
      for(const k in changes)orig.set(k,new Set(cur.get(k)));
      msg.textContent='✓ wrote '+j.written+' bridge(s). Run ./build_collections.py';
    }else{msg.textContent='error: '+(j.error||'unknown');}
  }catch(err){msg.textContent='error: '+err;}
  refresh();
};
// full-size preview: in-page lightbox instead of a new tab — click outside (or Esc) to close
const lb=document.createElement('div');lb.className='lightbox';lb.innerHTML='<img alt="">';
document.body.appendChild(lb);
const lbImg=lb.querySelector('img');
const lbClose=()=>{lb.classList.remove('open');lbImg.removeAttribute('src');};
document.addEventListener('click',e=>{
  const a=e.target.closest('a.open'); if(!a)return;
  e.preventDefault();
  lbImg.src=a.getAttribute('href');
  lb.classList.add('open');
});
lb.addEventListener('click',e=>{if(e.target!==lbImg)lbClose();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')lbClose();});
refresh();
</script>
</body></html>""")
    return '\n'.join(P)


# ---------------------------------------------------------------- server

def write_changes(changes):
    """Merge picks into config/bridge_photo_picks.json — only the bridges sent change.
    Picks are stored time-sorted so the gallery order is stable and diffable."""
    config = json.loads(CONFIG.read_text())
    picks = config.setdefault('picks', {})
    manifests = load_manifests()
    ts = {}
    for slug, (_src, photos, _hs) in manifests.items():
        for p in photos:
            ts[(slug, p['id'])] = p.get('timestamp') or ''
    for bridge, refs in changes.items():
        refs = sorted(refs, key=lambda r: ts.get((r['trip'], r['id']), ''))
        picks[bridge] = [{'trip': r['trip'], 'id': r['id']} for r in refs]
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    return len(changes)


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
                written = write_changes(data.get('changes', {}))
                payload = json.dumps({'ok': True, 'written': written}).encode()
            except Exception as e:                 # noqa: BLE001 — report back to the page
                payload = json.dumps({'ok': False, 'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return Handler


def main():
    if not CONFIG.exists():
        print(f'missing {CONFIG} — create it with a "sources" block first')
        sys.exit(1)
    cands = build_candidates()
    if not cands:
        print('no bridges in the config "sources" block — nothing to pick')
        sys.exit(1)
    page = render(cands)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(page))
    port = httpd.server_address[1]
    url = f'http://127.0.0.1:{port}/'
    for key, c in cands.items():
        print(f'  {key}: {c["total"]} candidates ({len(c["picked"])} already picked)'
              + (f'  ⚠ {c["note"]}' if c['note'] else ''))
    print(f'serving {url}  (Ctrl-C to stop)')
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped.')


if __name__ == '__main__':
    main()
