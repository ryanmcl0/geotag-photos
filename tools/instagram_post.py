#!/usr/bin/env python3
"""Post one pulled Xiaohongshu-tab draft to Instagram (@ryanmcl0; the Instagram tab is the urbex account, by hand): the media checks, R2 and the Graph API behind ./post.py ig.

post.py does the draft side (the fresh pull, the question, the posted mark); this module does the rest:

  prepare(post, manifest, staging)   the items in the draft's order, checked against Instagram's rules BEFORE
                                     anything is uploaded: at most 20, images 4:5 to 1.91:1 (never cropped here:
                                     an item outside that is a problem and blocks the post), videos MP4 within the
                                     duration/size limits, the caption within 2200 characters / 30 hashtags /
                                     20 mentions (more than 5 hashtags is a note: Instagram's newer cap). A
                                     trial reel (post.trial, set on the Posts page) only for a single-video Reel.
                                     Images go out as sRGB JPEG copies (at most 1440 px wide, the widest
                                     Instagram keeps, and 8 MB) written to `staging`; the originals and the
                                     pulled folder are only ever read.
  Setup()                            ~/.config/content-engine/: config.json (from tools/content-engine/
                                     config.example.json) + secrets/instagram_token,
                                     secrets/r2_access_key_id, secrets/r2_secret_access_key. Anything missing is
                                     listed in .missing, never raised. publish_enabled false = nothing goes out.
  publish_items(items, caption, ig, r2)
                                     the 24 h publishing limit first, then every item to R2 (Instagram fetches
                                     each from a short-lived presigned URL), the containers (one IMAGE, one REELS
                                     for a single video, a trial reel when the draft asks, or carousel items + a
                                     CAROUSEL), media_publish, the permalink. The R2 objects are deleted again
                                     in every case, failures included.

Not possible through the API: attaching Instagram music (the draft's song is only noted) and, without the
Facebook Page search permission, a location tag (skipped).

The R2 presigning and the Graph API publisher are copied from the video project's reelbot
(video-editor/tools/reelbot/r2.py and instagram.py, 2026-11-06), with carousels added; the two repos do not
import each other.
"""
import datetime as dt
import hashlib
import hmac
import io
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ACCOUNT = 'ryanmcl0'                 # the only Instagram account this posts to
CONFIG_DIR = Path(os.environ.get('CONTENT_ENGINE_DIR') or Path.home() / '.config' / 'content-engine')
SECRETS = ('instagram_token', 'r2_access_key_id', 'r2_secret_access_key')

MAX_ITEMS = 20                       # carousel items (Meta's content publishing docs: "limited to 20")
MIN_RATIO, MAX_RATIO = 4 / 5, 1.91   # image width / height
RATIO_SLACK = 0.002                  # 1080x1350 and 1080x566 sit right on the edges
JPEG_MAX = 8 * 1024 * 1024
MAX_WIDTH = 1440
CAPTION_MAX, HASHTAG_MAX, MENTION_MAX = 2200, 30, 20
# Instagram announced a cap of 5 hashtags per post (Dec 2025) while the API docs still say 30: past 5 is a note,
# past 30 still blocks. The Posts page's Suggest tags button stops at 5 too.
HASHTAG_SOFT = 5
# Trial reels (shown to non-followers first): post.trial → the IG User Media trial_params graduation_strategy.
# auto = Instagram shares it to followers if it performs well, manual = he shares it from the app.
TRIAL_STRATEGY = {'auto': 'SS_PERFORMANCE', 'manual': 'MANUAL'}
# Videos: a single video goes out as a Reel, a video inside a carousel follows the feed-video limits.
REEL_SECONDS, REEL_BYTES = (3, 15 * 60), 300 * 1024 * 1024
CAROUSEL_VIDEO_SECONDS, CAROUSEL_VIDEO_BYTES = (3, 60), 100 * 1024 * 1024

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp'}
VIDEO_EXTS = {'.mp4', '.mov', '.m4v'}
NEEDS_BLUR = '_NEEDS-BLUR'           # post.py's mark for a copy whose faces still need blurring by hand


# ---------------------------------------------------------------- setup

