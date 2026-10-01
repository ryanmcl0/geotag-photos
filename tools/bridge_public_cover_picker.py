#!/usr/bin/env python3
"""Pick each bridge's PUBLIC thumbnail for the locked Bridges preview
(config/bridge_public_covers.json).

Locked visitors see the Bridges page as a gallery-less preview: one thumbnail per
bridge, never a private photo (see _bridge_preview in build_collections.py). This
picker shows, per bridge that has photos:

  - every PUBLIC photo of that bridge (public trip, in the public manifest), or
  - for bridges with no public photos at all, the PHONE photos from the visit:
    phone shots within 5 km of the bridge, plus phone shots taken during the
    camera session (camera files are on UK time, the phone on China time, so the
    window is shifted +8h, padded 90 min either side).

One pick per bridge (click to choose, click again to clear back to auto). The
pre-selected cell is the current pick, else what the build would auto-choose.
Apply writes only the changed bridges. Then run ./build_collections.py.

    tools/bridge_public_cover_picker.py

Reads web/collections/china.all.json, so run build_collections.py first.
"""
import html
import json
import math
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_privacy  # noqa: E402

TRIPS = ROOT / 'web' / 'trips'
PHONE = ROOT / 'web' / 'phone' / 'trips'
FULL = ROOT / 'web' / 'collections' / 'china.all.json'
ROSTER = ROOT / 'config' / 'china_bridges.json'
CONFIG = ROOT / 'config' / 'bridge_public_covers.json'

PHONE_RADIUS_KM = 5.0
CAMERA_TO_PHONE = timedelta(hours=8)     # camera UK time → phone China time
WINDOW_PAD = timedelta(minutes=90)


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def dist_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    x = (lon2 - lon1) * p * math.cos((lat1 + lat2) / 2 * p)
    y = (lat2 - lat1) * p
    return 6371 * math.hypot(x, y)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '')[:19])
    except ValueError:
        return None


# ---------------------------------------------------------------- candidates

def public_ids(slug, cache, public_trips):
    if slug not in cache:
        man = _load(TRIPS / slug / 'manifest.json') if public_trips.get(slug) else None
        cache[slug] = {p['id'] for p in (man or {}).get('photos', [])}
    return cache[slug]


def full_photos(slug, cache):
    if slug not in cache:
        man = photo_privacy.load_full_manifest(TRIPS / slug) or {}
        cache[slug] = {p['id']: p for p in man.get('photos', [])}
    return cache[slug]


def load_phone():
    out = []
    for mf in sorted(PHONE.glob('phone-*/manifest.json')):
        if mf.parent.name.startswith('phone-misc-'):
            continue
        for p in (_load(mf) or {}).get('photos', []):
            out.append((mf.parent.name, p))
    return out


