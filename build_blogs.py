#!/usr/bin/env python3
"""
Build the blog pages from their source files.

Parallels build_collections.py: each blog's SOURCE OF TRUTH is a markdown source
file (content/blogs/<slug>.md, registered in config/blogs.json) that interleaves
prose with blocks of photo filepaths. This script parses the source, resolves each
photo line to an already-processed photo ({trip, id} → existing R2/local webp), and
renders static pages:

  web/blogs.html          — index of blog tiles (title · year · words · photos · read time)
  web/blogs/<slug>.html   — the post: alternating text sections and justified photo grids

Photo lines that are NOT processed trip photos (map screenshots, phone pictures,
desktop files) are compressed as "blog assets" into a pseudo-trip
hosted-photos/blog-<slug>[-private]/{thumbnails,display}/ — uploaded to R2 and
served by the same image proxy; '-private' (used for non-public blogs) keeps the
existing middleware/proxy gating working unchanged.

Source format (Google-Docs-flavoured markdown):
  ### / ## / # headings; blank-line-separated paragraphs; lines that are absolute
  image filepaths form a photo grid (consecutive path lines = one grid); '\' escapes
  are stripped. An optional '{...}' suffix on a photo line is ignored (reserved).

Usage:
  ./build_blogs.py                # build all 'live' blogs + the index
  ./build_blogs.py --blog SLUG    # rebuild one blog (+ the index)
  ./build_blogs.py --dry-run      # parse + resolve, report, write nothing
  ./build_blogs.py --force-assets # re-compress blog assets even if outputs exist
"""

import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

import click

import build_collections as bc
import photo_privacy

ROOT = Path(__file__).parent.resolve()
WEB = ROOT / 'web'
WEB_TRIPS = WEB / 'trips'
HOSTED = ROOT / 'hosted-photos'
BLOGS_CONFIG = ROOT / 'config' / 'blogs.json'
OUT_DIR = WEB / 'blogs'

IMG_EXTS = r'jpe?g|png|heic|webp|gif|tiff?'
PHOTO_LINE = re.compile(rf'^(/|~/).+\.({IMG_EXTS})\s*(\{{[^}}]*\}})?\s*$', re.IGNORECASE)
STEM_NORM = re.compile(r'-Enhanced|-NR|-SAI|-2$')

ASSET_DISPLAY_LONGEST = 2160
ASSET_THUMB_LONGEST = 400
ASSET_QUALITY = 85


def load_blogs():
    cfg = json.loads(BLOGS_CONFIG.read_text())
    return cfg.get('blogs', [])


def road_km_for(blog):
    """Total road-trip km for a blog. Precedence: explicit 'km' in config/blogs.json
    (use this for non-China trips — GPS route length is unreliable, canyon/desert drift
    inflated Mongolia's to 8600 vs the real 2800) → Σ km of matching
    china_road_trips.json legs (same numbers as china#roads). None → no km shown."""
    if blog.get('km') is not None:
        return blog['km']
    want = set(blog.get('trips', []))
    roster_path = ROOT / 'config' / 'china_road_trips.json'
    if roster_path.exists():
        try:
            legs = json.loads(roster_path.read_text()).get('trips', [])
            total = sum(leg.get('km', 0) for leg in legs
                        if bc.slugify(leg.get('trip', '')) in want)
            if total:
                return round(total)
        except (OSError, json.JSONDecodeError):
            pass
    return None


def asset_slug(blog):
    """Pseudo-trip for this blog's non-trip images. Private blogs get a '-private'
    suffix so the existing middleware path rule and proxy index gate them."""
    return f"blog-{blog['slug']}" + ('' if blog.get('public') else '-private')


# ---------------------------------------------------------------- source parsing

def parse_source(text):
    """→ list of blocks: {'type':'heading','level':n,'text':s} |
    {'type':'text','paragraphs':[s]} | {'type':'photos','paths':[s]}.
    Consecutive photo lines (blank lines allowed between) merge into one grid."""
    blocks = []
    paragraphs = []
    para_lines = []

    def flush_para():
        nonlocal para_lines
        if para_lines:
            paragraphs.append(' '.join(para_lines))
            para_lines = []

    def flush_text():
        nonlocal paragraphs
        flush_para()
        if paragraphs:
            blocks.append({'type': 'text', 'paragraphs': paragraphs})
            paragraphs = []

    for raw in text.splitlines():
        line = re.sub(r'\\(.)', r'\1', raw.rstrip())
        if not line.strip():
            flush_para()
            continue
        if PHOTO_LINE.match(line.strip()):
            flush_text()
            path = re.sub(r'\s*\{[^}]*\}\s*$', '', line.strip())
            if blocks and blocks[-1]['type'] == 'photos':
                blocks[-1]['paths'].append(path)
            else:
                blocks.append({'type': 'photos', 'paths': [path]})
            continue
        m = re.match(r'^(#{1,6})\s*(.+)$', line.strip())
        if m:
            flush_text()
            blocks.append({'type': 'heading', 'level': len(m.group(1)), 'text': m.group(2).strip()})
            continue
        para_lines.append(line.strip())
    flush_text()
    return blocks