class Setup:
    """config.json + the three secret files. .missing lists what is not set up (plain words, no values)."""

    def __init__(self, folder=None):
        self.folder = Path(folder or CONFIG_DIR)
        self.missing = []
        cfg = {}
        path = self.folder / 'config.json'
        if not path.exists():
            self.missing.append(f'{path} (copy tools/content-engine/config.example.json there)')
        else:
            try:
                cfg = json.loads(path.read_text())
                if not isinstance(cfg, dict):
                    raise ValueError
            except (OSError, ValueError):
                self.missing.append(f'{path} is not valid JSON')
                cfg = {}
        self.enabled = cfg.get('publish_enabled') is True
        ig = cfg.get('instagram') or {}
        r2 = cfg.get('r2') or {}
        self.api = ig.get('api') or 'facebook'
        self.version = ig.get('version') or 'v23.0'
        self.user_id = _set(ig.get('user_id'))
        self.account_id = _set(r2.get('account_id'))
        self.bucket = _set(r2.get('bucket'))
        self.prefix = r2.get('prefix') or 'posts/'
        self.url_seconds = int(r2.get('url_seconds') or 3600)
        if self.api not in HOSTS:
            self.missing.append(f'config.json instagram.api must be "facebook" or "instagram", not "{self.api}"')
        for value, name in ((self.user_id, 'instagram.user_id'), (self.account_id, 'r2.account_id'),
                            (self.bucket, 'r2.bucket')):
            if cfg and not value:
                self.missing.append(f'config.json {name}')
        self._secrets = {}
        for name in SECRETS:
            try:
                value = (self.folder / 'secrets' / name).read_text().strip()
            except OSError:
                value = ''
            if value:
                self._secrets[name] = value
            else:
                self.missing.append(f'secrets/{name}')

    @property
    def ready(self):
        return not self.missing

    def publisher(self, http=None, sleep=time.sleep):
        return Publisher(self._secrets.get('instagram_token'), self.user_id, self.api, self.version,
                         http=http or urllib_http, sleep=sleep)

    def r2(self, http=None):
        return R2(self.account_id, self.bucket, self._secrets.get('r2_access_key_id'),
                  self._secrets.get('r2_secret_access_key'), self.url_seconds, http=http or urllib_request)


def _set(value):
    """A config value, or None for an empty one or an example placeholder like "<your id>"."""
    value = str(value).strip() if value is not None else ''
    return None if not value or value.startswith('<') else value


# ---------------------------------------------------------------- R2 (from reelbot/r2.py)

def _h(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _q(s):
    return urllib.parse.quote(s, safe='-_.~')


def presign(method, host, path, region, access_key, secret_key, expires, now=None, service='s3'):
    """A presigned https URL (AWS Signature V4, query string) for `method` on host+path, valid `expires` s."""
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date, day = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    scope = f'{day}/{region}/{service}/aws4_request'
    q = {'X-Amz-Algorithm': 'AWS4-HMAC-SHA256', 'X-Amz-Credential': f'{access_key}/{scope}', 'X-Amz-Date': amz_date,
         'X-Amz-Expires': str(int(expires)), 'X-Amz-SignedHeaders': 'host'}
    uri = urllib.parse.quote(path, safe='/-_.~')
    query = '&'.join(f'{_q(k)}={_q(v)}' for k, v in sorted(q.items()))
    creq = '\n'.join([method, uri, query, f'host:{host}\n', 'host', 'UNSIGNED-PAYLOAD'])
    sts = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(creq.encode()).hexdigest()])
    k = _h(_h(_h(_h(('AWS4' + secret_key).encode(), day), region), service), 'aws4_request')
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f'https://{host}{uri}?{query}&X-Amz-Signature={sig}'


def urllib_request(method, url, body_path=None, content_type=None):
    """The real transport → HTTP status. The URL is never printed (it carries a signature)."""
    data, headers = None, {}
    if body_path:
        data = open(body_path, 'rb')
        headers = {'Content-Length': str(os.path.getsize(body_path)),
                   'Content-Type': content_type or 'application/octet-stream'}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    finally:
        if data:
            data.close()


