#!/usr/bin/env python3
"""Pick the Highlights photos for each trip gallery (config/gallery_highlights.json).

Trip gallery pages (gallery.html) show their photos chronologically; a hand-picked
"Highlights" section at the top pulls the best shots out of the depths of the page.
This picker shows every processed trip as a collapsible section (newest first) with
all its photos in gallery order — multi-select the highlights, hit Apply.

    tools/gallery_highlights_picker.py [--public | --private]

A header filter switches between all photos, public only, and private only
(--public/--private just set the starting state). A photo counts as public when
its trip is public and it survives the privacy filter into manifest.json.

Apply rewrites the picks for the CHANGED trips only in config/gallery_highlights.json
(ids stored in time order, matching the gallery) and immediately re-emits
web/collections/gallery_highlights.json for the front-end, so a running dev server
shows the section on reload — no build needed. Trips with no picks get no entry and
no Highlights section. build_collections.py re-emits the web file on full builds.
"""
import html
import json
import sys
import threading
import webbrowser
from collections import OrderedDict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / 'web' / 'trips'
CONFIG = ROOT / 'config' / 'gallery_highlights.json'
WEB_OUT = ROOT / 'web' / 'collections' / 'gallery_highlights.json'

INITIAL_SHOWN = 150       # cells rendered visible per section; the rest reveal on demand


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def full_manifest(slug: str):
    return _load(TRIPS / slug / 'manifest.all.json') or _load(TRIPS / slug / 'manifest.json')


