#!/usr/bin/env python3
"""Pick tile covers visually instead of trawling the Edits folders for paths.

Run it with ONE section name from config/tile_covers.json:

    tools/tile_cover_picker.py galleries     # per-trip Galleries-index covers
    tools/tile_cover_picker.py bridges       # per-bridge subtile covers
    tools/tile_cover_picker.py roads         # per-road-trip subtile covers
    tools/tile_cover_picker.py provinces     # per-province subtile covers
    tools/tile_cover_picker.py roofs         # per-building subtile covers
    tools/tile_cover_picker.py china         # hub facet + hero covers
    tools/tile_cover_picker.py rooftopping   # rooftopping hero
    tools/tile_cover_picker.py blogs         # per-blog covers
    tools/tile_cover_picker.py home          # landing-page facet tiles

It spins up a localhost server and opens a gallery: one section per key in that
config block, each showing the LANDSCAPE photos that tile is allowed to
represent (trip photos for 'galleries'/'blogs'; the photos the build already
grouped into that subtile for the China-hub facets, read from
web/collections/*.json). Click one photo per tile, then hit Apply — it writes
the chosen LOCAL edit filepaths straight into config/tile_covers.json. Keys you
don't touch are left exactly as they are, so partial passes are fine. An
"Auto-pick" chip per tile clears a key back to "".

After applying, run ./build_collections.py (and ./build_blogs.py for blogs) so
the new covers take effect — this tool only edits the config.
"""
import html
import json
import re
import sys
import threading
import webbrowser
from collections import OrderedDict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / 'web' / 'trips'
COLLECTIONS = ROOT / 'web' / 'collections'
CONFIG = ROOT / 'config' / 'tile_covers.json'

SECTIONS = ['home', 'galleries', 'blogs', 'china', 'rooftopping',
            'provinces', 'bridges', 'roads', 'roofs']
INITIAL_SHOWN = 150       # cells rendered visible per tile; the rest reveal on demand


def norm_stem(spec: str) -> str:
    """Filename/stem reduced the way the build matches it (drops edit suffixes)."""
    return re.split(r'-Enhanced|-NR|-SAI', Path(spec).stem)[0]


# ---------------------------------------------------------------- data loading

def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def full_manifest(slug: str):
    """A trip's FULL manifest (all photos, incl. private) for path/meta lookup."""
    return _load(TRIPS / slug / 'manifest.all.json') or _load(TRIPS / slug / 'manifest.json')


def public_manifest(slug: str):
    """A trip's privacy-filtered manifest (public photos for public trips)."""
    return _load(TRIPS / slug / 'manifest.json')


# Per-trip lookup: slug -> (edits_path, {id: {fn, building}}) built from full
# manifests, so a collection photo ref ({trip, id}) resolves to a local filepath.
_INDEX = {}


def trip_index(slug: str):
    if slug not in _INDEX:
        m = full_manifest(slug) or {}
        base = ((m.get('source') or {}).get('photos_path') or '').rstrip('/')
        ids = {p['id']: {'fn': p.get('source_filename'), 'building': p.get('building')}
               for p in m.get('photos', [])}
        _INDEX[slug] = (base, ids)
    return _INDEX[slug]


def local_path(trip: str, pid: str) -> str:
    """The LOCAL edit filepath stored in tile_covers.json (or bare stem if the
    source filename is unknown — the build still resolves a bare stem)."""
    base, ids = trip_index(trip)
    fn = (ids.get(pid) or {}).get('fn')
    return f'{base}/{fn}' if base and fn else pid


def to_photo(trip: str, pid: str, ar, building=None, current=None) -> dict:
    """Front-end photo entry. `current` is the pinned spec to highlight against."""
    path = local_path(trip, pid)
    cur = bool(current) and norm_stem(current) == norm_stem(pid)
    return {
        'trip': trip, 'id': pid, 'ar': ar,
        'path': path,
        'thumb': f'hosted-photos/{trip}/thumbnails/{pid}.webp',
        'disp': f'hosted-photos/{trip}/display/{pid}.webp',
        'building': building or '',
        'cur': cur,
    }


def landscape(photos):
    return [p for p in photos if (p.get('ar') or 1) > 1]


def dedup(refs):
    seen, out = set(), []
    for r in refs:
        k = (r['trip'], r['id'])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ---------------------------------------------------------------- per-section candidates

