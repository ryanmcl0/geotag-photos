#!/usr/bin/env python3
"""Side strip: a portrait photo posted whole on the post's shape, the spare strip black with the place on it.

Instagram shows a portrait carousel at 4:5 at the tallest, so a 2:3 camera photo loses its top and bottom. With
"Preserve aspect ratio" ticked on a draft (Posts page), ./post.py pull keeps each unframed portrait photo whole
instead: the photo sits against one edge of a canvas at the post's shape, and the strip left over (a sixth of the
width for 2:3 on 4:5) is near-black with the country's flag at the top and the place running along it in spaced
Futura caps: a big line (the place) and a small one under it (the region and country). The strip changes sides
from one such slide to the next (right, left, right ...), so two strips meet in the swipe between slides and the
next two photos touch.

The text is the automatic place name (location_pin.label_for) at the level chosen for the post (place / region /
country, level_text), or whatever is typed per photo on the Posts page ("Big line", "Big line / small line",
"Big line /" for no small line, "off" for a plain strip). The flag follows where the photo was taken.

A strip thinner than MIN_STRIP of the width (a 3:4 phone photo on 4:5) has no room for the text, so such a photo
is left as it was (pulled whole, centre-cropped for Instagram).

  python3 tools/side_strip.py --trip 2024-guizhou --id RM109399-Enhanced-NR --src photo.jpg --out x.jpg \
      [--side left] [--level region] [--text "Guizhou / China"] [--ratio 0.8]
"""
import argparse
import io
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FLAGS = Path(__file__).resolve().parent / 'flags'      # flat flags by ISO code (flagcdn.com, public domain)
FLAG_URL = 'https://flagcdn.com/w320/{cc}.png'
SYSTEM_FUTURA = '/System/Library/Fonts/Supplemental/Futura.ttc'      # Futura Medium, the look he chose
REPO_FUTURA = ROOT / 'web' / 'fonts' / 'FuturaPT-Book.ttf'           # where the Mac's own Futura is missing
LOOK = 1                       # bump when the size or look changes: pull remakes copies made before
MIN_STRIP = 0.10               # of the canvas width; thinner than this has no room for the text
BG = (10, 10, 11)
FG = (255, 255, 255)
SUB = (145, 145, 146)
LEVELS = ('place', 'region', 'country')
SIDES = ('right', 'left')
# Sizes on a canvas 1350 high (Instagram's 1080 x 1350), where a 2:3 photo leaves a strip 180 wide.
BASE_H, BASE_STRIP = 1350, 180
BIG, SMALL, GAP = 62, 21, 20           # the two lines and the space between them
BIG_TRACK, SMALL_TRACK = 0.22, 5.2     # letter spacing: of the size (big line), px (small line)
FLAG_W, FLAG_TOP = 60, 70
FOOT = 84                              # the text ends this far above the bottom edge
CLEAR = 60                             # at least this much between the flag and the text


# ── which photos, which text ─────────────────────────────────────────────────────────────────────
def strip_share(w, h, ratio):
    """The part of the canvas width a w x h photo leaves free at full height on a canvas of shape `ratio`."""
    if not ratio or not w or not h:
        return 0.0
    return max(0.0, 1 - (w / h) / ratio)


def wanted(w, h, ratio):
    """Whether a w x h photo gets the strip on a post of shape `ratio`: narrower than it, with room for text."""
    return strip_share(w, h, ratio) >= MIN_STRIP


def level_text(name, region, level='place'):
    """(big line, small line) from the automatic (name, region line) at the level chosen for the post.
    place: "Guiyang" / "Guizhou, China". region: "Guizhou" / "China" (a place with no region over it, like
    Chongqing, keeps its name). country: "China" alone. The Posts page works out the same (stripAuto in
    web/js/posts.js)."""
    name, region = (name or '').strip(), (region or '').strip()
    parts = [p.strip() for p in region.split(',') if p.strip()]
    if level == 'country':
        return (parts[-1] if parts else name), ''
    if level == 'region' and len(parts) >= 2:
        return parts[0], ', '.join(parts[1:])
    return name, region


