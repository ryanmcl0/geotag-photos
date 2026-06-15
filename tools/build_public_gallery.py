#!/usr/bin/env python3
"""Generate a localhost gallery of every PUBLIC photo (those served without the
See All gate) for a quick visual privacy audit.

Public photo = a photo present in a public trip's web/trips/<slug>/manifest.json
(already privacy-filtered). Thumbnails/display images are read from hosted-photos/.
Output: _public_gallery.html at the project root; serve the root with http.server.
"""
import html
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / 'web' / 'trips'

idx = json.loads((TRIPS / 'index.json').read_text())['trips']
public = [t for t in idx if t.get('public')]

# year -> edits_dir -> list of (slug, trip_name, photo)
tree = defaultdict(lambda: defaultdict(list))
edits_trips = defaultdict(set)   # (year, edits) -> {trip names}
total = 0
for t in sorted(public, key=lambda t: t['id']):
    slug = t['id']
    year = t['year']
    mpath = TRIPS / slug / 'manifest.json'
    if not mpath.exists():
        continue
    m = json.loads(mpath.read_text())
    edits = (m.get('source') or {}).get('photos_path') or '(no edits dir)'
    name = m.get('trip_name') or t.get('name') or slug
    for ph in m.get('photos', []):
        tree[year][edits].append((slug, name, ph))
        edits_trips[(year, edits)].add(name)
        total += 1

parts = []
parts.append(f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Public photo audit — {total} photos</title>
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
a.cell:hover{{outline:2px solid #4a9eff}}
</style></head><body>
<header class=top>
<h1>Public photo audit</h1>
<span class=sub>{total} photos · {len(public)} public trips · click any photo to open full size</span>
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
            thumb = f"hosted-photos/{slug}/{ph.get('thumbnail', 'thumbnails/'+pid+'.webp')}"
            disp = f"hosted-photos/{slug}/{ph.get('display', 'display/'+pid+'.webp')}"
            bldg = ph.get('building')
            bhtml = f'<span class=bldg>🏠 {html.escape(bldg)}</span>' if bldg else ''
            parts.append(
                f'<a class=cell href="{html.escape(disp)}" target=_blank title="{html.escape(slug)} / {html.escape(pid)}">'
                f'{bhtml}<img loading=lazy src="{html.escape(thumb)}" alt="{html.escape(pid)}">'
                f'<span class=id>{html.escape(pid)}</span></a>')
        parts.append('</div></div>')
    parts.append('</section>')

parts.append('</body></html>')
out = ROOT / '_public_gallery.html'
out.write_text('\n'.join(parts))
print(f'Wrote {out} — {total} photos, {len(public)} public trips')
