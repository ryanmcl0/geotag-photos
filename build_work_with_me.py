#!/usr/bin/env python3
"""Build web/work-with-me.html: the Work With Me page.

The page body is templates/work-with-me.html. This adds the site head, nav and footer,
the coverflow picks from config/work_with_me.json ({"reel": [{"key", "caption"}]}) and
an SVG map of the China road trips with a pin for every city visited.

Everything on the page must already be public, so this checks every photo key (the
reel picks and every data-key in the template) with the same rules as the /photos
proxy and against the trip's public manifest. A reel pick that fails is dropped with
a warning; a template photo that fails stops the build. The map uses only public
trips' route files, public manifests and the public coverage layer.

Run by deploy.py; also runnable on its own: python3 build_work_with_me.py
"""
import html
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
WEB = REPO / 'web'
TRIPS = WEB / 'trips'
TEMPLATE = REPO / 'templates' / 'work-with-me.html'
CONFIG = REPO / 'config' / 'work_with_me.json'
OUT = WEB / 'work-with-me.html'

TITLE = "Work With Me · Ryan's Travels"
DESCRIPTION = ('Photos of your brand in the most incredible parts of the world: '
               'commissioned product shoots and sponsorships on expeditions across China, '
               'Central Asia and beyond.')
SHARE_IMAGE = 'https://ryanmcl.com/previews/work-with-me.jpg'


# ---------------------------------------------------------------- privacy
class PublicCheck:
    """Mirror of functions/photos/[[path]].ts: blocked never serves, force_public
    always does, otherwise a private trip or a listed private photo is private. On top
    of that the photo must be in the trip's public manifest."""

    def __init__(self):
        idx = json.loads((REPO / 'functions/photos/private_index.json').read_text())
        self.private_trips = set(idx.get('private_trips') or [])
        self.private = {k: set(v) for k, v in (idx.get('private_photos') or {}).items()}
        self.blocked = {k: set(v) for k, v in (idx.get('blocked_photos') or {}).items()}
        self.forced = {k: set(v) for k, v in (idx.get('force_public') or {}).items()}
        trips = json.loads((TRIPS / 'index.json').read_text())['trips']
        self.public_trips = {t['id'] for t in trips if t.get('public') is not False and not t.get('pending')}
        self._manifest = {}

    def manifest_ids(self, slug):
        if slug not in self._manifest:
            mp = TRIPS / slug / 'manifest.json'
            ids = set()
            if mp.exists():
                ids = {p.get('id') for p in json.loads(mp.read_text()).get('photos', [])}
            self._manifest[slug] = ids
        return self._manifest[slug]

    def why_not(self, key):
        """None when the key is public, else the reason it isn't."""
        parts = key.split('/')
        if len(parts) != 3 or parts[1] != 'display' or not parts[2].endswith('.webp'):
            return 'not a <slug>/display/<id>.webp key'
        slug, _, name = parts
        stem = name[:-5]
        if stem in self.blocked.get(slug, ()):
            return 'blocked'
        if slug not in self.public_trips:
            return 'trip not public'
        if stem not in self.forced.get(slug, ()) and (
                slug in self.private_trips or stem in self.private.get(slug, ())):
            return 'private'
        if stem not in self.manifest_ids(slug):
            return 'not in the public manifest'
        if not (TRIPS / key).exists():
            return 'file missing'
        return None


# ---------------------------------------------------------------- China map
def _route_lines(slug):
    rp = TRIPS / slug / 'route.geojson'
    if not rp.exists():
        return
    for f in json.loads(rp.read_text()).get('features', []):
        g = f.get('geometry') or {}
        if g.get('type') == 'LineString':
            yield g['coordinates']
        elif g.get('type') == 'MultiLineString':
            yield from g['coordinates']


def _rdp(p, eps):
    if len(p) < 3:
        return p
    (x1, y1), (x2, y2) = p[0], p[-1]
    dx, dy = x2 - x1, y2 - y1
    n = math.hypot(dx, dy)
    best, idx = -1.0, 0
    for i in range(1, len(p) - 1):
        x0, y0 = p[i]
        dd = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / n if n > 1e-12 else math.hypot(x0 - x1, y0 - y1)
        if dd > best:
            best, idx = dd, i
    if best > eps:
        return _rdp(p[:idx + 1], eps)[:-1] + _rdp(p[idx:], eps)
    return [p[0], p[-1]]


