"""post.py merge3: the two-way sync of post drafts between localhost and the live site.

    venv/bin/python -m unittest tests/test_post_sync.py
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location('post', ROOT / 'post.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


def post(i, **k):
    return {'id': i, 'name': i, 'photos': [], **k}


B = [post('a'), post('b'), post('c')]
ids = lambda posts: [p['id'] for p in posts]          # noqa: E731


class Merge3(unittest.TestCase):
    def test_edits_on_different_posts_combine(self):
        out, c = P.merge3(B, [post('a'), post('b'), post('c', caption='laptop')],
                          [post('a'), post('b', posted=True), post('c')])
        self.assertEqual(out, [post('a'), post('b', posted=True), post('c', caption='laptop')])
        self.assertEqual(c, [])

    def test_new_local_draft_stays_on_top_and_a_live_delete_holds(self):
        out, _ = P.merge3(B, [post('n')] + B, [post('a'), post('b')])
        self.assertEqual(ids(out), ['n', 'a', 'b'])

    def test_same_post_changed_on_both_sides_keeps_live_and_reports_laptop(self):
        out, c = P.merge3(B, [post('a'), post('b', caption='L'), post('c')],
                          [post('a'), post('b', caption='R'), post('c')])
        self.assertEqual(out[1]['caption'], 'R')
        self.assertEqual(c[0][0]['caption'], 'L')

    def test_deleted_on_one_side_edited_on_the_other_is_kept(self):
        out, _ = P.merge3(B, [post('a'), post('c')], [post('a'), post('b', caption='R'), post('c')])
        self.assertEqual(ids(out), ['a', 'b', 'c'])

    def test_local_reorder_wins_when_live_did_not_reorder(self):
        out, _ = P.merge3(B, [post('c'), post('a'), post('b')], [post('z'), post('a'), post('b'), post('c')])
        self.assertEqual(ids(out), ['z', 'c', 'a', 'b'])

    def test_unchanged_and_deleted_on_both(self):
        self.assertEqual(P.merge3(B, B, B), (B, []))
        self.assertEqual(P.merge3(B, [post('a')], [post('a')])[0], [post('a')])


def line(path, **k):
    return {'id': path, 'path': path, 'added': '2026-11-07', **k}


QB = {'items': [line('/a.mp4', status='valid'), line('/b.mp4', status='pending')], 'span_days': 14}


class MergeQueue(unittest.TestCase):
    """The Reels tab's post queue: his new line on the laptop and queue_sync's statuses on the live site combine."""

    def test_a_new_line_here_and_a_status_there_combine(self):
        local = {'items': QB['items'] + [line('/c.mp4', status='pending')], 'span_days': 14}
        remote = {'items': [QB['items'][0], line('/b.mp4', status='invalid', why='no plan')], 'span_days': 14,
                  'schedule': {'per_day': 1}}
        out = P.merge_queue(QB, local, remote)
        self.assertEqual([i['path'] for i in out['items']], ['/a.mp4', '/b.mp4', '/c.mp4'])
        self.assertEqual(out['items'][1]['status'], 'invalid')
        self.assertEqual(out['schedule'], {'per_day': 1})

    def test_the_slider_moved_here_wins_over_an_unchanged_live_copy(self):
        out = P.merge_queue(QB, dict(QB, span_days=7, span_set='t'), QB)
        self.assertEqual((out['span_days'], out['span_set']), (7, 't'))

    def test_a_removed_line_stays_removed(self):
        out = P.merge_queue(QB, {'items': QB['items'][:1], 'span_days': 14}, QB)
        self.assertEqual([i['path'] for i in out['items']], ['/a.mp4'])

    def test_the_shortlist_merges_line_by_line(self):
        sb = dict(QB, shortlist=[line('/s1.mp4')])
        local = dict(sb, shortlist=sb['shortlist'] + [line('/s2.mp4')])
        remote = dict(sb, shortlist=[], items=QB['items'] + [line('/s1.mp4', status='pending')])
        out = P.merge_queue(sb, local, remote)      # moved to the queue there, another shortlisted here
        self.assertEqual([i['path'] for i in out['shortlist']], ['/s2.mp4'])
        self.assertEqual([i['path'] for i in out['items']], ['/a.mp4', '/b.mp4', '/s1.mp4'])
        self.assertNotIn('shortlist', P.merge_queue(QB, QB, QB))

    def test_a_side_without_a_queue_never_empties_the_other(self):
        # a live site still on the old functions/api/posts.ts drops the field on every save
        self.assertEqual(P.merge_queue(QB, QB, None), QB)
        self.assertIsNone(P.merge_queue(None, None, None))


class MirrorCarriesTheQueue(unittest.TestCase):
    """RemoteMirror._sync_set with a fake local and live store: the queue goes up; a live site on the old
    functions/api/posts.ts (it drops the field) neither empties the laptop's queue nor gets it re-sent every tick."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (P.read_doc, P.write_doc, P.SYNC_BASE)
        P.SYNC_BASE = Path(self.tmp.name) / 'base.json'
        self.docs = {'L': {'version': 1, 'posts': B, 'reel_queue': QB}, 'R': {'version': 1, 'posts': B}}
        self.writes = []

        def read(base, env, which='main'):
            return json.loads(json.dumps(self.docs[base]))

        def write(base, env, which, posts, v, settings=None, reel_queue=None, reel_rejects=None, reel_music_pool=None):
            self.writes.append(base)
            d = {'version': v + 1, 'posts': posts}
            for f, val in (('reel_queue', reel_queue), ('reel_rejects', reel_rejects),
                           ('reel_music_pool', reel_music_pool)):
                if val is not None and (base == 'L' or self.live_keeps):
                    d[f] = val
            self.docs[base] = d
        P.read_doc, P.write_doc = read, write
        self.m = P.RemoteMirror({}, 'R', 'L')
        self.m.base = {'main': {'posts': B, 'settings': {}}}

    def tearDown(self):
        P.read_doc, P.write_doc, P.SYNC_BASE = self.orig
        self.tmp.cleanup()

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.m._sync_set('main')

    def test_the_queue_goes_up_and_stays_here_when_the_live_site_drops_it(self):
        self.live_keeps = False
        self.assertEqual(self.tick(), 'up')
        self.assertEqual(self.tick(), 'same')
        self.assertEqual(self.writes, ['R'])
        self.assertEqual(self.docs['L']['reel_queue'], QB)

    def test_a_live_site_that_keeps_it_matches(self):
        self.live_keeps = True
        self.tick()
        self.assertEqual(self.docs['R']['reel_queue'], QB)
        self.assertEqual(self.tick(), 'same')


