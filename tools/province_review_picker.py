#!/usr/bin/env python3
"""Review which province each photo of a China trip is filed under.

The China page's Provinces facet files every photo by a point-in-polygon lookup
of its map position. This picker shows one section per province the trip touched,
grouped by day, and flags the photos worth a second look:

  * folder  — the raw day folder ("Day 12 [Qinghai, Tibet] …") doesn't list the
              province the lookup chose
  * border  — another province lies within 5 km of the photo

One tab per province shows the photos currently filed under it (with a flagged-only
filter). Select photos (shift-click for a range) and move them to another province;
double-click opens an in-page viewer. Save writes the corrections to
config/province_overrides.json (only photos whose province differs from the lookup)
and rebuilds the local collections.

--suggest takes a JSON file {photo id: {"province": ..., "reason": ...}}: those moves
open pre-applied but unsaved, so they're reviewed like any other change.

    ./venv/bin/python tools/province_review_picker.py [trip-slug] [--port N] [--suggest file.json]
"""
import json
import math
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import photo_privacy  # noqa: E402
from build_collections import PROVINCE_OVERRIDES, ProvinceIndex  # noqa: E402

GEOJSON = ROOT / 'config' / 'geo' / 'china_provinces.geojson'
DAY_RE = re.compile(r'^\s*day\s*(\d+)\s*\[([^\]]*)\]\s*(.*)$', re.I)
BORDER_KM = 5.0
LOCAL_OFFSET_H = 8   # China time, for the labels


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def offset_point(lat, lon, km, bearing_deg):
    b = math.radians(bearing_deg)
    return (lat + km / 111.2 * math.cos(b),
            lon + km / (111.2 * math.cos(math.radians(lat))) * math.sin(b))


def local_label(ts: str) -> str:
    try:
        t = datetime.fromisoformat(ts.replace('Z', '+00:00')) + timedelta(hours=LOCAL_OFFSET_H)
    except (AttributeError, ValueError):
        return ''
    return t.strftime('%d %b %H:%M')


def build_records(slug: str, index: ProvinceIndex, suggestions=None):
    manifest = photo_privacy.load_full_manifest(ROOT / 'web' / 'trips' / slug)
    if not manifest:
        raise SystemExit(f'No manifest for {slug}')
    overrides = (load_json(PROVINCE_OVERRIDES, {}) or {}).get(slug) or {}
    records = []
    for ph in manifest.get('photos', []):
        lat, lon = ph.get('lat'), ph.get('lon')
        if lat is None or lon is None:
            continue
        geo = index.lookup(lat, lon)
        m = DAY_RE.match(ph.get('building') or '')
        listed = [x.strip() for x in m.group(2).split(',') if x.strip()] if m else []
        near = sorted({p for p in (index.lookup(*offset_point(lat, lon, BORDER_KM, b))
                                   for b in range(0, 360, 30)) if p and p != geo})
        saved_as = overrides.get(ph['id']) or geo
        sug = (suggestions or {}).get(ph['id'])
        if isinstance(sug, str):
            sug = {'province': sug}
        flags = []
        if listed and geo not in listed:
            flags.append('folder')
        if near:
            flags.append('border')
        records.append({
            'id': ph['id'],
            'thumb': f"/hosted-photos/{slug}/{ph.get('thumbnail') or 'thumbnails/' + ph['id'] + '.webp'}",
            'display': f"/hosted-photos/{slug}/{ph.get('display') or 'display/' + ph['id'] + '.webp'}",
            'lat': round(lat, 5), 'lon': round(lon, 5),
            'geo': geo, 'savedAs': saved_as,
            'assigned': (sug or {}).get('province') or saved_as,
            'suggested': (sug or {}).get('reason') or ('suggested' if sug else ''),
            'day': int(m.group(1)) if m else None,
            'dayTitle': (m.group(3).strip() if m else (ph.get('building') or '')),
            'listed': listed, 'near': near, 'flags': flags,
            'src': ph.get('gps_source') or '',
            'ts': ph.get('timestamp') or '', 'time': local_label(ph.get('timestamp') or ''),
        })
    records.sort(key=lambda r: (r['day'] if r['day'] is not None else 999, r['ts'], r['id']))
    return records


