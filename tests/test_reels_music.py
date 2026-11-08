"""tools/reels_local.py's Music pool endpoints: the review folders read live, and a song played by its id only (a review
file inside a review folder, a library song through the manifest from the music library folders), with Range support.
Temp folders only.

    python3 -m unittest tests.test_reels_music
"""
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reels_local_music", HERE.parent / "tools" / "reels_local.py")
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)

SONG = bytes(range(256)) * 8


class MusicEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        t = Path(cls._tmp.name)
        dl = t / "Downloads"
        rev = dl / "reel-song-candidates-2026-11-08"
        (rev / "from-dm").mkdir(parents=True)
        (rev / "Hans Zimmer - Gom Jabbar.mp3").write_bytes(SONG)
        (rev / "from-dm" / "Easy Lover.m4a").write_bytes(b"m4a")
        (rev / "CANDIDATES.md").write_text("notes")
        (dl / "elsewhere").mkdir()
        (dl / "elsewhere" / "private.mp3").write_bytes(b"no")
        (rev / "leak.mp3").symlink_to(dl / "elsewhere" / "private.mp3")
        lib = t / "music"
        lib.mkdir()
        (lib / "Hans Zimmer - Mountains.mp3").write_bytes(b"mountains")
        (t / "other.mp3").write_bytes(b"no")
        man = t / "music.json"
        man.write_text(json.dumps({"pool": [{"id": "library/Mountains", "file_name": "Hans Zimmer - Mountains.mp3"}],
                                   "library": [{"id": "library/Bad", "file_name": "../other.mp3"},
                                               {"id": "library/Gone", "file_name": "gone.mp3"}]}))
        rl.DOWNLOADS, rl.MUSIC_ROOTS, rl.MUSIC_MANIFEST = dl, [lib], man
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), rl.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls._tmp.cleanup()

    def get(self, path, headers=None, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        if host:
            req.add_unredirected_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def play(self, ref, **kw):
        return self.get("/reels/music/play?ref=" + urllib.parse.quote(ref), **kw)

    def test_the_review_list_is_read_live_audio_only_no_links_out(self):
        code, _, body = self.get("/reels/music/review")
        ids = [f["id"] for f in json.loads(body)["files"]]
        self.assertEqual(code, 200)
        self.assertEqual(ids, ["review/reel-song-candidates-2026-11-08/Hans Zimmer - Gom Jabbar.mp3",
                               "review/reel-song-candidates-2026-11-08/from-dm/Easy Lover.m4a"])

    def test_a_review_song_plays_with_ranges(self):
        code, h, body = self.play("review/reel-song-candidates-2026-11-08/Hans Zimmer - Gom Jabbar.mp3",
                                  headers={"Range": "bytes=10-19"})
        self.assertEqual((code, body, h["Content-Type"]), (206, SONG[10:20], "audio/mpeg"))
        self.assertEqual(h["Content-Range"], f"bytes 10-19/{len(SONG)}")

    def test_a_library_song_plays_through_the_manifest(self):
        code, h, body = self.play("library/Mountains")
        self.assertEqual((code, body), (200, b"mountains"))

    def test_nothing_else(self):
        for ref in ("review/reel-song-candidates-2026-11-08/leak.mp3",
                    "review/reel-song-candidates-2026-11-08/CANDIDATES.md",
                    "review/reel-song-candidates-2026-11-08/../elsewhere/private.mp3",
                    "review/elsewhere/private.mp3", "library/Bad", "library/Gone", "library/Unknown",
                    "/etc/passwd", "library/Mountains/../x", ""):
            self.assertEqual(self.play(ref)[0], 404, ref)

    def test_a_rebound_host_name_gets_nothing(self):
        self.assertEqual(self.play("library/Mountains", host="evil.example")[0], 404)


if __name__ == "__main__":
    unittest.main()
