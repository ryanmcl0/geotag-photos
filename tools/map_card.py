#!/usr/bin/env python3
"""Location context cards for post drafts.

A lot of the photos are unremarkable on their own and the location is the
story, so a post can carry a map slide showing where the shot was taken.
Three styles, picked per photo with the map button on the posts page:

  route   the trip's GPX track through the frame, with the photo point on it
  pin     the photo pinned to the spot with a leader line
  china   the pin, plus a nested locator framed to the whole country

Place names come from Esri's World Boundaries and Places reference layer over
the imagery (the same overlay the site map uses): towns, regions and borders,
denser the closer the card. Nothing else is written: no coordinates, no scale
bar, no attribution strip.

Cards render at whatever shape the post is (post.py works that out), so the
layout is derived from the frame rather than fixed pixel positions, and the
marker is always projected from the real coordinate rather than pinned to the
middle of the frame.

Imagery is Esri World Imagery, the same tiles the site map uses, with Esri
World Hillshade multiplied over the nested locator to give the terrain depth.
Tiles are cached under .tilecache/ so re-pulling a post costs nothing.

Usable on its own to preview a card:

  python3 tools/map_card.py --trip 2026-china-cny --id RM103838 \
      --style china --size 1080x1350 --out /tmp/card.jpg
"""

import io
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_TRIPS = ROOT / 'web' / 'trips'
CHINA_GEOJSON = ROOT / 'config' / 'geo' / 'china_provinces.geojson'
TILE_CACHE = ROOT / '.tilecache'

ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/{svc}/MapServer/tile/{z}/{y}/{x}'
IMAGERY = ESRI.format(svc='World_Imagery', z='{z}', y='{y}', x='{x}')
HILLSHADE = ESRI.format(svc='Elevation/World_Hillshade', z='{z}', y='{y}', x='{x}')
LABELS = ESRI.format(svc='Reference/World_Boundaries_and_Places', z='{z}', y='{y}', x='{x}')
# Bumped whenever cards change look, so post.py re-renders the ones already pulled.
CARD_VERSION = 2   # 2: place names

STYLES = ('route', 'pin', 'china')

# How much ground the close map covers, across the long edge of the card.
CLOSE_SPAN_M = 12000
# Mainland China, trimmed in from the true extremes so the locator crops close
# to the country instead of framing a lot of sea. (south, west), (north, east)
CHINA_BOUNDS = ((20.4, 76.5), (50.8, 132.0))

TILE = 256
MAX_RENDER_PX = 4200      # ceiling on the supersampled canvas
UA = 'geotag-photos post map cards (personal, non-commercial)'


# ---------------------------------------------------------------- projection

def _world_px(lat, lon, zoom):
    """Web Mercator pixel coordinates at a (possibly fractional) zoom."""
    scale = TILE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    s = math.sin(math.radians(max(-85.05112878, min(85.05112878, lat))))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def _zoom_for_span(lat, width_px, span_m):
    """Zoom at which `span_m` metres of ground occupy `width_px` pixels."""
    mpp = span_m / float(width_px)
    return math.log2(156543.03392 * math.cos(math.radians(lat)) / mpp)


def _zoom_to_fit(bounds, size):
    """Largest zoom at which `bounds` still fits inside `size` (contain)."""
    (s, w), (n, e) = bounds
    x0, y0 = _world_px(n, w, 0)
    x1, y1 = _world_px(s, e, 0)
    bw, bh = max(1e-6, abs(x1 - x0)), max(1e-6, abs(y1 - y0))
    return min(math.log2(size[0] / bw), math.log2(size[1] / bh))


# ---------------------------------------------------------------- tiles

_session = None


def _http():
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
        _session.headers['User-Agent'] = UA
    return _session