def _china_tile(tile_id):
    d = _load(COLLECTIONS / 'china.all.json') or {}
    return next((t for t in d.get('tiles', []) if t['id'] == tile_id), None)


def _subtiles(tile):
    """All subtiles of a hub tile, flattening the section grouping used by roofs."""
    if not tile:
        return []
    if tile.get('subtiles'):
        return tile['subtiles']
    return [s for sec in tile.get('sections', []) for s in sec.get('subtiles', [])]


def _tile_pool(tile):
    return dedup(p for s in _subtiles(tile) for p in s.get('photos', []))


def trip_landscape(slug, current, public_only):
    m = (public_manifest if public_only else full_manifest)(slug)
    if not m:
        return []
    base = ((m.get('source') or {}).get('photos_path') or '').rstrip('/')
    out = []
    for p in landscape(m.get('photos', [])):
        pid = p['id']
        fn = p.get('source_filename')
        path = f'{base}/{fn}' if base and fn else pid
        entry = to_photo(slug, pid, p.get('ar'), p.get('building'), current)
        entry['path'] = path
        out.append(entry)
    return out


def build_candidates(section, config):
    """OrderedDict key -> {'photos': [...], 'total': int, 'current': spec, 'note': str}."""
    block = config.get(section) or {}
    keys = [k for k in block if not k.startswith('_')]
    out = OrderedDict()

    def add(key, photos, note=''):
        out[key] = {'photos': photos, 'total': len(photos),
                    'current': block.get(key) or '', 'note': note}

    if section == 'galleries':
        # manifest.json is the right candidate set either way: it's the public
        # subset for public trips and the full (all-hidden) set for private trips.
        for slug in keys:
            add(slug, trip_landscape(slug, block.get(slug), public_only=True),
                '' if (TRIPS / slug).exists() else 'trip not processed')

    elif section in ('bridges', 'roads', 'provinces'):
        tile = _china_tile(section)
        by_title = {s.get('title'): s for s in _subtiles(tile)}
        for key in keys:
            s = by_title.get(key)
            photos = [to_photo(p['trip'], p['id'], p.get('ar'), current=block.get(key))
                      for p in landscape(s.get('photos', []))] if s else []
            add(key, photos, '' if s else 'no photos on the map for this tile')

    elif section == 'roofs':
        # 'roofs' covers are keyed by building name on the (world) rooftopping page,
        # which is the full 396-building set — not the China-only roofs tile.
        rt = _load(COLLECTIONS / 'rooftopping.json') or {}
        tile = next((t for t in rt.get('tiles', []) if t['id'] == 'roofs'), None)
        by_title = {s.get('title'): s for s in _subtiles(tile)}
        for key in keys:
            s = by_title.get(key)
            photos = [to_photo(p['trip'], p['id'], p.get('ar'), current=block.get(key))
                      for p in landscape(s.get('photos', []))] if s else []
            add(key, photos, '' if s else 'no photos for this building')

    elif section in ('china', 'rooftopping', 'home'):
        # Facet/hero covers: candidate pool = every landscape photo the facet groups.
        china = {fid: _tile_pool(_china_tile(fid))
                 for fid in ('roofs', 'roads', 'bridges', 'provinces')}
        china_all = dedup(p for pool in china.values() for p in pool)
        rt = _load(COLLECTIONS / 'rooftopping.json') or {}
        rt_tile = next((t for t in rt.get('tiles', []) if t['id'] == 'roofs'), None)
        rt_pool = _tile_pool(rt_tile)
        pools = {
            'hero': china_all, 'roofs': china['roofs'], 'roads': china['roads'],
            'bridges': china['bridges'], 'provinces': china['provinces'],
            'china': china_all, 'rooftopping': rt_pool,
        }
        for key in keys:
            pool = pools.get(key)
            if pool is None:
                add(key, [], 'no photo pool — set this one by hand (e.g. a screenshot)')
                continue
            photos = [to_photo(p['trip'], p['id'], p.get('ar'), current=block.get(key))
                      for p in landscape(pool)]
            add(key, photos)

    elif section == 'blogs':
        reg = (_load(ROOT / 'config' / 'blogs.json') or {}).get('blogs') or []
        trips_for = {b['slug']: b.get('trips', []) for b in reg}
        for key in keys:
            photos = []
            for slug in trips_for.get(key, []):
                photos += trip_landscape(slug, block.get(key), public_only=False)
            add(key, dedup(photos),
                '' if trips_for.get(key) else 'no trips mapped in blogs.json')
    else:
        for key in keys:
            add(key, [])

    return out


