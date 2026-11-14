"""Posts ▸ Reels in sections (owner 2026-11-14): the parts of the tab that decide something, run under node with a
stub window (no browser): the Footage section's figures, sort and filters (posts-footage.js, against the
footage_usage.py contract), To review's choice of reels (posts-review.js) and what the post queue says has been decided
about a reel (posts-queue.js decisionOf). Skipped when node is missing.

    python3 -m unittest tests.test_reels_tab
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "web" / "js"

# loads the given files into a stub window and runs the case named in argv against the JSON on stdin
RUNNER = r"""
const fs = require('fs');
const store = {};
global.window = global;
global.location = { hostname: 'localhost', hash: '', pathname: '/posts.html', search: '' };
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
global.document = { addEventListener() {}, dispatchEvent() {}, getElementById() { return null; } };
global.CustomEvent = function (t, o) { this.type = t; this.detail = o && o.detail; };
let docBody = null;
global.fetch = async (url) => ({ ok: !!docBody, status: docBody ? 200 : 404,
    json: async () => JSON.parse(docBody), text: async () => docBody });
const [, , dir, ...files] = process.argv;
files.forEach(f => new Function(fs.readFileSync(dir + '/' + f, 'utf8'))());
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
    const out = {};
    if (input.footage) {
        const F = window.PostsFootage;
        const rep = input.footage;
        const names = r => r.map(t => t.name);
        out.summary = F.summary(rep);
        out.defaultOrder = names(F.tripRows(rep, 'drone', null, false).listed);
        out.privateTrips = names(F.tripRows(rep, 'drone', null, false).priv);
        out.noReelsOnly = names(F.tripRows(rep, 'drone', null, true).listed);
        out.byReelsDesc = names(F.tripRows(rep, 'drone', { col: 'reels', dir: 'desc' }, false).listed);
        out.byLeftDesc = names(F.tripRows(rep, 'drone', { col: 'left', dir: 'desc' }, false).listed);
        out.allOrder = names(F.tripRows(rep, 'all', null, false).listed);
        const t = rep.trips[0];
        out.side = { drone: F.sideFigs(t, 'drone'), all: F.sideFigs(t, 'all') };
        out.pcts = [F.fmtPct(null), F.fmtPct(0), F.fmtPct(0.4), F.fmtPct(33.6)];
        out.countNone = F.count();
        docBody = JSON.stringify(rep);
        await F.load(true);
        out.count = F.count();
        docBody = '<!DOCTYPE html><html>';           // the dev server's fallback page for a missing file
        await F.load(true);
        out.countMissing = F.count();
    }
    if (input.posts) {
        docBody = JSON.stringify(input.posts);
        await window.PostsQueue.ready();
        out.decisions = input.paths.map(p => window.PostsQueue.decisionOf(p));
        out.counts = window.PostsQueue.counts();
        if (input.reels) {
            window.ReelsSite = { state: () => ({ queue: [], posted: input.posted || {}, captions: {} }) };
            window.PostsReview.setDoc(input.reels);
            out.pending = window.PostsReview.pending(input.reels).map(r => r.id);
            out.reviewCount = window.PostsReview.count();
            out.tripNames = input.reels.reels.map(r => window.PostsReview.tripName(r));
        }
    }
    console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def dev(clips, used, hours=0.0):
    return {"clips": clips, "used": used, "hours": hours, "hours_used": 0.0}


def trip(name, drone, other, reels, status="public", **k):
    """drone, other: (clips, used)."""
    by = {"drone": dev(*drone, hours=1.0), "camera": dev(*other, hours=2.0)}
    t = {"key": name, "name": name, "folder": name, "year": 2024, "status": status,
         "clips": drone[0] + other[0], "used": drone[1] + other[1], "pct": 0.0, "hours": 3.0, "hours_used": 0.0,
         "by_device": by, "private_clips": 0, "reels": reels, "reels_unresolved": 0, "reels_by_group": {},
         "drafts": 0, "drafts_only_clips": 0, "posted": None, "parts_kind": "day", "parts": []}
    t.update(k)
    return t


REPORT = {
    "version": 1, "made": "2026-11-14T10:30:00", "devices": ["drone", "camera", "action", "360", "phone"],
    "totals": {"clips": 1000, "used": 120, "pct": 12.0, "hours": 9.0, "hours_used": 1.0,
               "by_device": {"drone": dev(400, 100), "camera": dev(600, 20)},
               "reels": 30, "reels_unresolved": 1, "drafts": 4, "private_clips": 90, "trips": 6,
               "trips_without_reels": 2},
    "trips": [
        trip("Well used", (100, 60), (200, 10), 20),
        trip("Barely used", (200, 4), (100, 0), 3),
        trip("No reels", (80, 0), (50, 0), 0),
        trip("No drone", (0, 0), (250, 10), 7),
        trip("Nothing public", (0, 0), (0, 0), 0),
        trip("Secret", (0, 0), (0, 0), 0, status="private", private_clips=90),
    ],
    "unmatched_clips": [], "unresolved_reels": ["/x/a.mp4"],
}