def build_candidates():
    full = _load(FULL)
    if not full:
        sys.exit(f'missing {FULL.relative_to(ROOT)} (run ./build_collections.py first)')
    bridges = next((t for t in full['tiles'] if t['id'] == 'bridges'), None)
    if not bridges:
        sys.exit('no bridges tile in china.all.json')
    roster = {b['name']: b for b in (_load(ROSTER) or {}).get('bridges', [])}
    picks = (_load(CONFIG) or {}).get('covers', {})
    idx = _load(TRIPS / 'index.json') or {}
    public_trips = {t['id']: t.get('public', False) for t in idx.get('trips', [])}
    pub_cache, full_cache = {}, {}
    phone = None

    subs = (bridges.get('subtiles') or []) + [s for sec in bridges.get('sections') or []
                                              for s in sec.get('subtiles') or []]
    cands = []
    for s in subs:
        photos = s.get('photos') or []
        if not photos:
            continue
        b = roster.get(s['title'], {})
        lat, lon = b.get('lat'), b.get('lon')
        pick = picks.get(s['title']) or {}
        pub = [p for p in photos if p['id'] in public_ids(p['trip'], pub_cache, public_trips)]
        cells = []
        if pub:
            # what the build picks with no config entry: own cover if public, else first landscape
            cov = s.get('cover') or {}
            auto = next((p for p in pub if p['trip'] == cov.get('trip') and p['id'] == cov.get('id')), None) \
                or next((p for p in pub if p.get('ar', 1) > 1), pub[0])
            for p in pub:
                meta = full_photos(p['trip'], full_cache).get(p['id'], {})
                d = (round(dist_km(lat, lon, meta['lat'], meta['lon']), 1)
                     if lat is not None and meta.get('lat') is not None else None)
                cells.append({
                    'kind': 'trip', 'trip': p['trip'], 'id': p['id'],
                    'thumb': f'hosted-photos/{p["trip"]}/thumbnails/{p["id"]}.webp',
                    'disp': f'hosted-photos/{p["trip"]}/display/{p["id"]}.webp',
                    'label': ' · '.join(x for x in [(meta.get('timestamp') or '')[:16].replace('T', ' '),
                                                    f'{d} km' if d is not None else ''] if x),
                    'ts': meta.get('timestamp') or '',
                    'auto': p is auto,
                    'sel': pick.get('trip') == p['trip'] and pick.get('id') == p['id'],
                })
            cells.sort(key=lambda c: (not c['auto'], c['ts']))
            source = f'{len(pub)} public of {len(photos)}'
        else:
            if phone is None:
                phone = load_phone()
            times = sorted(t for t in (parse_ts(full_photos(p['trip'], full_cache).get(p['id'], {}).get('timestamp'))
                                       for p in photos) if t)
            lo = times[0] + CAMERA_TO_PHONE - WINDOW_PAD if times else None
            hi = times[-1] + CAMERA_TO_PHONE + WINDOW_PAD if times else None
            for pt, p in phone:
                d = (dist_km(lat, lon, p['lat'], p['lon'])
                     if lat is not None and p.get('lat') is not None else None)
                t = parse_ts(p.get('timestamp'))
                near = d is not None and d <= PHONE_RADIUS_KM
                during = t is not None and lo is not None and lo <= t <= hi
                if not (near or during):
                    continue
                cells.append({
                    'kind': 'phone', 'trip': pt, 'id': p['id'],
                    'thumb': f'web/phone/trips/{pt}/{p.get("thumbnail") or "thumbnails/" + p["id"] + ".webp"}',
                    'disp': f'web/phone/trips/{pt}/{p.get("display") or "display/" + p["id"] + ".webp"}',
                    'label': ' · '.join(x for x in [(p.get('timestamp') or '')[:16].replace('T', ' '),
                                                    f'{d:.1f} km' if d is not None else 'no GPS',
                                                    'near' if near else '', 'during visit' if during else ''] if x),
                    'ts': p.get('timestamp') or '',
                    'auto': False,
                    'sel': pick.get('phone_trip') == pt and pick.get('id') == p['id'],
                })
            cells.sort(key=lambda c: c['ts'])
            source = f'no public photos ({len(photos)} private) · {len(cells)} phone photos from the visit'
        if not any(c['sel'] for c in cells):
            for c in cells:
                c['sel'] = c['auto']
        cands.append({'title': s['title'], 'rank': s.get('rank'), 'source': source,
                      'phone': not pub, 'cells': cells,
                      'picked': bool(pick)})
    return cands


# ---------------------------------------------------------------- page