# ---------------------------------------------------------------- HTML

PAGE_CSS = """
:root{--bg:#111;--panel:#1b1b1d;--fg:#eee;--muted:#9a9a9f;--line:#2c2c30;--pin:#ffd27a;--ok:#5ad17e;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header.top{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);
  padding:11px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header.top h1{font-size:16px;margin:0;font-weight:600}
header.top .sub{color:var(--muted)}
header.top nav{display:none;flex-basis:100%;gap:6px;flex-wrap:wrap;justify-content:flex-start;
  max-height:42vh;overflow:auto;margin-top:8px}
header.top nav.open{display:flex}
header.top nav a{color:var(--fg);text-decoration:none;background:#26262a;padding:3px 9px;border-radius:12px;font-size:12px;white-space:nowrap}
header.top nav a:hover{background:#34343a}
header.top nav a.done{color:var(--ok)}
header.top button.navtoggle{margin-left:auto;background:#26262a;color:var(--fg);border:1px solid var(--line);
  border-radius:12px;padding:3px 11px;font-size:12px;cursor:pointer}
header.top button.navtoggle:hover{background:#34343a}
.sec{padding:16px 18px;border-bottom:1px solid var(--line)}
.sec h2{font-size:15px;margin:0 0 2px;font-weight:600;cursor:pointer;user-select:none}
.sec h2 .caret{display:inline-block;width:1em;color:var(--muted);transition:transform .1s}
.sec.collapsed h2 .caret{transform:rotate(-90deg)}
.sec.collapsed .grid,.sec.collapsed .more{display:none}
.sec .meta{color:var(--muted);font-size:12px;margin-bottom:9px}
.sec .meta .pin{color:var(--pin)}
.sec .meta .picked{color:var(--ok)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.cell{position:relative;background:#000;border-radius:4px;overflow:hidden;aspect-ratio:3/2;cursor:pointer}
.cell img{width:100%;height:100%;object-fit:cover;display:block;background:#222}
.cell .id{position:absolute;left:0;right:0;bottom:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(transparent,rgba(0,0,0,.8));color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell .bldg{position:absolute;top:0;left:0;right:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(rgba(0,0,0,.75),transparent);color:var(--pin);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell:hover{outline:2px solid #4a9eff}
.cell.cur{outline:2px solid var(--pin)}
.cell.sel{outline:3px solid var(--ok)}
.cell.sel img{opacity:.45}
.cell .check{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;
  font-size:30px;color:var(--ok);text-shadow:0 0 6px #000;display:none}
.cell.sel .check{display:block}
.cell .open{position:absolute;top:3px;right:3px;z-index:3;width:22px;height:22px;border-radius:50%;
  background:rgba(0,0,0,.6);color:#fff;text-decoration:none;display:none;align-items:center;justify-content:center;font-size:13px}
.cell:hover .open{display:flex}
.cell .open:hover{background:#4a9eff}
.cell.auto{aspect-ratio:3/2;display:flex;align-items:center;justify-content:center;text-align:center;
  background:#202024;color:var(--muted);font-size:12px;border:1px dashed var(--line)}
.cell.auto.sel{outline:3px solid var(--ok);color:var(--fg)}
.cell.hidden{display:none}
.more{margin-top:10px;display:flex;gap:8px;align-items:center}
.more button{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:6px 12px;font-size:12px;cursor:pointer}
.more button:hover{background:#34343a}
.more .left{color:var(--muted);font-size:12px}
.empty{color:var(--muted);font-size:12px;font-style:italic;padding:4px 0}
.applybar{position:fixed;right:16px;bottom:16px;z-index:50;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:8px;box-shadow:0 6px 24px rgba(0,0,0,.5);min-width:200px}
.applybar .n b{color:var(--ok)}
.applybar button{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:7px 11px;font-size:13px;cursor:pointer}
.applybar button.apply{background:#2c6b3f;border-color:#2c6b3f;color:#fff;font-weight:600}
.applybar button.apply:hover{background:#357d49}
.applybar button:disabled{opacity:.4;cursor:default}
.applybar .msg{font-size:12px;color:var(--muted)}
"""