@unittest.skipUnless(shutil.which("node"), "node isn't installed")
class ReelsTab(unittest.TestCase):
    def run_js(self, files, payload):
        p = subprocess.run(["node", "-e", RUNNER, "x", str(JS), *files], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    # ── Footage ──
    def test_footage_summary(self):
        s = self.run_js(["posts-footage.js"], {"footage": REPORT})["summary"]
        self.assertEqual(s["reels"], 30)
        self.assertEqual((s["drone"]["used"], s["drone"]["clips"]), (100, 400))
        self.assertEqual(s["dronePct"], 25)
        self.assertEqual((s["all"]["used"], s["all"]["clips"]), (120, 1000))
        # trips with footage and no reels: not the private one, not the one with no public clips
        self.assertEqual(s["noReels"], 1)
        self.assertEqual(s["unresolved"], 1)

    def test_footage_default_order_puts_the_gaps_first(self):
        out = self.run_js(["posts-footage.js"], {"footage": REPORT})
        # least used share of drone clips first; trips with no drone clips last (bigger first)
        self.assertEqual(out["defaultOrder"], ["No reels", "Barely used", "Well used", "No drone", "Nothing public"])
        self.assertEqual(out["privateTrips"], ["Secret"])
        self.assertEqual(out["noReelsOnly"], ["No reels"])

    def test_footage_sorts_and_camera_filter(self):
        out = self.run_js(["posts-footage.js"], {"footage": REPORT})
        self.assertEqual(out["byReelsDesc"][:3], ["Well used", "No drone", "Barely used"])
        self.assertEqual(out["byLeftDesc"][:2], ["Barely used", "No reels"])
        # all cameras: No drone has 10/250 = 4 %, below Barely used (4/300); Nothing public (no clips) last
        self.assertEqual(out["allOrder"][0], "No reels")
        self.assertEqual(out["allOrder"][-1], "Nothing public")
        # the column beside the bars: every other camera, or the drone when the bars show all cameras
        self.assertEqual((out["side"]["drone"]["clips"], out["side"]["drone"]["used"]), (200, 10))
        self.assertEqual((out["side"]["all"]["clips"], out["side"]["all"]["used"]), (100, 60))
        self.assertEqual(out["pcts"], ["—", "0%", "<1%", "34%"])

    def test_footage_report_missing_or_present(self):
        out = self.run_js(["posts-footage.js"], {"footage": REPORT})
        self.assertIsNone(out["countNone"])
        self.assertEqual(out["count"], "25%")
        self.assertIsNone(out["countMissing"])          # an HTML fallback page is no report

    # ── Review: what has been decided, and what is left to judge ──
    def test_decisions_and_to_review(self):
        sfc = "/Volumes/Crucial X9/Short Form Content"
        reels = {"reels": [
            {"id": "a", "path": f"{sfc}/Trip A/1 Days/a.mp4", "delivered": "2026-11-01", "trip": "ta"},
            {"id": "b", "path": f"{sfc}/Trip A/1 Days/b.mp4", "delivered": "2026-11-08", "trip": "ta"},
            {"id": "c", "path": f"{sfc}/Trip B/2 Shoots/c.mp4", "delivered": "2026-11-08", "trip": "tb"},
            {"id": "d", "path": f"{sfc}/Trip B/2 Shoots/d.mp4", "delivered": "2026-11-03", "trip": "tb"},
            {"id": "e", "path": f"{sfc}/Trip B/2 Shoots/e.mp4", "delivered": "2026-11-09", "trip": "tb"},
            {"id": "f", "path": f"{sfc}/Trip B/2 Shoots/f.mp4", "delivered": "2026-11-09", "trip": "tb"},
            {"id": "g", "path": "/elsewhere/g.mp4", "delivered": "2026-10-01", "trip": "tc"},
            {"id": "h", "path": f"{sfc}/Trip A/1 Days/h.mp4", "delivered": "2026-11-10", "trip": "ta"},
        ]}
        posts = {"version": 3, "posts": [], "reel_queue": {
            "items": [{"id": "1", "path": f"{sfc}/Trip A/1 Days/a.mp4", "status": "scheduled"},
                      {"id": "2", "path": "/Users/me/day/reels/c.mp4", "status": "posted"}],   # another copy of c
            "shortlist": [{"id": "3", "path": f"{sfc}/Trip B/2 Shoots/d.mp4"}]},
            "reel_rejects": {"items": [{"id": "4", "path": f"{sfc}/Trip B/2 Shoots/e.mp4", "reason": "too slow"}]}}
        out = self.run_js(["posts-queue.js", "posts-review.js"], {
            "posts": posts, "reels": reels, "posted": {"h": "2026-11-12"},
            "paths": [r["path"] for r in reels["reels"]]})
        kinds = [d and d["kind"] for d in out["decisions"]]
        self.assertEqual(kinds, ["queue", None, "posted", "shortlist", "rejected", None, None, None])
        self.assertEqual(out["decisions"][2]["at"], "/Users/me/day/reels/c.mp4")   # Undo acts on the stored line
        self.assertEqual(out["decisions"][4]["reason"], "too slow")
        # not queued, shortlisted, rejected or posted (h is ticked Posted on the Profile list); newest first
        self.assertEqual(out["pending"], ["f", "b", "g"])
        self.assertEqual(out["reviewCount"], 3)
        self.assertEqual(out["tripNames"][:3], ["Trip A", "Trip A", "Trip B"])
        self.assertEqual(out["tripNames"][6], "tc")
        self.assertEqual(out["counts"], {"loaded": True, "queue": 1, "shortlist": 1, "rejected": 1})


if __name__ == "__main__":
    unittest.main()
