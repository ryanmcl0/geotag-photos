#!/usr/bin/env python3
"""Location pin: the place name baked onto a post photo, in its calmest corner.

The posts page marks a photo with the 📍 button (style A, press again for C); ./post.py pull then stamps the label
onto the pulled copy (post.py sync_post_dir, like the face blur). The name is worked out from where the photo was
taken and can be overridden per photo at pull time (./post.py pull N --location 3 "Tsagaan Suvarga").

  A  "stacked" (default): the name in Bebas Kai, a spaced Futura region line under it, a pin to the left; no box,
     a soft shadow carries it, and it turns near-black over bright sky
  C  "editorial": Futura caps, a hairline, the region underneath

Placement: always a corner, tight to the edge (never the centre, never lowered into the frame), the left two first
and top right last (Instagram's carousel counter, "1/18", sits there); the calmest wins, scored on edges + local contrast (busy subject), colour spread (a box straddling sky and rock) and
faces (the face index's boxes for the photo). A frame taller than 4:5 keeps the label inside its centre 4:5 band, so
cropping it to 4:5 in the app cannot cut the label off. Sized from the frame width, small whatever the space.

The name: a site from his own rosters within its radius (energy sites, bridges, mountains: "Lenghu Oil Base
Ruins"), else the big town around (place_names: "Kashgar"), else the nearest town or village ("Sesto"), else the
region; the second line is the province / region and country, never a repeat of the name.

  python3 tools/location_pin.py --trip 2026-china-cny --id RM107255 [--style C] [--text "Name / Region"] --out x.jpg
"""
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FONTS = ROOT / 'web' / 'fonts'
BEBAS, FUT_BOOK, FUT_BOLD = (str(FONTS / f) for f in ('BebasKai.ttf', 'FuturaPT-Book.ttf', 'FuturaPT-Bold.ttf'))
FACE_DB = ROOT / 'local_browse' / 'face_index.sqlite'
RIGHT_BIAS = 0.5               # extra score on the right corners: the left side wins unless it is much busier
WORK_W = 1080                  # placement is worked out on a copy this wide, then scaled to the photo
ROSTERS = (('config/china_energy_sites.json', 'sites', 3.0), ('config/china_bridges.json', 'bridges', 3.0),
           ('config/mountains.json', 'mountains', 2.0))
STYLES = ('A', 'C')


# ── the name ─────────────────────────────────────────────────────────────────────────────────────
_places = _prov = _sites = None


def _km(a, b, c, d):
    return 6371 * math.hypot(math.radians(d - b) * math.cos(math.radians((a + c) / 2)), math.radians(c - a))


def _lookups():
    global _places, _prov, _sites
    if _places is None:
        from build_collections import ProvinceIndex
        from place_names import PlaceIndex
        _places = PlaceIndex()
        _prov = ProvinceIndex(ROOT / 'config' / 'geo' / 'china_provinces.geojson')
        _sites = []
        for path, key, radius in ROSTERS:
            try:
                items = json.loads((ROOT / path).read_text()).get(key) or []
            except (OSError, ValueError):
                continue
            _sites += [(x['lat'], x['lon'], x.get('radius_km') or radius, x['name'].split(' (')[0])
                       for x in items if x.get('lat') is not None and x.get('lon') is not None]
    return _places, _prov, _sites


def photo_record(trip, pid):
    """The photo's manifest entry (the full manifest: private photos post too)."""
    import photo_privacy
    man = photo_privacy.load_full_manifest(ROOT / 'web' / 'trips' / trip) or {}
    return next((p for p in man.get('photos', []) if p['id'] == pid), None)


def label_for(trip, pid):
    """(name, region line) for a photo, or (None, None) when it has no position."""
    ph = photo_record(trip, pid)
    if not ph or ph.get('lat') is None:
        return None, None
    lat, lon = ph['lat'], ph['lon']
    places, prov, sites = _lookups()
    pl = places.lookup(lat, lon) or {}
    site = min(((_km(lat, lon, la, lo), n) for la, lo, r, n in sites if _km(lat, lon, la, lo) <= r), default=None)
    head = (site and site[1]) or pl.get('town') or pl.get('spot') or pl.get('city')
    if not head:
        return None, None
    from place_names import COUNTRY_NAMES
    cc = pl.get('cc')
    if cc == 'CN':
        region = prov.lookup(lat, lon)
    else:
        region = (pl.get('town') or pl.get('city')) if head not in (pl.get('city'), pl.get('town')) else None
    parts = [x for x in (region, COUNTRY_NAMES.get(cc, cc)) if x and x.lower() != head.lower()]
    parts = [x for i, x in enumerate(parts) if x.lower() not in (p.lower() for p in parts[:i])]
    return head, ', '.join(parts)