def inline_html(s):
    """Escape + the little markdown the sources actually use (**bold**, *italic*)."""
    s = html.escape(s, quote=False)
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)', r'<em>\1</em>', s)
    return s


# ---------------------------------------------------------------- photo resolution

def load_all_records():
    """Every processed photo across every trip (full manifests, private included)."""
    trip_meta = photo_privacy.load_trip_meta()
    records = []
    for mf in sorted(WEB_TRIPS.glob('*/manifest.json')):
        slug = mf.parent.name
        man = photo_privacy.load_full_manifest(mf.parent)
        for ph in (man or {}).get('photos', []):
            records.append({
                'trip': slug, 'id': ph['id'],
                'ts': ph.get('timestamp') or '',
                'public': trip_meta.get(slug, False),
            })
    return records


class PhotoResolver:
    def __init__(self):
        self.records = load_all_records()
        self.index = bc.build_id_index(self.records)

    def resolve(self, path, preferred_trips):
        """Edit filepath → manifest photo ref, preferring the blog's trips (and their
        -private halves), then the trip owning the file's edits dir, then any trip."""
        stem = Path(path).stem
        norm = STEM_NORM.split(stem)[0]
        prefs = []
        for t in preferred_trips:
            prefs += [t] if t.endswith('-private') else [t, f'{t}-private']
        keys = [f'{t}/{s}' for t in prefs for s in (stem, norm)]
        keys += [f'{t}/{s}' for t in bc.trips_for_spec(path) for s in (stem, norm)]
        keys += [stem, norm]
        for k in keys:
            rec = self.index.get(k)
            if rec:
                return bc._photo_ref(rec)
        return None


# ---------------------------------------------------------------- blog assets

def _compress(src: Path, dst: Path, max_long: int):
    from PIL import Image, ImageOps
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)   # phone photos carry rotation in EXIF only
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        w, h = img.size
        if max(w, h) > max_long:
            scale = max_long / max(w, h)
            img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, 'WEBP', quality=ASSET_QUALITY, method=4)
        return img.size


def locate_asset_source(path: str):
    """Resolve an original image filepath, allowing for known drive renames and
    trailing-space dir quirks. Returns None if unreadable — note macOS privacy
    protection (TCC) can block ~/Desktop unless the running terminal has Full Disk
    Access, so run build_blogs.py yourself to pick up Desktop screenshots."""
    candidates = [path,
                  path.replace('/Volumes/My Passport for Mac', '/Volumes/RYAN'),
                  re.sub(r'\s+/', '/', path)]
    candidates += [re.sub(r'\s+/', '/', candidates[1])]
    for c in dict.fromkeys(candidates):
        p = Path(c).expanduser()
        try:
            if p.exists():
                p.open('rb').close()   # TCC can pass exists() yet deny reads
                return p
        except (OSError, PermissionError):
            continue
    return None


def asset_stem(path: str) -> str:
    """Stable id for a non-trip image, derived from its filename."""
    return re.sub(r'[^A-Za-z0-9._-]+', '-', Path(path).stem).strip('-')


def ensure_asset_links(aslug):
    trip_dir = WEB_TRIPS / aslug
    trip_dir.mkdir(parents=True, exist_ok=True)
    for sub in ('thumbnails', 'display'):
        (HOSTED / aslug / sub).mkdir(parents=True, exist_ok=True)
        link = trip_dir / sub
        if link.is_symlink() or link.exists():
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link)
        link.symlink_to(os.path.relpath(HOSTED / aslug / sub, trip_dir))