def render(section, cands):
    P = []
    P.append('<!doctype html><html lang=en><head><meta charset=utf-8>')
    P.append('<meta name=viewport content="width=device-width,initial-scale=1">')
    P.append(f'<title>tile cover picker — {html.escape(section)}</title>')
    P.append(f'<style>{PAGE_CSS}</style></head><body>')
    P.append('<header class=top>')
    P.append(f'<h1>tile covers · {html.escape(section)}</h1>')
    P.append(f'<span class=sub>{len(cands)} tiles · pick one landscape photo per tile · '
             '<span style="color:var(--pin)">▦</span> current · '
             '<span style="color:var(--ok)">▦</span> your pick</span>')
    P.append(f'<button class=navtoggle id=navtoggle aria-expanded=false>jump to tile ({len(cands)}) ▾</button>')
    P.append('<nav id=nav>')
    for i, key in enumerate(cands):
        P.append(f'<a href="#s{i}" data-jump="{i}">{html.escape(key)}</a>')
    P.append('</nav></header>')

    for i, (key, info) in enumerate(cands.items()):
        photos, total, cur, note = info['photos'], info['total'], info['current'], info['note']
        # Tiles that already have a pin start collapsed (title only) — click to expand;
        # their hidden grids also skip loading images until opened.
        collapsed = ' collapsed' if cur else ''
        P.append(f'<section class="sec{collapsed}" id="s{i}" data-key="{html.escape(key)}">')
        P.append(f'<h2><span class=caret>▾</span> {html.escape(key)}</h2>')
        bits = [f'{total} landscape photo{"s" if total != 1 else ""}']
        if cur:
            bits.append(f'<span class=pin>pinned: {html.escape(Path(cur).name)}</span>')
        if note:
            bits.append(html.escape(note))
        P.append(f'<div class=meta>{" · ".join(bits)} <span class=picked id="m{i}"></span></div>')
        P.append('<div class=grid>')
        # auto-pick / clear chip
        autosel = ' sel' if not cur else ''
        P.append(f'<div class="cell auto{autosel}" data-val="">Auto-pick<br>(clear pin)<span class=check>✓</span></div>')
        for n, ph in enumerate(photos):
            # render every candidate but hide the overflow (lazy imgs in hidden
            # cells don't fetch until revealed); always keep the pinned one visible.
            hidden = ' hidden' if n >= INITIAL_SHOWN and not ph['cur'] else ''
            cls = 'cell' + (' cur sel' if ph['cur'] else '') + hidden
            bldg = f'<span class=bldg>🏠 {html.escape(ph["building"])}</span>' if ph['building'] else ''
            P.append(
                f'<div class="{cls}" data-val="{html.escape(ph["path"])}" '
                f'title="{html.escape(ph["path"])}">'
                f'{bldg}'
                f'<a class=open href="{html.escape(ph["disp"])}" target=_blank rel=noopener title="open full size">⤢</a>'
                f'<span class=check>✓</span>'
                f'<img loading=lazy src="{html.escape(ph["thumb"])}" alt="{html.escape(ph["id"])}">'
                f'<span class=id>{html.escape(ph["id"])}</span></div>')
        if not photos:
            P.append('<div class=empty>no candidate photos — pick Auto-pick or set this key by hand</div>')
        P.append('</div>')
        if total > INITIAL_SHOWN:
            P.append(f'<div class=more data-step="{INITIAL_SHOWN}">'
                     f'<button class=showmore>Show more</button>'
                     f'<button class=showall>Show all {total}</button>'
                     f'<span class=left>{total - INITIAL_SHOWN} more hidden</span></div>')
        P.append('</section>')

    P.append(f'<script>const SECTION={json.dumps(section)};</script>')
    P.append(r"""
<div class=applybar>
  <div class=n><b id=nchg>0</b> tile(s) changed</div>
  <button class=apply id=apply disabled>Apply to config</button>
  <button id=reset>Reset picks</button>
  <div class=msg id=msg></div>
</div>
<script>
// collapsible jump-to nav (collapsed by default so it doesn't eat the viewport)
const nav=document.getElementById('nav'), navBtn=document.getElementById('navtoggle');
navBtn.onclick=()=>{const o=nav.classList.toggle('open');navBtn.setAttribute('aria-expanded',o);
  navBtn.textContent=`jump to tile (${nav.children.length}) `+(o?'▴':'▾');};
nav.addEventListener('click',e=>{if(e.target.tagName==='A'){nav.classList.remove('open');
  navBtn.setAttribute('aria-expanded',false);navBtn.textContent=`jump to tile (${nav.children.length}) ▾`;
  document.getElementById('s'+e.target.dataset.jump)?.classList.remove('collapsed');}});  // expand on jump
// collapse/expand a tile by clicking its title (pinned tiles start collapsed)
document.querySelectorAll('.sec h2').forEach(h=>{
  h.addEventListener('click',()=>h.closest('.sec').classList.toggle('collapsed'));
});
// per-tile "Show more / Show all" — reveal hidden overflow cells incrementally
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
// per-section: original pinned value (data-val of the .cur cell, or '' if none)
const secs=[...document.querySelectorAll('.sec')];
const orig=new Map(), pick=new Map();
for(const s of secs){
  const cur=s.querySelector('.cell.cur');
  orig.set(s.dataset.key, cur?cur.dataset.val:'');
}
const nchg=document.getElementById('nchg'), applyBtn=document.getElementById('apply'), msg=document.getElementById('msg');
function changed(){let n=0;for(const[k,v]of pick)if(v!==orig.get(k))n++;return n;}
function refresh(){
  const n=changed();
  nchg.textContent=n;
  applyBtn.disabled=n===0;
  for(let i=0;i<secs.length;i++){
    const k=secs[i].dataset.key, m=document.getElementById('m'+i);
    if(pick.has(k)&&pick.get(k)!==orig.get(k)){
      const v=pick.get(k);
      m.textContent='→ '+(v?v.split('/').pop():'auto-pick');
    }else m.textContent='';
  }
  for(const a of document.querySelectorAll('nav a[data-jump]')){
    const k=secs[+a.dataset.jump].dataset.key;
    a.classList.toggle('done', pick.has(k)&&pick.get(k)!==orig.get(k));
  }
}
document.addEventListener('click',e=>{
  if(e.target.closest('.open'))return;
  const cell=e.target.closest('.cell'); if(!cell)return;
  const sec=cell.closest('.sec'), k=sec.dataset.key;
  for(const c of sec.querySelectorAll('.cell')) c.classList.remove('sel');
  cell.classList.add('sel');
  pick.set(k, cell.dataset.val);
  refresh();
});
document.getElementById('reset').onclick=()=>{
  pick.clear();
  for(const s of secs){
    for(const c of s.querySelectorAll('.cell')) c.classList.remove('sel');
    const cur=s.querySelector('.cell.cur'), auto=s.querySelector('.cell.auto');
    (cur||auto).classList.add('sel');
  }
  msg.textContent=''; refresh();
};
applyBtn.onclick=async()=>{
  const changes={};
  for(const[k,v]of pick)if(v!==orig.get(k))changes[k]=v;
  applyBtn.disabled=true; msg.textContent='saving…';
  try{
    const r=await fetch('/apply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({section:SECTION,changes})});
    const j=await r.json();
    if(j.ok){
      for(const k in changes)orig.set(k,changes[k]);
      msg.textContent='✓ wrote '+j.written+' key(s). Run ./build_collections.py';
    }else{msg.textContent='error: '+(j.error||'unknown');}
  }catch(err){msg.textContent='error: '+err;}
  refresh();
};
refresh();
</script>
</body></html>""")
    return '\n'.join(P)


