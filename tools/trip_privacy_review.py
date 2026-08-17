#!/usr/bin/env python3
"""Per-folder public/private review page for ONE trip.

The site-wide audit (public_photo_audit.py) answers "across everything, what is
public that shouldn't be". This answers a different question, the one you get when
onboarding a split trip: "here is a new trip, folder by folder, which of these can
go public". It is organised by `section` — where the edit was filed relative to the
edits root — because that is how the decision is actually made (this bridge yes,
that bridge no), and it shows each section's configured default from
config/photo_privacy.json `section_rules`.

Each photo carries ONE flag meaning "the opposite of my section's default":
  · in a private-by-default section (bridges, Guangdong) flagging = make it PUBLIC
  · in a public-by-default section (the province folders) flagging = make it PRIVATE
so a click always means the same thing — "not the default" — and the page emits
force_public / force_private accordingly.

Existing decisions in config/photo_privacy.json are pre-loaded, so a half-finished
review resumes instead of restarting. Selections also persist in localStorage.

  python3 tools/trip_privacy_review.py 2026-china-cny
  ./serve.sh          # then open http://localhost:8000/_trip_privacy_review.html

Save the JSON the page produces, then apply it to config/photo_privacy.json.
"""
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_privacy as pp  # noqa: E402

slug = sys.argv[1] if len(sys.argv) > 1 else '2026-china-cny'
trip_dir = ROOT / 'web' / 'trips' / slug
manifest_path = trip_dir / 'manifest.all.json'
if not manifest_path.exists():
    manifest_path = trip_dir / 'manifest.json'
if not manifest_path.exists():
    sys.exit(f"✗ no manifest for {slug} — process the trip first")

manifest = json.loads(manifest_path.read_text())
photos = manifest.get('photos', [])
overrides = pp.load_overrides()
rules = pp.load_section_rules(overrides).get(slug, [])
seeded_public = set((overrides.get('force_public') or {}).get(slug, []))
seeded_private = set((overrides.get('force_private') or {}).get(slug, []))

by_section = defaultdict(list)
for ph in photos:
    by_section[ph.get('section') or '(root)'].append(ph)


def section_default(name):
    v = pp.section_verdict(None if name == '(root)' else name, rules)
    return v  # True public, False private, None unset


# Sections with no rule fall back to "auto" — the roof/bridge rules decide. Flagging
# there is ambiguous, so they are shown but marked, rather than silently treated as
# one or the other.
order = sorted(by_section, key=lambda s: (section_default(s) is not False, s))
n_priv_sections = sum(1 for s in order if section_default(s) is False)

seed = {}
for s in order:
    d = section_default(s)
    for ph in by_section[s]:
        pid = ph['id']
        if d is False and pid in seeded_public:
            seed[pid] = 1
        elif d is True and pid in seeded_private:
            seed[pid] = 1