def split_text(text, auto_head='', auto_sub=''):
    """A typed text as (big line, small line), or None for "off". "Name" keeps the automatic small line,
    "Name / line" sets both, "Name /" drops the small line; nothing typed is the automatic pair."""
    text = (text or '').strip()
    if not text:
        return auto_head or '', auto_sub or ''
    if text.lower() == 'off':
        return None
    head, slash, sub = text.partition('/')
    return head.strip(), (sub.strip() if slash else (auto_sub or ''))


def auto_text(trip, pid):
    """(name, region line, country code) for a photo from where it was taken; Nones when it has no position."""
    try:
        import location_pin
        name, region = location_pin.label_for(trip, pid)
        ph = location_pin.photo_record(trip, pid)
        cc = None
        if ph and ph.get('lat') is not None:
            cc = (location_pin._lookups()[0].lookup(ph['lat'], ph['lon']) or {}).get('cc')
        return name, region, (cc or None)
    except Exception:           # a phone photo, a trip with no manifest: no automatic text
        return None, None, None


# ── drawing ──────────────────────────────────────────────────────────────────────────────────────
def _font(size):
    size = max(1, int(round(size)))
    try:
        return ImageFont.truetype(SYSTEM_FUTURA, size, index=0)
    except OSError:
        return ImageFont.truetype(str(REPO_FUTURA), size)


