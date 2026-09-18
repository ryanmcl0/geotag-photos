#!/usr/bin/env python3
"""Select public photos to hide from one trip's gallery.

The picker intentionally starts with the *public* manifest: every image shown is
currently visible on the normal site. Click selects a photo for hiding; Apply
adds those ids to config/photo_privacy.json's ``force_blocked`` list and rebuilds
the local manifests. They are hidden even in an owner/See All session. Existing
privacy rules are never removed here, except a previously selected picker photo is
migrated from the old, merely-gated ``force_private`` list.

    ./venv/bin/python tools/trip_hide_picker.py 2026-06-italy-dolomites
"""
import html
import json
import subprocess
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVACY = ROOT / 'config' / 'photo_privacy.json'


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def gallery_photos(slug: str):
    manifest = load_json(ROOT / 'web' / 'trips' / slug / 'manifest.json', None)
    if not manifest:
        raise ValueError(f'No public manifest for {slug}')
    return manifest.get('photos') or []


def page(slug: str, photos: list[dict]) -> str:
    cells = []
    for p in photos:
        pid = p['id']
        thumb = p.get('thumbnail') or f'thumbnails/{pid}.webp'
        display = p.get('display') or f'display/{pid}.webp'
        cells.append(
            f'<div class="photo" role="button" tabindex="0" data-id="{html.escape(pid)}" '
            f'data-display="/hosted-photos/{html.escape(slug)}/{html.escape(display)}" '
            f'title="{html.escape(pid)}">'
            f'<img loading="lazy" src="/hosted-photos/{html.escape(slug)}/{html.escape(thumb)}" '
            f'alt="{html.escape(pid)}"><span class="id">{html.escape(pid)}</span>'
            f'<span class="state">HIDE</span><button class="open" type="button" title="Open full-screen viewer">⤢</button></div>')
    data = json.dumps({'slug': slug})
    return f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hide photos — {html.escape(slug)}</title>
