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