class R2Error(Exception):
    pass


class R2:
    """Cloudflare R2 through presigned URLs: https://<account>.r2.cloudflarestorage.com/<bucket>/<key>, region auto."""

    def __init__(self, account_id, bucket, access_key, secret_key, url_seconds=3600, http=urllib_request, clock=None):
        if not (account_id and bucket and access_key and secret_key):
            raise R2Error('R2 is not set up: config r2.account_id, r2.bucket and secrets '
                          'r2_access_key_id, r2_secret_access_key')
        self.host = f'{account_id}.r2.cloudflarestorage.com'
        self.bucket, self._ak, self._sk = bucket, access_key, secret_key
        self.url_seconds = int(url_seconds)
        self.http, self.clock = http, clock

    def url(self, method, key, expires=None):
        now = self.clock() if self.clock else None
        return presign(method, self.host, f'/{self.bucket}/{key}', 'auto', self._ak, self._sk,
                       expires or self.url_seconds, now)

    def put(self, path, key, content_type=None):
        st = self.http('PUT', self.url('PUT', key, 900), Path(path), content_type)
        if st not in (200, 201):
            raise R2Error(f'upload failed: HTTP {st}')

    def get_url(self, key):
        return self.url('GET', key)

    def delete(self, key):
        st = self.http('DELETE', self.url('DELETE', key, 300))
        if st not in (200, 204, 404):
            raise R2Error(f'delete failed: HTTP {st}')


# ---------------------------------------------------------------- Graph API (from reelbot/instagram.py)

HOSTS = {'instagram': 'https://graph.instagram.com', 'facebook': 'https://graph.facebook.com'}


class InstagramError(Exception):
    pass


def urllib_http(method, url, params):
    data = None
    if method == 'GET':
        url = f'{url}?{urllib.parse.urlencode(params)}'
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {'error': {'message': f'HTTP {e.code}'}}


class Publisher:
    """Instagram content publishing. The token travels in the request only; errors are reported without URLs."""

    def __init__(self, token, user_id, api='facebook', version='v23.0', http=urllib_http, sleep=time.sleep,
                 poll_seconds=5, poll_timeout=900):
        if not token:
            raise InstagramError('no Instagram token: write secrets/instagram_token')
        if not user_id:
            raise InstagramError('config instagram.user_id is not set')
        self._token, self.user = token, str(user_id)
        self.base = f'{HOSTS[api]}/{version}'
        self.http, self.sleep = http, sleep
        self.poll_seconds, self.poll_timeout = poll_seconds, poll_timeout

    def _do(self, method, path, **params):
        r = self.http(method, f'{self.base}/{path}', {**params, 'access_token': self._token})
        if not isinstance(r, dict) or r.get('error'):
            msg = ((r or {}).get('error') or {}).get('message', 'failed') if isinstance(r, dict) else 'failed'
            raise InstagramError(f"{path.split('/')[-1]}: {msg}")
        return r

    def posts_left(self):
        r = self._do('GET', f'{self.user}/content_publishing_limit', fields='config,quota_usage')
        d = (r.get('data') or [{}])[0]
        total = int((d.get('config') or {}).get('quota_total') or 0)
        return max(0, total - int(d.get('quota_usage') or 0)), total

    def create_image(self, image_url, caption):
        return self._do('POST', f'{self.user}/media', image_url=image_url, caption=caption)['id']

    def create_reel(self, video_url, caption, share_to_feed=True, trial=None):
        """A REELS container; trial 'auto'/'manual' makes it a trial reel (trial_params, an object param, goes
        JSON-encoded like every object the Graph API takes in a form body; no share_to_feed with it)."""
        params = {'media_type': 'REELS', 'video_url': video_url, 'caption': caption}
        if trial:
            params['trial_params'] = json.dumps({'graduation_strategy': TRIAL_STRATEGY[trial]})
        else:
            params['share_to_feed'] = 'true' if share_to_feed else 'false'
        return self._do('POST', f'{self.user}/media', **params)['id']

    def create_carousel_item(self, url, kind):
        if kind == 'video':
            return self._do('POST', f'{self.user}/media', media_type='VIDEO', video_url=url,
                            is_carousel_item='true')['id']
        return self._do('POST', f'{self.user}/media', image_url=url, is_carousel_item='true')['id']

    def create_carousel(self, children, caption):
        return self._do('POST', f'{self.user}/media', media_type='CAROUSEL', children=','.join(children),
                        caption=caption)['id']

    def wait(self, container):
        waited = 0
        while True:
            r = self._do('GET', container, fields='status_code,status')
            code = r.get('status_code')
            if code in ('FINISHED', 'PUBLISHED'):
                return code
            if code in ('ERROR', 'EXPIRED'):
                raise InstagramError(f"Instagram could not process an item: {code} {r.get('status', '')}".strip())
            if waited >= self.poll_timeout:
                raise InstagramError(f'still {code} after {waited} s')
            self.sleep(self.poll_seconds)
            waited += self.poll_seconds

    def publish(self, container):
        return self._do('POST', f'{self.user}/media_publish', creation_id=container)['id']

    def permalink(self, media_id):
        return self._do('GET', media_id, fields='permalink').get('permalink')


