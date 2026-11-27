#!/usr/bin/env python3
"""tools/side_strip.py and its place in ./post.py pull: a portrait photo kept whole on the post's shape, the
spare strip black with the place on it ("Preserve aspect ratio" on a draft).

    venv/bin/python -m unittest tests/test_side_strip.py
"""
import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import side_strip as S  # noqa: E402

_spec = importlib.util.spec_from_file_location('post', ROOT / 'post.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


def photo(path, size=(400, 600), seed=0):
    """A noisy JPEG (so any change to its pixels shows) with a capture date."""
    rng = np.random.default_rng(seed)
    img = Image.fromarray(rng.integers(60, 256, (size[1], size[0], 3), dtype=np.uint8))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = '2024:03:11 23:57:33'
    img.save(path, 'JPEG', quality=97, subsampling=0, exif=exif)
    return path


def is_bar(img, box):
    """Whether the box is the strip's own near-black."""
    a = np.asarray(img.crop(box).convert('RGB'), dtype=int)
    return bool((np.abs(a - np.array(S.BG)).max() <= 6))


class WhichPhotos(unittest.TestCase):
    def test_a_2_3_photo_on_4_5_gets_a_sixth(self):
        self.assertAlmostEqual(S.strip_share(4000, 6000, 0.8), 1 / 6, places=3)
        self.assertTrue(S.wanted(4000, 6000, 0.8))

    def test_too_thin_or_no_strip_at_all(self):
        self.assertFalse(S.wanted(3024, 4032, 0.8))      # 3:4 on 4:5: a sliver, no room for text
        self.assertFalse(S.wanted(4000, 5000, 0.8))      # already the post's shape
        self.assertFalse(S.wanted(6000, 4000, 0.8))      # wider than the post
        self.assertFalse(S.wanted(4000, 6000, None))


class Text(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(S.level_text('Guiyang', 'Guizhou, China'), ('Guiyang', 'Guizhou, China'))
        self.assertEqual(S.level_text('Guiyang', 'Guizhou, China', 'region'), ('Guizhou', 'China'))
        self.assertEqual(S.level_text('Guiyang', 'Guizhou, China', 'country'), ('China', ''))

    def test_a_place_with_no_region_over_it_keeps_its_name(self):
        self.assertEqual(S.level_text('Chongqing', 'China', 'region'), ('Chongqing', 'China'))
        self.assertEqual(S.level_text('Chongqing', 'China', 'country'), ('China', ''))

    def test_no_position(self):
        self.assertEqual(S.level_text(None, None, 'region'), ('', ''))

    def test_typed_text(self):
        auto = ('Guiyang', 'Guizhou, China')
        self.assertEqual(S.split_text('', *auto), auto)
        self.assertEqual(S.split_text('Southwest China', *auto), ('Southwest China', 'Guizhou, China'))
        self.assertEqual(S.split_text('Guizhou / March 2024', *auto), ('Guizhou', 'March 2024'))
        self.assertEqual(S.split_text('Somewhere /', *auto), ('Somewhere', ''))
        self.assertIsNone(S.split_text(' OFF ', *auto))


class Compose(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.img = Image.fromarray(rng.integers(60, 256, (1350, 900, 3), dtype=np.uint8))

    def test_the_photo_is_whole_and_the_strip_is_on_the_right(self):
        out = S.compose(self.img, 0.8, 'right', 'Guiyang', 'Guizhou, China', 'cn')
        self.assertEqual(out.size, (1080, 1350))
        self.assertTrue((np.asarray(out.crop((0, 0, 900, 1350))) == np.asarray(self.img)).all())
        self.assertTrue(is_bar(out, (900, 0, 1080, 40)))            # above the flag
        self.assertTrue(is_bar(out, (900, 1300, 1080, 1350)))       # under the text
        self.assertFalse(is_bar(out, (900, 600, 1080, 1260)))       # the text is in there

    def test_left_strip(self):
        out = S.compose(self.img, 0.8, 'left', 'Chongqing', 'China', 'cn')
        self.assertTrue((np.asarray(out.crop((180, 0, 1080, 1350))) == np.asarray(self.img)).all())
        self.assertTrue(is_bar(out, (0, 0, 180, 40)))

    def test_no_text_no_flag_is_a_plain_strip(self):
        out = S.compose(self.img, 0.8, 'right')
        self.assertTrue(is_bar(out, (900, 0, 1080, 1350)))

    def test_a_long_name_stays_inside_the_strip(self):
        out = S.compose(self.img, 0.8, 'right', 'Lenghu Oil Base Ruins Of The Far West', 'x' * 80, 'cn')
        self.assertTrue(is_bar(out, (900, 1350 - 60, 1080, 1350)))  # the foot stays clear
        self.assertTrue(is_bar(out, (900, 115, 1080, 160)))         # and the gap under the flag

    def test_a_photo_at_the_posts_shape_gets_nothing(self):
        self.assertIsNone(S.compose(self.img.resize((1080, 1350)), 0.8))

    def test_a_flag_that_is_not_bundled_is_left_out_offline(self):
        self.assertIsNone(S.flag_image('zz', 60, fetch=False))
        self.assertIsNone(S.flag_image('../x', 60))
        self.assertEqual(S.flag_image('cn', 60).size[0], 60)


class Stamp(unittest.TestCase):
    def test_in_place_keeps_the_capture_date_and_does_not_strip_twice(self):
        tmp = Path(tempfile.mkdtemp())
        f = photo(tmp / 'a.jpg')
        self.assertTrue(S.stamp(f, f, 0.8, 'right', 'Guiyang', 'Guizhou, China', 'cn'))
        with Image.open(f) as im:
            self.assertEqual(im.size, (480, 600))
            self.assertEqual(im.getexif().get_ifd(0x8769)[0x9003], '2024:03:11 23:57:33')
        before = f.read_bytes()
        self.assertFalse(S.stamp(f, f, 0.8, 'left', 'Guiyang', '', 'cn'))
        self.assertEqual(f.read_bytes(), before)


class PullWithStrips(unittest.TestCase):
    """strip_sigs + sync_post_dir end to end, on a three-photo portrait draft."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'Edits').mkdir()
        self.post_dir = self.tmp / 'post'
        self.post_dir.mkdir()
        self.srcs = [photo(self.tmp / 'Edits' / f'RM{n}.jpg', seed=n) for n in (1, 2, 3)]
        self.src_bytes = [s.read_bytes() for s in self.srcs]
        self.refs = [{'trip': 't', 'id': f'RM{n}', 'ar': 0.667} for n in (1, 2, 3)]
        self.plan = [(i, r, s, self.post_dir / f'{i:02d}_{s.name}')
                     for i, (r, s) in enumerate(zip(self.refs, self.srcs), 1)]
        self.post = {'id': 'p', 'name': 'China', 'photos': self.refs, 'whole': True}
        self._auto = S.auto_text
        S.auto_text = lambda trip, pid: ('Guiyang', 'Guizhou, China', 'CN')

    def tearDown(self):
        S.auto_text = self._auto

    def pull(self, found=None):
        strips, notes = P.strip_sigs(self.post, self.plan, 0.8)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = P.sync_post_dir(self.post_dir, self.plan, found=found, shape=0.8, strips=strips)[0]
        return n, strips, out.getvalue()

    def size(self, i):
        with Image.open(self.plan[i][3]) as im:
            return im.size

    def test_not_ticked_means_no_strips(self):
        self.post.pop('whole')
        self.assertEqual(P.strip_sigs(self.post, self.plan, 0.8), ({}, []))

    def test_sides_alternate_from_the_right_and_the_text_follows_the_level(self):
        strips, _ = P.strip_sigs(self.post, self.plan, 0.8)
        self.assertEqual([strips[s.name].split('|')[3] for s in self.srcs], ['right', 'left', 'right'])
        self.assertEqual(strips['RM1.jpg'], f'strip|Guiyang|Guizhou, China|right|CN|v{S.LOOK}')
        self.post['stripLevel'] = 'region'
        strips, _ = P.strip_sigs(self.post, self.plan, 0.8)
        self.assertEqual(strips['RM1.jpg'].split('|')[1:3], ['Guizhou', 'China'])

    def test_typed_text_off_and_a_framed_photo(self):
        self.refs[0]['stripText'] = 'Southwest | China /'
        self.refs[1]['crop'] = [0, 0.1, 1, 0.9]            # framed with ✂: cut to the shape, no strip
        self.refs[2]['stripText'] = 'off'
        strips, _ = P.strip_sigs(self.post, self.plan, 0.8)
        self.assertEqual(strips['RM1.jpg'].split('|')[1:5], ['Southwest / China', '', 'right', 'CN'])
        self.assertNotIn('RM2.jpg', strips)
        self.assertEqual(strips['RM3.jpg'].split('|')[1:5], ['', '', 'left', ''])   # the next strip, no flag

    def test_a_nearly_4_5_photo_is_left_alone_with_a_note(self):
        photo(self.srcs[1], size=(450, 600), seed=9)       # 3:4
        strips, notes = P.strip_sigs(self.post, self.plan, 0.8)
        self.assertNotIn('RM2.jpg', strips)
        self.assertEqual(len(notes), 1)
        self.assertEqual(strips['RM3.jpg'].split('|')[3], 'left')

    def test_copies_get_the_strip_sources_never_change_and_the_next_pull_is_quiet(self):
        n, strips, _ = self.pull()
        self.assertEqual(n, 3)
        self.assertEqual([self.size(i) for i in range(3)], [(480, 600)] * 3)
        with Image.open(self.plan[1][3]) as im:
            self.assertTrue(is_bar(im, (0, 0, 80, 20)))        # second slide: strip on the left
        self.assertEqual([s.read_bytes() for s in self.srcs], self.src_bytes)
        found = []
        self.assertEqual(self.pull(found)[0], 0)
        self.assertEqual(found, [])
        state = json.loads((self.post_dir / '.pull_state.json').read_text())
        self.assertEqual(state['located']['RM1.jpg'], strips['RM1.jpg'])

    def test_new_words_remake_that_copy_only(self):
        self.pull()
        self.refs[2]['stripText'] = 'Guizhou'
        n, _, out = self.pull()
        self.assertEqual(n, 1)
        self.assertIn('03_RM3.jpg: whole, strip on the right: Guizhou / Guizhou, China', out)

    def test_unticking_brings_the_plain_photo_back(self):
        self.pull()
        self.post['whole'] = False
        self.assertEqual(self.pull()[0], 3)
        self.assertEqual(self.size(0), (400, 600))

    def test_a_reexport_over_a_copy_gets_its_strip_again_and_edits_gets_the_clean_one(self):
        self.pull()
        photo(self.plan[0][3], seed=42)                    # Lightroom exports the photo itself, 2:3
        clean = self.plan[0][3].read_bytes()
        found = []
        self.pull(found)
        self.assertEqual(self.size(0), (480, 600))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['new'].parent.name, P.STAGED)
        self.assertEqual(found[0]['new'].read_bytes(), clean)

    def test_a_copy_changed_by_hand_with_its_strip_on_never_goes_to_edits(self):
        self.pull()
        with Image.open(self.plan[0][3]) as im:
            im.convert('L').convert('RGB').save(self.plan[0][3], 'JPEG', quality=90)
        found = []
        _, _, out = self.pull(found)
        self.assertEqual(found, [])
        self.assertIn('not offered for Edits', out)
        self.assertEqual(self.size(0), (480, 600))

    def test_backfill_does_not_take_a_strip_copy_for_a_reedit(self):
        self.pull()
        manifest = {'name': 'China', 'photos': [
            {'order': i, 'trip': r['trip'], 'id': r['id'], 'source': str(s), 'copied_to': str(d)}
            for i, r, s, d in self.plan]}
        (self.post_dir / 'manifest.json').write_text(json.dumps(manifest))
        import os
        import time
        for _, _, _, d in self.plan:                       # newer than the source, as after any pull
            os.utime(d, (time.time() + 60, time.time() + 60))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            P.cmd_backfill(argparse.Namespace(dest=str(self.tmp), dry_run=True))
        self.assertIn('No re-edits to write back.', out.getvalue())


if __name__ == '__main__':
    unittest.main()