class AssetStore:
    """Non-trip images (screenshots, phone pics) compressed to webp in a blog's
    pseudo-trip; hands back {trip, id, ar} refs. The compressed webp is the durable
    artifact: once built, later runs reuse it and never need the original again — so
    nothing raw is kept and a sandboxed rebuild (no ~/Desktop access) still resolves
    every asset. Returns None only when neither a built webp nor a readable source
    exists. self.unresolved collects those paths for reporting."""

    def __init__(self, aslug, force=False, dry_run=False):
        self.slug = aslug
        self.force = force
        self.dry_run = dry_run
        self.used_ids = []
        self.unresolved = []
        self.compressed = 0

    def add(self, path: str):
        from PIL import Image
        stem = asset_stem(path)
        disp = HOSTED / self.slug / 'display' / f'{stem}.webp'
        thumb = HOSTED / self.slug / 'thumbnails' / f'{stem}.webp'

        # Reuse the already-compressed webp unless forced — no original needed.
        if disp.exists() and not self.force:
            with Image.open(disp) as im:
                ar = round(im.width / im.height, 3)
            self.used_ids.append(stem)
            return {'trip': self.slug, 'id': stem, 'ar': ar}

        src = locate_asset_source(path)
        if src is None:
            self.unresolved.append(path)
            return None
        if self.dry_run:
            with Image.open(src) as im:
                ar = round(im.width / im.height, 3)
            self.used_ids.append(stem)
            return {'trip': self.slug, 'id': stem, 'ar': ar}
        _compress(src, disp, ASSET_DISPLAY_LONGEST)
        _compress(src, thumb, ASSET_THUMB_LONGEST)
        self.compressed += 1
        with Image.open(disp) as im:
            ar = round(im.width / im.height, 3)
        self.used_ids.append(stem)
        return {'trip': self.slug, 'id': stem, 'ar': ar}

    def prune_unused(self):
        """Drop previously-compressed assets the source no longer references."""
        if self.dry_run:
            return
        keep = set(self.used_ids)
        for sub in ('thumbnails', 'display'):
            d = HOSTED / self.slug / sub
            if d.is_dir():
                for f in d.glob('*.webp'):
                    if f.stem not in keep:
                        f.unlink()


# ---------------------------------------------------------------- build

# Read-time model: prose at READ_WPM words/min, plus dwell time per photo. These
# blogs are photo-heavy and people actually flick through and linger on the images,
# so the per-photo dwell dominates the estimate and must not be undercounted.
READ_WPM = 200
SECONDS_PER_PHOTO = 8


def humanize_minutes(m):
    if m < 60:
        return f'~{int(round(m / 5) * 5)} min'
    h = m / 60
    lo, hi = int(h), int(h) + 1
    if h - lo < 0.25:
        return f'~{lo} hr'
    return f'{lo}-{hi} hr'


def build_blog(blog, resolver, force_assets=False, dry_run=False, echo=click.echo):
    src_path = ROOT / blog['source']
    if not src_path.exists():
        echo(f"  ⚠️  {blog['slug']}: source file missing ({blog['source']}) — skipped")
        return None
    blocks = parse_source(src_path.read_text())
    # A leading heading is the post's own title line — show it in the hero
    # instead of duplicating it at the top of the body.
    display_title = blog['title']
    if blocks and blocks[0]['type'] == 'heading':
        display_title = re.sub(r'\s*\d{4}\s*$', '', blocks.pop(0)['text'])
    aslug = asset_slug(blog)
    assets = AssetStore(aslug, force=force_assets, dry_run=dry_run)
    warnings = []
    sections = []
    n_photos = 0
    words = 0
    first_cover = None

    for b in blocks:
        if b['type'] == 'heading':
            words += len(b['text'].split())
            sections.append({'type': 'heading', 'level': b['level'],
                             'html': inline_html(b['text'])})
        elif b['type'] == 'text':
            words += sum(len(p.split()) for p in b['paragraphs'])
            sections.append({'type': 'text',
                             'paragraphs': [inline_html(p) for p in b['paragraphs']]})
        else:
            refs = []
            for path in b['paths']:
                ref = resolver.resolve(path, blog.get('trips', []))
                if ref is None:
                    try:
                        ref = assets.add(path)   # reuse built webp, else compress source
                    except Exception as e:
                        warnings.append(f'{path} (compress failed: {e})')
                        continue
                    if ref is None:
                        continue                 # tracked in assets.unresolved
                elif first_cover is None and ref.get('ar', 0) >= 1.2:
                    first_cover = ref
                refs.append(ref)
            if refs:
                n_photos += len(refs)
                sections.append({'type': 'photos', 'photos': refs})

    warnings.extend(assets.unresolved)

    assets.prune_unused()
    if not dry_run and assets.used_ids:
        ensure_asset_links(aslug)

    read_minutes = words / READ_WPM + n_photos * SECONDS_PER_PHOTO / 60
    data = {
        'slug': blog['slug'], 'title': blog['title'], 'display_title': display_title,
        'year': blog['year'],
        'public': bool(blog.get('public')),
        'ui': blog.get('ui') or 'v2',   # v2 = day rail + progress bar (set "ui":"v1" to opt out)
        'sections': sections,
        'stats': {'words': words, 'photos': n_photos,
                  'read': humanize_minutes(read_minutes),
                  'km': road_km_for(blog)},
    }

    # cover: user-pinned in tile_covers.json 'blogs', else first landscape trip photo
    spec = bc.cover_spec('blogs', blog['slug'])
    cover = bc.resolve_cover(spec, resolver.index, None) if spec else None
    data['cover'] = cover or first_cover

    echo(f"  {blog['slug']}: {len(sections)} sections, {words:,} words, "
         f"{n_photos:,} photos, {len(assets.used_ids)} assets "
         f"({assets.compressed} newly compressed), read {data['stats']['read']}"
         + (f", ⚠️ {len(warnings)} unresolved" if warnings else ''))
    if warnings:
        n_desktop = sum(1 for w in warnings if '/Desktop/' in w)
        for w in warnings:
            echo(f"      ⚠️  unresolved: {w}")
        if n_desktop:
            echo(f"      → {n_desktop} are ~/Desktop screenshots: re-run build_blogs.py in a "
                 f"terminal with Full Disk Access to compress them in.")
    return data