<style>
:root{{--bg:#111;--panel:#1a1a1c;--line:#333;--fg:#eee;--muted:#aaa;--red:#cd4247}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.top{{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:16px 22px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{margin:0;color:var(--muted)}} .sub b{{color:#fff}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;padding:18px 22px 105px}}
.photo{{border:2px solid var(--line);border-radius:7px;overflow:hidden;padding:0;background:#000;position:relative;aspect-ratio:1.35;cursor:pointer;color:inherit}}
.photo img{{width:100%;height:100%;object-fit:cover;display:block}} .photo:hover{{border-color:#8dbcec}} .photo:focus-visible{{outline:3px solid #8dbcec;outline-offset:2px}}
.id{{position:absolute;left:0;right:0;bottom:0;padding:16px 7px 5px;text-align:left;font-size:11px;background:linear-gradient(transparent,rgba(0,0,0,.9));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.state{{display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#6d2024;color:#ffd9d9;border-radius:16px;padding:5px 12px;font-weight:700;letter-spacing:.08em;font-size:12px}}
.photo.selected{{border-color:#ec676a}} .photo.selected img{{opacity:.48}} .photo.selected .state{{display:block}}
.open{{position:absolute;top:7px;right:7px;display:none;background:#111c;color:#fff;border:1px solid #aaa;border-radius:5px;padding:3px 6px;cursor:pointer;font-size:14px}} .photo:hover .open,.photo:focus-within .open{{display:block}}
.bar{{position:fixed;right:18px;bottom:18px;z-index:10;background:var(--panel);border:1px solid var(--line);box-shadow:0 8px 28px #0008;border-radius:10px;padding:11px 13px;display:flex;align-items:center;gap:12px}}
.bar .count{{color:var(--muted)}} .bar .count b{{color:#fff;font-size:17px}} button.apply{{background:var(--red);border:0;border-radius:7px;color:#fff;padding:8px 12px;font-weight:700;cursor:pointer}} button.apply:disabled{{opacity:.45;cursor:not-allowed}} .msg{{max-width:310px;color:var(--muted);font-size:12px}}
.viewer{{display:none;position:fixed;inset:0;z-index:20;background:#000e;align-items:center;justify-content:center}} .viewer.open{{display:flex}}
.viewer img{{max-width:94vw;max-height:90vh;object-fit:contain}} .viewer button{{position:absolute;background:#222d;color:#fff;border:1px solid #777;border-radius:6px;font-size:25px;line-height:1;padding:7px 11px;cursor:pointer}} .close{{top:17px;right:18px}} .prev{{left:18px;top:50%}} .next{{right:18px;top:50%}} .viewer .label{{position:absolute;bottom:18px;color:#ddd;font-size:13px;text-shadow:0 1px 3px #000}}
</style>
<header class="top"><h1>Hide photos · {html.escape(slug)}</h1><p class="sub"><b>Every photo below is currently public in this gallery.</b> Click any photo to select it for hiding. Selections are only applied after you press Apply. Use ⤢ for the full-screen viewer.</p></header>
<main class="grid">{''.join(cells)}</main>
<div class="bar"><span class="count"><b id="count">0</b> selected to hide</span><button id="apply" class="apply" disabled>Apply hiding</button><span id="msg" class="msg"></span></div>
<div id="viewer" class="viewer"><button class="close" title="Close">×</button><button class="prev" title="Previous">‹</button><img alt=""><button class="next" title="Next">›</button><span class="label"></span></div>
<script>const CFG={data};
const cells=[...document.querySelectorAll('.photo')], selected=new Set(); let viewing=-1;
const count=document.querySelector('#count'), apply=document.querySelector('#apply'), msg=document.querySelector('#msg');
function refresh(){{count.textContent=selected.size;apply.disabled=!selected.size;cells.forEach(c=>c.classList.toggle('selected',selected.has(c.dataset.id)));}}
function show(i){{viewing=(i+cells.length)%cells.length;const c=cells[viewing],v=document.querySelector('#viewer');v.querySelector('img').src=c.dataset.display;v.querySelector('.label').textContent=c.dataset.id+' · '+(viewing+1)+' / '+cells.length;v.classList.add('open');}}
cells.forEach((c,i)=>{{c.addEventListener('click',e=>{{if(e.target.closest('.open')) return;selected.has(c.dataset.id)?selected.delete(c.dataset.id):selected.add(c.dataset.id);refresh();}});c.addEventListener('keydown',e=>{{if(e.key==='Enter'||e.key===' '){{e.preventDefault();c.click();}}}});c.querySelector('.open').onclick=e=>{{e.stopPropagation();show(i);}};}});
document.querySelector('.close').onclick=()=>document.querySelector('#viewer').classList.remove('open');document.querySelector('.prev').onclick=()=>show(viewing-1);document.querySelector('.next').onclick=()=>show(viewing+1);
document.querySelector('#viewer').onclick=e=>{{if(e.target.id==='viewer')e.currentTarget.classList.remove('open');}};
document.addEventListener('keydown',e=>{{const v=document.querySelector('#viewer');if(v.classList.contains('open')){{if(e.key==='Escape')v.classList.remove('open');if(e.key==='ArrowLeft')show(viewing-1);if(e.key==='ArrowRight')show(viewing+1);}}}});
apply.onclick=async()=>{{apply.disabled=true;msg.textContent='Applying and rebuilding…';try{{const r=await fetch('/apply',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ids:[...selected]}})}});const j=await r.json();msg.textContent=j.ok?j.message:'Error: '+j.error;if(j.ok){{selected.clear();refresh();}}else apply.disabled=false;}}catch(e){{msg.textContent='Error: '+e;apply.disabled=false;}}}};refresh();
</script>'''


def apply(slug: str, ids: list[str]) -> str:
    valid = {p['id'] for p in gallery_photos(slug)}
    chosen = sorted(set(ids) & valid)
    if not chosen:
        return 'No photos selected.'
    cfg = load_json(PRIVACY, {})
    force_blocked = cfg.setdefault('force_blocked', {})
    force_blocked[slug] = sorted(set(force_blocked.get(slug) or []) | set(chosen))
    # The first version of this picker used force_private. A full hide supersedes
    # that less strict rule, so a repeated selection never leaves confusing state.
    force_private = cfg.get('force_private') or {}
    if slug in force_private:
        force_private[slug] = sorted(set(force_private[slug]) - set(chosen))
        if not force_private[slug]:
            del force_private[slug]
    PRIVACY.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    result = subprocess.run([sys.executable, str(ROOT / 'build_collections.py')], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout[-1000:])
    return f'{len(chosen)} photo(s) fully hidden. Local preview rebuilt.'


def handler(slug: str, initial_page: str):
    class Picker(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path in ('/', '/picker', '/index.html'):
                data = initial_page.encode()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
                return
            super().do_GET()

        def do_POST(self):
            if self.path != '/apply':
                self.send_error(404); return
            try:
                n = int(self.headers.get('Content-Length', 0))
                message = apply(slug, (json.loads(self.rfile.read(n) or b'{}').get('ids') or []))
                data = {'ok': True, 'message': message}
            except Exception as exc:  # report errors in the picker
                data = {'ok': False, 'error': str(exc)}
            raw = json.dumps(data).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    return Picker


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else '2026-06-italy-dolomites'
    photos = gallery_photos(slug)
    if not photos:
        raise SystemExit(f'No public photos to review for {slug}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(slug, page(slug, photos)))
    url = f'http://localhost:{server.server_address[1]}/'
    print(f'hide picker · {slug} · {len(photos)} public photos')
    print(f'serving {url} (Ctrl-C to stop)')
    threading.Timer(.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
