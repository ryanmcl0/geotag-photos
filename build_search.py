#!/usr/bin/env python3
"""
Build the site-wide search index behind search.html.

Writes two files, mirroring the public/private split the collections use:
  web/collections/search.json       public: only what a locked visitor can already see
  web/collections/search.all.json   everything, behind the See All gate (the middleware
                                    gates every collections/ file not on its public list)

The public file is built ONLY from public sources: public trips' filtered
manifest.json (never manifest.all.json), the public china.json / china.preview.json,
public blogs, and covers that are already public. Nothing in it is locked: tiles
the public pages show behind the password (private blogs, all-private province
tiles) are left out, so no result leads to the unlock prompt.

Each item is one thing on the site a search can land on: a trip (map + gallery), a
place on the map (a photo cluster), a China province / road leg / bridge / energy
site, an Urbex building, a blog or a video. Items carry the places they
are in (`p`: provinces, cities, regions; `cc`: country codes), so typing a place
finds everything filed there, not only things named after it.

Runs from build_collections.py and deploy.py; standalone: ./build_search.py
"""

import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import photo_privacy
from build_collections import PROVINCE_ZH_EN, ProvinceIndex

ROOT = Path(__file__).parent.resolve()
WEB = ROOT / 'web'
WEB_TRIPS = WEB / 'trips'
OUT_DIR = WEB / 'collections'
PRIVATE_INDEX = ROOT / 'functions' / 'photos' / 'private_index.json'
PROVINCE_GEOJSON = ROOT / 'config' / 'geo' / 'china_provinces.geojson'
PROVINCE_OVERRIDES = ROOT / 'config' / 'province_overrides.json'
# {focus: {photo id: {y: %}}}: hand-set place-banner crops (see the file's comment)
BANNER_FOCUS = ROOT / 'config' / 'banner_focus.json'

# Sections only the full index has. Their names live here, not in the public
# page script, so a locked visitor's download never mentions them.
GATED_SECTIONS = [
    {'k': 'roof', 'title': 'Urbex', 'unit': ['building', 'buildings'], 'grid': True},
    {'k': 'video', 'title': 'Videos', 'unit': ['video', 'videos'], 'grid': True},
]

# Cluster labels that name no place.
GENERIC_LABELS = {'drone', 'unknown', 'misc', 'phone', 'edits', 'other'}
DAY_PREFIX = re.compile(r'^\s*day\s*(\d+(?:\s*[&-]\s*\d+)?)\s*[:\-–]*\s*', re.I)
NAME_PREFIX = re.compile(r'^\d{4}[:\d]*\s+')
TAGS = re.compile(r'<[^>]+>')
ROAD_CHINA = ('CN', 'HK', 'MO')


def _load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _cover(c):
    """Tile cover in the compact shape the page renders ({trip,id} or {src})."""
    if not c:
        return None
    if c.get('src'):
        return {'src': c['src']}
    if c.get('trip') and c.get('id'):
        return {'trip': c['trip'], 'id': c['id']}
    return None


def _month_range(dates):
    try:
        a = date.fromisoformat(dates['start'][:10])
        b = date.fromisoformat(dates['end'][:10])
    except (KeyError, TypeError, ValueError):
        return ''
    if (a.year, a.month) == (b.year, b.month):
        return a.strftime('%b %Y')
    if a.year == b.year:
        return f"{a.strftime('%b')} - {b.strftime('%b %Y')}"
    return f"{a.strftime('%b %Y')} - {b.strftime('%b %Y')}"


def _top_places(counter, min_share=0.05, min_n=2):
    """Provinces holding a real share of the photos, biggest first."""
    total = sum(counter.values())
    return [p for p, n in counter.most_common()
            if n >= min_n and (n / total >= min_share or n >= 20)] if total else []


