#!/usr/bin/env python3
"""reels_local — the tiny local helper behind Posts ▸ Reels (localhost only, never deployed).

The site's dev server (wrangler pages dev) serves web/ but cannot write files, so the reel posting state and the 4K
export requests are written by this helper instead, next to the reel manifest in the local-only web/phone/reels/:

    state.json          {"queue": [reel ids, in posting order], "posted": {id: "YYYY-MM-DD"}, "captions": {id: text}}
    export_queue.json   {"items": [{"id", "requested", "status": "queued"|"running"|"done"|"failed", "caption", ...}]}
    song_feedback.json  {"items": [{"id", "song", "plan", "verdict": "pool"|"reel", "note", "requested",
                                    "status": "pending"|"applied"|"queued"|"failed", ...}]}

The 4K render itself is the video project's job (tools/vlog/reel_export.py reads export_queue.json, renders one reel
at a time and marks each item done); the browser only ever asks for it. Song feedback works the same way: the
video project's tools/vlog/song_feedback.py reads song_feedback.json ("pool": move the song out of the music
library; "reel": not this song for this reel, queued for a new one) and marks each item.

Endpoints (CORS open to the local site; listens on 127.0.0.1 only, so nothing else on the network can write):
    GET  /reels/state            the state (an empty one if none yet)
    PUT  /reels/state            replace it (validated)
    GET  /reels/export           the export queue
    POST /reels/export           {"ids": [...], "captions": {id: text}} → queue the ids not already queued
    GET  /reels/song-feedback    the song feedback
    POST /reels/song-feedback    {"id", "verdict": "pool"|"reel"|"undo", "note"} → record a rejection of the reel's
                                 song (the song and plan are taken from reels.json, not from the browser); "undo"
                                 withdraws that reel's rejections the pipeline hasn't acted on yet
    GET  /reveal?trip=&id=       show the photo's EDITED file (the Lightroom export the site was built from) in
                                 Finder: the lightbox's "Finder" button on localhost. The file is looked up by
                                 the trip manifest's source folder + the photo's source filename, never from
                                 a path the browser sends.
    GET/HEAD /reels/files/<folder>/<name>   a reel video or cover from web/phone/reels/files/, with Range support
                                 (206 + Content-Range, streamed in chunks). The dev server ignores Range and sends the
                                 whole file for every seek, which is what made the videos lag on the external SSD. Only
                                 files that resolve under one of that folder's own links are served, nothing else.

    tools/reels_local.py [--port 8791]          (serve.sh starts it)
"""
import datetime as dt
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent / "web" / "phone" / "reels"
STATE = ROOT / "state.json"
QUEUE = ROOT / "export_queue.json"
SONGS = ROOT / "song_feedback.json"
FILES = ROOT / "files"
MAX_BODY = 2_000_000
CHUNK = 1 << 20
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def clean_state(s):
    if not isinstance(s, dict):
        raise ValueError("state must be an object")
    queue = [x for x in s.get("queue", []) if isinstance(x, str)][:5000]
    posted = {k: v for k, v in (s.get("posted") or {}).items() if isinstance(k, str) and isinstance(v, str)}
    captions = {k: v[:5000] for k, v in (s.get("captions") or {}).items() if isinstance(k, str) and isinstance(v, str)}
    return {"queue": list(dict.fromkeys(queue)), "posted": posted, "captions": captions,
            "updated": dt.datetime.now().isoformat(timespec="seconds")}


def known_ids():
    doc = read(ROOT / "reels.json", {})
    return {r["id"] for r in doc.get("reels", []) if isinstance(r, dict) and "id" in r}


def song_feedback(body):
    """Apply one POST /reels/song-feedback to song_feedback.json; → (code, response)."""
    rid, verdict = body.get("id"), body.get("verdict")
    if verdict not in ("pool", "reel", "undo"):
        return 400, {"error": "verdict must be pool, reel or undo"}
    reel = next((r for r in read(ROOT / "reels.json", {}).get("reels", [])
                 if isinstance(r, dict) and r.get("id") == rid), None)
    if not reel:
        return 400, {"error": "unknown reel"}
    fb = read(SONGS, {"items": []})
    if verdict == "undo":
        fb["items"] = [it for it in fb["items"] if not (it.get("id") == rid and it.get("status") == "pending")]
    elif not reel.get("song"):
        return 400, {"error": "this reel has no song on record"}
    elif not any(it.get("id") == rid and it.get("verdict") == verdict and it.get("status") == "pending"
                 for it in fb["items"]):
        item = {"id": rid, "song": reel["song"], "plan": (reel.get("source") or {}).get("plan"),
                "trip": reel.get("trip"), "verdict": verdict,
                "requested": dt.datetime.now().isoformat(timespec="seconds"), "status": "pending"}
        if isinstance(body.get("note"), str) and body["note"].strip():
            item["note"] = body["note"].strip()[:500]
        fb["items"].append(item)
    write(SONGS, fb)
    return 200, fb


WEB = Path(__file__).resolve().parent.parent / "web"
_edit_index = {}          # photos_path -> {basename: full path}, built on first use per trip


def edit_path(trip, photo_id):
    """The edited file on disk behind one hosted photo, or None (unknown trip/id, folder not mounted)."""
    if not trip or not photo_id or "/" in trip or "\\" in trip:
        return None
    for root in (WEB / "trips" / trip, WEB / "phone" / "trips" / trip):
        man = read(root / "manifest.json", None)
        if man:
            break
    else:
        return None
    photos_path = ((man.get("source") or {}).get("photos_path") or "").strip()
    name = next((ph.get("source_filename") for ph in man.get("photos", []) if ph.get("id") == photo_id), None)
    if not photos_path or not name or not os.path.isdir(photos_path):
        return None
    if photos_path not in _edit_index:
        idx = {}
        for d, _, files in os.walk(photos_path):
            for f in files:
                idx.setdefault(f, os.path.join(d, f))
        _edit_index[photos_path] = idx
    hit = _edit_index[photos_path].get(name)
    if hit and not os.path.exists(hit):            # exported again since the index was built
        _edit_index.pop(photos_path, None)
        return edit_path(trip, photo_id)
    return hit


