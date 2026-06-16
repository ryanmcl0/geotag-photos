#!/usr/bin/env python3
"""Generate a localhost gallery for a quick visual privacy audit.

Default (public audit): every PUBLIC photo — those served without the See All gate.
Use it to catch photos that should be private.

With --private (private audit): every NON-public photo — whole private trips plus
the per-photo private subset hidden from public trips. Use it to catch photos that
are hidden but should be public.

A photo is public when it's present in a public trip's web/trips/<slug>/manifest.json
(already privacy-filtered); after a split the full set lives in manifest.all.json, so
a public trip's private subset = manifest.all.json minus manifest.json. Private trips
aren't split, so their manifest.json is the full (all-hidden) set.

Thumbnails/display images are read from hosted-photos/ (present regardless of privacy).
Output: _public_photo_audit.html (or _private_photo_audit.html with --private) at the
project root; serve the root with http.server.
"""
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

PRIVATE = '--private' in sys.argv
ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / 'web' / 'trips'

idx = json.loads((TRIPS / 'index.json').read_text())['trips']


def _load(slug, name):
    p = TRIPS / slug / name
    return json.loads(p.read_text()) if p.exists() else None


# year -> edits_dir -> list of (slug, trip_name, photo)
tree = defaultdict(lambda: defaultdict(list))
edits_trips = defaultdict(set)   # (year, edits) -> {trip names}
total = 0
n_trips = 0
for t in sorted(idx, key=lambda t: t['id']):
    slug = t['id']
    year = t['year']
    m = _load(slug, 'manifest.json')
    if not m:
        continue
    if not PRIVATE:
        if not t.get('public'):
            continue
        photos, src = m.get('photos', []), m
    elif not t.get('public'):
        photos, src = m.get('photos', []), m        # private trip: all photos are hidden
    else:
        full = _load(slug, 'manifest.all.json')
        if not full:
            continue                                # public trip, no split → nothing hidden
        pub_ids = {p['id'] for p in m.get('photos', [])}
        photos = [p for p in full.get('photos', []) if p['id'] not in pub_ids]
        src = full
        if not photos:
            continue
    edits = (src.get('source') or {}).get('photos_path') or '(no edits dir)'
    name = src.get('trip_name') or t.get('name') or slug
    for ph in photos:
        tree[year][edits].append((slug, name, ph))
        edits_trips[(year, edits)].add(name)
        total += 1
    n_trips += 1

LABEL = 'Private' if PRIVATE else 'Public'
KIND = 'hidden (non-public)' if PRIVATE else 'public'