# ---------------------------------------------------------------- the items and their checks

def kind_of(path):
    ext = Path(path).suffix.lower()
    return 'video' if ext in VIDEO_EXTS else 'image' if ext in IMAGE_EXTS else None


def ratio_name(w, h):
    """'4:5', '1.91:1' ... for the common shapes, else the plain number."""
    r = w / h
    for name, v in (('1:1', 1), ('4:5', 4 / 5), ('5:4', 5 / 4), ('3:2', 3 / 2), ('2:3', 2 / 3), ('16:9', 16 / 9),
                    ('9:16', 9 / 16), ('1.91:1', 1.91), ('4:3', 4 / 3), ('3:4', 3 / 4)):
        if abs(r - v) < 0.005:
            return name
    return f'{r:.2f}:1'


def post_items(post, manifest, folder):
    """The files that go out, in the draft's order: each carousel photo as pulled (re-exports included), its
    location card right after it when the photo is marked with the map button. A draft with no carousel photos
    posts its Phone items instead (a single phone video = a Reel); otherwise Phone stays behind the scenes."""
    folder = Path(folder)
    maps = {m.get('order'): m.get('file') for m in manifest.get('maps') or []}
    items = []
    for ph in manifest.get('photos') or []:
        items.append({'path': Path(ph['copied_to']), 'what': 'photo'})
        if ph.get('order') in maps:
            items.append({'path': Path(maps[ph['order']]), 'what': 'location card'})
    if not items:
        for ph in manifest.get('phone') or []:
            items.append({'path': Path(ph['copied_to']), 'what': 'phone'})
    for n, it in enumerate(items, 1):
        it['n'] = n
        it['kind'] = kind_of(it['path'])
        try:
            it['label'] = str(it['path'].relative_to(folder))
        except ValueError:
            it['label'] = it['path'].name
    return items


def hashtags(caption):
    return re.findall(r'(?<![\w#])#\w+', caption)


def caption_problems(caption):
    out = []
    if len(caption) > CAPTION_MAX:
        out.append(f'the caption is {len(caption)} characters; Instagram takes at most {CAPTION_MAX}')
    tags = hashtags(caption)
    if len(tags) > HASHTAG_MAX:
        out.append(f'the caption has {len(tags)} hashtags; Instagram takes at most {HASHTAG_MAX}')
    mentions = re.findall(r'(?<![\w@])@[\w.]+', caption)
    if len(mentions) > MENTION_MAX:
        out.append(f'the caption has {len(mentions)} @mentions; Instagram takes at most {MENTION_MAX}')
    return out