# ---------------------------------------------------------------- server

def write_changes(section, changes):
    """Merge picks into config/tile_covers.json, preserving key order & untouched keys."""
    config = json.loads(CONFIG.read_text())
    block = config.setdefault(section, {})
    for key, val in changes.items():
        block[key] = val
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
                written = write_changes(data['section'], data.get('changes', {}))
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
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not args:
        print('usage: tile_cover_picker.py <section>\n  sections: ' + ', '.join(SECTIONS))
        sys.exit(1)
    section = args[0]
    config = json.loads(CONFIG.read_text())
    if section not in config:
        print(f'"{section}" is not a section in {CONFIG.name}.\n  available: '
              + ', '.join(k for k in config if not k.startswith('_')))
        sys.exit(1)

    cands = build_candidates(section, config)
    if not cands:
        print(f'No tiles found for section "{section}".')
        sys.exit(1)
    page = render(section, cands)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(page))
    port = httpd.server_address[1]
    url = f'http://127.0.0.1:{port}/'
    npics = sum(len(c['photos']) for c in cands.values())
    print(f'tile cover picker · section "{section}" · {len(cands)} tiles · {npics} candidate photos')
    print(f'serving {url}  (Ctrl-C to stop)')
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped.')


if __name__ == '__main__':
    main()