def _tile(url_tmpl, z, x, y):
    """One 256px tile as a PIL image, from the disk cache when we have it. The
    reference (labels) layer is transparent: kept RGBA, cached as PNG."""
    from PIL import Image
    svc = re.search(r'services/(.+?)/MapServer', url_tmpl).group(1).replace('/', '_')
    clear = svc.startswith('Reference')
    path = TILE_CACHE / svc / str(z) / f'{x}_{y}.{"png" if clear else "jpg"}'
    mode = 'RGBA' if clear else 'RGB'
    if path.exists():
        try:
            return Image.open(path).convert(mode)
        except OSError:
            path.unlink(missing_ok=True)
    url = url_tmpl.format(z=z, x=x, y=y)
    r = _http().get(url, timeout=20)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear:
        img.save(path, 'PNG')
    else:
        img.save(path, 'JPEG', quality=92)
    return img


class View:
    """A map viewport: what is drawn, and where a coordinate lands in it.

    Tiles only exist at whole zooms, so a view whose natural zoom is fractional
    is rendered one zoom in and scaled down at the end. Everything (imagery,
    markers, the pinned photo) is drawn at that larger size and comes down
    together, which is also what antialiases the vector work.
    """

    def __init__(self, lat, lon, zoom, size):
        self.lat, self.lon = lat, lon
        self.out_w, self.out_h = int(size[0]), int(size[1])
        zoom = max(0.0, min(19.0, zoom))
        zi = min(19, math.ceil(zoom - 1e-9))
        scale = 2 ** (zi - zoom)
        # Keep the supersampled canvas sane on very wide cards.
        while zi > 0 and max(self.out_w, self.out_h) * scale > MAX_RENDER_PX:
            zi -= 1
            scale = 2 ** (zi - zoom)
        self.zi = max(0, zi)
        self.scale = scale
        self.w = max(1, int(round(self.out_w * scale)))
        self.h = max(1, int(round(self.out_h * scale)))
        cx, cy = _world_px(lat, lon, self.zi)
        self.left = cx - self.w / 2.0
        self.top = cy - self.h / 2.0

    def project(self, lat, lon):
        """Coordinate to pixel, in this view's (supersampled) space."""
        x, y = _world_px(lat, lon, self.zi)
        return x - self.left, y - self.top

    def base(self, hillshade=False):
        from PIL import Image, ImageChops
        img = self._mosaic(IMAGERY)
        if hillshade:
            shade = self._mosaic(HILLSHADE)
            img = Image.blend(img, ImageChops.multiply(img, shade), 0.5)
        return img

    def _mosaic(self, url_tmpl, zi=None, clear=False):
        """The tiles covering the view at zoom zi (default: the view's own), on a canvas at that zoom's
        scale; `clear` keeps a transparent layer transparent."""
        from PIL import Image
        zi = self.zi if zi is None else zi
        k = 2 ** (zi - self.zi)
        left, top, w, h = self.left * k, self.top * k, max(1, int(self.w * k)), max(1, int(self.h * k))
        canvas = (Image.new('RGBA', (w, h), (0, 0, 0, 0)) if clear
                  else Image.new('RGB', (w, h), (17, 17, 17)))
        span = 2 ** zi
        tx0, ty0 = math.floor(left / TILE), math.floor(top / TILE)
        tx1 = math.floor((left + w - 1) / TILE)
        ty1 = math.floor((top + h - 1) / TILE)
        for ty in range(ty0, ty1 + 1):
            if ty < 0 or ty >= span:
                continue
            for tx in range(tx0, tx1 + 1):
                try:
                    tile = _tile(url_tmpl, zi, tx % span, ty)
                except Exception as e:            # one dead tile must not kill the card
                    print(f'   ⚠️  tile {zi}/{tx}/{ty} failed: {e}')
                    continue
                at = (int(round(tx * TILE - left)), int(round(ty * TILE - top)))
                if clear:
                    canvas.alpha_composite(tile, at)
                else:
                    canvas.paste(tile, at)
        return canvas

    def labels(self):
        """The place-name layer for this view, transparent. Drawn from one zoom further out than the imagery
        and enlarged to it: the canvas is later shrunk by up to 2x, which would leave the labels unreadably
        small, and one zoom out also keeps them to the names that matter at this scale."""
        from PIL import Image
        zi = max(0, self.zi - 1)
        layer = self._mosaic(LABELS, zi=zi, clear=True)
        return layer.resize((self.w, self.h), Image.LANCZOS)


# ---------------------------------------------------------------- drawing

