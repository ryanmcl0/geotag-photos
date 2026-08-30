#!/usr/bin/env python3
"""Local server for the video browser.

  python3 video_browse/serve.py [--port 8765]

Serves the web UI, proxy media (with HTTP Range support so <video> can seek),
original NAS files as a fallback, and a tiny JSON API:

  GET  /api/index      clip index + per-clip proxy/poster/strip availability
  GET  /api/progress   proxy generation progress
  GET  /api/cuts       saved cuts (the "videos" being assembled)
  POST /api/cuts       replace saved cuts (UI posts the whole list)
  POST /api/export     {"cut_id": ...} -> FCPXML + best-effort DaVinci Resolve import
  POST /api/reveal     {"id": ...} -> reveal original file in Finder

Local-only: binds 127.0.0.1. Nothing here is ever deployed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'video_browse' / 'web'
LIB = ROOT / 'local_videos'


def _media_dir():
    # Mirrors ingest.py: config media_dir may point at the NAS or an external
    # SSD; defaults to local_videos/media.
    try:
        d = json.loads((ROOT / 'config' / 'video_browse.json').read_text()).get('media_dir')
        if d:
            return Path(d)
    except (OSError, json.JSONDecodeError):
        pass
    return LIB / 'media'


MEDIA = _media_dir()
CUTS_PATH = LIB / 'cuts.json'
EXPORTS = LIB / 'exports'

CUTS_LOCK = threading.Lock()

MIME = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
        '.json': 'application/json', '.mp4': 'video/mp4', '.mov': 'video/quicktime',
        '.jpg': 'image/jpeg', '.png': 'image/png', '.svg': 'image/svg+xml'}


def load_index():
    p = LIB / 'index.json'
    if not p.exists():
        return {'clips': []}
    return json.loads(p.read_text())


def load_cuts():
    if CUTS_PATH.exists():
        try:
            return json.loads(CUTS_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {'cuts': []}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # quiet
        pass

    # ── helpers ──────────────────────────────────────────────────────────

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, download_name=None):
        """Serve a file with Range support (needed for <video> seeking)."""
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return
        ctype = MIME.get(path.suffix.lower(), 'application/octet-stream')
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if download_name:
            self.send_header('Content-Disposition', f'attachment; filename="{download_name}"')
        self.end_headers()
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 512, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}')

    def clip_by_id(self, cid):
        for c in load_index()['clips']:
            if c['id'] == cid:
                return c
        return None

    # ── GET ──────────────────────────────────────────────────────────────

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ('/', '/index.html'):
            return self.send_file(WEB / 'index.html')
        if path.startswith('/api/'):
            return self.api_get(path)
        if path.startswith('/media/'):
            name = Path(path[len('/media/'):]).name  # no traversal
            return self.send_file(MEDIA / name)
        if path.startswith('/original/'):
            cid = Path(path[len('/original/'):]).name
            clip = self.clip_by_id(cid)
            if not clip:
                self.send_error(404)
                return
            return self.send_file(Path(clip['path']))
        f = WEB / Path(path).name
        if f.is_file():
            return self.send_file(f)
        self.send_error(404)

    def api_get(self, path):
        if path == '/api/index':
            idx = load_index()
            have = set(os.listdir(MEDIA)) if MEDIA.is_dir() else set()
            for c in idx['clips']:
                c['proxy'] = f"{c['id']}.mp4" in have
                c['poster'] = f"{c['id']}.jpg" in have
                c['strip'] = f"{c['id']}.strip.jpg" in have
            return self.send_json(idx)
        if path == '/api/progress':
            p = LIB / 'proxy_progress.json'
            if p.exists():
                try:
                    return self.send_json(json.loads(p.read_text()))
                except json.JSONDecodeError:
                    pass
            return self.send_json({})
        if path == '/api/cuts':
            return self.send_json(load_cuts())
        self.send_error(404)

    # ── POST ─────────────────────────────────────────────────────────────

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            body = self.read_body()
        except json.JSONDecodeError:
            return self.send_json({'error': 'bad json'}, 400)
        if path == '/api/cuts':
            with CUTS_LOCK:
                LIB.mkdir(exist_ok=True)
                CUTS_PATH.write_text(json.dumps(body, indent=1))
            return self.send_json({'ok': True})
        if path == '/api/reveal':
            clip = self.clip_by_id(body.get('id', ''))
            if not clip:
                return self.send_json({'error': 'unknown clip'}, 404)
            subprocess.Popen(['open', '-R', clip['path']])
            return self.send_json({'ok': True})
        if path == '/api/export':
            return self.api_export(body)
        self.send_error(404)

    def api_export(self, body):
        cuts = load_cuts()
        cut = next((c for c in cuts.get('cuts', []) if c.get('id') == body.get('cut_id')), None)
        if not cut:
            return self.send_json({'error': 'unknown cut'}, 404)
        clips_by_id = {c['id']: c for c in load_index()['clips']}
        clips = [clips_by_id[cid] for cid in cut.get('clips', []) if cid in clips_by_id]
        if not clips:
            return self.send_json({'error': 'cut has no clips'}, 400)
        missing = [c['name'] for c in clips if not Path(c['path']).is_file()]
        if missing:
            return self.send_json({'error': f'source files unreachable (NAS mounted?): '
                                            f'{", ".join(missing[:3])}'}, 400)
        sys.path.insert(0, str(ROOT / 'video_browse'))
        import resolve_export
        result = resolve_export.export_cut(cut, clips, EXPORTS)
        # Remember which timeline this cut owns. Exporting again then appends the
        # newly added clips to that same timeline instead of building a fresh one,
        # even if the cut has been renamed here since.
        tl = (result.get('resolve') or {}).get('timeline')
        if tl and cut.get('resolve_timeline') != tl:
            with CUTS_LOCK:
                saved = load_cuts()
                for c in saved.get('cuts', []):
                    if c.get('id') == cut.get('id'):
                        c['resolve_timeline'] = tl
                        c['resolve_project'] = result['resolve'].get('project')
                LIB.mkdir(exist_ok=True)
                CUTS_PATH.write_text(json.dumps(saved, indent=1))
        return self.send_json(result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=None)
    args = ap.parse_args()
    port = args.port
    if port is None:
        try:
            port = json.loads((ROOT / 'config' / 'video_browse.json').read_text()).get('port', 8765)
        except OSError:
            port = 8765
    LIB.mkdir(exist_ok=True)
    try:
        MEDIA.mkdir(parents=True, exist_ok=True)
    except OSError:
        print(f'warning: media dir unreachable ({MEDIA}) - proxies/posters unavailable')
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'video browser: http://localhost:{port}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
