"""tools/reels_local.py /reels/files: reel videos and covers with Range support, and nothing outside the delivery links.

    python3 -m unittest tests.test_reels_media
"""
import importlib.util
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reels_local", HERE.parent / "tools" / "reels_local.py")
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)

DATA = bytes(range(256)) * 40          # 10,240 bytes, every offset recognisable


class MediaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "delivery" / "sub").mkdir(parents=True)
        (tmp / "delivery" / "a.mp4").write_bytes(DATA)
        (tmp / "delivery" / "sub" / "b.jpg").write_bytes(b"jpegbytes")
        (tmp / "delivery" / "notes.txt").write_bytes(b"not media")
        (tmp / "secret").mkdir()
        (tmp / "secret" / "s.mp4").write_bytes(b"secret")
        (tmp / "delivery" / "leak.mp4").symlink_to(tmp / "secret" / "s.mp4")      # a link inside a delivery folder
        files = tmp / "files"
        files.mkdir()
        (files / "reels").symlink_to(tmp / "delivery")
        (files / "stray.mp4").write_bytes(b"x")                                    # not under a folder link
        rl.FILES = files
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), rl.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._tmp.cleanup()

    def get(self, path, headers=None, method="GET"):
        req = urllib.request.Request(self.base + path, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_whole_file(self):
        code, h, body = self.get("/reels/files/reels/a.mp4")
        self.assertEqual((code, body), (200, DATA))
        self.assertEqual(h["Accept-Ranges"], "bytes")
        self.assertEqual(h["Content-Type"], "video/mp4")
        self.assertEqual(h["Content-Length"], str(len(DATA)))

    def test_range_is_206_with_the_right_bytes(self):
        code, h, body = self.get("/reels/files/reels/a.mp4", {"Range": "bytes=1000-1999"})
        self.assertEqual(code, 206)
        self.assertEqual(body, DATA[1000:2000])
        self.assertEqual(h["Content-Range"], f"bytes 1000-1999/{len(DATA)}")
        self.assertEqual(h["Content-Length"], "1000")

    def test_open_ended_suffix_and_clamped_ranges(self):
        n = len(DATA)
        self.assertEqual(self.get("/reels/files/reels/a.mp4", {"Range": "bytes=10000-"})[2], DATA[10000:])
        code, h, body = self.get("/reels/files/reels/a.mp4", {"Range": "bytes=-100"})
        self.assertEqual((code, body, h["Content-Range"]), (206, DATA[-100:], f"bytes {n - 100}-{n - 1}/{n}"))
        code, h, body = self.get("/reels/files/reels/a.mp4", {"Range": "bytes=10000-999999"})
        self.assertEqual((code, body), (206, DATA[10000:]))

    def test_unsatisfiable_range_is_416(self):
        code, h, _ = self.get("/reels/files/reels/a.mp4", {"Range": "bytes=99999-"})
        self.assertEqual((code, h["Content-Range"]), (416, f"bytes */{len(DATA)}"))

    def test_garbage_range_gets_the_whole_file(self):
        self.assertEqual(self.get("/reels/files/reels/a.mp4", {"Range": "bytes=abc-def"})[::2], (200, DATA))
        self.assertEqual(self.get("/reels/files/reels/a.mp4", {"Range": "bytes=0-1,5-9"})[::2], (200, DATA))

    def test_head_has_headers_and_no_body(self):
        code, h, body = self.get("/reels/files/reels/a.mp4", method="HEAD")
        self.assertEqual((code, body, h["Content-Length"]), (200, b"", str(len(DATA))))

    def test_cover_in_a_subfolder(self):
        code, h, body = self.get("/reels/files/reels/sub/b.jpg")
        self.assertEqual((code, body, h["Content-Type"]), (200, b"jpegbytes", "image/jpeg"))

    def test_percent_encoded_names_work(self):
        (Path(rl.FILES) / "reels" / "with space.mp4").write_bytes(b"hi")
        self.assertEqual(self.get("/reels/files/reels/with%20space.mp4")[::2], (200, b"hi"))

    def test_escapes_are_refused(self):
        for p in ("/reels/files/reels/../stray.mp4", "/reels/files/reels/%2e%2e/stray.mp4",
                  "/reels/files/reels/..%2fstray.mp4", "/reels/files/reels/sub/../../../secret/s.mp4",
                  "/reels/files/stray.mp4",                # in files/ but not inside a folder link
                  "/reels/files/reels/leak.mp4",           # a link inside the folder that leads elsewhere
                  "/reels/files//etc/hosts", "/reels/files/%2fetc/hosts", "/reels/files/reels/a.mp4%00.jpg",
                  "/reels/files/reels/notes.txt",          # not a media type
                  "/reels/files/reels/", "/reels/files/", "/reels/files/reels/nope.mp4"):
            self.assertEqual(self.get(p)[0], 404, p)

    def test_a_rebound_host_name_is_refused(self):
        code, _, _ = self.get("/reels/files/reels/a.mp4", {"Host": "evil.example:8791"})
        self.assertEqual(code, 404)

    def test_only_get_and_head(self):
        for m in ("PUT", "POST", "DELETE"):
            req = urllib.request.Request(self.base + "/reels/files/reels/a.mp4", data=b"x", method=m)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertIn(cm.exception.code, (404, 501), m)


if __name__ == "__main__":
    unittest.main()