PAGE_CSS = """
:root{--bg:#111;--panel:#1b1b1d;--fg:#eee;--muted:#9a9a9f;--line:#2c2c30;--pin:#ffd27a;--ok:#5ad17e;--phone:#7fb4ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header.top{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);
  padding:11px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header.top h1{font-size:16px;margin:0;font-weight:600}
header.top .sub{color:var(--muted)}
header.top nav{display:flex;gap:6px;flex-wrap:wrap}
header.top nav a{color:var(--fg);text-decoration:none;background:#26262a;padding:3px 9px;border-radius:12px;font-size:12px;white-space:nowrap}
header.top nav a.phone{color:var(--phone)}
header.top nav a:hover{background:#34343a}
.sec{padding:16px 18px;border-bottom:1px solid var(--line)}
.sec h2{font-size:15px;margin:0 0 2px;font-weight:600}
.sec .meta{color:var(--muted);font-size:12px;margin-bottom:9px}
.sec.phone .meta{color:var(--phone)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.cell{position:relative;background:#000;border-radius:4px;overflow:hidden;aspect-ratio:3/2;cursor:pointer}
.cell img{width:100%;height:100%;object-fit:cover;display:block;background:#222}
.cell .id{position:absolute;left:0;right:0;bottom:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(transparent,rgba(0,0,0,.8));color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell .info{position:absolute;top:0;left:0;right:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(rgba(0,0,0,.75),transparent);color:var(--pin);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell .auto{position:absolute;bottom:16px;left:4px;font-size:10px;background:#444;color:#fff;padding:1px 5px;border-radius:8px}
.cell:hover{outline:2px solid #4a9eff}
.cell.sel{outline:3px solid var(--ok)}
.cell .check{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;
  font-size:30px;color:var(--ok);text-shadow:0 0 6px #000;display:none}
.cell.sel .check{display:block}
.cell .open{position:absolute;top:3px;right:3px;z-index:3;width:24px;height:24px;border-radius:50%;
  background:rgba(0,0,0,.6);color:#fff;text-decoration:none;display:none;align-items:center;justify-content:center;font-size:13px}
.cell:hover .open{display:flex}
.cell .open:hover{background:#4a9eff}
.empty{color:var(--muted);font-size:12px;font-style:italic;padding:4px 0}
.applybar{position:fixed;right:16px;bottom:16px;z-index:50;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:8px;box-shadow:0 6px 24px rgba(0,0,0,.5);min-width:220px}
.applybar .n b{color:var(--ok)}
.applybar button{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:7px 11px;font-size:13px;cursor:pointer}
.applybar button.apply{background:#2c6b3f;border-color:#2c6b3f;color:#fff;font-weight:600}
.applybar button:disabled{opacity:.4;cursor:default}
.applybar .msg{font-size:12px;color:var(--muted)}
.lightbox{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.9);display:none;align-items:center;justify-content:center}
.lightbox.open{display:flex}
.lightbox img{max-width:92vw;max-height:86vh;object-fit:contain;box-shadow:0 8px 40px rgba(0,0,0,.8)}
.lightbox .cap{position:absolute;bottom:14px;left:0;right:0;text-align:center;color:#ccc;font-size:13px}
.lightbox .cap b{color:var(--ok)}
.lightbox .nav{position:absolute;top:50%;transform:translateY(-50%);font-size:40px;color:#fff;background:none;border:0;cursor:pointer;padding:20px;opacity:.6}
.lightbox .nav:hover{opacity:1}
.lightbox .prev{left:0}.lightbox .next{right:0}
"""