# ---------------------------------------------------------------- rendering

def photo_url(ref, kind):
    from urllib.parse import quote
    return f"/trips/{ref['trip']}/{kind}/{quote(ref['id'])}.webp"


NAV = '''    <nav class="topnav">
        <div class="nav-links">
            <a href="/index.html">Home</a>
            <a href="/map.html">Map</a>
            <a href="/china.html">China</a>
            <a href="/blogs.html"{blogs_active}>Blogs</a>
            <a href="/rooftopping.html" data-gated>Rooftopping</a>
        </div>
        <a class="nav-name" href="/index.html">Ryan McLoughlin</a>
        <span class="nav-spacer"></span>
        <div class="nav-links"><a id="seeall-link" href="#">See All</a></div>
    </nav>'''

PSWP = '''    <div class="pswp" tabindex="-1" role="dialog" aria-hidden="true">
        <div class="pswp__bg"></div>
        <div class="pswp__scroll-wrap">
            <div class="pswp__container">
                <div class="pswp__item"></div><div class="pswp__item"></div><div class="pswp__item"></div>
            </div>
            <div class="pswp__ui pswp__ui--hidden">
                <div class="pswp__top-bar">
                    <div class="pswp__counter"></div>
                    <button class="pswp__button pswp__button--close" title="Close (Esc)"></button>
                    <button class="pswp__button pswp__button--zoom" title="Zoom in/out"></button>
                    <div class="pswp__preloader"><div class="pswp__preloader__icn">
                        <div class="pswp__preloader__cut"><div class="pswp__preloader__donut"></div></div>
                    </div></div>
                </div>
                <button class="pswp__button pswp__button--arrow--left" title="Previous (arrow left)"></button>
                <button class="pswp__button pswp__button--arrow--right" title="Next (arrow right)"></button>
                <div class="pswp__caption"><div class="pswp__caption__center"></div></div>
            </div>
        </div>
    </div>'''

POST_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>{title} · Ryan's Travels</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe.min.css"/>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/default-skin/default-skin.min.css"/>
    <link rel="stylesheet" href="/css/site.css"/>
    <link rel="stylesheet" href="/css/blog.css"/>
</head>
<body>
{nav}

    <header class="hero blog-hero"{hero_style}>
        <div class="hero-inner">
            <h1>{title}</h1>
            <div class="blog-meta">{year}{km}<span class="blog-meta-line">{words:,} words · {photos:,} photos · {read} read</span></div>
        </div>
    </header>

    <main class="blog-wrap">
        <article id="blog"></article>
    </main>

{pswp}

    <script>window.BLOG = {data};</script>
    <script>document.body.classList.add('blog-' + (window.BLOG.ui || 'v1'));</script>
    <script src="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/photoswipe@4.1.3/dist/photoswipe-ui-default.min.js"></script>
    <script src="/js/unlock.js"></script>
    <script src="/js/gallery.js"></script>
    <script src="/js/blog.js"></script>
</body>
</html>
'''

INDEX_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Blogs · Ryan's Travels</title>
    <link rel="stylesheet" href="/css/site.css"/>
    <link rel="stylesheet" href="/css/blog.css"/>
</head>
<body>
{nav}

    <main class="wrap">
        <h1 class="page-heading">Travel Blogs</h1>
        <p class="page-sub page-sub--note">Current efforts to write up some explorations across Asia. These are pretty time consuming to write, so will gradually build up over time (months - years turnaround per trip...)</p>
        <div class="tiles blog-tiles">
{tiles}
        </div>
    </main>

    <script src="/js/unlock.js"></script>
    <script>
    document.querySelectorAll('.tile-img').forEach(img => {{
        img.addEventListener('error', () => {{
            const d = document.createElement('div');
            d.className = 'tile-cover-locked';
            d.innerHTML = '<span class="pad">🔒</span>Locked';
            img.replaceWith(d);
        }});
    }});
    </script>
</body>
</html>
'''