def media_path(rel):
    """The file behind /reels/files/<rel>, or None. It must sit under the real folder one of files/'s own entries
    (a delivery-folder link) points to: no '..', no absolute paths, and a link inside a delivery folder that leads
    elsewhere is refused because the resolved path is checked, not the one asked for."""
    if not rel or "\0" in rel or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or len(parts) < 2:
        return None
    if Path(parts[-1]).suffix.lower() not in MEDIA_TYPES:
        return None
    try:
        top = (FILES / parts[0]).resolve(strict=True)
        path = (FILES / rel).resolve(strict=True)
        if not top.is_dir() or not path.is_file() or not path.is_relative_to(top):
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path


def parse_range(header, size):
    """('full', 0, size-1) with no usable Range header, ('part', first, last) for one satisfiable range,
    ('bad', 0, 0) when it lies outside the file. Several ranges are answered with the whole file (allowed)."""
    if not header or not header.startswith("bytes=") or "," in header:
        return "full", 0, size - 1
    a, _, b = header[6:].strip().partition("-")
    try:
        if a == "":                                  # bytes=-N: the last N bytes
            n = int(b)
            if n <= 0:
                return "bad", 0, 0
            first, last = max(size - n, 0), size - 1
        else:
            first = int(a)
            last = min(int(b), size - 1) if b else size - 1
    except ValueError:
        return "full", 0, size - 1
    if first < 0 or first >= size or last < first:
        return "bad", 0, 0
    return "part", first, last


def reveal(path):
    import subprocess
    subprocess.Popen(["open", "-R", path])


class Handler(BaseHTTPRequestHandler):
    server_version = "reels_local/1"

    def _cors(self):
        origin = self.headers.get("Origin", "")
        self.send_header("Access-Control-Allow-Origin", origin or "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _media(self, head):
        """A reel video/cover with Range support, streamed from disk in chunks (never read whole into memory)."""
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        path = None
        if host in ("127.0.0.1", "localhost"):       # not a rebound DNS name
            path = media_path(unquote(self.path.split("?")[0][len("/reels/files/"):]))
        if path is None:
            return self._json(404, {"error": "not found"})
        try:
            f = open(path, "rb")
            st = os.fstat(f.fileno())
        except OSError:
            return self._json(404, {"error": "not found"})
        with f:
            size = st.st_size
            kind, first, last = parse_range(self.headers.get("Range"), size)
            if kind == "bad":
                self.send_response(416)
                self._cors()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206 if kind == "part" else 200)
            self._cors()
            self.send_header("Content-Type", MEDIA_TYPES[path.suffix.lower()])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
            if kind == "part":
                self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            self.send_header("Content-Length", str(last - first + 1))
            self.end_headers()
            if head:
                return
            left = last - first + 1
            try:
                f.seek(first)
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass                                 # the player seeked away or closed: normal

    def do_HEAD(self):
        if self.path.startswith("/reels/files/"):
            return self._media(True)
        self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path.startswith("/reels/files/"):
            return self._media(False)
        if self.path.split("?")[0] == "/reveal":
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            path = edit_path((q.get("trip") or [""])[0], (q.get("id") or [""])[0])
            if not path:
                return self._json(404, {"error": "no edited file found for this photo (trip folder mounted?)"})
            reveal(path)
            return self._json(200, {"path": path})
        if self.path.split("?")[0] == "/reels/state":
            return self._json(200, read(STATE, {"queue": [], "posted": {}, "captions": {}}))
        if self.path.split("?")[0] == "/reels/export":
            return self._json(200, read(QUEUE, {"items": []}))
        if self.path.split("?")[0] == "/reels/song-feedback":
            return self._json(200, read(SONGS, {"items": []}))
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path.split("?")[0] != "/reels/state":
            return self._json(404, {"error": "not found"})
        try:
            s = clean_state(self._body())
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        write(STATE, s)
        self._json(200, s)

    def do_POST(self):
        if self.path.split("?")[0] == "/reels/song-feedback":
            try:
                return self._json(*song_feedback(self._body()))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if self.path.split("?")[0] != "/reels/export":
            return self._json(404, {"error": "not found"})
        try:
            body = self._body()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        ids = [x for x in body.get("ids", []) if isinstance(x, str)]
        caps = body.get("captions") or {}
        valid = known_ids()
        q = read(QUEUE, {"items": []})
        open_ids = {it["id"] for it in q["items"] if it.get("status") in ("queued", "running")}
        added = []
        for i in ids:
            if i not in valid or i in open_ids:
                continue
            item = {"id": i, "requested": dt.datetime.now().isoformat(timespec="seconds"), "status": "queued"}
            if isinstance(caps.get(i), str):
                item["caption"] = caps[i][:5000]
            q["items"].append(item)
            added.append(i)
        write(QUEUE, q)
        self._json(200, {"added": added, "queue": q})

    def log_message(self, fmt, *args):
        sys.stderr.write("reels_local: " + (fmt % args) + "\n")


def main(argv):
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8791
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"reels_local: port {port} busy ({e}); already running?")
        return 0
    print(f"reels_local: http://127.0.0.1:{port} → {ROOT}")
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
