"""tools/reels_local.py GET /reels/status: the video project's status report made on demand (its own tool asked with
--print), cached for a few seconds; None when the tool is missing or answers nonsense, so the page reads the file."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reels_local_status", HERE.parent / "tools" / "reels_local.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


class LiveStatusTest(unittest.TestCase):
    def setUp(self):
        R._STATUS.update(at=0.0, doc=None)

    def checkout(self, d):
        ve = Path(d) / "video-editor"
        (ve / ".venv" / "bin").mkdir(parents=True)
        (ve / ".venv" / "bin" / "python").write_text("")
        (ve / "tools" / "vlog").mkdir(parents=True)
        (ve / "tools" / "vlog" / "reel_status.py").write_text("")
        return ve

    def test_asks_the_tool_and_caches_the_answer(self):
        with tempfile.TemporaryDirectory() as d:
            ve = self.checkout(d)
            calls = []
            doc = {"version": 2, "made": "2026-11-14T21:00:00", "now": []}

            def run(argv, cwd):
                calls.append((argv, cwd))
                return json.dumps(doc)

            t = [100.0]
            got = R.live_status(run, lambda: t[0], [Path(d) / "missing", ve])
            self.assertEqual(got, doc)
            self.assertEqual(calls[0][0][1:], [str(ve / "tools" / "vlog" / "reel_status.py"), "--print"])
            t[0] += R.STATUS_TTL - 1
            R.live_status(run, lambda: t[0], [ve])
            self.assertEqual(len(calls), 1)                      # within the cache time: not asked again
            t[0] += 2
            R.live_status(run, lambda: t[0], [ve])
            self.assertEqual(len(calls), 2)

    def test_no_tool_or_nonsense_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(R.live_status(lambda a, c: "{}", lambda: 1.0, [Path(d) / "missing"]))
            ve = self.checkout(d)
            self.assertIsNone(R.live_status(lambda a, c: "<html>", lambda: 1.0, [ve]))
            self.assertIsNone(R.live_status(lambda a, c: json.dumps({"made": "x"}), lambda: 1.0, [ve]))


if __name__ == "__main__":
    unittest.main()
