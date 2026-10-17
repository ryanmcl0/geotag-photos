"""post.py merge3: the two-way sync of post drafts between localhost and the live site.

    venv/bin/python -m unittest tests/test_post_sync.py
"""
import importlib.util
import sys
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


if __name__ == '__main__':
    unittest.main()