class Builder:
    def __init__(self, public: bool, echo):
        self.public = public
        self.echo = echo
        self.items = []
        self.access = _load(PRIVATE_INDEX, {}) or {}
        self.index = (_load(WEB_TRIPS / 'index.json', {}) or {}).get('trips', [])
        self.prov_index = ProvinceIndex(PROVINCE_GEOJSON)
        self.overrides = _load(PROVINCE_OVERRIDES, {}) or {}
        self.manifests = self._manifests()
        self.photo_prov = self._photo_provinces()

    # ------------------------------------------------------------ sources

    def trip_visible(self, t):
        if t.get('pending'):
            return False
        if not self.public:
            return True
        slug = t['id']
        return (t.get('public') is not False and not slug.endswith('-private')
                and slug not in (self.access.get('private_trips') or []))

    def _manifests(self):
        out = {}
        for t in self.index:
            if not self.trip_visible(t):
                continue
            d = WEB_TRIPS / t['id']
            # Public: the filtered manifest.json is exactly what the map serves a
            # locked visitor. Full: the canonical complete manifest.
            m = _load(d / 'manifest.json') if self.public else photo_privacy.load_full_manifest(d)
            if self.public and m and m.get('filtered') is None and (d / 'manifest.all.json').exists():
                # A full manifest.json next to an .all file means a split is mid-way;
                # stay closed rather than guess.
                m = None
            if m and m.get('photos'):
                out[t['id']] = m
        return out

    def _photo_provinces(self):
        """(trip, id) -> province, from each China cluster's centre (hand
        corrections from province_overrides.json win)."""
        out = {}
        for slug, m in self.manifests.items():
            fixes = self.overrides.get(slug) or {}
            for c in m.get('clusters', []):
                if c.get('country') not in ROAD_CHINA or c.get('lat') is None:
                    continue
                prov = self.prov_index.lookup(c['lat'], c['lon'])
                for pid in c.get('photo_ids') or []:
                    p = fixes.get(pid) or prov
                    if p:
                        out[(slug, pid)] = p
        return out

    def provinces_of(self, photos):
        return _top_places(Counter(self.photo_prov[(p['trip'], p['id'])] for p in photos or []
                                   if (p.get('trip'), p.get('id')) in self.photo_prov))

    def add(self, **item):
        self.items.append({k: v for k, v in item.items() if v not in (None, '', [], {})})

    # ------------------------------------------------------------ trips + map places

    def trips(self):
        regions = _load(OUT_DIR / ('gallery_regions.json' if self.public else 'gallery_regions.all.json'), {}) or {}
        if not self.public:
            regions = {**(_load(OUT_DIR / 'gallery_regions.json', {}) or {}), **regions}
        covers = _load(OUT_DIR / 'gallery_covers.json', {}) or {}
        n_places = 0
        for t in self.index:
            slug = t['id']
            m = self.manifests.get(slug)
            if not m:
                continue
            ids = {p['id'] for p in m['photos']}
            name = NAME_PREFIX.sub('', t.get('name') or slug)
            ym = re.match(r'^(\d{4})', t.get('name') or '')
            year = int(ym.group(1)) if ym else t.get('year')
            cover = covers.get(slug)
            if not cover or cover.get('id') not in ids:
                land = [p for p in m['photos'] if (p.get('ar') or 1.5) >= 1.3] or m['photos']
                cover = {'trip': slug, 'id': land[len(land) // 3]['id']}
            provs = Counter(self.photo_prov[(slug, p['id'])] for p in m['photos']
                            if (slug, p['id']) in self.photo_prov)
            region = regions.get(slug, '')
            self.add(k='trip', id=slug, t=name,
                     s=' · '.join(x for x in (_month_range(t.get('dates') or {}), region) if x),
                     u=f"gallery.html?trip={slug}&year={year}",
                     m=f"map.html?mode=trip&trip={slug}",
                     c=_cover(cover), cc=t.get('countries') or m.get('countries'),
                     p=_top_places(provs), y=[year] if year else None, n=len(ids))

            # Map places: the trip's photo clusters, one result per label.
            groups = defaultdict(list)
            for c in m.get('clusters', []):
                label = (c.get('location') or '').strip()
                if not label or label.lower() in GENERIC_LABELS or not re.search(r'[A-Za-z]', label):
                    continue
                groups[label].append(c)
            for label, cs in groups.items():
                cs.sort(key=lambda c: -len(c.get('photo_ids') or []))
                big = cs[0]
                pids = [i for c in cs for i in (c.get('photo_ids') or [])]
                if not pids:
                    continue
                day = DAY_PREFIX.match(label)
                title = DAY_PREFIX.sub('', label).strip() or label
                prov = Counter(self.photo_prov.get((slug, i)) for i in pids if (slug, i) in self.photo_prov)
                self.add(k='place', t=title,
                         s=' · '.join(x for x in (name, f"Day {day.group(1)}" if day else '') if x),
                         u=f"map.html?mode=trip&trip={slug}&at={big['lat']:.4f},{big['lon']:.4f}",
                         c={'trip': slug, 'id': big['photo_ids'][0]},
                         cc=sorted({c['country'] for c in cs if c.get('country')}),
                         p=[p for p, _ in prov.most_common(2)], y=[year] if year else None, n=len(pids))
                n_places += 1
        return n_places

    # ------------------------------------------------------------ China hub

    def china(self):
        data = _load(OUT_DIR / ('china.json' if self.public else 'china.all.json'), {}) or {}
        tiles = {t['id']: t for t in data.get('tiles', [])}
        if self.public:
            # Bridges: the public preview list (gallery-less), when it is built.
            prev = _load(OUT_DIR / 'china.preview.json', {}) or {}
            for t in prev.get('tiles', []):
                if t['id'] == 'bridges' and not t.get('locked'):
                    tiles['bridges'] = t
        subs = lambda t: (t.get('subtiles') or []) + [s for sec in (t.get('sections') or [])
                                                      for s in (sec.get('subtiles') or [])]
        visited = {}
        for fid, tile in tiles.items():
            # China · Architecture is a subset of Urbex (same buildings, same photos):
            # its buildings are searched once, under Urbex.
            if tile.get('locked') or fid == 'roofs':
                continue
            for s in subs(tile):
                photos = s.get('photos') or []
                n = s.get('count') or len(photos)
                # Locked tiles (all-private photos) and not-yet-visited ones have
                # nothing a search can open.
                if s.get('locked') or (not (s.get('done') or photos) and fid != 'bridges'):
                    continue
                if fid == 'provinces':
                    visited[s['title']] = s['id']
                    self.add(k='province', id=s['id'], t=s['title'], s='Province of China',
                             u=f"china.html#provinces/{s['id']}",
                             m=f"map.html?collection=china&facet=provinces&sub={s['id']}",
                             c=_cover(s.get('cover')), cc=['CN'], p=[s['title']], n=n,
                             y=s.get('years'))
                elif fid == 'roads':
                    self.add(k='road', t=s['title'], s=s.get('subtitle'),
                             u=f"china.html#roads/{s['id']}",
                             m=f"map.html?mode=trip&trip={s['trip']}" if s.get('trip') else None,
                             c=_cover(s.get('cover')), cc=['CN'], p=self.provinces_of(photos), n=n)
                elif fid == 'bridges':
                    st = s.get('status')
                    bits = [f"{s['height_m']} m" if s.get('height_m') else '', s.get('province'),
                            s.get('pending') or ('Under construction' if st == 'uc' else '')]
                    self.add(k='bridge', t=s['title'], s=' · '.join(b for b in bits if b),
                             x=s.get('name_zh'),
                             # The public preview opens as a list; a bridge deep link
                             # there asks for the password, so link the list.
                             u=f"china.html#bridges/{s['id']}" if photos and not self.public else 'china.html#bridges',
                             c=_cover(s.get('cover')), cc=['CN'], p=[s['province']] if s.get('province') else None,
                             n=n if (s.get('done') or photos) else None)
                elif fid == 'energy':
                    sec = next((x['title'] for x in tile.get('sections') or []
                                if s in (x.get('subtiles') or [])), 'Energy & Infrastructure')
                    self.add(k='energy', t=s['title'], s=sec, u=f"china.html#energy/{s['id']}",
                             c=_cover(s.get('cover')), cc=['CN'], p=self.provinces_of(photos), n=n,
                             y=s.get('years'))
        return visited

    # ------------------------------------------------------------ Urbex (gated)

    def urbex(self):
        data = _load(OUT_DIR / 'rooftopping.json', {}) or {}
        for tile in data.get('tiles', []):
            for sec in tile.get('sections') or []:
                for s in sec.get('subtiles') or []:
                    photos = s.get('photos') or []
                    place = [x for x in (s.get('city'), s.get('country')) if x]
                    self.add(k='roof', t=s['title'],
                             s=' · '.join(x for x in (s.get('height_label') or ('Underground' if s.get('underground') else ''),
                                                      s.get('city'), s.get('country')) if x),
                             u=f"rooftopping.html#{tile['id']}/{s['id']}", c=_cover(s.get('cover')),
                             p=sorted(set(place + self.provinces_of(photos))), n=s.get('count') or len(photos),
                             y=s.get('years'))

    # ------------------------------------------------------------ blogs + videos

    def blogs(self):
        for meta_path in sorted((WEB / 'blogs').glob('*.json')):
            meta = _load(meta_path, {}) or {}
            slug = meta.get('slug') or meta_path.stem
            html_path = meta_path.with_suffix('.html')
            if not html_path.exists():
                continue
            if self.public and meta.get('public') is not True:
                continue
            paras = []
            m = re.search(r'window\.BLOG = (\{.*?\});?\s*</script>', html_path.read_text(), re.S)
            blog = json.loads(m.group(1)) if m else {}
            # Index = position among the page's text paragraphs (blog.js numbers
            # them the same way, so a result can open at its paragraph).
            for sec in blog.get('sections', []):
                if sec.get('type') == 'text':
                    paras += [re.sub(r'\s+', ' ', TAGS.sub('', p)).strip() for p in sec.get('paragraphs', [])]
            st = meta.get('stats') or {}
            self.add(k='blog', id=slug, t=meta.get('title') or slug,
                     s=' · '.join(x for x in (str(meta.get('year') or ''),
                                              f"{st['words']:,} words" if st.get('words') else '') if x),
                     u=f"blogs/{slug}.html", c=_cover(meta.get('cover')),
                     y=[meta['year']] if meta.get('year') else None, b=paras or None)

    def videos(self):
        for v in (_load(ROOT / 'config' / 'videos.json', {}) or {}).get('videos', []):
            yt = v.get('youtube')
            self.add(k='video', t=v['title'], s=v.get('location'), u='videos.html',
                     c={'src': f"https://i.ytimg.com/vi/{yt}/mqdefault.jpg"} if yt else None,
                     p=[v['location']] if v.get('location') else None)

    # ------------------------------------------------------------ place directory

    def places(self, visited):
        """Places the page can recognise as a whole query and open a place card for."""
        out = []
        zh = {en: name for name, en in PROVINCE_ZH_EN.items()}
        for en in sorted(set(PROVINCE_ZH_EN.values()) - {'Taiwan', 'Hong Kong', 'Macau'}):   # HK/Macau come in as countries
            sid = visited.get(en)
            out.append({'n': en, 'k': 'province', 'of': 'China',
                        'a': [zh[en], re.sub(r'(省|市|自治区|特别行政区|壮族|回族|维吾尔)', '', zh[en])],
                        'u': f"china.html#provinces/{sid}" if sid else None,
                        'm': f"map.html?collection=china&facet=provinces&sub={sid}" if sid else None})
        return [{k: v for k, v in p.items() if v} for p in out]

    def build(self):
        n_places = self.trips()
        visited = self.china()
        if not self.public:
            self.urbex()
            self.videos()
        self.blogs()
        # Banner crops ride on the covers they belong to, so a variant only ever
        # carries crops for photos it already shows.
        focus = (_load(BANNER_FOCUS, {}) or {}).get('focus', {})
        for it in self.items:
            c = it.get('c')
            if c and c.get('id') in focus:
                c['f'] = focus[c['id']]['y']
        doc = {'generated': date.today().isoformat(), 'public': self.public,
               'places': self.places(visited), 'items': self.items}
        if not self.public:
            doc['sections'] = GATED_SECTIONS
        return doc, n_places


def build(echo=print):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for public, name in ((True, 'search.json'), (False, 'search.all.json')):
        doc, n_places = Builder(public, echo).build()
        path = OUT_DIR / name
        path.write_text(json.dumps(doc, ensure_ascii=False, separators=(',', ':')))
        kinds = Counter(i['k'] for i in doc['items'])
        echo(f"✓ Wrote {path.relative_to(ROOT)} ({len(doc['items'])} items: "
             f"{', '.join(f'{k} {n}' for k, n in kinds.most_common())}; "
             f"{path.stat().st_size // 1024} KB)")


if __name__ == '__main__':
    build()
