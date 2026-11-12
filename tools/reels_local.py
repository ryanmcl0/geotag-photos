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

    GET  /reels/music/review     the songs waiting for his review, read live from ~/Downloads/reel-song-candidates-*/
                                 (any depth): [{"id": "review/<folder>/<file>", "folder", "name", "size", "mtime"}].
                                 The Music pool section adds one by its id; the video project's queue_sync.py copies
                                 it into the music library (never moves it) at the next sync.
    GET/HEAD /reels/music/play?ref=<id>   one song, to listen to on the page (Range support, streamed): a review
                                 file by its "review/<folder>/<file>" id, inside a review folder only; a library song
                                 by its "library/<track id>" id, looked up in web/phone/reels/music.json (the video
                                 project's manifest) and served only from the music library folders. Nothing is
                                 copied into web/, and the deployed site never has this helper.

Posts ▸ Business (localhost only): ready-made posts for a second Instagram account, kept in a content folder outside
this repo. ~/.config/content-engine/business/config.json names it ("content_folder"; its instagram.username is shown
on the tab). The folder holds content.json (the items, written by the tool that makes them, read only here) and
state.json (the schedule, written here, by that tool's own post command and by the reel bot when it posts):
    {"version": 1, "rev": n, "start": "YYYY-MM-DD", "every": days, "queue": [ids in posting order],
     "posted": {id: {"date", "permalink"?, "media_id"?, "via"}}, "captions": {id: text}, "skipped": {id: date}, ...}
    GET  /business               {"ok", "folder", "account", "content", "state"}; ok false with a reason when the folder
                                 isn't set up (the tab stays hidden) or can't be read (not mounted)
    PUT  /business/state         {"rev", "start", "every", "queue", "posted", "captions"}: only those fields change,
                                 and only when rev is the current one (409 + the current state otherwise, so a mark the
                                 bot wrote meanwhile is never lost); ids must be items of content.json; only from a
                                 page on this Mac (Origin localhost)
    GET/HEAD /business/files/<path>   a file content.json lists (a reel, its silent copy, a cover, a slide), with Range
                                 support; nothing else in the folder

    tools/reels_local.py [--port 8791]          (serve.sh starts it)
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile
import threading
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
# the Music pool section: his review folders and the music library (read only: songs are only listened to here)
DOWNLOADS = Path.home() / "Downloads"
REVIEW_GLOB = "reel-song-candidates-*"
MUSIC_ROOTS = [Path("/Volumes/Crucial X9/music"), Path.home() / "Downloads" / "Edit music"]
MUSIC_MANIFEST = ROOT / "music.json"
AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav",
               ".flac": "audio/flac", ".ogg": "audio/ogg"}
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


def review_songs(root=None):
    """The audio files in ~/Downloads/reel-song-candidates-*/ (any depth), never through a link out of the folder."""
    root = Path(root or DOWNLOADS)
    out = []
    for d in sorted(root.glob(REVIEW_GLOB)):
        if not d.is_dir():
            continue
        base = d.resolve()
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in AUDIO_TYPES:
                continue
            try:
                if not f.resolve().is_relative_to(base):
                    continue
                st = f.stat()
            except OSError:
                continue
            out.append({"id": f"review/{d.name}/{f.relative_to(d).as_posix()}", "folder": d.name, "name": f.name,
                        "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def song_path(ref, root=None, roots=None, manifest=None):
    """The audio file behind a Music pool id, or None. "review/<folder>/<file>": inside a review folder only.
    "library/<track id>": the manifest's file name for that id, inside one of the music library folders. Never a path
    the browser sends."""
    if not isinstance(ref, str) or "\0" in ref or "\\" in ref:
        return None
    parts = ref.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] == "review" and len(parts) >= 3 and Path(parts[1]).match(REVIEW_GLOB):
        base = Path(root or DOWNLOADS) / parts[1]
        rel = "/".join(parts[2:])
        cands = [(base, base / rel)]
    elif parts[0] == "library" and len(parts) == 2:
        doc = read(Path(manifest or MUSIC_MANIFEST), {})
        rows = (doc.get("pool") or []) + (doc.get("removed") or []) + (doc.get("library") or [])
        name = next((r.get("file_name") for r in rows if isinstance(r, dict) and r.get("id") == ref), None)
        if not name or "/" in name or name in (".", ".."):
            return None
        cands = [(Path(r), Path(r) / name) for r in (roots or MUSIC_ROOTS)]
    else:
        return None
    for base, p in cands:
        try:
            real, top = p.resolve(strict=True), base.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if real.is_file() and real.is_relative_to(top) and real.suffix.lower() in AUDIO_TYPES:
            return real
    return None


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


BUSINESS_CONFIG = (Path(os.environ.get("CONTENT_ENGINE_DIR") or Path.home() / ".config" / "content-engine")
                   / "business" / "config.json")
BUSINESS_LOCK = threading.Lock()
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def business_config():
    cfg = read(BUSINESS_CONFIG, {})
    return cfg if isinstance(cfg, dict) else {}


def business_folder(cfg=None):
    f = (cfg if cfg is not None else business_config()).get("content_folder")
    return Path(os.path.expanduser(f)) if isinstance(f, str) and f.strip() else None


def business_doc(cfg=None):
    """What the tab needs: the content, the state and the account, or why there is none."""
    cfg = business_config() if cfg is None else cfg
    folder = business_folder(cfg)
    if folder is None:
        return {"ok": False, "reason": "not set up"}
    account = ((cfg.get("instagram") or {}).get("username") or "").lstrip("@") or None
    if account and account.startswith("<"):           # the config's "<the account's handle>" placeholder
        account = None
    content = read(folder / "content.json", None)
    if not isinstance(content, dict) or not isinstance(content.get("items"), list):
        why = "unreachable" if not folder.is_dir() else "no content.json"
        return {"ok": False, "reason": why, "folder": str(folder), "account": account}
    state = read(folder / "state.json", {})
    return {"ok": True, "folder": str(folder), "account": account, "content": content,
            "state": state if isinstance(state, dict) else {}}


def business_ids(content):
    return {it["id"] for it in content.get("items", []) if isinstance(it, dict) and isinstance(it.get("id"), str)}


def clean_business_state(body, current, content):
    """The new state.json from a PUT, or raises ValueError. The tab's fields come from the body (ids checked against
    content.json), everything else stays as it is on disk (what the bot and the post command keep there)."""
    if not isinstance(body, dict):
        raise ValueError("state must be an object")
    ids = business_ids(content)
    out = dict(current)
    start = body.get("start")
    if start not in (None, "") and not (isinstance(start, str) and DAY.fullmatch(start)):
        raise ValueError("start must be YYYY-MM-DD")
    out["start"] = start or None
    try:
        every = int(body.get("every") or 1)
    except (TypeError, ValueError):
        raise ValueError("every must be a number of days")
    out["every"] = min(max(every, 1), 30)
    out["queue"] = list(dict.fromkeys(x for x in body.get("queue") or [] if x in ids))
    posted = {}
    for k, v in (body.get("posted") or {}).items():
        if k not in ids or not isinstance(v, dict) or not DAY.fullmatch(str(v.get("date") or "")):
            continue
        posted[k] = {f: v[f] for f in ("date", "permalink", "media_id", "via") if isinstance(v.get(f), str)}
        if posted[k].get("permalink") and not posted[k]["permalink"].startswith("https://"):
            del posted[k]["permalink"]
    out["posted"] = posted
    out["captions"] = {k: v[:5000] for k, v in (body.get("captions") or {}).items() if k in ids and isinstance(v, str)}
    out["version"] = 1
    out["rev"] = int(current.get("rev") or 0) + 1
    out["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    return out


def put_business_state(body, cfg=None):
    """→ (code, response). 409 with the current state when the page's rev is not the one on disk."""
    doc = business_doc(cfg)
    if not doc["ok"]:
        return 404, {"error": f"content folder {doc['reason']}"}
    folder = Path(doc["folder"])
    with BUSINESS_LOCK:
        current = read(folder / "state.json", {})
        current = current if isinstance(current, dict) else {}
        if not isinstance(body, dict) or body.get("rev") != current.get("rev", 0):
            return 409, {"error": "changed elsewhere", "state": current}
        try:
            new = clean_business_state(body, current, doc["content"])
        except ValueError as e:
            return 400, {"error": str(e)}
        write(folder / "state.json", new)
    return 200, new


def business_file(rel, cfg=None):
    """The file behind /business/files/<rel>, or None: only a path content.json lists, inside the folder."""
    doc = business_doc(cfg)
    if not doc["ok"] or not rel or "\0" in rel or "\\" in rel:
        return None
    listed = set()
    for it in doc["content"]["items"]:
        if isinstance(it, dict):
            listed.update(x for x in [it.get("file"), it.get("silent"), it.get("cover"), *(it.get("files") or [])]
                          if isinstance(x, str))
    if rel not in listed or Path(rel).suffix.lower() not in MEDIA_TYPES:
        return None
    try:
        top = Path(doc["folder"]).resolve(strict=True)
        path = (top / rel).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(top):
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path


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

    def _local_host(self):
        return self.headers.get("Host", "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")   # not a rebound name

    def _media(self, head):
        """A reel video/cover with Range support, streamed from disk in chunks (never read whole into memory)."""
        path = None
        if self._local_host():
            path = media_path(unquote(self.path.split("?")[0][len("/reels/files/"):]))
        if path is None:
            return self._json(404, {"error": "not found"})
        return self._stream(path, head, MEDIA_TYPES[path.suffix.lower()])

    def _page_on_this_mac(self):
        """A write from the Posts page served on this Mac (or no Origin: curl, the tests), never from another site."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = origin.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
        return host in ("localhost", "127.0.0.1", "[::1]")

    def _business_media(self, head):
        path = None
        if self._local_host():
            path = business_file(unquote(self.path.split("?")[0][len("/business/files/"):]))
        if path is None:
            return self._json(404, {"error": "not found"})
        return self._stream(path, head, MEDIA_TYPES[path.suffix.lower()])

    def _song(self, head):
        """A song for the Music pool section's play button: by its id only (song_path), streamed like a reel."""
        from urllib.parse import parse_qs, urlsplit
        path = None
        if self._local_host():
            path = song_path((parse_qs(urlsplit(self.path).query).get("ref") or [""])[0])
        if path is None:
            return self._json(404, {"error": "not found"})
        return self._stream(path, head, AUDIO_TYPES[path.suffix.lower()])

    def _stream(self, path, head, ctype):
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
            self.send_header("Content-Type", ctype)
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
        if self.path.startswith("/business/files/"):
            return self._business_media(True)
        if self.path.split("?")[0] == "/reels/music/play":
            return self._song(True)
        self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path.startswith("/reels/files/"):
            return self._media(False)
        if self.path.startswith("/business/files/"):
            return self._business_media(False)
        if self.path.split("?")[0] == "/business":
            return self._json(200, business_doc())
        if self.path.split("?")[0] == "/reels/music/play":
            return self._song(False)
        if self.path.split("?")[0] == "/reels/music/review":
            return self._json(200, {"folders": str(DOWNLOADS / REVIEW_GLOB), "files": review_songs()})
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
        if self.path.split("?")[0] == "/business/state":
            if not self._page_on_this_mac():
                return self._json(403, {"error": "only the Posts page on this Mac"})
            try:
                body = self._body()
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(*put_business_state(body))
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
