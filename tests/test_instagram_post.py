"""./post.py ig: one Xiaohongshu-tab draft → Instagram (@ryanmcl0), with fakes for every outside call.

    venv/bin/python -m unittest tests/test_instagram_post.py

Nothing here reaches Instagram, R2, the live site or the RYAN drive: the two /api/posts documents (localhost
and live) are an in-memory store behind post.api_call, the pull writes a temp folder, the Graph API and R2
are fakes, and the config folder is a temp one.
"""
import contextlib
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from PIL import Image, ImageCms

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
_spec = importlib.util.spec_from_file_location('post', ROOT / 'post.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
import instagram_post as IG                            # noqa: E402  (the module post.py imports)

LIVE = 'https://live.example'
ENV = {'CF_POSTS_PASSWORD': 'x', 'CF_SITE_URL': LIVE}
LINK = 'https://www.instagram.com/p/abc/'


class Store:
    """Both sides' posts documents ('main' and 'auto'), answering like functions/api/posts.ts: GET the doc,
    PUT against baseVersion or 409 with the current doc."""

    def __init__(self, posts, local_up=True):
        self.docs = {(b, w): {'version': 1, 'posts': copy.deepcopy(posts) if w == 'main' else []}
                     for b in (P.LOCAL_BASE, LIVE) for w in P.SETS}
        self.up = {P.LOCAL_BASE: local_up, LIVE: True}
        self.puts = []

    def doc(self, base, which='main'):
        return self.docs[(base, which)]

    def post(self, base, pid):
        return next(p for p in self.doc(base)['posts'] if p['id'] == pid)

    def __call__(self, url, env, method='GET', body=None, timeout=30):
        base, _, rest = url.partition('/api/posts')
        if not self.up.get(base):
            raise urllib.error.URLError('connection refused')
        d = self.doc(base, 'auto' if 'set=auto' in rest else 'main')
        if method == 'GET':
            return copy.deepcopy(d)
        if body['baseVersion'] != d['version']:
            raise urllib.error.HTTPError(url, 409, 'Conflict', {}, io.BytesIO(json.dumps(d).encode()))
        d['version'] += 1
        d['posts'] = copy.deepcopy(body['posts'])
        self.puts.append(base)
        return {'version': d['version']}


class FakeGraph:
    """Graph API answers; every call recorded without the token. fail='CAROUSEL' fails that container."""

    def __init__(self, fail=None, quota=(2, 100), on_publish=None):
        self.calls, self.n, self.fail, self.quota, self.on_publish = [], 0, fail, quota, on_publish

    def __call__(self, method, url, params):
        assert params.get('access_token') == 'TOKEN'
        path = url.split('/v23.0/', 1)[1]
        self.calls.append((method, path, {k: v for k, v in params.items() if k != 'access_token'}))
        last = path.rsplit('/', 1)[-1]
        if last == 'content_publishing_limit':
            return {'data': [{'quota_usage': self.quota[0], 'config': {'quota_total': self.quota[1]}}]}
        if last == 'media':
            if self.fail and params.get('media_type') == self.fail:
                return {'error': {'message': 'Media upload has failed'}}
            self.n += 1
            return {'id': f'C{self.n}'}
        if last == 'media_publish':
            if self.on_publish:
                self.on_publish()
            return {'id': 'M1'}
        if last.startswith('C'):
            return {'status_code': 'FINISHED'}
        if last == 'M1':
            return {'permalink': LINK}
        return {'error': {'message': 'unexpected'}}

    def media(self):
        return [c[2] for c in self.calls if c[1].endswith('/media')]


class FakeR2:
    def __init__(self):
        self.objects, self.puts, self.deletes = {}, [], []

    def __call__(self, method, url, body_path=None, content_type=None):
        key = urllib.parse.unquote(urllib.parse.urlsplit(url).path).split('/', 2)[2]
        if method == 'PUT':
            self.objects[key] = Path(body_path).read_bytes()
            self.puts.append((key, content_type, Path(body_path).name))
            return 200
        if method == 'DELETE':
            self.deletes.append(key)
            self.objects.pop(key, None)
            return 204
        return 400


def draft(pid='p1', num=1, photos=('a', 'b', 'c'), **k):
    return {'id': pid, 'num': num, 'name': f'Draft {num}', 'caption': 'Rooftops at dusk #urbex', 'platform': 'xhs',
            'photos': [{'trip': 't', 'id': i} for i in photos], **k}


class IgCase(unittest.TestCase):
    """cmd_ig end to end against the fakes."""

    shapes = {}                       # photo id → (w, h); default 4:5

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dest = self.tmp / 'Posts'
        self.cfg = self.tmp / 'content-engine'
        self.write_setup(True)
        self.graph, self.r2 = FakeGraph(), FakeR2()
        self.pulls = []
        self.store = Store([draft()])
        self.patches = [
            mock.patch.object(P, 'api_call', lambda *a, **k: self.store(*a, **k)),
            mock.patch.object(P, 'require_env', lambda: dict(ENV)),
            mock.patch.object(P, 'cmd_pull', self.fake_pull),
            mock.patch.object(P, '_ask_mirror', lambda: None),
            mock.patch.object(P, 'SYNC_BASE', self.tmp / 'posts_sync_base.json'),
            mock.patch.object(P, 'SYNC_BACKUPS', self.tmp / 'posts_backups'),
            mock.patch.object(P, 'IG_LOG', self.tmp / 'instagram_posts.jsonl'),
            mock.patch.object(IG, 'CONFIG_DIR', self.cfg),
            mock.patch.object(IG, 'urllib_http', lambda *a: self.graph(*a)),
            mock.patch.object(IG, 'urllib_request', lambda *a: self.r2(*a)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_setup(self, enabled, secrets=IG.SECRETS):
        (self.cfg / 'secrets').mkdir(parents=True, exist_ok=True)
        (self.cfg / 'config.json').write_text(json.dumps({
            'publish_enabled': enabled,
            'instagram': {'api': 'facebook', 'version': 'v23.0', 'user_id': '17840000000000000'},
            'r2': {'account_id': 'acct', 'bucket': 'uploads', 'prefix': 'posts/', 'url_seconds': 3600}}))
        for name in IG.SECRETS:
            f = self.cfg / 'secrets' / name
            if name in secrets:
                f.write_text('TOKEN\n' if name == 'instagram_token' else 'KEY\n')
            else:
                f.unlink(missing_ok=True)

    def fake_pull(self, args):
        """What ./post.py pull leaves in a post folder: NN_<file> copies, maps/ cards, manifest.json."""
        self.pulls.append(args.id)
        base = args.url.partition('/api/posts')[0]
        doc = self.store.doc(base)
        post = next(p for p in doc['posts'] if p['id'] == args.id)
        folder = P.post_dir(Path(args.dest), post, doc['posts'], move=False, quiet=True)
        (folder / 'maps').mkdir(parents=True, exist_ok=True)
        photos, maps = [], []
        for i, ref in enumerate(post['photos'], 1):
            f = folder / f"{i:02d}_{ref['id']}.jpg"
            Image.new('RGB', self.shapes.get(ref['id'], (400, 500)), (20 * i, 90, 140)).save(f, quality=90)
            photos.append({'order': i, 'trip': ref['trip'], 'id': ref['id'], 'source': f'/src/{ref["id"]}.jpg',
                           'copied_to': str(f)})
            if ref.get('map'):
                m = folder / 'maps' / f"{i:02d}_{ref['id']}_map.jpg"
                Image.new('RGB', (400, 500), (0, 0, 0)).save(m)
                maps.append({'order': i, 'style': ref['map'], 'file': str(m)})
        manifest = {'name': post['name'], 'version': doc['version'], 'photos': photos}
        if maps:
            manifest['maps'] = maps
        (folder / 'manifest.json').write_text(json.dumps(manifest))

    def run_ig(self, which='1', **flags):
        args = mock.Mock(post=which, dest=str(self.dest), yes=flags.get('yes', True), dry=flags.get('dry', False))
        out = io.StringIO()
        code = None
        with contextlib.redirect_stdout(out):
            try:
                P.cmd_ig(args)
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code

    def nothing_went_out(self):
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.r2.puts, [])
        for base in (P.LOCAL_BASE, LIVE):
            self.assertNotIn('posted', self.store.post(base, 'p1'))
        self.assertFalse(P.IG_LOG.exists())


class Posting(IgCase):
    def test_carousel_goes_out_in_the_drafts_order_and_is_marked_posted_everywhere(self):
        self.store = Store([draft(photos=('c', 'a', 'b'))])
        P.SYNC_BASE.write_text(json.dumps({w: {'posts': self.store.doc(LIVE, w)['posts'], 'settings': {}}
                                           for w in P.SETS}))        # the state both sides last agreed on
        self.store.post(P.LOCAL_BASE, 'p1')['photos'][1]['map'] = 'pin'   # an edit only the page has so far
        out, code = self.run_ig()
        self.assertIsNone(code, out)
        self.assertEqual(self.pulls, ['p1'])
        # uploaded in the draft's order (the location card right after its photo), as JPEG
        self.assertEqual([name for _, _, name in self.r2.puts],
                         ['01_c.jpg', '02_a.jpg', '02_a_map.jpg', '03_b.jpg'])
        self.assertEqual({ct for _, ct, _ in self.r2.puts}, {'image/jpeg'})
        media = self.graph.media()
        keys = [k for k, _, _ in self.r2.puts]
        for call, key in zip(media[:4], keys):
            self.assertEqual(call['is_carousel_item'], 'true')
            self.assertIn(f'/uploads/{key}?', call['image_url'])
        self.assertEqual(media[4], {'media_type': 'CAROUSEL', 'children': 'C1,C2,C3,C4',
                                    'caption': 'Rooftops at dusk #urbex'})
        self.assertEqual(self.graph.calls[0][1], '17840000000000000/content_publishing_limit')
        self.assertIn(('POST', '17840000000000000/media_publish', {'creation_id': 'C5'}), self.graph.calls)
        # R2 emptied again
        self.assertEqual(sorted(self.r2.deletes), sorted(keys))
        self.assertEqual(self.r2.objects, {})
        # the Posts page's own posted flag + the permalink, on both sides (through merge3, the local map edit kept)
        for base in (P.LOCAL_BASE, LIVE):
            p = self.store.post(base, 'p1')
            self.assertIs(p['posted'], True)
            self.assertEqual(p['permalink'], LINK)
            self.assertEqual(p['photos'][1].get('map'), 'pin')
        self.assertTrue(P.SYNC_BASE.exists())
        log = [json.loads(x) for x in P.IG_LOG.read_text().splitlines()]
        self.assertEqual((log[0]['post_id'], log[0]['permalink'], log[0]['media_id']), ('p1', LINK, 'M1'))
        self.assertIn(LINK, out)
        self.assertNotIn('TOKEN', out)
        self.assertNotIn('KEY', out)

    def test_a_phone_edit_landing_meanwhile_survives_the_mark(self):
        self.store = Store([draft(), draft('p2', 2)])

        def phone_edit():                       # the phone edits another draft on the live site mid-post
            live = self.store.doc(LIVE)
            live['posts'][1]['caption'] = 'from the phone'
            live['version'] += 1
        self.graph.on_publish = phone_edit
        out, code = self.run_ig()
        self.assertIsNone(code, out)
        for base in (P.LOCAL_BASE, LIVE):
            self.assertIs(self.store.post(base, 'p1')['posted'], True)
            self.assertEqual(self.store.post(base, 'p2')['caption'], 'from the phone')

    def test_without_the_dev_server_it_pulls_from_and_marks_the_live_site(self):
        self.store = Store([draft()], local_up=False)
        out, code = self.run_ig()
        self.assertIsNone(code, out)
        self.assertEqual(self.store.post(LIVE, 'p1')['permalink'], LINK)
        self.assertIn('Marked posted on the live site', out)

    def test_by_name_too(self):
        out, code = self.run_ig('draft 1')
        self.assertIsNone(code, out)
        self.assertIs(self.store.post(LIVE, 'p1')['posted'], True)


class Refusals(IgCase):
    def test_publish_enabled_false_posts_nothing(self):
        self.write_setup(False)
        out, code = self.run_ig()
        self.assertIsNone(code)
        self.assertIn('Posting is off (publish_enabled)', out)
        self.assertIn('Rooftops at dusk #urbex', out)        # the confirmation still shows everything
        self.assertEqual(self.pulls, ['p1'])
        self.nothing_went_out()

    def test_not_set_up_is_a_message_not_a_crash(self):
        shutil.rmtree(self.cfg)
        out, code = self.run_ig()
        self.assertIn('not set up:', out)
        self.assertIn('config.json', out)
        self.assertIn('Posting is off (publish_enabled)', out)
        self.nothing_went_out()
        self.write_setup(True, secrets=('instagram_token',))
        out, code = self.run_ig()
        self.assertIn('secrets/r2_access_key_id', out)
        self.assertIn('Not posted', code)
        self.nothing_went_out()

    def test_shape_outside_4_5_to_1_91_blocks_and_nothing_is_cropped(self):
        self.shapes = {'b': (400, 600)}                      # 2:3
        out, code = self.run_ig()
        self.assertIn('Not posted', code)
        self.assertIn('2:3', out)
        self.assertIn('outside the 4:5 to 1.91:1', out)
        self.nothing_went_out()
        with Image.open(next(self.dest.glob('XHS */02_b.jpg'))) as im:
            self.assertEqual(im.size, (400, 600))                # the pulled copy untouched

    def test_the_question_defaults_to_no(self):
        with mock.patch('sys.stdin') as stdin, mock.patch('builtins.input', return_value=''):
            stdin.isatty.return_value = True
            out, code = self.run_ig(yes=False)
        self.assertIn('Not posted.', out)
        self.nothing_went_out()

    def test_dry_stops_after_showing(self):
        out, code = self.run_ig(dry=True)
        self.assertIn('Dry run: nothing posted.', out)
        self.assertIn('01_a.jpg', out)
        self.nothing_went_out()

    def test_the_plan_shows_a_trial_reel_and_blocks_it_on_a_carousel(self):
        self.store = Store([draft(trial='auto'), draft('p2', 2, trial='manual')])
        out, _ = self.run_ig('1', dry=True)
        self.assertIn('Trial reel: shown to non-followers first; shared to followers automatically if it performs '
                      'well', out)
        self.assertIn('goes out as a carousel: trial applies only to a single-video Reel', out)
        self.assertIn('1 problem(s)', out)
        out, _ = self.run_ig('2', dry=True)
        self.assertIn('Trial reel: shown to non-followers first; you share it to followers yourself in the app', out)
        out, code = self.run_ig('1')
        self.assertIn('Not posted', code)
        self.nothing_went_out()

    def test_a_failed_post_deletes_the_uploads_and_marks_nothing(self):
        self.graph = FakeGraph(fail='CAROUSEL')
        out, code = self.run_ig()
        self.assertIn('Not posted', code)
        self.assertIn('Media upload has failed', code)
        self.assertEqual(len(self.r2.puts), 3)
        self.assertEqual(sorted(self.r2.deletes), sorted(k for k, _, _ in self.r2.puts))
        self.assertEqual(self.r2.objects, {})
        for base in (P.LOCAL_BASE, LIVE):
            self.assertNotIn('posted', self.store.post(base, 'p1'))
        self.assertFalse(P.IG_LOG.exists())

    def test_a_used_up_limit_uploads_nothing(self):
        self.graph = FakeGraph(quota=(100, 100))
        out, code = self.run_ig()
        self.assertIn('limit is used up', code)
        self.assertEqual(self.r2.puts, [])
        self.assertNotIn('posted', self.store.post(LIVE, 'p1'))

    def test_a_draft_changed_after_the_pull_is_not_posted(self):
        real = IG.prepare

        def prepare_then_edit(*a, **k):
            plan = real(*a, **k)
            self.store.post(P.LOCAL_BASE, 'p1')['caption'] = 'edited meanwhile'
            return plan
        with mock.patch.object(IG, 'prepare', prepare_then_edit):
            out, code = self.run_ig()
        self.assertIn('the draft changed since the pull', code)
        self.assertEqual(self.graph.calls, [])

    def test_already_posted_and_other_accounts_and_the_instagram_tab(self):
        # the Xiaohongshu tab is @ryanmcl0 + Xiaohongshu; the Instagram tab is the urbex account, posted by hand
        self.store = Store([draft(posted=True, permalink=LINK), draft('p2', 2, account='urbex'),
                            draft('p3', 3, platform=None), draft('p4', 4, platform='ig', name='Urbex roofs')])
        self.assertIn('already marked posted', self.run_ig('1')[1])
        out, code = self.run_ig('2')
        self.assertIn('only @ryanmcl0', out)
        self.assertIn('Not posted', code)
        self.assertIn('urbex account, posted by hand', self.run_ig('3')[1])
        self.assertIn('urbex account, posted by hand', self.run_ig('urbex roofs')[1])
        self.assertEqual(self.graph.calls, [])


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, n, size=(400, 500), ext='.jpg'):
        photos = []
        for i in range(1, n + 1):
            f = self.tmp / f'{i:02d}_x{i}{ext}'
            Image.new('RGB', size, (i, i, i)).save(f)
            photos.append({'order': i, 'trip': 't', 'id': f'x{i}', 'copied_to': str(f)})
        return {'photos': photos}, {'id': 'p', 'name': 'n', 'platform': 'xhs', 'photos': [{'trip': 't', 'id': f'x{i}'}
                                                                        for i in range(1, n + 1)]}

    def test_more_than_twenty_items_blocks(self):
        manifest, post = self.folder(21)
        plan = IG.prepare(post, manifest, self.tmp, self.tmp / 'stage')
        self.assertTrue(any('at most 20' in p for p in IG.blocking(plan)))
        manifest, post = self.folder(20)
        self.assertEqual(IG.blocking(IG.prepare(post, manifest, self.tmp, self.tmp / 'stage')), [])

    def test_images_go_out_as_srgb_jpeg_copies_and_the_original_is_untouched(self):
        src = self.tmp / 'big.png'
        Image.new('RGB', (3000, 3750), (200, 30, 30)).save(src)
        before = hashlib.md5(src.read_bytes()).hexdigest()
        (w, h), size, note = IG.stage_image(src, self.tmp / 'out.jpg')
        self.assertEqual((w, h), (3000, 3750))
        self.assertEqual(hashlib.md5(src.read_bytes()).hexdigest(), before)
        with Image.open(self.tmp / 'out.jpg') as im:
            self.assertEqual((im.format, im.mode, im.size), ('JPEG', 'RGB', (1440, 1800)))
            icc = ImageCms.ImageCmsProfile(io.BytesIO(im.info['icc_profile']))
            self.assertIn('sRGB', ImageCms.getProfileDescription(icc))
        self.assertLessEqual(size, IG.JPEG_MAX)

    def test_shape_edges(self):
        for size, ok in (((1080, 1350), True), ((1080, 566), True), ((1080, 1440), False), ((1920, 1000), False)):
            manifest, post = self.folder(1, size)
            plan = IG.prepare(post, manifest, self.tmp, self.tmp / f'stage{size}')
            self.assertEqual(not IG.blocking(plan), ok, size)

    def test_needs_blur_copy_blocks(self):
        manifest, post = self.folder(1)
        f = Path(manifest['photos'][0]['copied_to'])
        g = f.with_name(f.stem + '_NEEDS-BLUR.jpg')
        f.rename(g)
        manifest['photos'][0]['copied_to'] = str(g)
        self.assertTrue(any('blur it by hand' in p for p in IG.blocking(IG.prepare(post, manifest, self.tmp,
                                                                                   self.tmp / 's'))))

    def test_caption_limits(self):
        self.assertEqual(IG.caption_problems('ok #a #b @c'), [])
        self.assertTrue(IG.caption_problems(' '.join(f'#t{i}' for i in range(31))))
        self.assertTrue(IG.caption_problems('x' * 2201))

    def test_more_than_five_hashtags_is_a_note_not_a_block(self):
        manifest, post = self.folder(1)
        post['caption'] = 'Issyk-Kul ' + ' '.join(f'#t{i}' for i in range(6))
        plan = IG.prepare(post, manifest, self.tmp, self.tmp / 's')
        self.assertEqual(IG.blocking(plan), [])
        self.assertIn('6 hashtags: Instagram has capped posts at 5 since Dec 2025; extra ones may be dropped or '
                      'refused', plan['notes'])
        post['caption'] = '#a #b #c #d #e'
        self.assertFalse(any('hashtags' in n for n in IG.prepare(post, manifest, self.tmp, self.tmp / 's2')['notes']))
        post['caption'] = ' '.join(f'#t{i}' for i in range(31))          # past 30 still blocks, without the note
        plan = IG.prepare(post, manifest, self.tmp, self.tmp / 's3')
        self.assertTrue(any('at most 30' in p for p in IG.blocking(plan)))
        self.assertFalse(any('hashtags' in n for n in plan['notes']))

    def video_draft(self, **k):
        f = self.tmp / '01_clip.mp4'
        f.write_bytes(b'\0' * 10)
        manifest = {'photos': [], 'phone': [{'order': 1, 'trip': 'phone-x', 'file': 'clip.mp4', 'copied_to': str(f)}]}
        post = {'id': 'p', 'name': 'n', 'platform': 'xhs', 'photos': [],
                'phone': [{'trip': 'phone-x', 'file': 'clip.mp4'}], **k}
        probe = lambda p: {'format': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': 20.0, 'width': 1080, 'height': 1920}  # noqa
        return IG.prepare(post, manifest, self.tmp, self.tmp / 's', probe=probe)

    def test_trial_reel_only_for_a_single_video(self):
        plan = self.video_draft(trial='manual')
        self.assertEqual((plan['type'], plan['trial'], IG.blocking(plan)), ('REELS', 'manual', []))
        self.assertIsNone(self.video_draft()['trial'])
        self.assertTrue(any('only be auto or manual' in p for p in IG.blocking(self.video_draft(trial='yes'))))
        manifest, post = self.folder(1)
        post['trial'] = 'auto'
        problems = IG.blocking(IG.prepare(post, manifest, self.tmp, self.tmp / 's1'))
        self.assertEqual(problems, ['trial reel is on, but this draft goes out as a photo: trial applies only to a '
                                    'single-video Reel; switch it off or make the draft one video'])

    def test_videos_are_checked_not_converted(self):
        f = self.tmp / '01_clip.mov'
        f.write_bytes(b'\0' * 10)
        manifest = {'photos': [], 'phone': [{'order': 1, 'trip': 'phone-x', 'file': 'clip.mov',
                                             'copied_to': str(f)}]}
        post = {'id': 'p', 'name': 'n', 'platform': 'xhs', 'photos': [], 'phone': [{'trip': 'phone-x', 'file': 'clip.mov'}]}
        probe = lambda p: {'format': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': 2.0, 'width': 1080, 'height': 1920}  # noqa
        plan = IG.prepare(post, manifest, self.tmp, self.tmp / 's', probe=probe)
        self.assertEqual(plan['type'], 'REELS')
        problems = ' '.join(IG.blocking(plan))
        self.assertIn('not an MP4', problems)
        self.assertIn('2.0 s', problems)
        self.assertEqual(plan['items'][0]['staged'], f)

    def test_the_song_is_noted_not_attached(self):
        manifest, post = self.folder(1)
        post['song'] = 'Some Song'
        notes = IG.prepare(post, manifest, self.tmp, self.tmp / 's')['notes']
        self.assertTrue(any('Some Song' in n and 'cannot add music' in n for n in notes))


class SingleItems(unittest.TestCase):
    def publisher(self, graph):
        return IG.Publisher('TOKEN', '17840000000000000', http=graph, sleep=lambda s: None)

    def test_one_photo_is_one_image_container(self):
        graph, r2 = FakeGraph(), FakeR2()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'a.jpg'
            f.write_bytes(b'jpeg')
            res = IG.publish_items([{'staged': f, 'kind': 'image'}], 'cap', self.publisher(graph),
                                   IG.R2('acct', 'uploads', 'AK', 'SK', http=r2))
        media = graph.media()
        self.assertEqual(len(media), 1)
        self.assertEqual(set(media[0]), {'image_url', 'caption'})
        self.assertEqual((res['media_id'], res['permalink'], res['left']), ('M1', LINK, 97))
        self.assertEqual(r2.objects, {})

    def test_one_video_is_a_reel_shared_to_the_feed(self):
        graph, r2 = FakeGraph(), FakeR2()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'clip.mp4'
            f.write_bytes(b'mp4')
            IG.publish_items([{'staged': f, 'kind': 'video'}], 'cap', self.publisher(graph),
                             IG.R2('acct', 'uploads', 'AK', 'SK', http=r2))
        m = graph.media()[0]
        self.assertEqual((m['media_type'], m['share_to_feed']), ('REELS', 'true'))
        self.assertEqual(r2.puts[0][1], 'video/mp4')
        self.assertEqual(r2.objects, {})

    def test_a_trial_reel_sends_trial_params_and_no_share_to_feed(self):
        for trial, strategy in (('auto', 'SS_PERFORMANCE'), ('manual', 'MANUAL')):
            graph, r2 = FakeGraph(), FakeR2()
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / 'clip.mp4'
                f.write_bytes(b'mp4')
                IG.publish_items([{'staged': f, 'kind': 'video'}], 'cap', self.publisher(graph),
                                 IG.R2('acct', 'uploads', 'AK', 'SK', http=r2), trial=trial)
            m = graph.media()[0]
            self.assertEqual(m['media_type'], 'REELS')
            self.assertEqual(json.loads(m['trial_params']), {'graduation_strategy': strategy})
            self.assertNotIn('share_to_feed', m)
            # what goes over the wire: the object JSON-encoded inside the form body
            body = urllib.parse.parse_qs(urllib.parse.urlencode(m))
            self.assertEqual(body['trial_params'], [f'{{"graduation_strategy": "{strategy}"}}'])

    def test_a_trial_reel_needs_one_video_before_anything_is_uploaded(self):
        graph, r2 = FakeGraph(), FakeR2()
        with self.assertRaises(IG.InstagramError):
            IG.publish_items([{'staged': 'a.jpg', 'kind': 'image'}], 'cap', self.publisher(graph),
                             IG.R2('acct', 'uploads', 'AK', 'SK', http=r2), trial='auto')
        self.assertEqual((graph.calls, r2.puts), ([], []))

    def test_carousel_video_items(self):
        graph, r2 = FakeGraph(), FakeR2()
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / 'a.jpg', Path(d) / 'b.mp4'
            a.write_bytes(b'1')
            b.write_bytes(b'2')
            steps = []
            IG.publish_items([{'staged': a, 'kind': 'image'}, {'staged': b, 'kind': 'video'}], 'cap',
                             self.publisher(graph), IG.R2('acct', 'uploads', 'AK', 'SK', http=r2),
                             progress=steps.append)
        self.assertEqual(steps, ['Uploading 1/2 to R2', 'Uploading 2/2 to R2', 'Instagram is processing 1/2',
                                 'Instagram is processing 2/2',
                                 'Instagram is building the carousel', 'Publishing'])
        m = graph.media()
        self.assertEqual((m[1]['media_type'], m[1]['is_carousel_item']), ('VIDEO', 'true'))
        self.assertIn('video_url', m[1])
        self.assertEqual(m[2]['children'], 'C1,C2')


class SigV4(unittest.TestCase):
    def test_aws_published_presigned_url_example(self):
        # docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html: GET examplebucket/test.txt, 24 h
        url = IG.presign('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', 'us-east-1', 'AKIAIOSFODNN7EXAMPLE',
                         'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 86400,
                         dt.datetime(2013, 5, 24, 0, 0, 0, tzinfo=dt.timezone.utc))
        self.assertTrue(url.endswith(
            'X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404'), url)

    def test_placeholders_from_the_example_count_as_not_set_up(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(ROOT / 'tools' / 'content-engine' / 'config.example.json', Path(d) / 'config.json')
            s = IG.Setup(d)
        self.assertFalse(s.enabled)
        for what in ('instagram.user_id', 'r2.account_id', 'r2.bucket', 'secrets/instagram_token'):
            self.assertTrue(any(what in m for m in s.missing), what)


if __name__ == '__main__':
    unittest.main()