def render(cands):
    E = html.escape
    P = ['<!doctype html><html lang=en><head><meta charset=utf-8>',
         '<meta name=viewport content="width=device-width,initial-scale=1">',
         '<title>bridge public thumbnails</title>',
         f'<style>{PAGE_CSS}</style></head><body>',
         '<header class=top><h1>bridge public thumbnails</h1>',
         '<span class=sub>one pick per bridge · click to choose, click again for auto · '
         'photo viewer: ⤢ or double-click, ←/→ to move, space to pick · '
         '<span style="color:var(--phone)">blue</span> = phone photos (no public shots)</span><nav>']
    for i, c in enumerate(cands):
        P.append(f'<a href="#s{i}" class="{"phone" if c["phone"] else ""}">'
                 f'{E(str(c["rank"] or "·"))} {E(c["title"].replace(" Bridge", ""))}</a>')
    P.append('</nav></header>')
    for i, c in enumerate(cands):
        P.append(f'<section class="sec{" phone" if c["phone"] else ""}" id="s{i}" data-key="{E(c["title"])}">')
        P.append(f'<h2>{E(str(c["rank"] or "·"))} · {E(c["title"])}</h2>')
        P.append(f'<div class=meta>{E(c["source"])}{" · has a saved pick" if c["picked"] else ""}</div>')
        P.append('<div class=grid>')
        for ph in c['cells']:
            P.append(
                f'<div class="cell{" sel" if ph["sel"] else ""}" data-kind="{ph["kind"]}" '
                f'data-trip="{E(ph["trip"])}" data-id="{E(ph["id"])}" data-disp="{E(ph["disp"])}" '
                f'data-auto="{1 if ph["auto"] else 0}" title="{E(ph["trip"] + "/" + ph["id"])}">'
                f'<span class=info>{E(ph["label"])}</span>'
                f'<a class=open href="{E(ph["disp"])}" title="view large">⤢</a>'
                f'<span class=check>✓</span>'
                f'{"<span class=auto>auto</span>" if ph["auto"] else ""}'
                f'<img loading=lazy src="{E(ph["thumb"])}" alt="{E(ph["id"])}">'
                f'<span class=id>{E(ph["id"])}</span></div>')
        if not c['cells']:
            P.append('<div class=empty>no candidates: no public photos and no phone photos near the bridge or during the visit</div>')
        P.append('</div></section>')
    P.append(r"""
<div class=applybar>
  <div class=n><b id=nchg>0</b> bridge(s) changed</div>
  <button class=apply id=apply disabled>Apply to config</button>
  <button id=reset>Reset</button>
  <div class=msg id=msg></div>
</div>
<div class=lightbox id=lb><button class="nav prev">‹</button><img alt=""><button class="nav next">›</button><div class=cap></div></div>
<script>
const secs=[...document.querySelectorAll('.sec')];
const keyOf=c=>c?c.dataset.kind+'|'+c.dataset.trip+'|'+c.dataset.id:'';
const orig=new Map(), cur=new Map();
secs.forEach(s=>{const k=keyOf(s.querySelector('.cell.sel'));orig.set(s.dataset.key,k);cur.set(s.dataset.key,k);});
const nchg=document.getElementById('nchg'), applyBtn=document.getElementById('apply'), msg=document.getElementById('msg');
function refresh(){
  let n=0; for(const[k,v]of cur) if(v!==orig.get(k)) n++;
  nchg.textContent=n; applyBtn.disabled=n===0;
}
function choose(cell){
  const sec=cell.closest('.sec'), k=sec.dataset.key;
  const was=cell.classList.contains('sel');
  sec.querySelectorAll('.cell.sel').forEach(c=>c.classList.remove('sel'));
  if(!was){cell.classList.add('sel');cur.set(k,keyOf(cell));}
  else{  // clear → back to the auto pick (or nothing, for phone-only bridges)
    const a=sec.querySelector('.cell[data-auto="1"]');
    if(a)a.classList.add('sel');
    cur.set(k,keyOf(a));
  }
  refresh();
}
// lightbox over one bridge's cells: arrows move, space picks, Esc / click outside closes
const lb=document.getElementById('lb'), lbImg=lb.querySelector('img'), lbCap=lb.querySelector('.cap');
let lbCells=[], lbI=0;
function show(){
  const c=lbCells[lbI]; lbImg.src=c.dataset.disp;
  lbCap.innerHTML=(c.classList.contains('sel')?'<b>✓ picked</b> · ':'')+c.querySelector('.info').textContent+
    ' · '+c.dataset.id+' · '+(lbI+1)+'/'+lbCells.length;
}
function openLb(cell){lbCells=[...cell.closest('.grid').querySelectorAll('.cell')];lbI=lbCells.indexOf(cell);show();lb.classList.add('open');}
function closeLb(){lb.classList.remove('open');lbImg.removeAttribute('src');}
lb.querySelector('.prev').onclick=e=>{e.stopPropagation();lbI=(lbI-1+lbCells.length)%lbCells.length;show();};
lb.querySelector('.next').onclick=e=>{e.stopPropagation();lbI=(lbI+1)%lbCells.length;show();};
lb.addEventListener('click',e=>{if(e.target===lb)closeLb();});
lbImg.addEventListener('click',()=>{choose(lbCells[lbI]);show();});
document.addEventListener('keydown',e=>{
  if(!lb.classList.contains('open'))return;
  if(e.key==='Escape')closeLb();
  else if(e.key==='ArrowLeft'){lbI=(lbI-1+lbCells.length)%lbCells.length;show();}
  else if(e.key==='ArrowRight'){lbI=(lbI+1)%lbCells.length;show();}
  else if(e.key===' '){e.preventDefault();choose(lbCells[lbI]);show();}
});
document.addEventListener('click',e=>{
  const o=e.target.closest('a.open');
  if(o){e.preventDefault();openLb(o.closest('.cell'));return;}
  const cell=e.target.closest('.grid .cell'); if(cell)choose(cell);
});
document.addEventListener('dblclick',e=>{const c=e.target.closest('.grid .cell');if(c){choose(c);openLb(c);}});
document.getElementById('reset').onclick=()=>{
  secs.forEach(s=>{
    const k=s.dataset.key, o=orig.get(k); cur.set(k,o);
    s.querySelectorAll('.cell').forEach(c=>c.classList.toggle('sel',keyOf(c)===o));
  });
  msg.textContent=''; refresh();
};
applyBtn.onclick=async()=>{
  const changes={};
  for(const[k,v]of cur){
    if(v===orig.get(k))continue;
    if(!v){changes[k]=null;continue;}
    const[kind,trip,id]=v.split('|');
    changes[k]=kind==='phone'?{phone_trip:trip,id}:{trip,id};
  }
  applyBtn.disabled=true; msg.textContent='saving…';
  try{
    const r=await fetch('/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({changes})});
    const j=await r.json();
    if(j.ok){for(const k in changes)orig.set(k,cur.get(k));msg.textContent='✓ wrote '+j.written+' bridge(s). Run ./build_collections.py';}
    else msg.textContent='error: '+(j.error||'unknown');
  }catch(err){msg.textContent='error: '+err;}
  refresh();
};
refresh();
</script></body></html>""")
    return '\n'.join(P)