def rej(path, reason='song doesn\'t fit', **k):
    return {'id': path, 'path': path, 'reason': reason, 'rejected': '2026-11-08T18:00:00Z', **k}


RB = {'items': [rej('/a.mp4'), rej('/b.mp4', 'too slow')], 'updated': 't0'}


class MergeRejects(unittest.TestCase):
    """The Reels tab's reject list: merged like the queue, line by line, and never emptied by a side without one."""

    def test_a_reject_here_and_the_macs_note_there_combine(self):
        local = {'items': RB['items'] + [rej('/c.mp4', 'boring')], 'updated': 't1'}
        remote = {'items': [dict(RB['items'][0], noted='2026-11-08T18:05:00Z'), RB['items'][1]], 'updated': 't0',
                  'synced': 's1'}
        out = P.merge_rejects(RB, local, remote)
        self.assertEqual([i['path'] for i in out['items']], ['/a.mp4', '/b.mp4', '/c.mp4'])
        self.assertEqual(out['items'][0]['noted'], '2026-11-08T18:05:00Z')
        self.assertEqual((out['updated'], out['synced']), ('t1', 's1'))

    def test_an_undo_stays_undone(self):
        out = P.merge_rejects(RB, {'items': RB['items'][1:], 'updated': 't1'}, RB)
        self.assertEqual([i['path'] for i in out['items']], ['/b.mp4'])

    def test_a_reason_changed_here_wins_over_an_unchanged_live_copy(self):
        local = {'items': [rej('/a.mp4', 'wrong place name'), RB['items'][1]], 'updated': 't1'}
        out = P.merge_rejects(RB, local, RB)
        self.assertEqual(out['items'][0]['reason'], 'wrong place name')

    def test_a_side_without_a_list_never_empties_the_other(self):
        self.assertEqual(P.merge_rejects(RB, RB, None), RB)
        self.assertEqual(P.merge_rejects(None, None, RB), RB)
        self.assertIsNone(P.merge_rejects(None, None, None))

    def test_the_queue_and_the_rejects_merge_on_their_own(self):
        self.assertEqual(set(P.MERGE_FIELD), set(P.REEL_FIELDS))
        self.assertNotIn('shortlist', P.merge_rejects(RB, RB, RB))


