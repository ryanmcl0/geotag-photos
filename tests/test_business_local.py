"""tools/reels_local.py /business: the Business tab's content folder, its shared state.json and its files.

- No config: no tab (ok false, "not set up"); a folder that isn't there: "unreachable".
- PUT /business/state changes only the tab's fields, only at the current rev (409 + the state on disk otherwise),
  drops unknown ids and non-https links, keeps what other writers keep there, and refuses another site's page.
- /business/files serves only what content.json lists, inside the folder, with Range.

    python3 -m unittest tests.test_business_local
"""
import importlib.util
import json
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

VIDEO = bytes(range(256)) * 8
CONTENT = {"version": 1, "post_command": "post {ref}", "items": [
    {"id": "reel-1", "kind": "reel", "n": 1, "title": "One", "file": "reels/one.mp4", "silent": "reels-silent/one.mp4",
     "cover": "covers/reel-1.jpg", "caption": "One."},
    {"id": "car-a", "kind": "carousel", "title": "A", "files": ["carousels/a/01.jpg"], "cover": "carousels/a/01.jpg",
     "caption": "A."}]}


class BusinessLocalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), rl.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.folder = tmp / "content"
        for rel, data in (("reels/one.mp4", VIDEO), ("reels-silent/one.mp4", b"s"), ("covers/reel-1.jpg", b"c"),
                          ("carousels/a/01.jpg", b"j"), ("carousels/a/02.jpg", b"unlisted"), ("notes.mp4", b"n")):
            (self.folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / rel).write_bytes(data)
        (tmp / "outside.mp4").write_bytes(b"secret")
        (self.folder / "reels" / "leak.mp4").symlink_to(tmp / "outside.mp4")
        (self.folder / "content.json").write_text(json.dumps(
            {**CONTENT, "items": CONTENT["items"] + [{"id": "leak", "kind": "reel", "n": 2, "title": "x",
                                                      "file": "reels/leak.mp4", "caption": "x"}]}))
        self.state = {"version": 1, "rev": 3, "start": "2026-11-16", "every": 1, "queue": ["reel-1", "car-a"],
                      "posted": {}, "captions": {}, "skipped": {"car-a": "2026-11-15"}, "bot_note": "kept"}
        (self.folder / "state.json").write_text(json.dumps(self.state))
        self.cfg = tmp / "business" / "config.json"
        self.cfg.parent.mkdir()
        self.cfg.write_text(json.dumps({"content_folder": str(self.folder), "instagram": {"username": "@brand"}}))
        rl.BUSINESS_CONFIG = self.cfg

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def doc(self):
        return json.loads(self.call("/business")[2])

    def test_the_document(self):
        d = self.doc()
        self.assertEqual((d["ok"], d["account"], d["state"]["rev"]), (True, "brand", 3))
        self.assertEqual([it["id"] for it in d["content"]["items"]][:2], ["reel-1", "car-a"])

    def test_no_config_no_tab_and_a_missing_folder(self):
        self.cfg.unlink()
        self.assertEqual(self.doc(), {"ok": False, "reason": "not set up"})
        self.cfg.write_text(json.dumps({"content_folder": str(self.folder / "gone")}))
        self.assertEqual(self.doc()["reason"], "unreachable")

    def test_a_save_at_the_current_rev(self):
        body = {"rev": 3, "start": "2026-11-20", "every": 2, "queue": ["car-a", "reel-1", "nope", "car-a"],
                "posted": {"reel-1": {"date": "2026-11-12", "via": "hand", "permalink": "javascript:alert(1)"},
                           "nope": {"date": "2026-11-12"}},
                "captions": {"car-a": "Edited.", "nope": "x"}, "skipped": {}, "bot_note": "changed"}
        code, _, out = self.call("/business/state", "PUT", body, {"Origin": "http://localhost:8788"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads((self.folder / "state.json").read_text()), self.state)   # the NAS waits for Send
        s = json.loads((self.cfg.parent / "draft.json").read_text())
        self.assertEqual(s, json.loads(out))
        self.assertEqual((s["rev"], s["start"], s["every"], s["queue"]), (4, "2026-11-20", 2, ["car-a", "reel-1"]))
        self.assertEqual(s["per_day"], 1)                                                    # absent: one a day
        self.assertEqual(s["posted"], {"reel-1": {"date": "2026-11-12", "via": "hand"}})
        self.assertEqual(s["captions"], {"car-a": "Edited."})
        self.assertEqual((s["skipped"], s["bot_note"]), ({"car-a": "2026-11-15"}, "kept"))   # not the tab's to change

    def test_posts_a_day_is_kept_and_clamped(self):
        for sent, kept in ((2, 2), (9, 5), (0, 1)):
            body = {**self.state, "per_day": sent}
            code, _, out = self.call("/business/state", "PUT", body, {"Origin": "http://localhost:8788"})
            self.assertEqual((code, json.loads(out)["per_day"]), (200, kept))
            self.state = {**self.state, "rev": json.loads(out)["rev"]}
        code, _, _ = self.call("/business/state", "PUT", {**self.state, "per_day": "x"}, {"Origin": "http://localhost:8788"})
        self.assertEqual(code, 400)

    def test_review_verdicts(self):
        body = {**self.state, "reviews": {
            "reel-1": {"verdict": "post", "stars": 4, "note": " keep the hook ", "time": "2026-11-15T20:00:00",
                       "queued": True},
            "car-a": {"verdict": "shortlist", "note": "slide 3 text too small", "status": "handled", "stars": 5},
            "leak": {"verdict": "shortlist"},                       # a shortlist needs its note
            "nope": {"verdict": "post"}, }}
        code, _, out = self.call("/business/state", "PUT", body, {"Origin": "http://localhost:8788"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(out)["reviews"], {
            "reel-1": {"verdict": "post", "stars": 4, "note": "keep the hook", "time": "2026-11-15T20:00:00",
                       "queued": True},
            "car-a": {"verdict": "shortlist", "note": "slide 3 text too small", "status": "handled"}})
        # a page that doesn't send reviews leaves them as they are
        code, _, out = self.call("/business/state", "PUT", {**self.state, "rev": 4})
        self.assertEqual((code, sorted(json.loads(out)["reviews"])), (200, ["car-a", "reel-1"]))
        bad = {**self.state, "rev": 5, "reviews": {"reel-1": {"verdict": "maybe"}, "car-a": {"verdict": "reject"}}}
        self.assertEqual(json.loads(self.call("/business/state", "PUT", bad)[2])["reviews"], {"car-a": {"verdict": "reject"}})

    def test_a_stale_rev_gets_the_state_on_disk(self):
        code, _, out = self.call("/business/state", "PUT", {**self.state, "rev": 2, "queue": []})
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(out)["state"]["queue"], ["reel-1", "car-a"])
        self.assertEqual(json.loads((self.folder / "state.json").read_text())["rev"], 3)

    def test_send_to_nas(self):
        self.assertFalse(self.doc()["unsent"])
        body = {**self.state, "queue": ["car-a"], "every": 2, "posted": {"reel-1": {"date": "2026-11-14", "via": "hand"}},
                "reviews": {"car-a": {"verdict": "post", "stars": 5}}}
        self.assertEqual(self.call("/business/state", "PUT", body)[0], 200)
        d = self.doc()
        self.assertTrue(d["unsent"])
        self.assertEqual((d["state"]["queue"], d["nas"]["rev"]), (["car-a"], 3))
        # meanwhile the bot posts car-a and marks a skip on the NAS: both show through the draft, and survive the send
        nas = {**self.state, "posted": {"car-a": {"date": "2026-11-15", "via": "bot"}}, "skipped": {"x": "y"}}
        (self.folder / "state.json").write_text(json.dumps(nas))
        self.assertEqual(sorted(self.doc()["state"]["posted"]), ["car-a", "reel-1"])
        self.assertEqual(self.call("/business/send-to-nas", "POST", {}, {"Origin": "https://example.com"})[0], 403)
        code, _, out = self.call("/business/send-to-nas", "POST", {})
        self.assertEqual((code, json.loads(out)["sent"]), (200, True))
        s = json.loads((self.folder / "state.json").read_text())
        self.assertEqual((s["rev"], s["queue"], s["every"], s["skipped"], s["bot_note"]), (4, ["car-a"], 2, {"x": "y"}, "kept"))
        self.assertEqual(s["posted"], {"car-a": {"date": "2026-11-15", "via": "bot"},
                                       "reel-1": {"date": "2026-11-14", "via": "hand"}})
        self.assertEqual(s["reviews"], {"car-a": {"verdict": "post", "stars": 5}})
        self.assertFalse((self.cfg.parent / "draft.json").exists())
        self.assertFalse(self.doc()["unsent"])
        self.assertEqual(json.loads(self.call("/business/send-to-nas", "POST", {})[2])["sent"], False)

    def test_bad_values_and_other_sites_are_refused(self):
        self.assertEqual(self.call("/business/state", "PUT", {**self.state, "start": "soon"})[0], 400)
        code, _, _ = self.call("/business/state", "PUT", self.state, {"Origin": "https://example.com"})
        self.assertEqual(code, 403)
        self.assertEqual(json.loads((self.folder / "state.json").read_text())["rev"], 3)

    def test_the_avatar(self):
        self.assertIsNone(self.doc()["avatar"])
        self.assertEqual(self.call("/business/files/avatar.png")[0], 404)
        (self.folder / "avatar.png").write_bytes(b"logo")
        self.assertEqual(self.doc()["avatar"], "avatar.png")
        self.assertEqual(self.call("/business/files/avatar.png")[2], b"logo")

    def test_files_listed_in_content_only(self):
        code, h, body = self.call("/business/files/reels/one.mp4", headers={"Range": "bytes=10-19"})
        self.assertEqual((code, body, h["Content-Range"]), (206, VIDEO[10:20], f"bytes 10-19/{len(VIDEO)}"))
        self.assertEqual(self.call("/business/files/carousels/a/01.jpg")[2], b"j")
        for path in ("carousels/a/02.jpg", "notes.mp4", "state.json", "content.json", "reels/../notes.mp4",
                     "reels/leak.mp4"):
            self.assertEqual(self.call("/business/files/" + path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