def _shadow(img, box, radius, blur, alpha=140):
    """Soft drop shadow under a rectangle, composited in place."""
    from PIL import Image, ImageDraw, ImageFilter
    pad = blur * 3
    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0 - 1, y0 + pad // 3, x1 + 1, y1 + pad // 3),
                        radius=radius, fill=(0, 0, 0, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(layer)


def _dot(draw, x, y, r, ring=0, w=2):
    """The photo's position: white dot, dark halo, optional ring."""
    draw.ellipse((x - r - w, y - r - w, x + r + w, y + r + w), fill=(0, 0, 0, 105))
    draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 255))
    if ring:
        draw.ellipse((x - ring, y - ring, x + ring, y + ring),
                     outline=(255, 255, 255, 225), width=max(1, int(round(w))))


def _leader(draw, box, x, y, w):
    """Line from the nearest corner or edge of the photo card to the point."""
    bx0, by0, bx1, by1 = box
    if bx0 < x < bx1 and by0 < y < by1:
        return
    ax = bx0 if x < bx0 else (bx1 if x > bx1 else x)
    ay = by0 if y < by0 else (by1 if y > by1 else y)
    dx, dy = x - ax, y - ay
    length = math.hypot(dx, dy)
    gap = w * 7
    if length < gap * 2:
        return
    k = (length - gap) / length
    draw.line((ax, ay, ax + dx * k, ay + dy * k), fill=(255, 255, 255, 235),
              width=max(1, int(round(w))))


def _paste_card(img, photo_path, box, border, radius):
    """The photo itself, as a white-bordered print pinned to the map."""
    from PIL import Image
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    _shadow(img, (x0, y0, x1, y1), radius, max(2, int(border * 1.6)))
    frame = Image.new('RGBA', (x1 - x0, y1 - y0), (255, 255, 255, 255))
    inner = (x1 - x0 - 2 * border, y1 - y0 - 2 * border)
    if inner[0] > 0 and inner[1] > 0:
        photo = Image.open(photo_path).convert('RGB')
        photo = photo.resize(inner, Image.LANCZOS)
        frame.paste(photo, (border, border))
    img.alpha_composite(frame, (x0, y0))


# ---------------------------------------------------------------- locator