class MirrorCarriesTheRejects(MirrorCarriesTheQueue):
    """The same fake stores, now with a reject list on the laptop: it goes up beside the queue, an older live site that
    drops it neither empties the laptop's nor gets it re-sent every tick, and a live undo comes down."""

    def setUp(self):
        super().setUp()
        self.docs['L']['reel_rejects'] = RB

    def test_the_rejects_go_up_and_stay_here_when_the_live_site_drops_them(self):
        self.live_keeps = False
        self.assertEqual(self.tick(), 'up')
        self.assertEqual(self.tick(), 'same')
        self.assertEqual(self.writes, ['R'])
        self.assertEqual(self.docs['L']['reel_rejects'], RB)

    def test_a_live_site_that_keeps_them_matches_and_an_undo_there_comes_down(self):
        self.live_keeps = True
        self.tick()
        self.assertEqual(self.docs['R']['reel_rejects'], RB)
        self.assertEqual(self.tick(), 'same')
        self.docs['R'] = dict(self.docs['R'], version=self.docs['R']['version'] + 1,
                              reel_rejects={'items': RB['items'][:1], 'updated': 't2'})
        self.assertEqual(self.tick(), 'down')
        self.assertEqual([i['path'] for i in self.docs['L']['reel_rejects']['items']], ['/a.mp4'])
        self.assertEqual(self.docs['L']['reel_queue'], QB)          # the queue untouched by it


class CopyStateCarriesBoth(unittest.TestCase):
    """copy_state (serve.sh's copy down / up): the queue and the reject list travel with the drafts; a source without
    them keeps the destination's."""

    def setUp(self):
        self.orig = (P.read_doc, P.write_doc)
        self.docs = {('S', 'main'): {'version': 3, 'posts': B, 'reel_rejects': RB},
                     ('D', 'main'): {'version': 1, 'posts': B, 'reel_queue': QB},
                     ('S', 'auto'): {'version': 1, 'posts': []}, ('D', 'auto'): {'version': 1, 'posts': []}}
        self.sent, self.pools = [], []

        def read(base, env, which='main'):
            return json.loads(json.dumps(self.docs[(base, which)]))

        def write(base, env, which, posts, v, settings=None, reel_queue=None, reel_rejects=None, reel_music_pool=None):
            self.sent.append((base, which, reel_queue, reel_rejects))
            self.pools.append(reel_music_pool)
        P.read_doc, P.write_doc = read, write

    def tearDown(self):
        P.read_doc, P.write_doc = self.orig

    def test_both_fields_are_carried(self):
        self.assertEqual(P.copy_state('S', 'D', {}, 'copied', quiet=True), 1)
        self.assertEqual(self.sent, [('D', 'main', QB, RB)])


def pline(i, status='in', **k):
    return {'id': f'library/{i}', 'tid': i, 'title': i, 'status': status, **k}


MB = {'items': [pline('Time'), pline('Mountains')], 'removed': [], 'updated': 't0'}


class MergeMusicPool(unittest.TestCase):
    """The Reels tab's music pool: both lists merged line by line; a side without it never empties the other."""

    def test_an_add_here_and_the_macs_mark_there_combine(self):
        local = {'items': MB['items'] + [pline('Gom', 'waiting', added='t1')], 'removed': [], 'updated': 't1'}
        remote = {'items': [pline('Time', applied='s1'), pline('Mountains')], 'removed': [], 'updated': 't0',
                  'synced': 's1'}
        out = P.merge_music_pool(MB, local, remote)
        self.assertEqual([i['tid'] for i in out['items']], ['Time', 'Mountains', 'Gom'])
        self.assertEqual(out['items'][0]['applied'], 's1')
        self.assertEqual((out['updated'], out['synced']), ('t1', 's1'))

    def test_a_removal_here_moves_the_line(self):
        local = {'items': [pline('Time')], 'removed': [pline('Mountains', 'waiting', removed='t1')], 'updated': 't1'}
        out = P.merge_music_pool(MB, local, MB)
        self.assertEqual([i['tid'] for i in out['items']], ['Time'])
        self.assertEqual([i['tid'] for i in out['removed']], ['Mountains'])

    def test_a_side_without_a_pool_never_empties_the_other(self):
        self.assertEqual(P.merge_music_pool(MB, MB, None), MB)
        self.assertEqual(P.merge_music_pool(None, None, MB), MB)
        self.assertIsNone(P.merge_music_pool(None, None, None))

    def test_every_reel_field_has_its_merge(self):
        self.assertEqual(set(P.MERGE_FIELD), set(P.REEL_FIELDS))
        self.assertIn('reel_music_pool', P.REEL_FIELDS)