def china_svg(check, echo=print):
    """Routes of the public China road trips + one pin per mainland city visited.
    No national outline, so nothing disputed is drawn."""
    sys.path.insert(0, str(REPO))
    import place_names

    trips = json.loads((TRIPS / 'index.json').read_text())['trips']
    china = [t['id'] for t in trips
             if t['id'] in check.public_trips and 'CN' in (t.get('countries') or [])
             and not t['id'].startswith('phone-')]

    K = math.cos(math.radians(34))
    proj = lambda lon, lat: (lon * K, -lat)
    segs = []
    for slug in china:
        for line in _route_lines(slug):
            seg, prev = [], None
            for lon, lat, *_ in line:
                if prev and (abs(lon - prev[0]) > 1.2 or abs(lat - prev[1]) > 1.2):
                    if len(seg) > 1:
                        segs.append(seg)
                    seg = []
                seg.append(proj(lon, lat))
                prev = (lon, lat)
            if len(seg) > 1:
                segs.append(seg)

    # Places: public photos of public trips + the public coverage layer (~1 km).
    pts = set()
    for t in trips:
        if t['id'] not in check.public_trips or t['id'].startswith('phone-'):
            continue
        mp = TRIPS / t['id'] / 'manifest.json'
        if not mp.exists():
            continue
        for p in json.loads(mp.read_text()).get('photos', []):
            if p.get('lat') is not None and p.get('lon') is not None:
                pts.add((round(p['lat'], 2), round(p['lon'], 2)))
    cov = TRIPS / 'private_coverage.json'
    if cov.exists():
        for t in json.loads(cov.read_text()).get('trips', []):
            for p in t.get('points', []):
                pts.add((round(p['lat'], 2), round(p['lon'], 2)))

    pi = place_names.PlaceIndex()
    cities = {}
    for lat, lon in pts:
        lk = pi.lookup(lat, lon)
        if not lk or not lk['city_key'].startswith('cn-') or lk['city_key'][3:5] in ('71', '81', '82'):
            continue
        c = cities.setdefault(lk['city_key'], [0, 0.0, 0.0])
        c[0] += 1; c[1] += lat; c[2] += lon
    pins = [proj(lo / n, la / n) for n, la, lo in cities.values()]

    xs = [x for s in segs for x, _ in s] + [x for x, _ in pins]
    ys = [y for s in segs for _, y in s] + [y for _, y in pins]
    if not xs:
        return ''
    minx, maxx, miny, maxy = min(xs) - 1.6, max(xs) + 1.6, min(ys) - 1.4, max(ys) + 1.4
    W = 1000.0
    S = W / (maxx - minx)
    H = (maxy - miny) * S
    P = lambda x, y: ((x - minx) * S, (y - miny) * S)

    CITY = [('Kashgar', 75.99, 39.47), ('Urumqi', 87.62, 43.83), ('Hotan', 79.92, 37.11), ('Golmud', 94.90, 36.40),
            ('Xining', 101.78, 36.62), ('Lanzhou', 103.83, 36.06), ('Guiyang', 106.63, 26.65), ('Xi’an', 108.94, 34.34),
            ('Yinchuan', 106.23, 38.49), ('Dunhuang', 94.66, 40.14), ('Beijing', 116.40, 39.90), ('Shanghai', 121.47, 31.23),
            ('Chengdu', 104.07, 30.67), ('Chongqing', 106.55, 29.56), ('Shenzhen', 114.06, 22.54), ('Lhasa', 91.13, 29.65)]
    EXP = [('Tasha Gudao', 76.3, 37.9, 'middle', 0, 40), ('Hoh Xil', 93.3, 35.1, 'start', 16, 34),
           ('The highest bridges', 105.2, 26.4, 'end', -14, 34)]
    near_route = lambda cx, cy: min(math.hypot(cx - x, cy - y) for s in segs for x, y in s[::5]) < 0.45
    near_pin = lambda cx, cy: bool(pins) and min(math.hypot(cx - x, cy - y) for x, y in pins) < 0.9
    labels = ''
    for name, lon, lat in CITY:
        cx, cy = proj(lon, lat)
        if near_route(cx, cy) or near_pin(cx, cy):
            x, y = P(cx, cy)
            labels += f'<text x="{x + 8:.1f}" y="{y + 5:.1f}">{html.escape(name)}</text>'
    exp = ''
    for name, lon, lat, anc, dx, dy in EXP:
        x, y = P(*proj(lon, lat))
        exp += (f'<g class="exp"><circle cx="{x:.1f}" cy="{y:.1f}" r="6"/><line x1="{x:.1f}" y1="{y:.1f}" '
                f'x2="{x + dx:.1f}" y2="{y + dy:.1f}"/><text x="{x + dx:.1f}" y="{y + dy + 16:.1f}" '
                f'text-anchor="{anc}">{html.escape(name)}</text></g>')
    route_d = ''.join('M' + ' L'.join(f'{(x - minx) * S:.1f},{(y - miny) * S:.1f}' for x, y in _rdp(s, 0.012))
                      for s in segs)
    pin_svg = ''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4"/>' for x, y in (P(*p) for p in pins))
    echo(f"    China map: {len(china)} trips, {len(segs)} route segments, {len(pins)} cities")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" role="img" '
            f'aria-label="Roads driven and cities visited in China">'
            f'<path class="route" d="{route_d}"/><g class="pins">{pin_svg}</g>'
            f'<g class="labels">{labels}</g><g class="exps">{exp}</g></svg>')