def _china_rings(view, path=CHINA_GEOJSON):
    """Province outlines projected into `view`, thinned to the pixel grid."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        print(f'   ⚠️  province geometry unavailable ({e}); locator drawn bare')
        return []
    rings = []
    for feat in data.get('features', []):
        geom = feat.get('geometry') or {}
        polys = (geom.get('coordinates') or []) if geom.get('type') == 'MultiPolygon' \
            else [geom.get('coordinates') or []]
        for poly in polys:
            for ring in poly:
                pts, last = [], None
                for lon, lat in ring:
                    p = view.project(lat, lon)
                    if last is None or abs(p[0] - last[0]) >= 1 or abs(p[1] - last[1]) >= 1:
                        pts.append(p)
                        last = p
                if len(pts) >= 3:
                    rings.append(pts)
    return rings


def _locator(lat, lon, size, provinces=True):
    """The nested wide view: the country (or a wide region) with the point on it."""
    from PIL import Image, ImageDraw
    (s, w), (n, e) = CHINA_BOUNDS
    inside = s <= lat <= n and w <= lon <= e
    if provinces and inside:
        bounds = CHINA_BOUNDS
    else:
        # Outside China: a wide regional frame around the point instead.
        bounds = ((lat - 7.5, lon - 9.5), (lat + 7.5, lon + 9.5))
        provinces = False
    ctr = ((bounds[0][0] + bounds[1][0]) / 2.0, (bounds[0][1] + bounds[1][1]) / 2.0)
    view = View(ctr[0], ctr[1], _zoom_to_fit(bounds, size), size)
    img = view.base(hillshade=True).convert('RGBA')
    over = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    unit = max(1.0, min(img.size) / 150.0)
    if provinces:
        for ring in _china_rings(view):
            draw.polygon(ring, fill=(255, 255, 255, 18))
        for ring in _china_rings(view):
            draw.line(ring + [ring[0]], fill=(255, 255, 255, 128),
                      width=max(1, int(round(unit * 0.8))))
    x, y = view.project(lat, lon)
    _dot(draw, x, y, unit * 2.4, ring=unit * 5.6, w=unit)
    img.alpha_composite(over)
    return img.resize((view.out_w, view.out_h), Image.LANCZOS)


# ---------------------------------------------------------------- the card

def layout(w, h, photo_ar, inset_ar=0.72):
    """Where the furniture sits, in output pixels.

    Both nested boxes are sized from the frame HEIGHT with width only as a cap,
    so a portrait photo and a landscape photo carry the same visual weight. Size
    them by width alone and a 9:16 frame swallows half the card.
    """
    m = round(min(w, h) * 0.045)
    tw = round(min(w * 0.42, h * 0.30 * photo_ar))
    iw = round(min(w * 0.50, h * 0.34 / inset_ar))
    return {
        'margin': m,
        'thumb': {'x': round(w * 0.06), 'y': round(h * 0.13), 'w': tw,
                  'h': max(1, round(tw / photo_ar))},
        'inset': {'w': iw, 'h': round(iw * inset_ar),
                  'x': w - m - iw, 'y': h - m - round(iw * inset_ar)},
    }


def render_card(style, size, *, lat, lon, photo=None, photo_ar=1.5,
                route=None, close_span_m=CLOSE_SPAN_M):
    """One finished card. `size` is (width, height) in output pixels."""
    from PIL import Image, ImageDraw
    if style not in STYLES:
        raise ValueError(f'unknown map style: {style}')
    w, h = int(size[0]), int(size[1])
    view = View(lat, lon, _zoom_for_span(lat, max(w, h), close_span_m), (w, h))
    img = view.base().convert('RGBA')
    names = view.labels()
    sparse = _sparse(names)
    if not sparse:
        img.alpha_composite(names)
    s = view.scale                                    # output px -> render px
    unit = max(1.0, min(w, h) * s / 360.0)            # the mockup's 360px yardstick
    over = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)

    if style == 'route' and route:
        for seg in route:
            pts, last = [], None
            for plat, plon in seg:
                p = view.project(plat, plon)
                if last is None or abs(p[0] - last[0]) >= 1 or abs(p[1] - last[1]) >= 1:
                    pts.append(p)
                    last = p
            if len(pts) < 2:
                continue
            draw.line(pts, fill=(0, 0, 0, 115), width=max(2, int(round(unit * 3.0))),
                      joint='curve')
            draw.line(pts, fill=(255, 209, 102, 242), width=max(1, int(round(unit * 1.5))),
                      joint='curve')

    px, py = view.project(lat, lon)
    lay = layout(w, h, photo_ar)

    if style in ('pin', 'china') and photo:
        t = lay['thumb']
        border = max(2, int(round(unit * 5)))
        box = (t['x'] * s, t['y'] * s, (t['x'] + t['w']) * s, (t['y'] + t['h']) * s)
        _leader(draw, box, px, py, unit * 1.6)
        img.alpha_composite(over)
        over = Image.new('RGBA', img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(over)
        _paste_card(img, photo, box, border, max(2, int(round(unit * 2))))

    _dot(draw, px, py, unit * 5, ring=(unit * 13 if style == 'route' else 0), w=unit * 1.4)
    img.alpha_composite(over)
    out = img.resize((w, h), Image.LANCZOS).convert('RGB')
    if sparse:
        mx, my = px / s, py / s
        avoid = [(mx - unit * 16 / s, my - unit * 16 / s, mx + unit * 16 / s, my + unit * 16 / s)]
        if style in ('pin', 'china') and photo:
            t = lay['thumb']
            avoid.append((t['x'], t['y'], t['x'] + t['w'], t['y'] + t['h']))
        if style == 'china':
            ib = lay['inset']
            avoid.append((ib['x'], ib['y'], ib['x'] + ib['w'], ib['y'] + ib['h']))
        draw_towns(out, view, avoid)

    if style == 'china':
        box = lay['inset']
        loc = _locator(lat, lon, (box['w'], box['h']))
        out = out.convert('RGBA')
        border = max(1, round(min(w, h) * 0.004))
        frame = Image.new('RGBA', (box['w'] + 2 * border, box['h'] + 2 * border),
                          (255, 255, 255, 255))
        frame.paste(loc.convert('RGB'), (border, border))
        _shadow(out, (box['x'] - border, box['y'] - border,
                      box['x'] + box['w'] + border, box['y'] + box['h'] + border),
                border * 2, max(2, round(min(w, h) * 0.012)))
        out.alpha_composite(frame, (box['x'] - border, box['y'] - border))
        out = out.convert('RGB')
    return out



# ---------------------------------------------------------------- town names

FUTURA_BOLD = ROOT / 'web' / 'fonts' / 'FuturaPT-Bold.ttf'
_places = None


def _sparse(layer, floor=0.0015):
    """True when a label layer has next to nothing on it (Esri names little in rural China up close)."""
    import numpy as np
    a = np.asarray(layer.split()[-1])
    return (a > 40).mean() < floor


def draw_towns(img, view, avoid=(), max_n=8):
    """Town names from GeoNames (place_names.py) on a finished card, for where the reference layer is empty:
    the biggest towns in view first, a small dot and the name in Futura with a dark edge, each placed right of
    its dot (left when that runs off the card) and skipped when it would touch another name or a box in
    `avoid` (output pixels: the photo card, the marker)."""
    global _places
    from PIL import ImageDraw, ImageFont
    import sys as _sys
    if _places is None:
        _sys.path.insert(0, str(ROOT))
        from place_names import PlaceIndex
        _places = PlaceIndex()
    w, h = img.size
    k = 1 / view.scale

    def inv(x, y):            # output pixel -> lat, lon
        zx = (x / k + view.left) / (TILE * 2 ** view.zi)
        zy = (y / k + view.top) / (TILE * 2 ** view.zi)
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * zy)))), zx * 360.0 - 180.0
    north, west = inv(0, 0)
    south, east = inv(w, h)
    font = ImageFont.truetype(str(FUTURA_BOLD), max(12, int(min(w, h) * 0.028)))
    draw = ImageDraw.Draw(img)
    taken = [tuple(b) for b in avoid]
    pad = font.size * 0.3
    placed = 0
    for name, lat, lon, _ in _places.towns_in(south, west, north, east):
        x, y = view.project(lat, lon)
        x, y = x * k, y * k
        tw = draw.textlength(name, font=font)
        r = max(2, font.size * 0.18)
        for tx in (x + r * 2.5, x - r * 2.5 - tw):
            box = (tx - pad, y - font.size * 0.62 - pad, tx + tw + pad, y + font.size * 0.62 + pad)
            if box[0] < 0 or box[2] > w or box[1] < 0 or box[3] > h:
                continue
            if any(not (box[2] < a[0] or box[0] > a[2] or box[3] < a[1] or box[1] > a[3]) for a in taken):
                continue
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255), outline=(0, 0, 0), width=1)
            draw.text((tx, y), name, font=font, anchor='lm', fill=(255, 255, 255),
                      stroke_width=max(2, font.size // 9), stroke_fill=(20, 20, 20))
            taken.append(box)
            placed += 1
            break
        if placed >= max_n:
            break
    return placed

# ---------------------------------------------------------------- trip data

def photo_context(trip, photo_id, photos=None):
    """(record, display image path, route segments) for one trip photo."""
    if photos is None:
        photos = {}
        for name in ('manifest.json', 'manifest.all.json'):
            p = WEB_TRIPS / trip / name
            if p.exists():
                for ph in json.loads(p.read_text()).get('photos', []):
                    photos.setdefault(ph['id'], ph)
    ph = photos.get(photo_id) if isinstance(photos, dict) else None
    if not ph:
        return None, None, None
    img = None
    for key in ('display', 'thumbnail'):
        rel = ph.get(key)
        if rel and (WEB_TRIPS / trip / rel).exists():
            img = WEB_TRIPS / trip / rel
            break
    return ph, img, route_segments(trip)


def route_segments(trip):
    """The trip's GPX track as [[(lat, lon), ...], ...], or None."""
    path = WEB_TRIPS / trip / 'route.geojson'
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    segs = []
    for feat in data.get('features', []):
        geom = feat.get('geometry') or {}
        lines = ([geom.get('coordinates', [])] if geom.get('type') == 'LineString'
                 else geom.get('coordinates', []) if geom.get('type') == 'MultiLineString' else [])
        for line in lines:     # a route in parts (Mongolia's Gobi and Khövsgöl legs) is several lines
            segs.append([(c[1], c[0]) for c in line])
    return [s for s in segs if len(s) > 1] or None


