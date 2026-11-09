"""functions/api/posts.ts validates reel_music_pool, including the Rejected music box's lines (owner 2026-11-09). The
validator isn't exported, so its text is cut out of the file and run under node (stripping the TypeScript types);
skipped when node is missing.

    python3 -m unittest tests.test_posts_music_validation
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

TS = Path(__file__).resolve().parent.parent / "functions" / "api" / "posts.ts"

RUNNER = r"""
const fs = require('fs');
const { stripTypeScriptTypes } = require('module');
const src = fs.readFileSync(process.argv[1], 'utf8');
const consts = [...src.matchAll(/^const (MAX_POOL_LINES|POOL_STATUSES|POOL_TEXT|MAX_REJECT_LINES|REJECT_STATUSES|REJECT_TEXT)\b[^;]*;/gms)]
    .map(m => m[0]).join('\n');
const start = src.indexOf('function validMusicPool');
const end = src.indexOf('\n}\n', start) + 3;
const code = stripTypeScriptTypes(consts + '\n' + src.slice(start, end) + '\nmodule.exports = validMusicPool;');
const m = { exports: {} };
new Function('module', code)(m);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(c => m.exports(c))));
"""


def line(**k):
    return {"id": "r1", "action": "reject", "reel": "/x/day01_loop_globe.mp4", **k}


def pool(*rejects, **k):
    return {"items": [], "removed": [], "rejects": list(rejects), **k}


@unittest.skipUnless(shutil.which("node"), "node isn't installed")
class RejectedMusicValidation(unittest.TestCase):
    def run_cases(self, *cases):
        p = subprocess.run(["node", "-e", RUNNER, str(TS)], input=json.dumps(cases), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_good_lines_pass(self):
        got = self.run_cases(
            pool(), pool(line()), pool(line(reason="This music is all trash", added="2026-11-09T14:00:00Z", status="waiting")),
            pool(line(status="done", song="the lion king suite： part 2", title="The Lion King Suite", uses=2,
                      applied="2026-11-09T15:00:00Z")),
            pool(line(status="failed", why="no plan found for this reel")),
            pool({"id": "u1", "action": "undo", "song": "the lion king suite： part 2", "title": "x", "status": "waiting"}),
            {"items": [{"id": "library/Time"}]})                      # a pool with no rejects list is as before
        self.assertEqual(got, [True] * 6 + [True])

    def test_bad_lines_are_refused(self):
        bad = [
            pool(line(action="delete")),                              # unknown action
            pool(line(reel="   ")),                                   # nothing pasted
            pool({"id": "u", "action": "undo"}),                      # an undo names its song
            pool(line(status="in")),                                  # not a reject status
            pool(line(uses=-1)), pool(line(uses=1.5)),
            pool(line(reason="x" * 501)),                             # reason ≤ 500
            pool(line(extra="field")),                                # known fields only
            pool(line(id="")),
            pool(*[line(id=f"r{i}") for i in range(301)]),            # ≤ 300 lines
            {"items": [], "rejects": "nope"},
            {"items": [], "other": []},
        ]
        self.assertEqual(self.run_cases(*bad), [False] * len(bad))


if __name__ == "__main__":
    unittest.main()