parts = [f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Privacy review — {html.escape(slug)}</title>
<style>
:root{{--bg:#111;--panel:#1b1b1d;--fg:#eee;--muted:#9a9a9f;--line:#2c2c30;--pub:#5bd07f;--priv:#ff9a9a;--flag:#ffd27a}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
header.top{{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);
  padding:10px 18px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}}
header.top h1{{font-size:16px;margin:0;font-weight:600}}
header.top .sub{{color:var(--muted);font-size:12px}}
header.top nav{{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;max-width:62%;justify-content:flex-end}}
header.top nav a{{color:var(--fg);text-decoration:none;background:#26262a;padding:3px 8px;border-radius:11px;font-size:11px}}
header.top nav a:hover{{background:#34343a}}
header.top nav a.p{{border:1px solid #4a3a20}}
.sect{{padding:0 18px 20px}}
.shead{{position:sticky;top:44px;z-index:10;background:var(--bg);padding:14px 0 8px;
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-bottom:10px}}
.shead h2{{font-size:17px;margin:0;font-weight:600}}
.badge{{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}}
.badge.priv{{background:#3a1f1f;color:var(--priv)}}
.badge.pub{{background:#17351f;color:var(--pub)}}
.badge.auto{{background:#2a2a30;color:var(--muted)}}
.shead .cnt{{color:var(--muted);font-size:12px}}
.shead .acts{{margin-left:auto;display:flex;gap:6px}}
.shead button{{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;
  padding:4px 9px;font-size:12px;cursor:pointer}}
.shead button:hover{{background:#34343a}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:8px}}
.cell{{position:relative;background:#000;border-radius:4px;overflow:hidden;aspect-ratio:1/1;cursor:pointer}}
.cell img{{width:100%;height:100%;object-fit:cover;display:block;background:#222}}
.cell .id{{position:absolute;left:0;right:0;bottom:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(transparent,rgba(0,0,0,.85));color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.cell .bldg{{position:absolute;top:0;left:0;right:0;font-size:10px;padding:2px 4px;
  background:linear-gradient(rgba(0,0,0,.8),transparent);color:#ffd27a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.cell:hover{{outline:2px solid #4a9eff}}
.cell.on{{outline:3px solid var(--flag)}}
.cell.on img{{opacity:.45}}
.cell .mark{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;
  font-size:13px;font-weight:700;letter-spacing:.5px;padding:4px 8px;border-radius:6px;display:none;
  background:rgba(0,0,0,.7)}}
.cell.on .mark{{display:block}}
.cell.on.mkpub .mark{{color:var(--pub)}}
.cell.on.mkpriv .mark{{color:var(--priv)}}
.cell .open{{position:absolute;top:3px;right:3px;z-index:4;width:22px;height:22px;border-radius:50%;
  background:rgba(0,0,0,.65);color:#fff;text-decoration:none;display:none;align-items:center;justify-content:center;font-size:13px}}
.cell:hover .open{{display:flex}}
.cell .open:hover{{background:#4a9eff}}
.bar{{position:fixed;right:16px;bottom:16px;z-index:50;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:11px 13px;display:flex;flex-direction:column;gap:8px;
  box-shadow:0 6px 24px rgba(0,0,0,.55);min-width:210px}}
.bar .n{{font-size:12px}}
.bar .n b{{font-size:15px}}
.bar .pubn b{{color:var(--pub)}} .bar .privn b{{color:var(--priv)}}
.bar .row{{display:flex;gap:6px;flex-wrap:wrap}}
.bar button{{background:#26262a;color:var(--fg);border:1px solid var(--line);border-radius:7px;
  padding:6px 10px;font-size:12px;cursor:pointer}}
.bar button:hover{{background:#34343a}}
.bar button.go{{background:#2b4a6b;border-color:#3a5f85}}
.bar button.clear{{color:var(--priv)}}
</style></head><body>
<header class=top>
<h1>Privacy review · {html.escape(slug)}</h1>
<span class=sub>{len(photos)} photos · {len(order)} folders · click a photo to flip it away from its folder default · ⤢ full size</span>
<nav>"""]
for s in order:
    d = section_default(s)
    cls = 'p' if d is False else ''
    parts.append(f'<a class="{cls}" href="#s{abs(hash(s))}">{html.escape(s)} ({len(by_section[s])})</a>')
parts.append('</nav></header>')

for s in order:
    d = section_default(s)
    ph_list = sorted(by_section[s], key=lambda p: p.get('timestamp') or '')
    if d is False:
        badge, act = '<span class="badge priv">private by default</span>', 'make public'
    elif d is True:
        badge, act = '<span class="badge pub">public by default</span>', 'make private'
    else:
        badge, act = '<span class="badge auto">no rule — auto rules decide</span>', 'flag'
    parts.append(f'<section class=sect id="s{abs(hash(s))}">')
    parts.append(f'<div class=shead><h2>{html.escape(s)}</h2>{badge}'
                 f'<span class=cnt>{len(ph_list)} photos · <b class="sc" data-sec="{html.escape(s)}">0</b> flagged to {act}</span>'
                 f'<span class=acts><button data-all="{html.escape(s)}">flag all</button>'
                 f'<button data-none="{html.escape(s)}">clear folder</button></span></div>')
    parts.append('<div class=grid>')
    for ph in ph_list:
        pid = ph['id']
        thumb = f"hosted-photos/{slug}/{ph.get('thumbnail', 'thumbnails/' + pid + '.webp')}"
        disp = f"hosted-photos/{slug}/{ph.get('display', 'display/' + pid + '.webp')}"
        bldg = ph.get('building') or ''
        mark = 'PUBLIC' if d is False else ('PRIVATE' if d is True else 'FLAGGED')
        mcls = 'mkpub' if d is False else ('mkpriv' if d is True else '')
        bhtml = f'<span class=bldg>{html.escape(bldg)}</span>' if bldg else ''
        parts.append(
            f'<div class="cell {mcls}" data-id="{html.escape(pid)}" data-sec="{html.escape(s)}" '
            f'title="{html.escape(pid)} · {html.escape(ph.get("gps_source") or "")} · {html.escape((ph.get("timestamp") or "")[:16])}">'
            f'{bhtml}<a class=open href="{html.escape(disp)}" target=_blank rel=noopener>⤢</a>'
            f'<span class=mark>{mark}</span>'
            f'<img loading=lazy src="{html.escape(thumb)}" alt="{html.escape(pid)}">'
            f'<span class=id>{html.escape(pid)}</span></div>')
    parts.append('</div></section>')

cfg = {
    'slug': slug,
    'seed': seed,
    'defaults': {s: section_default(s) for s in order},
}
parts.append(f'<script>const CFG={json.dumps(cfg)};</script>')
parts.append(r"""
<div class=bar id=bar>
  <div class="n pubn">→ force_public: <b id=npub>0</b></div>
  <div class="n privn">→ force_private: <b id=npriv>0</b></div>
  <div class=row><button class=go id=dl>Download JSON</button><button id=cp>Copy</button></div>
  <div class=row><button class=clear id=clr>Clear all</button></div>
</div>
<script>
const KEY='tripreview.'+CFG.slug+'.v1';
let sel=new Set(JSON.parse(localStorage.getItem(KEY)||'null')||Object.keys(CFG.seed));
const cells=[...document.querySelectorAll('.cell')];
const idx=new Map(cells.map((c,i)=>[c,i]));
let anchor=null;
const save=()=>localStorage.setItem(KEY,JSON.stringify([...sel]));
function paint(){
  const per={};
  for(const c of cells){
    const on=sel.has(c.dataset.id);
    c.classList.toggle('on',on);
    if(on) per[c.dataset.sec]=(per[c.dataset.sec]||0)+1;
  }
  for(const el of document.querySelectorAll('.sc')) el.textContent=per[el.dataset.sec]||0;
  let pub=0,priv=0;
  for(const c of cells){
    if(!sel.has(c.dataset.id)) continue;
    const d=CFG.defaults[c.dataset.sec];
    if(d===false) pub++; else if(d===true) priv++;
  }
  npub.textContent=pub; npriv.textContent=priv;
}
document.addEventListener('click',e=>{
  if(e.target.closest('.open')) return;
  const b=e.target.closest('button');
  if(b&&b.dataset.all!==undefined){
    for(const c of cells) if(c.dataset.sec===b.dataset.all) sel.add(c.dataset.id);
    save();paint();return;
  }
  if(b&&b.dataset.none!==undefined){
    for(const c of cells) if(c.dataset.sec===b.dataset.none) sel.delete(c.dataset.id);
    save();paint();return;
  }
  const cell=e.target.closest('.cell');
  if(!cell) return;
  const i=idx.get(cell);
  if(e.shiftKey&&anchor!==null&&cells[anchor].dataset.sec===cell.dataset.sec){
    const [a,z]=[anchor,i].sort((x,y)=>x-y);
    const want=!sel.has(cell.dataset.id);
    for(let j=a;j<=z;j++){const k=cells[j].dataset.id; want?sel.add(k):sel.delete(k);}
    window.getSelection().removeAllRanges();
  }else{
    const k=cell.dataset.id;
    sel.has(k)?sel.delete(k):sel.add(k);
    anchor=i;
  }
  save();paint();
});
function payload(){
  const pub=[],priv=[];
  for(const c of cells){
    if(!sel.has(c.dataset.id)) continue;
    const d=CFG.defaults[c.dataset.sec];
    if(d===false) pub.push(c.dataset.id); else if(d===true) priv.push(c.dataset.id);
  }
  const u=a=>[...new Set(a)].sort();
  return JSON.stringify({force_public:{[CFG.slug]:u(pub)},force_private:{[CFG.slug]:u(priv)}},null,2);
}
dl.onclick=()=>{
  const b=new Blob([payload()],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b); a.download=CFG.slug+'-privacy.json'; a.click();
  URL.revokeObjectURL(a.href);
};
cp.onclick=async e=>{
  const t=payload();
  try{await navigator.clipboard.writeText(t);}
  catch(_){const x=document.createElement('textarea');x.value=t;document.body.appendChild(x);x.select();document.execCommand('copy');x.remove();}
  const o=e.target.textContent;e.target.textContent='Copied ✓';setTimeout(()=>e.target.textContent=o,1200);
};
clr.onclick=()=>{if(sel.size&&confirm('Clear all '+sel.size+' flags?')){sel.clear();save();paint();}};
paint();
</script>
</body></html>""")

out = ROOT / '_trip_privacy_review.html'
out.write_text('\n'.join(parts))
print(f"Wrote {out.relative_to(ROOT)} — {len(photos)} photos in {len(order)} folders "
      f"({n_priv_sections} private-by-default)")
print(f"Pre-loaded {len(seed)} existing decisions from config/photo_privacy.json")
for s in order:
    d = section_default(s)
    lbl = {False: 'private', True: 'public', None: 'auto'}[d]
    print(f"   {s:44} {len(by_section[s]):4} photos   default={lbl}")