def build_candidates():
    config = _load(CONFIG) or {}
    picks = config.get('highlights') or {}
    index = _load(TRIPS / 'index.json') or {}
    trips = sorted(index.get('trips', []),
                   key=lambda t: ((t.get('dates') or {}).get('start') or ''), reverse=True)

    cands = OrderedDict()
    for trip in trips:
        slug = trip['id']
        man = full_manifest(slug)
        if not man:
            continue                       # pending placeholder — nothing to pick yet
        photos = sorted(man.get('photos', []), key=lambda p: p.get('timestamp') or '')
        if not photos:
            continue
        # public = the trip is public AND the photo survives into the filtered
        # manifest.json (private trips have no public photos at all)
        pub_ids = set()
        if trip.get('public') is not False:
            pub_ids = {p['id'] for p in (_load(TRIPS / slug / 'manifest.json') or {}).get('photos', [])}
        picked = set(picks.get(slug) or [])
        cands[slug] = {
            'name': trip.get('name') or slug,
            'photos': [{
                'id': p['id'], 'ar': p.get('ar'),
                'thumb': f'hosted-photos/{slug}/thumbnails/{p["id"]}.webp',
                'disp': f'hosted-photos/{slug}/display/{p["id"]}.webp',
                'label': p.get('building') or p.get('section') or '',
                'sel': p['id'] in picked,
                'pub': p['id'] in pub_ids,
            } for p in photos],
            'total': len(photos),
            'n_picked': sum(1 for p in photos if p['id'] in picked),
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
.sec .meta .picked{color:var(--ok)}
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
.applybar{position:fixed;right:16px;bottom:16px;z-index:50;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:8px;box-shadow:0 6px 24px rgba(0,0,0,.5);min-width:220px}
.applybar .n b{color:var(--ok)}
.applybar button{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:7px 11px;font-size:13px;cursor:pointer}
.applybar button.apply{background:#2c6b3f;border-color:#2c6b3f;color:#fff;font-weight:600}
.applybar button.apply:hover{background:#357d49}
.applybar button:disabled{opacity:.4;cursor:default}
.applybar .msg{font-size:12px;color:var(--muted)}
.vis{display:flex;gap:6px}
.vis button{background:#26262a;color:var(--muted);border:1px solid var(--line);border-radius:12px;
  padding:3px 11px;font-size:12px;cursor:pointer}
.vis button.on{color:var(--fg);border-color:#4a9eff}
body.f-public .cell[data-pub="0"]{display:none}
body.f-private .cell[data-pub="1"]{display:none}
.sec.novis{display:none}
.lightbox{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.88);display:none;
  align-items:center;justify-content:center;cursor:zoom-out}
.lightbox.open{display:flex}
.lightbox img{max-width:94vw;max-height:90vh;object-fit:contain;box-shadow:0 8px 40px rgba(0,0,0,.8);cursor:pointer}
.lightbox .lbbar{position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;justify-content:center;
  gap:16px;padding:14px 12px 12px;background:linear-gradient(transparent,rgba(0,0,0,.75));
  font-size:13px;color:#ddd;cursor:default}
.lightbox .lbstate{cursor:pointer;padding:3px 12px;border-radius:12px;border:1px solid #666;color:#bbb;user-select:none}
.lightbox .lbstate.on{color:var(--ok);border-color:var(--ok)}
.lightbox .lbhint{color:#888;font-size:11px}
"""


def render(cands, visibility='all'):
    P = []
    P.append('<!doctype html><html lang=en><head><meta charset=utf-8>')
    P.append('<meta name=viewport content="width=device-width,initial-scale=1">')
    P.append('<title>gallery highlights picker</title>')
    body_cls = {'public': ' class=f-public', 'private': ' class=f-private'}.get(visibility, '')
    P.append(f'<style>{PAGE_CSS}</style></head><body{body_cls}>')
    P.append('<header class=top>')
    P.append('<h1>gallery highlights</h1>')
    P.append('<span class=sub>click a trip to expand · multi-select the photos for its '
             'Highlights section · <span style="color:var(--ok)">▦</span> picked</span>')
    P.append('<span class=vis id=vis>'
             + ''.join(f'<button data-vis="{v}"{" class=on" if v == visibility else ""}>{t}</button>'
                       for v, t in (('all', 'All'), ('public', 'Public only'), ('private', 'Private only')))
             + '</span>')
    P.append(f'<button class=navtoggle id=navtoggle aria-expanded=false>jump to trip ({len(cands)}) ▾</button>')
    P.append('<nav id=nav>')
    for i, (slug, info) in enumerate(cands.items()):
        P.append(f'<a href="#s{i}" data-jump="{i}">{html.escape(info["name"])}</a>')
    P.append('</nav></header>')

    for i, (slug, info) in enumerate(cands.items()):
        photos, total = info['photos'], info['total']
        P.append(f'<section class="sec collapsed" id="s{i}" data-key="{html.escape(slug)}">')
        P.append(f'<h2><span class=caret>▾</span> {html.escape(info["name"])}</h2>')
        P.append(f'<div class=meta>{total} photos, gallery order · '
                 f'<span class=picked id="m{i}">{info["n_picked"]} picked</span></div>')
        P.append('<div class=grid>')
        for n, ph in enumerate(photos):
            hidden = ' hidden' if n >= INITIAL_SHOWN and not ph['sel'] else ''
            cls = 'cell' + (' sel' if ph['sel'] else '') + hidden
            info_tag = f'<span class=info>{html.escape(ph["label"])}</span>' if ph['label'] else ''
            P.append(
                f'<div class="{cls}" data-id="{html.escape(ph["id"])}" data-pub="{1 if ph["pub"] else 0}" '
                f'title="{html.escape(slug + "/" + ph["id"])}">'
                f'{info_tag}'
                f'<a class=open href="{html.escape(ph["disp"])}" target=_blank rel=noopener title="open full size">⤢</a>'
                f'<span class=check>✓</span>'
                f'<img loading=lazy src="{html.escape(ph["thumb"])}" alt="{html.escape(ph["id"])}">'
                f'<span class=id>{html.escape(ph["id"])}</span></div>')
        P.append('</div>')
        if total > INITIAL_SHOWN:
            P.append(f'<div class=more data-step="{INITIAL_SHOWN}">'
                     f'<button class=showmore>Show more</button>'
                     f'<button class=showall>Show all {total}</button>'
                     f'<span class=left>{total - INITIAL_SHOWN} more hidden</span></div>')
        P.append('</section>')

    P.append(r"""
<div class=applybar>
  <div class=n><b id=nchg>0</b> galler(ies) changed</div>
  <button class=apply id=apply disabled>Apply to config</button>
  <button id=reset>Reset picks</button>
  <div class=msg id=msg></div>
</div>
<script>
// public / private / all visibility filter — hides non-matching cells and
// whole trips left with nothing to show (selections persist regardless)
const vis=document.getElementById('vis');
function applyVis(){
  const mode=document.body.classList.contains('f-public')?'public'
    :document.body.classList.contains('f-private')?'private':'all';
  document.querySelectorAll('.sec').forEach(s=>{
    const any=mode==='all'||s.querySelector(`.cell[data-pub="${mode==='public'?1:0}"]`);
    s.classList.toggle('novis',!any);
  });
}
vis.addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b)return;
  vis.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');
  document.body.classList.remove('f-public','f-private');
  if(b.dataset.vis==='public')document.body.classList.add('f-public');
  if(b.dataset.vis==='private')document.body.classList.add('f-private');
  applyVis();
});
applyVis();
// collapsible jump-to nav (collapsed by default so it doesn't eat the viewport)
const nav=document.getElementById('nav'), navBtn=document.getElementById('navtoggle');
navBtn.onclick=()=>{const o=nav.classList.toggle('open');navBtn.setAttribute('aria-expanded',o);
  navBtn.textContent=`jump to trip (${nav.children.length}) `+(o?'▴':'▾');};
nav.addEventListener('click',e=>{if(e.target.tagName==='A'){nav.classList.remove('open');
  navBtn.setAttribute('aria-expanded',false);navBtn.textContent=`jump to trip (${nav.children.length}) ▾`;
  document.getElementById('s'+e.target.dataset.jump)?.classList.remove('collapsed');}});
// collapse/expand a trip by clicking its title
document.querySelectorAll('.sec h2').forEach(h=>{
  h.addEventListener('click',()=>h.closest('.sec').classList.toggle('collapsed'));
});
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
// selection state: per trip, the set of picked photo ids
const secs=[...document.querySelectorAll('.sec')];
const orig=new Map(), cur=new Map();
for(const s of secs){
  const set=new Set([...s.querySelectorAll('.cell.sel')].map(c=>c.dataset.id));
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
  for(const a of document.querySelectorAll('nav a[data-jump]')){
    const k=secs[+a.dataset.jump].dataset.key;
    a.classList.toggle('done',!sameSet(cur.get(k),orig.get(k)));
  }
}
function toggleCell(cell){
  const k=cell.closest('.sec').dataset.key;
  const set=cur.get(k), id=cell.dataset.id;
  if(set.has(id)){set.delete(id);cell.classList.remove('sel');}
  else{set.add(id);cell.classList.add('sel');}
  refresh();
}
document.addEventListener('click',e=>{
  if(e.target.closest('.open'))return;
  const cell=e.target.closest('.cell'); if(!cell)return;
  toggleCell(cell);
});
document.getElementById('reset').onclick=()=>{
  for(const s of secs){
    const k=s.dataset.key, o=orig.get(k);
    cur.set(k,new Set(o));
    s.querySelectorAll('.cell').forEach(c=>c.classList.toggle('sel',o.has(c.dataset.id)));
  }
  msg.textContent=''; refresh();
};
applyBtn.onclick=async()=>{
  const changes={};
  for(const[k,set]of cur){
    if(!sameSet(set,orig.get(k)))changes[k]=[...set];
  }
  applyBtn.disabled=true; msg.textContent='saving…';
  try{
    const r=await fetch('/apply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({changes})});
    const j=await r.json();
    if(j.ok){
      for(const k in changes)orig.set(k,new Set(cur.get(k)));
      msg.textContent='✓ wrote '+j.written+' galler(ies) — live on reload';
    }else{msg.textContent='error: '+(j.error||'unknown');}
  }catch(err){msg.textContent='error: '+err;}
  refresh();
};
// full-size preview: in-page lightbox — arrows navigate the trip's visible photos,
// space / clicking the photo (or the badge) toggles it in the highlights,
// click outside or Esc closes
const lb=document.createElement('div');lb.className='lightbox';
lb.innerHTML='<img alt=""><div class=lbbar><span class=lbinfo></span><span class=lbstate></span>'
  +'<span class=lbhint>← → navigate · space or click photo to toggle · esc close</span></div>';
document.body.appendChild(lb);
const lbImg=lb.querySelector('img'),lbInfo=lb.querySelector('.lbinfo'),lbState=lb.querySelector('.lbstate');
let lbCells=[],lbIdx=-1;
const lbDisp=c=>c.querySelector('a.open').getAttribute('href');
function lbPaint(){
  const c=lbCells[lbIdx]; if(!c)return;
  lbImg.src=lbDisp(c);
  lbInfo.textContent=c.dataset.id+' · '+(lbIdx+1)+' / '+lbCells.length;
  const on=c.classList.contains('sel');
  lbState.textContent=on?'✓ in highlights':'add to highlights';
  lbState.classList.toggle('on',on);
  [lbIdx-1,lbIdx+1].forEach(i=>{if(lbCells[i])new Image().src=lbDisp(lbCells[i]);});  // preload neighbours
}
function lbShow(i){if(i<0||i>=lbCells.length)return;lbIdx=i;lbPaint();}
function lbToggle(){const c=lbCells[lbIdx];if(!c)return;toggleCell(c);lbPaint();}
const lbClose=()=>{lb.classList.remove('open');lbImg.removeAttribute('src');lbCells=[];lbIdx=-1;};
document.addEventListener('click',e=>{
  const a=e.target.closest('a.open'); if(!a)return;
  e.preventDefault();
  const cell=a.closest('.cell');
  // navigate within the photos actually on screen: same trip, current visibility
  // filter applied, "Show more" overflow excluded until revealed
  lbCells=[...cell.closest('.sec').querySelectorAll('.cell')].filter(c=>c.offsetParent!==null);
  lbIdx=lbCells.indexOf(cell);
  lb.classList.add('open');
  lbPaint();
});
lb.addEventListener('click',e=>{
  if(e.target===lbImg||e.target===lbState){lbToggle();return;}
  if(e.target.closest('.lbbar'))return;
  lbClose();
});
document.addEventListener('keydown',e=>{
  if(!lb.classList.contains('open'))return;
  if(e.key==='Escape')lbClose();
  else if(e.key==='ArrowRight'){e.preventDefault();lbShow(lbIdx+1);}
  else if(e.key==='ArrowLeft'){e.preventDefault();lbShow(lbIdx-1);}
  else if(e.key===' '||e.key==='Enter'){e.preventDefault();lbToggle();}
});
refresh();
</script>
</body></html>""")
    return '\n'.join(P)


# ---------------------------------------------------------------- server

def emit_web_highlights():
    """Validated {slug: [ids]} for the front-end → web/collections/gallery_highlights.json.
    Mirrors build_collections.emit_gallery_highlights so Apply is live immediately."""
    picks = (_load(CONFIG) or {}).get('highlights') or {}
    out = {}
    for slug, ids in picks.items():
        man = full_manifest(slug)
        if not man:
            continue
        known = {p['id'] for p in man.get('photos', [])}
        valid = [i for i in ids if i in known]
        if valid:
            out[slug] = valid
    WEB_OUT.parent.mkdir(parents=True, exist_ok=True)
    WEB_OUT.write_text(json.dumps(out, indent=2))
    return len(out)


def write_changes(changes):
    """Merge picks into config/gallery_highlights.json — only the trips sent change;
    an empty selection removes the trip's entry (no Highlights section). Ids are
    stored in gallery (time) order, then the web file is re-emitted."""
    config = json.loads(CONFIG.read_text())
    picks = config.setdefault('highlights', {})
    for slug, ids in changes.items():
        man = full_manifest(slug) or {}
        order = {p['id']: p.get('timestamp') or '' for p in man.get('photos', [])}
        ids = [i for i in ids if i in order]
        if ids:
            picks[slug] = sorted(ids, key=lambda i: order[i])
        else:
            picks.pop(slug, None)
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    emit_web_highlights()
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
        CONFIG.write_text(json.dumps({'highlights': {}}, indent=2) + '\n')
    visibility = ('public' if '--public' in sys.argv
                  else 'private' if '--private' in sys.argv else 'all')
    cands = build_candidates()
    if not cands:
        print('no processed trips found — nothing to pick')
        sys.exit(1)
    page = render(cands, visibility)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(page))
    port = httpd.server_address[1]
    url = f'http://127.0.0.1:{port}/'
    npics = sum(c['total'] for c in cands.values())
    npicked = sum(c['n_picked'] for c in cands.values())
    print(f'gallery highlights picker · {len(cands)} trips · {npics} photos · {npicked} already picked')
    print(f'serving {url}  (Ctrl-C to stop)')
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped.')


if __name__ == '__main__':
    main()