def render_post(data):
    cover = data.get('cover')
    hero_style = ''
    if cover:
        hero_style = f' style="--hero-img: url(\'{photo_url(cover, "display")}\')" data-cover'
    return POST_TEMPLATE.format(
        title=html.escape(data.get('display_title') or data['title'], quote=False),
        nav=NAV.format(blogs_active=' class="active"'),
        hero_style=hero_style,
        year=data['year'],
        km=f" · {data['stats']['km']:,} km" if data['stats'].get('km') else '',
        words=data['stats']['words'],
        photos=data['stats']['photos'],
        read=data['stats']['read'],
        pswp=PSWP,
        data=json.dumps({'sections': data['sections'], 'ui': data.get('ui', 'v1')},
                        separators=(',', ':')),
    )


def render_index(built, pending):
    tiles = []
    for d in built:
        cover = d.get('cover')
        img = (f'<img class="tile-img" loading="lazy" alt="" '
               f'onerror="this.style.display=\'none\'" src="{photo_url(cover, "display")}">'
               if cover else '')
        lock = '' if d['public'] else '<div class="lock-badge">🔒 See All</div>'
        gated = '' if d['public'] else ' data-gated'
        locked_cls = '' if d['public'] else ' tile--locked'
        s = d['stats']
        km = f" · {s['km']:,} km" if s.get('km') else ''
        tiles.append(f'''            <a class="tile blog-tile{locked_cls}" href="blogs/{d['slug']}.html"{gated}>
                {img}{lock}
                <div class="tile-overlay">
                    <div class="tile-title">{html.escape(d['title'], quote=False)}</div>
                    <div class="tile-sub">{d['year']}{km}</div>
                    <div class="tile-sub blog-tile-stats">{s['words']:,} words · {s['photos']:,} photos · {s['read']} read</div>
                </div>
            </a>''')
    for b in pending:
        tiles.append(f'''            <div class="tile tile--pending" aria-disabled="true">
                <div class="tile-inner"><div class="tile-title">{html.escape(b['title'], quote=False)}</div>
                    <div class="pending-tag">{b['year']} · Coming soon</div></div>
            </div>''')
    return INDEX_TEMPLATE.format(nav=NAV.format(blogs_active=' class="active"'),
                                 tiles='\n'.join(tiles))


# ---------------------------------------------------------------- CLI

@click.command()
@click.option('--blog', 'only_slug', default=None, help='Build only this blog slug (index still rebuilt)')
@click.option('--force-assets', is_flag=True, help='Re-compress blog assets even if outputs exist')
@click.option('--dry-run', is_flag=True, help='Parse + resolve and report; write nothing')
def main(only_slug, force_assets, dry_run):
    blogs = load_blogs()
    if only_slug and not any(b['slug'] == only_slug for b in blogs):
        raise click.ClickException(f'unknown blog slug: {only_slug}')
    resolver = PhotoResolver()
    click.echo(f"Resolving against {len(resolver.records):,} processed photos")

    built, pending = [], []
    for blog in blogs:
        if blog.get('status') == 'pending':
            pending.append(blog)
            continue
        if only_slug and blog['slug'] != only_slug:
            # keep its tile on the index from the existing page data, if built before
            prev = OUT_DIR / f"{blog['slug']}.json"
            if prev.exists():
                built.append(json.loads(prev.read_text()))
            continue
        data = build_blog(blog, resolver, force_assets, dry_run)
        if data:
            built.append(data)
            if not dry_run:
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                # tile metadata only — the page itself embeds its sections
                meta = {k: data[k] for k in ('slug', 'title', 'year', 'public', 'stats', 'cover')}
                (OUT_DIR / f"{blog['slug']}.json").write_text(json.dumps(meta))
                (OUT_DIR / f"{blog['slug']}.html").write_text(render_post(data))

    if not dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (WEB / 'blogs.html').write_text(render_index(built, pending))
        bc.save_dims_cache()
        click.echo(f"  ✓ web/blogs.html + {len(built)} post page(s)"
                   + (f" + {len(pending)} pending tile(s)" if pending else ''))


if __name__ == '__main__':
    main()
