#!/usr/bin/env python3
"""Dev server for the map. Sends no-cache headers so reprocessed trips always
show fresh (the default http.server caches manifest.json/route.geojson, which
leads to stale photo placements after a re-run)."""
import http.server
import socketserver
from pathlib import Path

PORT = 8000
WEB_DIR = Path(__file__).parent.parent / 'web'


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()


if __name__ == '__main__':
    import os
    os.chdir(WEB_DIR)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), NoCacheHandler) as httpd:
        print(f"Serving {WEB_DIR} at http://localhost:{PORT} (no-cache)")
        httpd.serve_forever()