def face_boxes(trip, pid, width):
    """The face index's boxes for the photo (found on its display image), scaled to `width`."""
    disp = ROOT / 'web' / 'trips' / trip / 'display' / f'{pid}.webp'
    if not FACE_DB.exists() or not disp.exists():
        return []
    try:
        with Image.open(disp) as im:
            k = width / im.size[0]
        con = sqlite3.connect(FACE_DB)
        rows = con.execute('SELECT x, y, w, h FROM faces WHERE source="camera" AND img=?',
                           (f'{trip}/display/{pid}.webp',)).fetchall()
    except (OSError, sqlite3.Error):
        return []
    return [tuple(int(v * k) for v in r) for r in rows]


# ── placement ────────────────────────────────────────────────────────────────────────────────────
def _busy(img, faces):
    import cv2
    g = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    mag = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    mean = cv2.blur(g, (15, 15))
    contrast = np.sqrt(np.maximum(cv2.blur(g * g, (15, 15)) - mean * mean, 0))
    b = cv2.GaussianBlur(mag + contrast * 2.0, (0, 0), 6)
    b = b / (np.percentile(b, 99) + 1e-6)
    for x, y, w, h in faces:                    # a face and the body under it: never under the label
        cv2.rectangle(b, (int(x - w), int(y - h)), (int(x + 2 * w), int(y + 4 * h)), 3.0, -1)
    return np.clip(b, 0, 3)


def place(img, box_w, box_h, faces=()):
    """→ (corner name, x, y): the calmest corner for the box, left corners first, top right last."""
    import cv2
    b = _busy(img, faces)
    H, W = b.shape
    m = int(W * 0.04)
    band = max(0, int((H - W * 1.25) / 2))      # taller than 4:5: stay inside its centre 4:5
    lab = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2LAB).astype(np.float32)
    best = None
    # The right side only wins on a much calmer patch, top right last: it sits under Instagram's carousel counter.
    for name, x, y, bias in (('top-left', m, band + m, 0.0), ('bottom-left', m, H - band - m - box_h, 0.15),
                             ('bottom-right', W - m - box_w, H - band - m - box_h, 0.15 + RIGHT_BIAS),
                             ('top-right', W - m - box_w, band + m, 0.3 + RIGHT_BIAS)):
        x, y = max(0, x), max(0, y)
        region = b[y:y + box_h, x:x + box_w]
        if not region.size:
            continue
        spread = float(lab[y:y + box_h, x:x + box_w].reshape(-1, 3).std(axis=0).mean()) / 40
        score = float(region.mean() + region.max() * 0.15 + spread + bias)
        if best is None or score < best[0]:
            best = (score, name, x, y)
    return best[1:]


# ── drawing ──────────────────────────────────────────────────────────────────────────────────────
def _pin(size, colour):
    s = size * 4
    im = Image.new('L', (s, int(s * 1.35)), 0)
    d = ImageDraw.Draw(im)
    r = s // 2
    d.ellipse((0, 0, s, s), fill=255)
    d.polygon([(s * 0.14, r * 1.45), (s * 0.86, r * 1.45), (r, s * 1.33)], fill=255)
    hole = s * 0.19
    d.ellipse((r - hole, r - hole, r + hole, r + hole), fill=0)
    im = im.resize((size, int(size * 1.35)), Image.LANCZOS)
    out = Image.new('RGBA', im.size, colour + (0,))
    out.putalpha(im)
    return out


def _spaced_w(font, text, spacing):
    return sum(font.getlength(ch) for ch in text) + spacing * max(len(text) - 1, 0)