class MirrorCarriesTheMusicPool(MirrorCarriesTheQueue):
    def setUp(self):
        super().setUp()
        self.docs['L']['reel_music_pool'] = MB

    def test_the_pool_goes_up_and_stays_here_when_the_live_site_drops_it(self):
        self.live_keeps = False
        self.assertEqual(self.tick(), 'up')
        self.assertEqual(self.tick(), 'same')
        self.assertEqual(self.docs['L']['reel_music_pool'], MB)

    def test_a_live_change_comes_down(self):
        self.live_keeps = True
        self.tick()
        self.assertEqual(self.docs['R']['reel_music_pool'], MB)
        self.docs['R'] = dict(self.docs['R'], version=self.docs['R']['version'] + 1,
                              reel_music_pool={'items': MB['items'][:1], 'removed': [], 'updated': 't2'})
        self.assertEqual(self.tick(), 'down')
        self.assertEqual([i['tid'] for i in self.docs['L']['reel_music_pool']['items']], ['Time'])


class ServerUp(unittest.TestCase):
    def test_a_non_json_answer_is_down_not_a_crash(self):
        orig = P.api_call
        P.api_call = lambda *a, **k: (_ for _ in ()).throw(ValueError('Expecting value'))
        try:
            self.assertFalse(P.server_up('http://example.invalid', {}))
        finally:
            P.api_call = orig


def noisy_jpeg(path, size=(600, 400), quality=100, date='2024:09:09 10:21:38'):
    """A hard-to-compress JPEG with a capture date in its EXIF."""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(0)
    img = Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = date   # DateTimeOriginal
    img.save(path, 'JPEG', quality=quality, subsampling=0, exif=exif)
    return path