def stage_image(src, dst):
    """src → an sRGB JPEG at dst (at most MAX_WIDTH wide and JPEG_MAX bytes). Returns (width, height) of the
    source as it displays (EXIF rotation applied), the staged size, and a note when the colour profile could not
    be converted. Only reads src."""
    from PIL import Image, ImageCms, ImageOps
    note = None
    with Image.open(src) as im:
        size = im.size[::-1] if im.getexif().get(0x0112) in (5, 6, 7, 8) else im.size   # as it displays
        if size[0] > 2 * MAX_WIDTH:
            # A JPEG decodes straight at 1/2, 1/4 or 1/8 size, far faster than decoding it all; asking for at
            # least twice the final width keeps the LANCZOS step below doing the real downscale (no visible loss).
            k = 2 * MAX_WIDTH / size[0]
            im.draft(im.mode, (math.ceil(im.width * k), math.ceil(im.height * k)))
        im = ImageOps.exif_transpose(im)
        icc = im.info.get('icc_profile')
        if im.width > MAX_WIDTH and im.mode in ('RGB', 'L', 'CMYK'):
            # Shrink before the colour conversion, so it runs on the final pixels rather than all of them.
            im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)), Image.LANCZOS)
        srgb = ImageCms.createProfile('sRGB')
        if icc:
            try:
                im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)), srgb,
                                               outputMode='RGB')
            except (ImageCms.PyCMSError, OSError, ValueError):
                note = 'its colour profile could not be converted, sent as plain RGB'
        if im.mode != 'RGB':
            im = im.convert('RGB')
        if im.width > MAX_WIDTH:
            im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)), Image.LANCZOS)
        profile = ImageCms.ImageCmsProfile(srgb).tobytes()
        for quality in (95, 90, 85, 80, 75, 70):
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=quality, subsampling=0 if quality >= 90 else 2,
                    icc_profile=profile, optimize=True)
            if buf.tell() <= JPEG_MAX:
                break
        Path(dst).write_bytes(buf.getvalue())
    return size, Path(dst).stat().st_size, note


def probe_video(path):
    """ffprobe → {'format', 'duration', 'width', 'height'}; raises OSError/ValueError when it can't."""
    if not shutil.which('ffprobe'):
        raise OSError('ffprobe not found (brew install ffmpeg)')
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=format_name,duration',
                          '-show_entries', 'stream=codec_type,width,height', '-of', 'json', str(path)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise ValueError(out.stderr.strip()[:200] or 'ffprobe failed')
    d = json.loads(out.stdout)
    v = next((s for s in d.get('streams', []) if s.get('codec_type') == 'video'), {})
    return {'format': (d.get('format') or {}).get('format_name', ''),
            'duration': float((d.get('format') or {}).get('duration') or 0),
            'width': v.get('width'), 'height': v.get('height')}


