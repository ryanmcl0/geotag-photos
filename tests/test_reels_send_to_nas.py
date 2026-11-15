"""tools/reels_local.py: Posts ▸ Reels ▸ Reviewed ▸ Posting schedule (owner 2026-11-15): the proposed schedule (the
video project's propose.py) and Send to NAS (its queue_sync.py, one run at a time, its RESULT line back to the page).
Fake checkouts and a fake process: nothing is run, nothing is sent."""
import importlib.util
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reels_local_send", HERE.parent / "tools" / "reels_local.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def checkout(root, name, tools):
    ve = Path(root) / name
    (ve / ".venv" / "bin").mkdir(parents=True)
    (ve / ".venv" / "bin" / "python").write_text("")
    (ve / "tools" / "reelbot").mkdir(parents=True)
    for t, text in tools.items():
        (ve / "tools" / "reelbot" / t).write_text(text)
    return ve


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = io.StringIO("".join(x + "\n" for x in lines))
        self.rc = rc

    def wait(self):
        return self.rc

    def kill(self):
        pass


class SendToNasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = checkout(self.tmp.name, "old", {"queue_sync.py": "no flag here"})
        self.new = checkout(self.tmp.name, "new", {"queue_sync.py": "ap.add_argument('--wait-bot')", "propose.py": "x"})
        R.SEND["run"] = None

    def tearDown(self):
        self.tmp.cleanup()

    def wait_done(self):
        for _ in range(100):
            if R.send_status()["state"] != "running":
                return R.send_status()
            time.sleep(0.01)
        self.fail("the run never ended")

    def test_the_first_checkout_that_has_send_to_nas_runs_it_and_its_result_comes_back(self):
        argv = []
        res = {"lines": 205, "to_bot": 152, "refused": [{"path": "/a.mp4", "why": "no such file"}]}

        def popen(a, **kw):
            argv.append((a, kw["cwd"]))
            return FakeProc(["checking", "RESULT " + json.dumps(res), "done"])
        code, run = R.send_start(popen=popen, checkouts=[self.old, self.new])
        self.assertEqual((code, run["state"]), (202, "running"))
        st = self.wait_done()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["result"], res)
        self.assertEqual(st["log"], ["checking", "done"])
        a, cwd = argv[0]
        self.assertEqual(cwd, str(self.new))
        self.assertEqual(a[1:], [str(self.new / "tools/reelbot/queue_sync.py"), "--site", "auto", "--wait-bot", "240",
                                 "--json"])

    def test_one_run_at_a_time(self):
        gate = []

        class Slow(FakeProc):
            def wait(self):
                while not gate:
                    time.sleep(0.01)
                return 0
        R.send_start(popen=lambda a, **kw: Slow(["RESULT {}"]), checkouts=[self.new])
        code, run = R.send_start(popen=lambda a, **kw: FakeProc([]), checkouts=[self.new])
        self.assertEqual((code, run["error"]), (409, "a send is already running"))
        gate.append(1)
        self.wait_done()

    def test_a_failure_is_said_in_plain_words(self):
        R.send_start(popen=lambda a, **kw: FakeProc(["Traceback", "SystemExit: the local Posts server isn't running"], rc=1),
                     checkouts=[self.new])
        st = self.wait_done()
        self.assertEqual(st["state"], "failed")
        self.assertIn("the local Posts server isn't running", st["error"])

    def test_no_checkout_with_the_tool(self):
        code, r = R.send_start(checkouts=[self.old])
        self.assertEqual(code, 503)
        self.assertIn("update its checkout", r["error"])

    def test_the_proposed_schedule_from_propose_py(self):
        seen = []

        def run(argv, cwd, data):
            seen.append((argv, cwd, json.loads(data)))
            return json.dumps({"count": 2, "rows": []})
        code, out = R.propose({"reel_queue": {"items": [{"path": "/a.mp4"}]}}, run=run, checkouts=[self.old, self.new])
        self.assertEqual((code, out["count"]), (200, 2))
        self.assertEqual(seen[0][1], str(self.new))
        self.assertEqual(seen[0][2], {"items": [{"path": "/a.mp4"}]})
        self.assertEqual(R.propose({}, run=run, checkouts=[self.new])[0], 400)


if __name__ == "__main__":
    unittest.main()