class FitUploadCap(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cap = P.UPLOAD_CAP

    def tearDown(self):
        P.UPLOAD_CAP = self.cap

    def test_under_the_cap_is_left_alone(self):
        f = noisy_jpeg(self.tmp / 'a.jpg')
        before = f.read_bytes()
        self.assertEqual(P.fit_upload_cap(f), (False, None))
        self.assertEqual(f.read_bytes(), before)

    def test_just_over_is_repacked_losslessly(self):
        from PIL import Image
        f = noisy_jpeg(self.tmp / 'a.jpg')
        pixels = Image.open(f).tobytes()
        P.UPLOAD_CAP = f.stat().st_size - 100
        done, note = P.fit_upload_cap(f)
        self.assertTrue(done, note)
        self.assertIn('losslessly', note)
        self.assertLessEqual(f.stat().st_size, P.UPLOAD_CAP)
        self.assertEqual(Image.open(f).tobytes(), pixels)

    def test_well_over_is_reencoded_at_full_size_with_metadata(self):
        from PIL import Image
        f = noisy_jpeg(self.tmp / 'a.jpg')
        P.UPLOAD_CAP = int(f.stat().st_size * 0.6)
        done, note = P.fit_upload_cap(f)
        self.assertTrue(done, note)
        self.assertIn('full size', note)
        self.assertLessEqual(f.stat().st_size, P.UPLOAD_CAP)
        im = Image.open(f)
        self.assertEqual(im.size, (600, 400))
        self.assertEqual(im.getexif().get_ifd(0x8769)[0x9003], '2024:09:09 10:21:38')

    def test_far_over_is_scaled_down_to_fit(self):
        from PIL import Image
        f = noisy_jpeg(self.tmp / 'a.jpg')
        P.UPLOAD_CAP = int(f.stat().st_size * 0.15)
        done, note = P.fit_upload_cap(f)
        self.assertTrue(done, note)
        self.assertIn('scaled', note)
        self.assertLessEqual(f.stat().st_size, P.UPLOAD_CAP)
        self.assertLess(Image.open(f).size[0], 600)

    def test_non_jpeg_over_the_cap_is_reported_not_touched(self):
        f = self.tmp / 'a.png'
        f.write_bytes(b'\x89PNG' + b'0' * 5000)
        P.UPLOAD_CAP = 1000
        done, note = P.fit_upload_cap(f)
        self.assertFalse(done)
        self.assertIn('not a JPEG', note)
        self.assertEqual(f.stat().st_size, 5004)

    def test_videos_are_ignored(self):
        f = self.tmp / 'a.mov'
        f.write_bytes(b'0' * 5000)
        P.UPLOAD_CAP = 1000
        self.assertEqual(P.fit_upload_cap(f), (False, None))


class PullShrinks(unittest.TestCase):
    """sync_post_dir end to end: the post copy fits, the source never changes,
    and the next pull neither recopies it nor takes it for a re-edit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cap = P.UPLOAD_CAP
        (self.tmp / 'Edits').mkdir()
        (self.tmp / 'post').mkdir()
        self.src = noisy_jpeg(self.tmp / 'Edits' / 'RM1.jpg')
        self.src_bytes = self.src.read_bytes()
        P.UPLOAD_CAP = int(self.src.stat().st_size * 0.6)
        self.ref = {'trip': 't', 'id': 'RM1'}
        self.dst = self.tmp / 'post' / '01_RM1.jpg'
        self.plan = [(1, self.ref, self.src, self.dst)]

    def tearDown(self):
        P.UPLOAD_CAP = self.cap

    def test_copy_fits_and_is_stable_across_pulls(self):
        found = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(P.sync_post_dir(self.tmp / 'post', self.plan, found=found)[0], 1)
            self.assertEqual(P.sync_post_dir(self.tmp / 'post', self.plan, found=found)[0], 0)
        self.assertLessEqual(self.dst.stat().st_size, P.UPLOAD_CAP)
        self.assertEqual(self.src.read_bytes(), self.src_bytes)
        self.assertEqual(found, [])
        state = json.loads((self.tmp / 'post' / '.pull_state.json').read_text())
        self.assertEqual(state['compressed']['RM1.jpg'], P._file_md5(self.dst))

    def test_oversize_reedit_is_staged_clean_before_shrinking(self):
        found = []
        with contextlib.redirect_stdout(io.StringIO()):
            P.sync_post_dir(self.tmp / 'post', self.plan, found=found)
            noisy_jpeg(self.dst, size=(620, 400))   # a re-export over the post copy
            reedit = self.dst.read_bytes()
            P.sync_post_dir(self.tmp / 'post', self.plan, found=found)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['new'].read_bytes(), reedit)   # Edits gets the clean one
        self.assertEqual(found[0]['new'].parent.name, P.STAGED)
        self.assertLessEqual(self.dst.stat().st_size, P.UPLOAD_CAP)


class PostDir(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ig = {'name': 'Trip', 'photos': []}
        self.xhs = {'name': 'Trip', 'photos': [], 'platform': 'xhs'}

    def test_prefix_by_platform(self):
        self.assertEqual(P.post_dir(self.root, self.ig, [self.ig]).name, 'IG Trip')
        self.assertEqual(P.post_dir(self.root, self.xhs, [self.xhs]).name, 'XHS Trip')

    def test_old_folder_goes_to_the_ig_draft_even_when_xhs_pulls_first(self):
        (self.root / 'Trip').mkdir()
        (self.root / 'Trip' / 'manifest.json').write_text('{}')
        both = [self.xhs, self.ig]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(P.post_dir(self.root, self.xhs, both).name, 'XHS Trip')
            self.assertTrue((self.root / 'Trip').exists())
            P.post_dir(self.root, self.ig, both)
        self.assertTrue((self.root / 'IG Trip' / 'manifest.json').exists())
        self.assertFalse((self.root / 'Trip').exists())

    def test_old_folder_goes_to_xhs_when_there_is_no_ig_draft(self):
        (self.root / 'Trip').mkdir()
        (self.root / 'Trip' / 'manifest.json').write_text('{}')
        with contextlib.redirect_stdout(io.StringIO()):
            P.post_dir(self.root, self.xhs, [self.xhs])
        self.assertTrue((self.root / 'XHS Trip' / 'manifest.json').exists())

    def test_dry_run_moves_nothing(self):
        (self.root / 'Trip').mkdir()
        (self.root / 'Trip' / 'manifest.json').write_text('{}')
        with contextlib.redirect_stdout(io.StringIO()):
            P.post_dir(self.root, self.ig, [self.ig], move=False)
        self.assertTrue((self.root / 'Trip').exists())

    def test_posted_prefix_follows_the_mark_both_ways(self):
        (self.root / 'IG Trip').mkdir()
        (self.root / 'IG Trip' / 'manifest.json').write_text('{}')
        self.ig['posted'] = True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(P.post_dir(self.root, self.ig, [self.ig]).name, 'Posted IG Trip')
            self.assertTrue((self.root / 'Posted IG Trip' / 'manifest.json').exists())
            self.ig['posted'] = False
            P.post_dir(self.root, self.ig, [self.ig])
        self.assertTrue((self.root / 'IG Trip' / 'manifest.json').exists())
        self.assertFalse((self.root / 'Posted IG Trip').exists())

    def test_old_plain_folder_goes_straight_to_posted(self):
        (self.root / 'Trip').mkdir()
        (self.root / 'Trip' / 'manifest.json').write_text('{}')
        self.ig['posted'] = True
        with contextlib.redirect_stdout(io.StringIO()):
            P.post_dir(self.root, self.ig, [self.ig])
        self.assertTrue((self.root / 'Posted IG Trip' / 'manifest.json').exists())

if __name__ == '__main__':
    unittest.main()