def prepare(post, manifest, folder, staging, probe=probe_video, progress=None):
    """Everything the confirmation shows, checked before any upload. progress(n, total), when given, is called
    as each item starts (staging reads every file off the drive, so a long carousel takes a while).

    Returns {'account', 'caption', 'type', 'items': [...], 'problems': [...], 'notes': [...]}; any problem (on
    the post or on an item) blocks posting. Images are staged as JPEG copies in `staging`."""
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    problems, notes = [], []
    caption = (post.get('caption') or '').strip()
    account = (post.get('account') or '').strip()
    if post.get('platform') != 'xhs':
        problems.append('this is an Instagram-tab draft (the urbex account, posted by hand); only Xiaohongshu-tab '
                        'drafts go to @ryanmcl0')
    if account and account != ACCOUNT:
        problems.append(f'the draft is marked for @{account}; only @{ACCOUNT} is set up for posting')
    if post.get('posted'):
        problems.append('the draft is already marked posted')

    want = [(p.get('trip'), p.get('id')) for p in post.get('photos') or []]
    have = [(p.get('trip'), p.get('id')) for p in manifest.get('photos') or []]
    if want != have:
        problems.append("the pulled folder does not match the draft's photos (one could not be found, or the "
                        'draft changed during the pull): run again')
    map_orders = {m.get('order') for m in manifest.get('maps') or []}
    for i, ref in enumerate(post.get('photos') or [], 1):
        if ref.get('map') and i not in map_orders:
            problems.append(f'photo {i:02d} is marked for a location card but none was rendered')

    items = post_items(post, manifest, folder)
    if not items:
        problems.append('nothing to post: the draft has no photos')
    if len(items) > MAX_ITEMS:
        problems.append(f'{len(items)} items; Instagram takes at most {MAX_ITEMS} in a carousel '
                        '(move some to the waitlist, location cards count too)')
    single = len(items) == 1
    for n, it in enumerate(items, 1):
        if progress:
            progress(n, len(items))
        it['problems'] = []
        src = it['path']
        if not src.exists():
            it['problems'].append('file not found in the post folder')
            continue
        it['bytes'] = src.stat().st_size
        if NEEDS_BLUR in src.stem:
            it['problems'].append('marked for blur but no face was found automatically: blur it by hand, '
                                  f'drop {NEEDS_BLUR} from the name, run again')
        if it['kind'] is None:
            it['problems'].append(f'{src.suffix or "no extension"} is neither a photo nor a video Instagram takes')
        elif it['kind'] == 'image':
            dst = staging / f"{it['n']:02d}" / f'{src.stem}.jpg'
            dst.parent.mkdir(exist_ok=True)
            try:
                (w, h), staged_bytes, note = stage_image(src, dst)
            except Exception as e:                              # noqa: BLE001 — any unreadable file is a problem
                it['problems'].append(f'could not read the image ({e})')
                continue
            it.update(width=w, height=h, ratio=w / h, staged=dst, staged_bytes=staged_bytes)
            if note:
                notes.append(f"{it['label']}: {note}")
            if not MIN_RATIO - RATIO_SLACK <= w / h <= MAX_RATIO + RATIO_SLACK:
                it['problems'].append(f'shape {ratio_name(w, h)} ({w}×{h}) is outside the 4:5 to 1.91:1 '
                                      'Instagram takes: crop it in Lightroom, re-export into the post folder, '
                                      'run again (nothing is cropped automatically)')
            if staged_bytes > JPEG_MAX:
                it['problems'].append(f'still {staged_bytes / 1e6:.1f} MB as a JPEG; Instagram takes at most 8 MB')
        else:
            it['staged'] = src                                  # videos go as they are: read, never changed
            try:
                v = probe(src)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                it['problems'].append(f'could not read the video ({e})')
                continue
            it.update(width=v.get('width'), height=v.get('height'), duration=v['duration'],
                      staged_bytes=it['bytes'])
            if v.get('width') and v.get('height'):
                it['ratio'] = v['width'] / v['height']
            if src.suffix.lower() != '.mp4' or 'mp4' not in v['format']:
                it['problems'].append('not an MP4: export it as MP4 (H.264/AAC) and run again')
            (lo, hi), cap, what = ((REEL_SECONDS, REEL_BYTES, 'a Reel') if single else
                                   (CAROUSEL_VIDEO_SECONDS, CAROUSEL_VIDEO_BYTES, 'a carousel video'))
            if not lo <= v['duration'] <= hi:
                it['problems'].append(f"{v['duration']:.1f} s; {what} must be {lo} s to {hi // 60} min"
                                      if hi >= 120 else
                                      f"{v['duration']:.1f} s; {what} must be {lo} to {hi} s")
            if it['bytes'] > cap:
                it['problems'].append(f"{it['bytes'] / 1e6:.0f} MB; {what} can be at most {cap // 2**20} MB")

    ratios = [it['ratio'] for it in items if it.get('ratio')]
    if len(items) > 1 and ratios and any(abs(r - ratios[0]) / ratios[0] > 0.01 for r in ratios):
        notes.append('the items are not all the same shape: Instagram shows every slide at the first one\'s '
                     'shape, so the others are cropped to it in the app')
    problems += caption_problems(caption)
    if HASHTAG_SOFT < len(hashtags(caption)) <= HASHTAG_MAX:
        notes.append(f'{len(hashtags(caption))} hashtags: Instagram has capped posts at {HASHTAG_SOFT} since Dec 2025; '
                     'extra ones may be dropped or refused')
    if not caption:
        notes.append('no caption')
    if (post.get('song') or '').strip():
        notes.append(f"song \"{post['song'].strip()}\" is not attached: Instagram's API cannot add music")
    if post.get('photos') and post.get('phone'):
        notes.append(f"{len(post['phone'])} Phone item(s) stay behind the scenes (not posted)")
    kinds = {it['kind'] for it in items}
    kind = ('CAROUSEL' if len(items) > 1 else 'REELS' if kinds == {'video'} else 'IMAGE')
    trial = post.get('trial') or None
    if trial and trial not in TRIAL_STRATEGY:
        problems.append(f'trial reel is "{trial}"; it can only be auto or manual: set it again on the Posts page')
    elif trial and items and kind != 'REELS':
        shape = 'a carousel' if kind == 'CAROUSEL' else 'a photo'
        problems.append(f'trial reel is on, but this draft goes out as {shape}: trial applies only to a '
                        'single-video Reel; switch it off or make the draft one video')
    return {'account': ACCOUNT, 'caption': caption, 'type': kind, 'trial': trial, 'items': items,
            'problems': problems, 'notes': notes}