# ---------------------------------------------------------------- page
def _nav():
    from build_blogs import NAV
    nav = NAV.format(blogs_active='')
    link = '<a href="/work-with-me.html">Work With Me</a>'
    if link in nav:
        nav = nav.replace(link, '<a href="/work-with-me.html" class="active">Work With Me</a>')
    return nav.replace('<button class="nav-more-toggle" type="button">',
                       '<button class="nav-more-toggle active" type="button">')


def build(echo=print):
    import re
    check = PublicCheck()
    body = TEMPLATE.read_text()
    if body.lstrip().startswith('<!--'):    # the template's own header comment
        body = body.split('-->', 1)[1]
    body = body.strip()

    # Sections parked in the template (kept for later, not on the site) are cut out
    # whole, so none of their markup or photos reach the published page.
    body, n_hidden = re.subn(r'[ \t]*<!-- hidden:start\b.*?<!-- hidden:end -->[ \t]*\n?', '',
                             body, flags=re.S)
    if '<!-- hidden:' in body:
        raise SystemExit('✗ Work With Me: unbalanced hidden:start / hidden:end markers in the template')
    if n_hidden:
        echo(f"    {n_hidden} hidden template block(s) left off the page")

    # Every photo hard-coded in the (visible) template must be public.
    bad = [(k, r) for k in re.findall(r'data-key="([^"]+)"', body) if (r := check.why_not(k))]
    if bad:
        raise SystemExit('✗ Work With Me: template photos that are not public: ' +
                         ', '.join(f'{k} ({r})' for k, r in bad))

    picks = []
    if CONFIG.exists():
        for p in json.loads(CONFIG.read_text()).get('reel', []):
            r = check.why_not(p.get('key', ''))
            if r:
                echo(f"    ⚠️  reel pick dropped, {r}: {p.get('key')}")
                continue
            picks.append({'key': p['key'], 'caption': p.get('caption', '')})
    else:
        echo(f"    ⚠️  {CONFIG.relative_to(REPO)} missing: the reel will be empty")

    if '{{CHINA_SVG}}' in body:
        body = body.replace('{{CHINA_SVG}}', china_svg(check, echo))
    reel_json = json.dumps(picks, ensure_ascii=False).replace('</', '<\\/')

    page = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <meta name="darkreader-lock">
    <script src="/js/i18n.js"></script>
    <title>{html.escape(TITLE)}</title>
    <meta name="description" content="{html.escape(DESCRIPTION)}">
    <meta property="og:type" content="website">
    <meta property="og:title" content="Work With Me · Ryan McLoughlin">
    <meta property="og:description" content="{html.escape(DESCRIPTION)}">
    <meta property="og:image" content="{SHARE_IMAGE}">
    <meta property="og:url" content="https://ryanmcl.com/work-with-me">
    <meta name="twitter:card" content="summary_large_image">
    <link rel="icon" type="image/svg+xml" href="/icons/favicon.svg">
    <link rel="stylesheet" href="/css/site.css"/>
    <link rel="stylesheet" href="/css/work-with-me.css"/>
    <script defer src="/js/analytics.js"></script>
    <script defer src="/js/photo-guard.js"></script>
</head>
<body>
{_nav()}

{body}

    <footer class="site-copyright">&copy; <span class="copyright-year">2026</span> Ryan McLoughlin. All photos and content are copyrighted. Please <a href="mailto:ryanmcloughlin20@gmail.com">contact me</a> before using any content.</footer>

    <script type="application/json" id="wwm-reel">{reel_json}</script>
    <script src="/js/unlock.js"></script>
    <script src="/js/work-with-me.js"></script>
</body>
</html>
'''
    OUT.write_text(page)
    echo(f"    ✓ {OUT.relative_to(REPO)}: {len(picks)} reel photos, {len(page) // 1024} KB")


if __name__ == '__main__':
    build()