def trip_provinces(records):
    """Every province the trip touches, in order of first appearance."""
    seen = []
    for r in records:
        for p in [r['geo'], r['assigned'], *r['listed']]:
            if p and p not in seen:
                seen.append(p)
    return seen


PAGE = r'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Province review</title>
<style>
:root{--bg:#111;--panel:#1a1a1c;--line:#333;--fg:#eee;--muted:#9a9a9a;--sel:#8dbcec;--flag:#e0a84a}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.top{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:14px 22px 0}
.row{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:18px;margin:0}
.hint{color:var(--muted);margin:4px 0 12px}
.note{color:#ffe1a8;margin:0 0 12px}
.tabs{display:flex;gap:4px}
.tab{background:none;border:0;border-bottom:3px solid transparent;color:var(--muted);font:600 16px/1 inherit;padding:10px 16px 12px;cursor:pointer}
.tab span{font-weight:400;margin-left:6px}
.tab.on{color:#fff;border-bottom-color:#fff}
.tab:hover{color:#fff}
.filter{margin-left:auto;color:var(--muted);cursor:pointer;user-select:none;padding-bottom:10px}
.filter input{vertical-align:-2px}
main{padding:6px 22px 110px}
.day{color:var(--muted);font-size:12px;margin:18px 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.cell{border:2px solid var(--line);border-radius:7px;overflow:hidden;background:#000;position:relative;aspect-ratio:1.35;cursor:pointer;user-select:none}
.cell img{width:100%;height:100%;object-fit:cover;display:block;pointer-events:none}
.cell:hover{border-color:#666}
.cell.selected{border-color:var(--sel);box-shadow:0 0 0 2px var(--sel)}
.cell.selected img{opacity:.55}
.cell .lab{position:absolute;left:0;right:0;bottom:0;padding:14px 6px 4px;font-size:11px;background:linear-gradient(transparent,rgba(0,0,0,.9));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cell .dot{position:absolute;top:7px;left:7px;width:11px;height:11px;border-radius:50%;background:var(--flag);box-shadow:0 0 0 2px #000a}
.cell .open{position:absolute;top:5px;right:5px;display:none;background:#111c;color:#fff;border:1px solid #aaa;border-radius:5px;padding:2px 6px;cursor:pointer;font-size:13px}
.cell:hover .open{display:block}
.empty{color:var(--muted);padding:30px 0}
.bar{position:fixed;left:50%;transform:translateX(-50%);width:max-content;bottom:16px;z-index:10;background:var(--panel);border:1px solid var(--line);box-shadow:0 8px 28px #0009;border-radius:10px;padding:10px 14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;max-width:calc(100vw - 32px)}
.bar .count{color:var(--muted)} .bar .count b{color:#fff}
.bar button{border:1px solid var(--line);background:#2a2a2d;color:#fff;border-radius:7px;padding:7px 12px;cursor:pointer;font-weight:600}
.bar button:hover:not(:disabled){border-color:var(--sel)}
.bar button:disabled{opacity:.4;cursor:not-allowed}
.bar button.save{background:#2d5d8a;border-color:#2d5d8a}
.bar .msg{color:var(--muted);font-size:12px;max-width:320px}
.sep{width:1px;height:24px;background:var(--line)}
.viewer{display:none;position:fixed;inset:0;z-index:20;background:#000f;align-items:center;justify-content:center}
.viewer.open{display:flex}
.viewer img{max-width:94vw;max-height:88vh;object-fit:contain}
.vbtn{position:absolute;background:#222d;color:#fff;border:1px solid #777;border-radius:6px;font-size:24px;line-height:1;padding:7px 11px;cursor:pointer}
.vclose{top:14px;right:14px} .vprev{left:14px;top:50%} .vnext{right:14px;top:50%}
.vcap{position:absolute;bottom:16px;left:0;right:0;text-align:center;color:#ddd;font-size:13px;text-shadow:0 1px 3px #000}
.vcap button{margin-left:12px;background:#2a2a2d;color:#fff;border:1px solid #777;border-radius:6px;padding:4px 10px;cursor:pointer}
</style>
<header class="top">
  <div class="row"><h1>Province review</h1></div>
  <p class="hint">Click photos to select them (shift-click selects a range), then choose which province they belong in. Double-click or ⤢ to view a photo large. Nothing changes until you press Save.</p>
  <p class="note" id="note" hidden></p>
  <div class="row"><nav class="tabs" id="tabs"></nav>
    <label class="filter" title="Photos whose day folder names a different province, that sit within 5 km of a border, or that you moved by hand"><input type="checkbox" id="flagonly"> Flagged only <span id="nflag"></span></label></div>
</header>
<main id="main"></main>
<div class="bar">
  <span class="count"><b id="nsel">0</b> selected</span>
  <span class="count">Move to</span>
  <span id="movebtns"></span>
  <button id="clear" type="button">Clear</button>
  <span class="sep"></span>
  <span class="count"><b id="nchg">0</b> unsaved</span>
  <button id="save" class="save" type="button" disabled>Save</button>
  <span id="msg" class="msg"></span>
</div>
<div class="viewer" id="viewer">
  <img id="vimg" alt="">
  <button class="vbtn vclose" id="vclose" title="Close (Esc)">×</button>
  <button class="vbtn vprev" id="vprev" title="Previous (←)">‹</button>
  <button class="vbtn vnext" id="vnext" title="Next (→)">›</button>
  <div class="vcap"><span id="vcap"></span><button id="vsel" type="button">Select</button></div>
</div>
<script>
const DATA = __DATA__;
const PROVS = DATA.provinces;
const byId = new Map(DATA.photos.map(p => [p.id, p]));
let saved = new Map(DATA.photos.map(p => [p.id, p.savedAs]));
const selected = new Set();
let tab = PROVS[0], order = [], lastIdx = -1, viewing = -1;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const folderOff = p => p.listed.length > 0 && !p.listed.includes(p.assigned);
const handMoved = p => p.assigned !== p.geo && !p.suggested;
const flagged = p => folderOff(p) || p.near.length > 0 || handMoved(p);
const flagWhy = p => [
  folderOff(p) ? `day folder says ${p.listed.join(', ')}` : '',
  p.near.length ? `within 5 km of ${p.near.join(', ')}` : '',
  handMoved(p) ? `moved by hand (map position is in ${p.geo || 'no province'})` : '',
].filter(Boolean).join('; ');

function render() {
  const onlyFlag = document.getElementById('flagonly').checked;
  document.getElementById('tabs').innerHTML = PROVS.map(pr =>
    `<button class="tab${pr === tab ? ' on' : ''}" data-prov="${esc(pr)}">${esc(pr)}<span>${DATA.photos.filter(p => p.assigned === pr).length}</span></button>`).join('');
  const inTab = DATA.photos.filter(p => p.assigned === tab);
  document.getElementById('nflag').textContent = `(${inTab.filter(flagged).length})`;
  const ps = onlyFlag ? inTab.filter(flagged) : inTab;
  order = ps.map(p => p.id);
  let html = '', day = null;
  ps.forEach((p, i) => {
    const d = p.day == null ? p.dayTitle : `Day ${p.day} · ${p.dayTitle}`;
    if (d !== day) { html += (day === null ? '' : '</div>') + `<div class="day">${esc(d)}</div><div class="grid">`; day = d; }
    html += `<div class="cell${selected.has(p.id) ? ' selected' : ''}" data-i="${i}" title="${esc(flagged(p) ? 'Flagged: ' + flagWhy(p) : p.id)}">` +
      `<img loading="lazy" src="${esc(p.thumb)}" alt="">${flagged(p) ? '<span class="dot"></span>' : ''}` +
      `<button class="open" type="button" title="View large">⤢</button><span class="lab">${esc(p.id)} · ${esc(p.time)}</span></div>`;
  });
  html += day === null ? `<div class="empty">No ${onlyFlag ? 'flagged ' : ''}photos in ${esc(tab)}.</div>` : '</div>';
  document.getElementById('main').innerHTML = html;
  document.getElementById('movebtns').innerHTML = PROVS.filter(pr => pr !== tab)
    .map(pr => `<button type="button" data-prov="${esc(pr)}">${esc(pr)}</button>`).join(' ');
  const pending = DATA.photos.filter(p => p.suggested && p.assigned !== saved.get(p.id) && p.assigned !== p.geo).length;
  const note = document.getElementById('note');
  note.hidden = !pending;
  if (pending) note.textContent = `${pending} photos from Days 12 and 13 have been moved to Tibet for you: they were taken south of the Tibet north gate on the G109, which the province map draws as Qinghai. Check them in the Tibet tab, then Save.`;
  refreshBar();
}

function refreshBar() {
  document.getElementById('nsel').textContent = selected.size;
  const changes = DATA.photos.filter(p => p.assigned !== saved.get(p.id)).length;
  document.getElementById('nchg').textContent = changes;
  document.getElementById('save').disabled = !changes;
  document.querySelectorAll('#movebtns button').forEach(b => b.disabled = !selected.size);
}

document.getElementById('tabs').onclick = e => {
  const b = e.target.closest('.tab'); if (!b) return;
  tab = b.dataset.prov; selected.clear(); lastIdx = -1; render(); window.scrollTo(0, 0);
};
document.getElementById('flagonly').onchange = () => { selected.clear(); lastIdx = -1; render(); };
document.getElementById('movebtns').onclick = e => {
  const b = e.target.closest('button'); if (!b || !selected.size) return;
  const n = selected.size;
  selected.forEach(id => { byId.get(id).assigned = b.dataset.prov; });
  selected.clear(); lastIdx = -1;
  const y = window.scrollY; render(); window.scrollTo(0, y);
  document.getElementById('msg').textContent = `${n} moved to ${b.dataset.prov}.`;
};
document.getElementById('clear').onclick = () => {
  selected.clear(); document.querySelectorAll('.cell.selected').forEach(c => c.classList.remove('selected')); refreshBar();
};
document.getElementById('main').addEventListener('click', e => {
  const cell = e.target.closest('.cell'); if (!cell) return;
  const i = +cell.dataset.i;
  if (e.target.closest('.open')) { show(i); return; }
  if (e.shiftKey && lastIdx >= 0) {
    for (let k = Math.min(lastIdx, i); k <= Math.max(lastIdx, i); k++) selected.add(order[k]);
    document.querySelectorAll('.cell').forEach(c => c.classList.toggle('selected', selected.has(order[+c.dataset.i])));
  } else {
    const id = order[i];
    selected.has(id) ? selected.delete(id) : selected.add(id);
    cell.classList.toggle('selected', selected.has(id));
  }
  lastIdx = i; refreshBar();
});
document.getElementById('main').addEventListener('dblclick', e => {
  const cell = e.target.closest('.cell'); if (cell) show(+cell.dataset.i);
});

function show(i) {
  if (!order.length) return;
  viewing = (i + order.length) % order.length;
  const p = byId.get(order[viewing]);
  document.getElementById('vimg').src = p.display;
  document.getElementById('vcap').textContent = `${p.id} · ${p.time} · ${p.day == null ? '' : 'Day ' + p.day + ' · '}${p.dayTitle} · ${viewing + 1} / ${order.length}` + (flagged(p) ? ` · flagged: ${flagWhy(p)}` : '');
  document.getElementById('vsel').textContent = selected.has(p.id) ? 'Selected ✓' : 'Select';
  document.getElementById('viewer').classList.add('open');
}
function toggleViewing() {
  const id = order[viewing]; if (!id) return;
  selected.has(id) ? selected.delete(id) : selected.add(id);
  const c = document.querySelector(`.cell[data-i="${viewing}"]`); if (c) c.classList.toggle('selected', selected.has(id));
  lastIdx = viewing; refreshBar(); show(viewing);
}
const closeViewer = () => document.getElementById('viewer').classList.remove('open');
document.getElementById('vclose').onclick = closeViewer;
document.getElementById('vprev').onclick = () => show(viewing - 1);
document.getElementById('vnext').onclick = () => show(viewing + 1);
document.getElementById('vsel').onclick = toggleViewing;
document.getElementById('viewer').onclick = e => { if (e.target.id === 'viewer') closeViewer(); };
document.addEventListener('keydown', e => {
  if (!document.getElementById('viewer').classList.contains('open')) return;
  if (e.key === 'Escape') closeViewer();
  else if (e.key === 'ArrowLeft') show(viewing - 1);
  else if (e.key === 'ArrowRight') show(viewing + 1);
  else if (e.key === ' ') { e.preventDefault(); toggleViewing(); }
});

document.getElementById('save').onclick = async () => {
  const btn = document.getElementById('save'), msg = document.getElementById('msg');
  btn.disabled = true; msg.textContent = 'Saving and rebuilding the China page…';
  try {
    const assign = Object.fromEntries(DATA.photos.map(p => [p.id, p.assigned]));
    const r = await fetch('/apply', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({assign})});
    const j = await r.json();
    if (j.ok) { saved = new Map(DATA.photos.map(p => [p.id, p.assigned])); msg.textContent = j.message; }
    else msg.textContent = 'Error: ' + j.error;
  } catch (err) { msg.textContent = 'Error: ' + err; }
  render();
};
window.addEventListener('beforeunload', e => {
  if (DATA.photos.some(p => p.assigned !== saved.get(p.id))) { e.preventDefault(); e.returnValue = ''; }
});
render();
</script>'''


def page(slug, records, provinces):
    data = {'slug': slug, 'provinces': provinces, 'photos': records}
    return PAGE.replace('__DATA__', json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))


def apply(slug: str, records, assign: dict) -> str:
    geo = {r['id']: r['geo'] for r in records}
    trip = {pid: prov for pid, prov in assign.items()
            if pid in geo and prov and prov != geo[pid]}
    cfg = load_json(PROVINCE_OVERRIDES, {}) or {}
    if trip:
        cfg[slug] = dict(sorted(trip.items()))
    else:
        cfg.pop(slug, None)
    PROVINCE_OVERRIDES.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    for r in records:
        r['assigned'] = r['savedAs'] = trip.get(r['id']) or r['geo']
    result = subprocess.run([sys.executable, str(ROOT / 'build_collections.py')], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-1000:] or result.stdout[-1000:])
    counts = {}
    for r in records:
        counts[r['assigned']] = counts.get(r['assigned'], 0) + 1
    summary = ', '.join(f'{p} {n}' for p, n in counts.items() if p)
    return f'Saved {len(trip)} correction(s). {summary}. China page rebuilt locally.'


def handler(slug, records, html_page):
    class Picker(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path in ('/', '/index.html'):
                data = html_page.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if not self.path.startswith('/hosted-photos/'):
                self.send_error(404)
                return
            super().do_GET()

        def do_POST(self):
            if self.path != '/apply':
                self.send_error(404)
                return
            try:
                n = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(n) or b'{}')
                data = {'ok': True, 'message': apply(slug, records, body.get('assign') or {})}
            except Exception as exc:  # report errors in the picker
                data = {'ok': False, 'error': str(exc)}
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return Picker


def main():
    args = sys.argv[1:]
    port = 8795
    if '--port' in args:
        i = args.index('--port')
        port = int(args[i + 1])
        del args[i:i + 2]
    suggestions = None
    if '--suggest' in args:
        i = args.index('--suggest')
        suggestions = load_json(Path(args[i + 1]), {})
        del args[i:i + 2]
    args = [a for a in args if a != '--no-open']
    slug = args[0] if args else '2026-07-china-qinghai-gansu-tibet'
    records = build_records(slug, ProvinceIndex(GEOJSON), suggestions)
    provinces = trip_provinces(records)
    html_page = page(slug, records, provinces)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), handler(slug, records, html_page))
    except OSError:
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(slug, records, html_page))
    url = f'http://localhost:{server.server_address[1]}/'
    counts = {p: sum(1 for r in records if r['assigned'] == p) for p in provinces}
    flagged = sum(1 for r in records if r['flags'])
    print(f'province review · {slug} · {len(records)} photos · {counts} · {flagged} flagged')
    print(f'serving {url} (Ctrl-C to stop)')
    if '--no-open' not in sys.argv:
        threading.Timer(.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