def _draw_spaced(d, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + spacing


class _Layout:
    """A style's measurements at a given frame width, and how to draw it at (x, y)."""

    def __init__(self, style, head, sub, unit):
        self.style, self.head, self.sub, self.u = style, head, sub or '', unit
        u = unit
        if style == 'C':
            self.f1 = ImageFont.truetype(FUT_BOLD, max(8, int(u * 0.026)))
            self.f2 = ImageFont.truetype(FUT_BOOK, max(8, int(u * 0.0195)))
            self.sp1, self.pin = u * 0.005, max(6, int(u * 0.020))
            self.t1 = head.upper()
            self.w = int(max(_spaced_w(self.f1, self.t1, self.sp1) + self.pin + u * 0.014,
                             self.f2.getlength(self.sub)))
            self.h = int(self.f1.size * 1.1 + u * 0.016 + (self.f2.size * 1.2 if self.sub else 0))
        else:
            self.f1 = ImageFont.truetype(BEBAS, max(8, int(u * 0.050)))
            self.f2 = ImageFont.truetype(FUT_BOOK, max(8, int(u * 0.0195)))
            self.sp1, self.sp2, self.pin = u * 0.003, u * 0.0035, max(6, int(u * 0.030))
            self.t1, self.t2 = head.upper(), self.sub.upper()
            self.w = self.pin + int(u * 0.013) + int(max(_spaced_w(self.f1, self.t1, self.sp1),
                                                         _spaced_w(self.f2, self.t2, self.sp2)))
            self.h = int(self.f1.size * 0.92 + (self.f2.size * 1.5 if self.sub else 0))

    def draw(self, layer, x, y, fg):
        d, u = ImageDraw.Draw(layer), self.u
        if self.style == 'C':
            layer.alpha_composite(_pin(self.pin, fg), (x, y + int(self.f1.size * 0.05)))
            _draw_spaced(d, (x + self.pin + int(u * 0.014), y), self.t1, self.f1, fg + (255,), self.sp1)
            ly = y + int(self.f1.size * 1.1 + u * 0.006)
            d.line((x, ly, x + self.w, ly), fill=fg + (200,), width=max(1, int(u / 540)))
            if self.sub:
                d.text((x, ly + int(u * 0.010)), self.sub, font=self.f2, fill=fg + (230,))
        else:
            layer.alpha_composite(_pin(self.pin, fg), (x, y + int(self.f1.size * 0.08)))
            _draw_spaced(d, (x + self.pin + int(u * 0.013), y - int(self.f1.size * 0.08)), self.t1, self.f1,
                         fg + (255,), self.sp1)
            if self.t2:
                _draw_spaced(d, (x + self.pin + int(u * 0.0145), y + int(self.f1.size * 0.95)), self.t2, self.f2,
                             fg + (235,), self.sp2)

    def shadow(self):
        return (self.u * (7 if self.style == 'C' else 10) / 1080, 0.5 if self.style == 'C' else 0.55)


def render(img, head, sub, style='A', faces=()):
    """→ (labelled RGB image, corner name). `faces` in img's own pixels."""
    style = style if style in STYLES else 'A'
    img = img.convert('RGB')
    W, H = img.size
    k = WORK_W / W
    work = img.resize((WORK_W, max(1, int(H * k))), Image.LANCZOS)
    small = _Layout(style, head, sub, WORK_W)
    corner, x, y = place(work, small.w, small.h, [tuple(int(v * k) for v in f) for f in faces])
    big = _Layout(style, head, sub, W)
    X, Y = int(x / k), int(y / k)
    region = np.asarray(img.crop((X, Y, X + big.w, Y + big.h)).convert('L'), dtype=np.float32) / 255
    fg, sh = ((20, 20, 20), (255, 255, 255)) if region.size and region.mean() > 0.68 else ((255, 255, 255), (0, 0, 0))
    base = img.convert('RGBA')
    layer = Image.new('RGBA', base.size, (0, 0, 0, 0))
    big.draw(layer, X, Y, fg)
    radius, opacity = big.shadow()
    alpha = layer.split()[-1].filter(ImageFilter.GaussianBlur(radius))
    shadow = Image.new('RGBA', layer.size, sh + (0,))
    shadow.putalpha(alpha.point(lambda a: int(a * opacity)))
    base.alpha_composite(shadow)
    base.alpha_composite(layer)
    return base.convert('RGB'), corner


def stamp(src, dst, head, sub, style='A', trip=None, pid=None):
    """Label the photo at src into dst (may be the same file), keeping its EXIF and colour profile.
    → the corner it went in."""
    with Image.open(src) as im:
        exif, icc = im.info.get('exif'), im.info.get('icc_profile')
        img = ImageOps.exif_transpose(im).convert('RGB')
    faces = face_boxes(trip, pid, img.size[0]) if trip and pid else []
    out, corner = render(img, head, sub, style, faces)
    kw = {'quality': 95, 'subsampling': 0}
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
    out.save(dst, 'JPEG', **kw)
    return corner


def main(argv):
    ap = argparse.ArgumentParser(description='Preview the location label on one photo.')
    ap.add_argument('--trip', required=True)
    ap.add_argument('--id', required=True)
    ap.add_argument('--style', default='A', choices=STYLES)
    ap.add_argument('--text', help='"Name" or "Name / Region" instead of the automatic one')
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    head, sub = label_for(a.trip, a.id)
    if a.text:
        head, _, given = a.text.partition(' / ')
        sub = given if given else sub
    src = ROOT / 'web' / 'trips' / a.trip / 'display' / f'{a.id}.webp'
    corner = stamp(src, a.out, head, sub, a.style, a.trip, a.id)
    print(f'{head} / {sub} → {corner}: {a.out}')


if __name__ == '__main__':
    main(sys.argv[1:])