def render_trip_route(trip, size, marks=()):
    """The trip's whole GPS route fitted into the frame, for a sense of where the trip went: the route in the
    route colour over the imagery (hillshade for relief), a white dot on each marked photo (marks: (lat, lon)).
    One card per trip, however many of its photos asked for it. Place names from the reference layer on top.
    → (image, why-not)."""
    from PIL import Image, ImageDraw
    segs = route_segments(trip)
    if not segs:
        return None, f'{trip} has no GPX route on record'
    w, h = int(size[0]), int(size[1])
    lats = [p[0] for s in segs for p in s]
    lons = [p[1] for s in segs for p in s]
    (s0, w0), (n0, e0) = (min(lats), min(lons)), (max(lats), max(lons))
    zoom = _zoom_to_fit(((s0, w0), (n0, e0)), (w * 0.84, h * 0.84))      # a margin all round
    zoom = min(zoom, 15.0)                                               # a short route stays a map
    x0, y0 = _world_px(n0, w0, 0)
    x1, y1 = _world_px(s0, e0, 0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2                                # the route's middle, in Mercator
    clon = cx / TILE * 360.0 - 180.0
    clat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy / TILE))))
    view = View(clat, clon, zoom, (w, h))
    img = view.base(hillshade=True).convert('RGBA')
    unit = max(1.0, min(w, h) * view.scale / 360.0)
    over = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    for seg in segs:
        pts, last = [], None
        for plat, plon in seg:
            q = view.project(plat, plon)
            if last is None or abs(q[0] - last[0]) >= 1 or abs(q[1] - last[1]) >= 1:
                pts.append(q)
                last = q
        if len(pts) < 2:
            continue
        draw.line(pts, fill=(0, 0, 0, 120), width=max(2, int(round(unit * 3.6))), joint='curve')
        draw.line(pts, fill=(255, 209, 102, 245), width=max(1, int(round(unit * 1.8))), joint='curve')
    img.alpha_composite(over)
    img.alpha_composite(view.labels())          # names stay readable over the route
    over = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    for mlat, mlon in marks:
        px, py = view.project(mlat, mlon)
        _dot(draw, px, py, unit * 4.5, w=unit * 1.4)
    img.alpha_composite(over)
    return img.resize((w, h), Image.LANCZOS).convert('RGB'), None