def _tracked(text, font, track, fill):
    """`text` with `track` px between the letters, on a tight transparent image (None when there is no ink)."""
    widths = [font.getlength(c) for c in text]
    asc, desc = font.getmetrics()
    w = int(sum(widths) + track * max(0, len(text) - 1)) + 4
    im = Image.new('RGBA', (max(1, w), asc + desc), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = 0.0
    for c, cw in zip(text, widths):
        d.text((x, 0), c, font=font, fill=fill)
        x += cw + track
    box = im.getbbox()
    return im.crop(box) if box else None


def _line(text, size, track_of, fill, max_len):
    """One line at `size`, shrunk until it is no longer than max_len."""
    im = _tracked(text, _font(size), track_of(size), fill)
    if im is not None and im.width > max_len:
        size *= max_len / im.width
        im = _tracked(text, _font(size), track_of(size), fill)
    return im


def flag_image(cc, width, fetch=True):
    """The country's flat flag `width` px wide (RGB), or None. Flags not in tools/flags are fetched once."""
    cc = (cc or '').strip().lower()
    if len(cc) != 2 or not cc.isalpha():
        return None
    path = FLAGS / f'{cc}.png'
    if not path.exists():
        if not fetch:
            return None
        try:
            req = urllib.request.Request(FLAG_URL.format(cc=cc), headers={'User-Agent': 'flag-fetch'})
            data = urllib.request.urlopen(req, timeout=15).read()
            if data[:8] != b'\x89PNG\r\n\x1a\n':
                return None
            FLAGS.mkdir(exist_ok=True)
            path.write_bytes(data)
        except Exception:
            return None
    try:
        with Image.open(path) as im:
            im = im.convert('RGBA')
    except OSError:
        return None
    back = Image.new('RGBA', im.size, BG + (255,))
    back.alpha_composite(im)
    width = max(1, int(round(width)))
    return back.convert('RGB').resize((width, max(1, round(width * im.height / im.width))), Image.LANCZOS)


def strip_image(width, height, side, head, sub, cc, icc=None):
    """The strip itself, width x height px: flag upright at the top, the two lines running along it (top to
    bottom on a right-hand strip, bottom to top on a left-hand one, so the letters' feet face the photo)."""
    u = height / BASE_H
    k = min(1.0, width / (BASE_STRIP * u))          # a thinner strip scales its contents down, a wider one does not
    bar = Image.new('RGB', (width, height), BG)
    top = FLAG_TOP * u
    flag = flag_image(cc, FLAG_W * u * k)
    if flag is not None:
        if icc:
            flag = _to_profile(flag, icc)
        fx = (width - flag.width) // 2
        bar.paste(flag, (fx, int(top)))
        ImageDraw.Draw(bar, 'RGBA').rectangle([fx, int(top), fx + flag.width - 1, int(top) + flag.height - 1],
                                              outline=(255, 255, 255, 40))
        top += flag.height
    head, sub = (head or '').strip().upper(), (sub or '').strip().upper()
    max_len = height - FOOT * u - top - CLEAR * u
    t1 = _line(head, BIG * u * k, lambda s: s * BIG_TRACK, FG, max_len) if head else None
    t2 = _line(sub, SMALL * u * k, lambda s: SMALL_TRACK * s / SMALL, SUB, max_len) if sub else None
    lines = [t for t in (t1, t2) if t is not None]
    if not lines:
        return bar
    gap = int(GAP * u * k) if len(lines) == 2 else 0
    blk = Image.new('RGBA', (max(t.width for t in lines), sum(t.height for t in lines) + gap), (0, 0, 0, 0))
    y = 0
    for t in lines:
        blk.paste(t, (0, y), t)
        y += t.height + gap
    r = blk.rotate(-90 if side == 'right' else 90, expand=True)
    bar.paste(r, ((width - r.width) // 2, int(height - FOOT * u - r.height)), r)
    return bar


def _to_profile(img, icc):
    """An sRGB image converted into the photo's colour profile, so the flag's colours come out right."""
    try:
        from PIL import ImageCms
        return ImageCms.profileToProfile(img, ImageCms.createProfile('sRGB'),
                                         ImageCms.ImageCmsProfile(io.BytesIO(icc)), outputMode='RGB')
    except Exception:
        return img


def compose(img, ratio, side='right', head='', sub='', cc=None, icc=None):
    """The upright photo `img` whole on a canvas of shape `ratio` (same height), the strip on `side`.
    None when the photo does not get one (wanted)."""
    img = img.convert('RGB')
    W, H = img.size
    if not wanted(W, H, ratio):
        return None
    side = side if side in SIDES else 'right'
    cw = round(H * ratio)
    bw = cw - W
    out = Image.new('RGB', (cw, H), BG)
    bar = strip_image(bw, H, side, head, sub, cc, icc)
    if side == 'right':
        out.paste(img, (0, 0))
        out.paste(bar, (W, 0))
    else:
        out.paste(bar, (0, 0))
        out.paste(img, (bw, 0))
    return out


def stamp(src, dst, ratio, side='right', head='', sub='', cc=None):
    """Put the photo at src on its canvas with the strip, into dst (may be the same file), keeping its EXIF
    and colour profile. → True, or False when the photo gets no strip (not narrower than the post's shape, e.g.
    a copy that already has its strip) and nothing is written."""
    if Path(src).suffix.lower() in ('.heic', '.heif'):
        from pillow_heif import register_heif_opener
        register_heif_opener()
    with Image.open(src) as im:
        exif, icc = im.info.get('exif'), im.info.get('icc_profile')
        img = ImageOps.exif_transpose(im).convert('RGB')
    out = compose(img, ratio, side, head, sub, cc, icc)
    if out is None:
        return False
    heic = Path(dst).suffix.lower() in ('.heic', '.heif')
    kw = {'quality': 95}
    if not heic:
        kw['subsampling'] = 0
    if exif:
        try:   # the pixels are upright now: drop the orientation tag so nothing rotates them again
            ex = Image.Exif()
            ex.load(exif)
            ex[0x0112] = 1
            kw['exif'] = ex.tobytes()
        except Exception:
            pass
    if icc:
        kw['icc_profile'] = icc
    out.save(dst, 'HEIF' if heic else 'JPEG', **kw)
    return True


def main(argv):
    ap = argparse.ArgumentParser(description='Preview the side strip on one photo.')
    ap.add_argument('--trip', required=True)
    ap.add_argument('--id', required=True)
    ap.add_argument('--src', help='the photo file (default: the trip display image)')
    ap.add_argument('--side', default='right', choices=SIDES)
    ap.add_argument('--level', default='place', choices=LEVELS)
    ap.add_argument('--text', help='"Big line" or "Big line / small line" instead of the automatic text; "off"')
    ap.add_argument('--ratio', type=float, default=0.8, help="the post's shape, width / height (default 4:5)")
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    name, region, cc = auto_text(a.trip, a.id)
    pair = split_text(a.text, *level_text(name, region, a.level))
    head, sub = pair if pair else ('', '')
    src = a.src or ROOT / 'web' / 'trips' / a.trip / 'display' / f'{a.id}.webp'
    done = stamp(src, a.out, a.ratio, a.side, head, sub, cc if pair else None)
    print(f'{head} / {sub} ({cc}) → {a.out}' if done else 'no strip: the photo is not narrow enough for this shape')


if __name__ == '__main__':
    main(sys.argv[1:])
