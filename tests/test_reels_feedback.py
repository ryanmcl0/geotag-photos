"""tools/reels_local.py POST /reels/feedback: the note he writes when he shortlists a reel on Posts ▸ Reels ▸ Review
("good, but something needs fixing"). One open note per reel, a new one replaces it, undo withdraws it, a note the
video project has handled is never touched; the reel's path and title come from reels.json, not from the browser."""
import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reels_local_feedback", HERE.parent / "tools" / "reels_local.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "reels.json").write_text(json.dumps({"reels": [
            {"id": "a", "path": "/sfc/2026 China Summer/5 Hooks/a.mp4", "title": "A", "trip": "t"},
            {"id": "b", "path": "/sfc/2024 Mongolia/2 Shoots/b.mp4", "title": "B", "trip": "m"}]}))
        self.old = (R.ROOT, R.FEEDBACK, R.RATINGS)
        R.ROOT, R.FEEDBACK, R.RATINGS = self.root, self.root / "reel_feedback.json", self.root / "reel_ratings.json"

    def tearDown(self):
        R.ROOT, R.FEEDBACK, R.RATINGS = self.old
        self.tmp.cleanup()

    def test_a_note_is_kept_replaced_and_withdrawn(self):
        t = dt.datetime(2026, 11, 14, 22, 30)
        code, doc = R.reel_feedback({"id": "a", "note": "  the last shot is too slow  "}, t)
        self.assertEqual(code, 200)
        self.assertEqual(doc["items"], [{"id": "a", "path": "/sfc/2026 China Summer/5 Hooks/a.mp4", "title": "A", "trip": "t",
                                         "note": "the last shot is too slow", "kind": "fix", "time": "2026-11-14T22:30:00",
                                         "status": "open"}])
        R.reel_feedback({"id": "a", "note": "swap the song"}, t)
        R.reel_feedback({"id": "b", "note": "pin too long"}, t)
        got = json.loads(R.FEEDBACK.read_text())["items"]
        self.assertEqual([(i["id"], i["note"]) for i in got], [("a", "swap the song"), ("b", "pin too long")])
        code, doc = R.reel_feedback({"id": "a", "undo": True}, t)
        self.assertEqual([i["id"] for i in doc["items"]], ["b"])

    def test_refusals_and_a_handled_note_stays(self):
        self.assertEqual(R.reel_feedback({"id": "zzz", "note": "x"})[0], 400)       # a reel the list does not know
        self.assertEqual(R.reel_feedback({"id": "a", "note": "   "})[0], 400)       # an empty note
        R.FEEDBACK.write_text(json.dumps({"items": [{"id": "a", "note": "old", "status": "handled"}]}))
        R.reel_feedback({"id": "a", "note": "new"})
        R.reel_feedback({"id": "a", "undo": True})
        self.assertEqual(json.loads(R.FEEDBACK.read_text())["items"], [{"id": "a", "note": "old", "status": "handled"}])
        long = R.reel_feedback({"id": "b", "note": "x" * 5000})[1]["items"][-1]["note"]
        self.assertEqual(len(long), R.NOTE_MAX)

    def test_a_fixed_reel_shortlisted_again_keeps_the_round_before(self):
        """The video project's fix ("handled", "fixed", "fixed_time") is never changed by the page: a new Shortlist
        note reopens the item with the fixed round in "history", undo puts it back exactly; a carried item and every
        field the video project wrote survive the page's writes."""
        t = dt.datetime(2026, 11, 16, 21, 0)
        fixed = {"id": "a", "path": "/sfc/2026 China Summer/5 Hooks/a.mp4", "title": "A", "trip": "t",
                 "note": "the last shot is too slow", "kind": "fix", "time": "2026-11-14T22:30:00", "status": "handled",
                 "fixed": "The last shot now runs at real speed and is 2 s shorter.", "fixed_time": "2026-11-16T20:00:00"}
        carried = {"id": "b", "path": "/sfc/2024 Mongolia/2 Shoots/b.mp4", "title": "B", "trip": "m", "kind": "carried",
                   "from": "a", "note": "the last shot is too slow", "status": "handled",
                   "fixed": "Same slow ending, shortened.", "fixed_time": "2026-11-16T20:05:00", "extra": {"x": 1}}
        R.FEEDBACK.write_text(json.dumps({"items": [fixed, carried]}))
        # the browser can't write the fixed text, the history or a kind of its own
        R.reel_feedback({"id": "a", "note": "still too slow", "fixed": "nothing", "history": [], "kind": "carried"}, t)
        items = json.loads(R.FEEDBACK.read_text())["items"]
        self.assertEqual(items[0], carried)                                   # untouched, every field kept
        new = items[1]
        self.assertEqual((new["id"], new["note"], new["kind"], new["status"]), ("a", "still too slow", "fix", "open"))
        self.assertNotIn("fixed", new)
        self.assertEqual(new["history"], [{k: v for k, v in fixed.items() if k != "id"}])
        # a second note replaces the open one and keeps the history
        R.reel_feedback({"id": "a", "note": "still too slow, and the song"}, t)
        items = json.loads(R.FEEDBACK.read_text())["items"]
        self.assertEqual(len([i for i in items if i["id"] == "a"]), 1)
        self.assertEqual(items[-1]["history"][0]["fixed"], fixed["fixed"])
        # undo: the handled round is back as the video project left it
        R.reel_feedback({"id": "a", "undo": True}, t)
        self.assertEqual(json.loads(R.FEEDBACK.read_text())["items"], [carried, fixed])
        # undo with nothing open never touches a handled item
        R.reel_feedback({"id": "b", "undo": True}, t)
        self.assertEqual(json.loads(R.FEEDBACK.read_text())["items"], [carried, fixed])
        # a Post + note on a fixed reel is its own item; the fix stays as it is
        R.reel_feedback({"id": "a", "note": "good now", "kind": "post"}, t)
        items = json.loads(R.FEEDBACK.read_text())["items"]
        self.assertIn(fixed, items)
        self.assertEqual(items[-1]["kind"], "post")
        self.assertNotIn("history", items[-1])
        # fixed, shortlisted again, fixed again, shortlisted a third time: both rounds in order, oldest first
        second = dict(fixed, note="still too slow", time="2026-11-17T09:00:00", fixed="Cut 1 s more.",
                      fixed_time="2026-11-17T12:00:00", history=[{k: v for k, v in fixed.items() if k != "id"}])
        R.FEEDBACK.write_text(json.dumps({"items": [second]}))
        R.reel_feedback({"id": "a", "note": "third time"}, t)
        h = json.loads(R.FEEDBACK.read_text())["items"][0]["history"]
        self.assertEqual([x["fixed"] for x in h], [fixed["fixed"], "Cut 1 s more."])
        self.assertTrue(all("history" not in x for x in h))
        R.reel_feedback({"id": "a", "undo": True}, t)
        self.assertEqual(json.loads(R.FEEDBACK.read_text())["items"], [second])

    def test_a_post_note_is_marked_post(self):
        doc = R.reel_feedback({"id": "a", "note": "great pacing, more of these", "kind": "post"})[1]
        self.assertEqual(doc["items"][-1]["kind"], "post")
        self.assertEqual(R.reel_feedback({"id": "b", "note": "x", "kind": "weird"})[1]["items"][-1]["kind"], "fix")

    def test_stars_are_kept_replaced_and_withdrawn(self):
        t = dt.datetime(2026, 11, 14, 23, 30)
        code, doc = R.reel_rating({"id": "a", "stars": 4}, t)
        self.assertEqual(code, 200)
        self.assertEqual(doc["items"], {"a": {"stars": 4, "path": "/sfc/2026 China Summer/5 Hooks/a.mp4", "title": "A",
                                              "trip": "t", "time": "2026-11-14T23:30:00"}})
        R.reel_rating({"id": "a", "stars": 5}, t)
        R.reel_rating({"id": "b", "stars": 2}, t)
        got = json.loads(R.RATINGS.read_text())["items"]
        self.assertEqual({k: v["stars"] for k, v in got.items()}, {"a": 5, "b": 2})
        self.assertEqual(list(R.reel_rating({"id": "a", "undo": True}, t)[1]["items"]), ["b"])

    def test_star_refusals(self):
        for bad in (0, 6, "3", 2.5, True, None):
            self.assertEqual(R.reel_rating({"id": "a", "stars": bad})[0], 400, bad)
        self.assertEqual(R.reel_rating({"id": "zzz", "stars": 3})[0], 400)
        self.assertFalse(R.RATINGS.exists())


if __name__ == "__main__":
    unittest.main()