def blocking(plan):
    """Every problem that stops the post: the post's own, then each item's (labelled)."""
    return plan['problems'] + [f"{it['label']}: {p}" for it in plan['items'] for p in it.get('problems', [])]


# ---------------------------------------------------------------- publishing

class LimitReached(Exception):
    pass


def clean(msg):
    """An error for the terminal and the log: no URLs (presigned ones carry a signature), short."""
    return re.sub(r'https?://\S+', '<url>', str(msg))[:300]


CONTENT_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.mp4': 'video/mp4'}


def publish_items(items, caption, ig, r2, prefix='posts/', share_to_feed=True, trial=None, progress=None):
    """The checked items → one Instagram post (trial 'auto'/'manual': a trial reel, single video only).
    Returns {'media_id', 'permalink', 'left', 'warnings'}.

    Raises LimitReached (nothing uploaded) when the 24 h quota is used up, and any R2/Instagram error as it
    comes; the uploaded objects are deleted in every case. A post that went out but whose link Instagram did
    not return comes back with permalink None and a warning. progress(text), when given, is called at each
    step (uploads, Instagram's processing, publishing), so a long carousel never looks stuck."""
    say = progress or (lambda text: None)
    if trial and not (len(items) == 1 and items[0]['kind'] == 'video'):
        raise InstagramError('a trial reel needs exactly one video')      # prepare() blocks this first
    left, total = ig.posts_left()
    if left <= 0:
        raise LimitReached(f"Instagram's 24-hour publishing limit is used up ({total} of {total})")
    keys, warnings = [], []
    try:
        urls = []
        for n, it in enumerate(items, 1):
            say(f'Uploading {n}/{len(items)} to R2')
            staged = Path(it['staged'])
            key = f'{prefix}{uuid.uuid4().hex}{staged.suffix.lower()}'
            keys.append(key)                     # before the upload: a half-finished one is deleted too
            r2.put(staged, key, CONTENT_TYPES.get(staged.suffix.lower()))
            urls.append(r2.get_url(key))
        if len(items) == 1:
            say('Instagram is processing it')
            container = (ig.create_reel(urls[0], caption, share_to_feed, trial) if items[0]['kind'] == 'video'
                         else ig.create_image(urls[0], caption))
        else:
            children = []
            for n, (it, url) in enumerate(zip(items, urls), 1):
                say(f'Instagram is processing {n}/{len(items)}')
                child = ig.create_carousel_item(url, it['kind'])
                ig.wait(child)
                children.append(child)
            say('Instagram is building the carousel')
            container = ig.create_carousel(children, caption)
        ig.wait(container)
        say('Publishing')
        media_id = ig.publish(container)
        link, err = None, None
        for _ in range(3):
            try:
                link = ig.permalink(media_id)
            except InstagramError as e:
                err = e
            if link:
                break
        if not link:
            warnings.append('posted, but Instagram did not return its link'
                            + (f': {clean(err)}' if err else ''))
    finally:
        for key in keys:
            try:
                r2.delete(key)
            except Exception as e:                   # noqa: BLE001 — a post that went out is still a post
                warnings.append(f'R2 object {key} not deleted: {clean(e)}')
    return {'media_id': media_id, 'permalink': link, 'left': left - 1, 'warnings': warnings}


def log_post(record, path):
    """One line per post in a local log (no secrets: ids, names, the permalink)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