# ---------------------------------------------------------------- server

def write_changes(changes):
    """Merge into config/bridge_public_covers.json; only the bridges sent change.
    A pick equal to the auto choice is still stored, so it survives cover edits."""
    config = _load(CONFIG) or {
        '_comment': 'Public thumbnail per bridge for the locked Bridges preview. '
                    '{trip,id} = a public photo of the bridge; {phone_trip,id} = a phone photo '
                    '(copied into web/previews/bridges/ by build_collections.py). '
                    'Unlisted bridges auto-pick. Edit with tools/bridge_public_cover_picker.py.',
        'covers': {}}
    covers = config.setdefault('covers', {})
    for bridge, ref in changes.items():
        if ref is None:
            covers.pop(bridge, None)
        else:
            covers[bridge] = ref
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    return len(changes)


def make_handler(page_html):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(ROOT), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ('/', '/index.html'):
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
                payload = json.dumps({'ok': True, 'written': write_changes(data.get('changes', {}))}).encode()
            except Exception as e:                 # noqa: BLE001 — report back to the page
                payload = json.dumps({'ok': False, 'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return Handler


def main():
    cands = build_candidates()
    page = render(cands)
    for c in cands:
        print(f'  {c["rank"] or "·":>3}  {c["title"]}: {c["source"]}')
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(page))
    url = f'http://127.0.0.1:{httpd.server_address[1]}/'
    print(f'serving {url}  (Ctrl-C to stop)')
    if '--no-open' not in sys.argv:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped.')


if __name__ == '__main__':
    main()
