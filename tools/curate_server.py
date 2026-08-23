#!/usr/bin/env python3
"""Local curation server behind the query box in the Posts "Auto curated" tab.

Preloads the photo pool, cached CLIP embeddings and the text encoder once,
then answers free-text curation queries ("truck stops in china", "central
asia landscapes") in well under a second. The Posts UI probes /health and
only shows the query box when this server is running, so production is
unaffected.

    ./venv/bin/python tools/curate_server.py [port]     # default 8799

Endpoints (JSON, CORS *):
  GET /health              {ok, ready}
  GET /curate?q=<text>     {posts: [...], meta: {...}} - up to one public and
                           one private post, same shape the auto set uses.
                           The UI inserts them into the auto doc itself.

Local-network only (binds 0.0.0.0 so the phone can use it via the LAN IP,
like serve.sh); it serves photo ids only, never image bytes.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_curate_posts as ac  # noqa: E402

STATE = {}
LOCK = threading.Lock()


def load():
    pool = ac.load_pool()
    embs = ac.load_embeddings(pool)
    kept = ac.prep_query_pool(pool, embs)   # also warms the text encoder
    STATE['pool'] = kept
    STATE['embs'] = embs
    print('✓ curate server ready', flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/health':
            self._send(200, {'ok': True, 'ready': 'pool' in STATE})
            return
        if u.path == '/curate':
            if 'pool' not in STATE:
                self._send(503, {'error': 'warming up, retry in a few seconds'})
                return
            q = (parse_qs(u.query).get('q') or [''])[0].strip()
            if not q:
                self._send(400, {'error': 'missing q'})
                return
            with LOCK:
                posts, meta = ac.build_query_posts(STATE['pool'], STATE['embs'], q)
            print(f"  '{q}' -> {meta['matches']} matches, {len(posts)} post(s)", flush=True)
            self._send(200, {'posts': posts, 'meta': meta})
            return
        self._send(404, {'error': 'not found'})

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    threading.Thread(target=load, daemon=True).start()
    srv = HTTPServer(('0.0.0.0', port), Handler)
    print(f'Curate server on http://0.0.0.0:{port} (loading pool + model in background...)',
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