def card_for_photo(style, trip, photo_id, size, photos=None):
    """Render a card for a photo already on the site. Returns (image, why-not)."""
    ph, img, route = photo_context(trip, photo_id, photos)
    if ph is None:
        return None, f'{photo_id} not in {trip} manifests'
    if ph.get('lat') is None or ph.get('lon') is None:
        return None, f'{photo_id} has no coordinates'
    if style in ('pin', 'china') and img is None:
        return None, f'{photo_id} has no local display copy to pin'
    if style == 'route' and not route:
        return None, f'{trip} has no route.geojson'
    return render_card(style, size, lat=ph['lat'], lon=ph['lon'], photo=img,
                       photo_ar=ph.get('ar') or 1.5, route=route), None


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trip', required=True)
    ap.add_argument('--id', required=True, help='photo id as it appears in the manifest')
    ap.add_argument('--style', default='china', choices=STYLES)
    ap.add_argument('--size', default='1080x1350', help='WxH, default 1080x1350')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    m = re.fullmatch(r'(\d+)x(\d+)', args.size)
    if not m:
        sys.exit('❌ --size must look like 1080x1350')
    card, why = card_for_photo(args.style, args.trip, args.id,
                               (int(m.group(1)), int(m.group(2))))
    if card is None:
        sys.exit(f'❌ {why}')
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    card.save(out, quality=93)
    print(f'✓ {args.style} card → {out} ({card.width}x{card.height})')


if __name__ == '__main__':
    main()