parts = []
parts.append(f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{LABEL} photo audit — {total} photos</title>
<style>
:root{{--bg:#111;--panel:#1b1b1d;--fg:#eee;--muted:#9a9a9f;--line:#2c2c30;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
header.top{{position:sticky;top:0;z-index:10;background:var(--panel);border-bottom:1px solid var(--line);
  padding:12px 18px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}}
header.top h1{{font-size:16px;margin:0;font-weight:600}}
header.top .sub{{color:var(--muted)}}
header.top nav{{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}}
header.top nav a{{color:var(--fg);text-decoration:none;background:#26262a;padding:3px 9px;border-radius:12px;font-size:12px}}
header.top nav a:hover{{background:#34343a}}
section.year{{padding:6px 18px 26px}}
h2.year{{font-size:22px;margin:22px 0 4px;border-bottom:2px solid var(--line);padding-bottom:6px}}
h2.year .cnt{{color:var(--muted);font-size:15px;font-weight:400}}
.edits{{margin:18px 0 6px}}
.edits h3{{font-size:14px;margin:0 0 2px;font-weight:600;color:#d9d9dd}}
.edits .meta{{color:var(--muted);font-size:12px;margin-bottom:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}}
.cell{{position:relative;background:#000;border-radius:4px;overflow:hidden;aspect-ratio:1/1}}
.cell img{{width:100%;height:100%;object-fit:cover;display:block;background:#222}}
.cell .id{{position:absolute;left:0;right:0;bottom:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(transparent,rgba(0,0,0,.8));color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.cell .bldg{{position:absolute;top:0;left:0;right:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(rgba(0,0,0,.75),transparent);color:#ffd27a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.cell{{cursor:pointer}}
.cell:hover{{outline:2px solid #4a9eff}}
.cell.sel{{outline:3px solid #ffd27a}}
.cell.sel img{{opacity:.4}}
.cell .open{{position:absolute;top:3px;right:3px;z-index:3;width:22px;height:22px;border-radius:50%;
  background:rgba(0,0,0,.6);color:#fff;text-decoration:none;display:none;align-items:center;justify-content:center;
  font-size:13px;line-height:1}}
.cell:hover .open{{display:flex}}
.cell .open:hover{{background:#4a9eff}}
.cell .check{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;
  font-size:30px;color:#ffd27a;text-shadow:0 0 6px #000;display:none}}
.cell.sel .check{{display:block}}
.selbar{{position:fixed;right:16px;bottom:16px;z-index:50;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:8px;
  box-shadow:0 6px 24px rgba(0,0,0,.5);min-width:180px}}
.selbar .n b{{color:#ffd27a}}
.selbar .row{{display:flex;gap:6px;flex-wrap:wrap}}
.selbar button{{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;
  padding:5px 9px;font-size:12px;cursor:pointer}}
.selbar button:hover{{background:#34343a}}
.selbar button.clear{{color:#ff9a9a}}
.selbar.empty{{opacity:.5}}
</style></head><body>
<header class=top>
<h1>{LABEL} photo audit</h1>
<span class=sub>{total} {KIND} photos · {n_trips} trips · click a photo to flag it · ⤢ to open full size</span>
<nav>""")
for y in sorted(tree, reverse=True):
    yn = sum(len(v) for v in tree[y].values())
    parts.append(f'<a href="#y{y}">{y} ({yn})</a>')
parts.append('</nav></header>')

for y in sorted(tree, reverse=True):
    yn = sum(len(v) for v in tree[y].values())
    parts.append(f'<section class=year id="y{y}"><h2 class=year>{y} <span class=cnt>· {yn} photos</span></h2>')
    for edits, photos in sorted(tree[y].items(), key=lambda kv: -len(kv[1])):
        trips = ', '.join(sorted(edits_trips[(y, edits)]))
        parts.append('<div class=edits>')
        parts.append(f'<h3>{html.escape(edits)} <span style="color:var(--muted);font-weight:400">· {len(photos)} photos</span></h3>')
        parts.append(f'<div class=meta>{html.escape(trips)}</div>')
        parts.append('<div class=grid>')
        for slug, name, ph in photos:
            pid = ph['id']
            key = f"{slug}/{pid}"
            thumb = f"hosted-photos/{slug}/{ph.get('thumbnail', 'thumbnails/'+pid+'.webp')}"
            disp = f"hosted-photos/{slug}/{ph.get('display', 'display/'+pid+'.webp')}"
            bldg = ph.get('building')
            bhtml = f'<span class=bldg>🏠 {html.escape(bldg)}</span>' if bldg else ''
            parts.append(
                f'<div class=cell data-key="{html.escape(key)}" title="{html.escape(key)}">'
                f'{bhtml}'
                f'<a class=open href="{html.escape(disp)}" target=_blank rel=noopener title="open full size">⤢</a>'
                f'<span class=check>✓</span>'
                f'<img loading=lazy src="{html.escape(thumb)}" alt="{html.escape(pid)}">'
                f'<span class=id>{html.escape(pid)}</span></div>')
        parts.append('</div></div>')
    parts.append('</section>')

parts.append(f"<script>const FLAG_KEY={json.dumps('privgallery.flagged.v1' if PRIVATE else 'pubgallery.flagged.v1')},"
             f"FLAG_FIELD={json.dumps('force_public' if PRIVATE else 'force_private')};</script>")
parts.append(r"""
<div class="selbar empty" id=selbar>
  <div class=n><b id=seln>0</b> flagged</div>
  <div class=row>
    <button id=copypaths>Copy slug/pid</button>
    <button id=copyjson>Copy JSON</button>
  </div>
  <div class=row><button class=clear id=clearsel>Clear</button></div>
</div>
<script>
const KEY=FLAG_KEY;
let sel=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'));
const selbar=document.getElementById('selbar'), seln=document.getElementById('seln');
const cells=[...document.querySelectorAll('.cell')];
const idx=new Map(cells.map((c,i)=>[c,i]));
let anchor=null;                                   // last plain-clicked cell index
const save=()=>localStorage.setItem(KEY,JSON.stringify([...sel]));
function paint(){
  for(const c of cells) c.classList.toggle('sel',sel.has(c.dataset.key));
  seln.textContent=sel.size;
  selbar.classList.toggle('empty',sel.size===0);
}
document.addEventListener('click',e=>{
  if(e.target.closest('.open')) return;            // let the open-full link through
  const cell=e.target.closest('.cell');
  if(!cell) return;
  const i=idx.get(cell);
  if(e.shiftKey && anchor!==null){                 // shift-click: flag whole range anchor..i
    const [a,b]=[anchor,i].sort((x,y)=>x-y);
    for(let j=a;j<=b;j++) sel.add(cells[j].dataset.key);
  }else{
    const k=cell.dataset.key;
    sel.has(k)?sel.delete(k):sel.add(k);
    anchor=i;
  }
  if(e.shiftKey) window.getSelection().removeAllRanges();   // kill browser text-select
  save(); paint();
});
const pathsText=()=>[...sel].sort().join('\n');
function jsonText(){
  const by={};
  for(const k of [...sel].sort()){const i=k.indexOf('/');(by[k.slice(0,i)]??=[]).push(k.slice(i+1));}
  return JSON.stringify({[FLAG_FIELD]:by},null,2);
}
async function copy(txt,btn){
  try{await navigator.clipboard.writeText(txt);}
  catch(_){const t=document.createElement('textarea');t.value=txt;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();}
  const o=btn.textContent; btn.textContent='Copied ✓'; setTimeout(()=>btn.textContent=o,1200);
}
copypaths.onclick=e=>copy(pathsText(),e.target);
copyjson.onclick=e=>copy(jsonText(),e.target);
clearsel.onclick=()=>{if(sel.size&&confirm('Clear '+sel.size+' flagged?')){sel.clear();save();paint();}};
paint();
</script>
</body></html>""")
out = ROOT / f'_{LABEL.lower()}_photo_audit.html'
out.write_text('\n'.join(parts))
print(f'Wrote {out} — {total} {KIND} photos across {n_trips} trips')
