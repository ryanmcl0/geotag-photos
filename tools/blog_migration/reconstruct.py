#!/usr/bin/env python3
"""
Reconstruct blog SOURCE FILES from Adobe Portfolio webpage exports.

For blogs whose 'source of truth' was built directly in Adobe Portfolio (no source
file), the saved-webpage export preserves everything we need:
  - the exact alternating sequence of text sections and photo grids,
  - every grid image at full res in the *_files folder WITH original EXIF
    (DateTimeOriginal + camera model + aperture/shutter survive Portfolio's resize).

Pipeline per blog (config/blogs.json entries with an 'export' dir):
  1. Parse the export HTML in document order → text modules (title/paragraphs) and
     photo grids (ordered local image files).
  2. Index the blog's trips' FULL manifests (public + '-private' halves):
     timestamp → photos, with aperture/shutter and aspect as tie-breakers.
  3. Match each grid image by EXIF time (auto-detecting any constant clock offset,
     e.g. camera on UK time) → original {trip, id} → its edits filepath.
  4. Unmatched images (map screenshots, phone photos) are archived into
     content/blogs/assets/exports/<slug>/ and referenced from there.
  5. Emit content/blogs/<slug>.md in the standard source-file format
     (## day headings, prose paragraphs, photo path blocks).
  6. Cross-check the matched filenames against the photo-record CSV, if present.

Usage:
  blog_migration/reconstruct.py [--blog SLUG] [--force]   # won't overwrite without --force
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from fractions import Fraction
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import click
from PIL import Image
from PIL.ExifTags import TAGS

ROOT = Path(__file__).resolve().parents[2]  # repo root (script lives in tools/blog_migration/)
sys.path.insert(0, str(ROOT))
import photo_privacy  # noqa: E402

WEB_TRIPS = ROOT / 'web' / 'trips'
CONTENT = ROOT / 'content' / 'blogs'
ASSET_ARCHIVE = CONTENT / 'assets'   # webp screenshots only; phone/trip photos ref originals

# Screenshots (map/satellite images that exist only in the export) are stored as a
# pre-compressed webp here — never raw. Slightly above the blog display spec so the
# build's final 2160/q85 pass defines quality (no visible double-compression).
SCREENSHOT_LONGEST = 2400
SCREENSHOT_QUALITY = 92
PHONE_TS = re.compile(r'(\d{8})[_-](\d{6})')


def build_phone_index(dirs):
    """timestamp-stem (YYYYMMDD_HHMMSS) → original phone-photo path, from config
    blogs.json 'phone_dirs'. Lets an export image be traced back to the full-quality
    original instead of keeping Adobe's downscaled copy."""
    idx = {}
    for d in dirs or []:
        p = Path(d)
        if not p.exists():
            continue
        for f in p.rglob('*'):
            if f.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.heic', '.heif'):
                continue
            m = PHONE_TS.search(f.name)
            if m:
                idx.setdefault(f'{m.group(1)}_{m.group(2)}', f)
    return idx


def compress_screenshot(src: Path, dst: Path):
    from PIL import Image, ImageOps
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        w, h = img.size
        if max(w, h) > SCREENSHOT_LONGEST:
            s = SCREENSHOT_LONGEST / max(w, h)
            img = img.resize((round(w * s), round(h * s)), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, 'WEBP', quality=SCREENSHOT_QUALITY, method=5)


# ---------------------------------------------------------------- export parsing

class PortfolioParser(HTMLParser):
    """Walks the export in document order → modules:
    {'type':'text','items':[('title'|'para', text)]} | {'type':'grid','images':[src]}"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.modules = []
        self.depth = 0
        self.module = None        # current module dict
        self.module_depth = None
        self.kinds = []           # 'title' | 'para' for each open div inside a text module
        self.buf = []

    def _classes(self, attrs):
        return (dict(attrs).get('class') or '').split()

    def _in_text(self):
        return self.module is not None and self.module['type'] == 'text'

    def _flush(self):
        """Every div edge inside a text module ends a paragraph. Portfolio puts body
        text BOTH directly inside class="main-text" divs (e.g. the opening paragraphs
        after each "Day N" title) AND in plain nested divs. Capturing only the nested
        ones silently dropped the opening of every day in 5 migrated blogs (fixed
        2026-10-17)."""
        text = re.sub(r'\s+', ' ', ''.join(self.buf)).replace('\xa0', ' ').strip()
        self.buf = []
        if not text or not self.kinds:
            return
        # Portfolio's rich-text tags some BODY lines with the 'title' class too
        # ("Thanks Xinjiang. See you next time."): a 'title' is a heading only if
        # it's a "Day N" header (those can be long: "Day 4: A - B - C 540km"), or
        # short and not a sentence (no closing . ! ?).
        is_day = bool(re.match(r'^Day\s*\d', text, re.I))
        kind = self.kinds[-1]
        if kind == 'title' and not (is_day or (len(text) <= 90
                                               and not re.search(r'[.!?]["\'”’)]?$', text))):
            kind = 'para'
        self.module['items'].append((kind, text))

    def handle_starttag(self, tag, attrs):
        if tag == 'img' and self.module and self.module['type'] == 'grid':
            cls = self._classes(attrs)
            if 'grid__item-image' in cls:
                src = dict(attrs).get('src') or ''
                if src:
                    self.module['images'].append(unescape(src))
        if tag == 'br' and self._in_text() and self.kinds:
            # <br><br> is Portfolio's paragraph break inside one div; a single
            # <br> is just a line break. Both used to glue sentences together
            # ("an hour later.Luckily ...").
            if ''.join(self.buf).rstrip(' \t').endswith('\n'):
                self._flush()
            else:
                self.buf.append('\n')
            return
        if tag != 'div':
            return
        self.depth += 1
        cls = self._classes(attrs)
        if 'project-module' in cls:
            self._close_module()
            kind = 'text' if 'text' in cls else 'grid' if 'media_collection' in cls else None
            if kind:
                self.module = ({'type': 'text', 'items': []} if kind == 'text'
                               else {'type': 'grid', 'images': []})
                self.module_depth = self.depth
            return
        if self._in_text():
            self._flush()
            parent = self.kinds[-1] if self.kinds else 'para'
            self.kinds.append('title' if 'title' in cls else
                              'para' if ('main-text' in cls or 'rich-text' in cls) else parent)

    def handle_endtag(self, tag):
        if tag != 'div':
            return
        if self._in_text() and self.depth > self.module_depth:
            self._flush()
            if self.kinds:
                self.kinds.pop()
        if self.module is not None and self.depth == self.module_depth:
            self._close_module()
        self.depth -= 1

    def handle_data(self, data):
        if self._in_text() and self.kinds:
            self.buf.append(data)

    def _close_module(self):
        if self.module is not None:
            if self.module['type'] == 'text' and self.module['items']:
                self.modules.append(self.module)
            elif self.module['type'] == 'grid' and self.module.get('images'):
                self.modules.append(self.module)
            self.module = None
            self.module_depth = None
        self.kinds = []
        self.buf = []


def parse_export(html_path: Path):
    p = PortfolioParser()
    p.feed(html_path.read_text(encoding='utf-8', errors='ignore'))
    p._close_module()
    return p.modules


def text_coverage(html_path: Path, modules) -> float:
    """Share of the export's body text (every title / main-text / rich-text div,
    read independently of how they nest) that made it into the parsed modules,
    measured on 5-word phrases. A low score means the parser dropped text."""
    html = html_path.read_text(encoding='utf-8', errors='ignore')
    cut = html.find('cover cover-normal')          # "other posts" tiles at the end
    if cut > 0:
        html = html[:cut]
    src = ' '.join(unescape(re.sub(r'<[^>]+>', ' ', m)) for m in re.findall(
        r'<div[^>]*class="[^"]*\b(?:title|main-text|rich-text)\b[^"]*"[^>]*>(.*?)</div>',
        html, re.S))
    got = ' '.join(t for m in modules if m['type'] == 'text' for _, t in m['items'])

    def grams(t):
        w = re.sub(r'[^a-z0-9]+', ' ', t.lower()).split()
        return {' '.join(w[i:i + 5]) for i in range(len(w) - 4)}
    want = grams(src)
    return len(want & grams(got)) / len(want) if want else 1.0


# ---------------------------------------------------------------- EXIF

def read_exif(path: Path):
    try:
        with Image.open(path) as im:
            w, h = im.size
            raw = im._getexif() or {}
    except Exception:
        return None
    d = {TAGS.get(k, k): v for k, v in raw.items()}
    ts = d.get('DateTimeOriginal') or d.get('DateTime')
    if d.get('Orientation') in (5, 6, 7, 8):   # EXIF-rotated (phone): true aspect is swapped
        w, h = h, w
    out = {'w': w, 'h': h, 'model': str(d.get('Model') or '').strip()}
    if ts:
        try:
            out['ts'] = datetime.strptime(str(ts), '%Y:%m:%d %H:%M:%S')
        except ValueError:
            pass
    fn = d.get('FNumber')
    if fn:
        try:
            out['aperture'] = f"f/{float(Fraction(str(fn))):g}"
        except (ValueError, ZeroDivisionError):
            pass
    et = d.get('ExposureTime')
    if et:
        try:
            f = Fraction(str(et)).limit_denominator(8000)
            out['shutter'] = f"1/{f.denominator}" if f.numerator == 1 else f"{float(f):g}"
        except (ValueError, ZeroDivisionError):
            pass
    return out


# ---------------------------------------------------------------- manifest pool

def load_pool(trips):
    """All photos of the blog's trips (+ -private halves), with edits filepath."""
    pool = []
    slugs = []
    for t in trips:
        slugs.append(t)
        if not t.endswith('-private'):
            slugs.append(f'{t}-private')
    for slug in slugs:
        tdir = WEB_TRIPS / slug
        man = photo_privacy.load_full_manifest(tdir) if tdir.exists() else None
        if not man:
            continue
        src_dir = ((man.get('source') or {}).get('photos_path') or '').rstrip('/')
        for ph in man.get('photos', []):
            ts = None
            if ph.get('timestamp'):
                try:
                    ts = datetime.strptime(ph['timestamp'][:19], '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    pass
            cs = ph.get('camera_settings') or {}
            pool.append({
                'trip': slug, 'id': ph['id'], 'ts': ts,
                'aperture': cs.get('aperture'), 'shutter': cs.get('shutter'),
                'path': f"{src_dir}/{ph.get('source_filename') or ph['id'] + '.jpg'}",
                'used': False,
            })
    return pool


def detect_offset(img_infos, by_ts):
    """Constant clock offset between export EXIF and manifest timestamps (camera on
    UK time etc.): the most common nearest-delta, if it beats no offset."""
    deltas = Counter()
    all_ts = sorted(by_ts)
    if not all_ts:
        return timedelta(0)
    for info in img_infos:
        ts = info.get('ts')
        if not ts:
            continue
        nearest = min(all_ts, key=lambda m: abs((m - ts).total_seconds()))
        d = round((nearest - ts).total_seconds())
        if abs(d) <= 6 * 3600:
            deltas[d] += 1
    if not deltas:
        return timedelta(0)
    best, n = deltas.most_common(1)[0]
    return timedelta(seconds=best) if n > max(3, deltas.get(0, 0)) else timedelta(0)


def match_photo(info, by_ts, offset):
    """EXIF info → manifest photo, trying exact then ±2s, settings/aspect tie-breaks."""
    ts = info.get('ts')
    if not ts:
        return None
    for dt in (0, 1, -1, 2, -2):
        cands = [p for p in by_ts.get(ts + offset + timedelta(seconds=dt), []) if not p['used']]
        if not cands:
            continue
        if len(cands) > 1 and info.get('aperture') and info.get('shutter'):
            exact = [p for p in cands if p['aperture'] == info['aperture']
                     and p['shutter'] == info['shutter']]
            if exact:
                cands = exact
        if len(cands) > 1:
            land = info['w'] >= info['h']
            orient = [p for p in cands if _is_land(p) == land]
            if orient:
                cands = orient
        cands[0]['used'] = True
        return cands[0]
    return None


_DIMS = {}


def _is_land(p):
    key = f"{p['trip']}/{p['id']}"
    if key not in _DIMS:
        try:
            with Image.open(WEB_TRIPS / p['trip'] / 'display' / f"{p['id']}.webp") as im:
                _DIMS[key] = im.width >= im.height
        except Exception:
            _DIMS[key] = True
    return _DIMS[key]


# ---------------------------------------------------------------- CSV cross-check

def csv_names(csv_path: Path):
    import csv as csvmod
    names = set()
    with csv_path.open() as f:
        for row in csvmod.reader(f):
            for cell in row:
                cell = cell.strip()
                if cell and not cell.lower().startswith('day '):
                    names.add(re.sub(r'\.(jpe?g|png)$', '', cell, flags=re.I))
    return names


# ---------------------------------------------------------------- main

def reconstruct(blog, phone_index, force=False, echo=click.echo):
    src_md = ROOT / blog['source']
    if src_md.exists() and not force:
        echo(f"  {blog['slug']}: source file already exists — skipped (use --force)")
        return
    export_dir = Path(blog['export'])
    htmls = sorted(export_dir.glob('*.html'))
    if not htmls:
        echo(f"  ⚠️  {blog['slug']}: no .html in {export_dir}")
        return
    html_path = htmls[0]
    files_dir = next(export_dir.glob('*_files'), None)
    modules = parse_export(html_path)
    n_text = sum(1 for m in modules if m['type'] == 'text')
    n_grid = sum(1 for m in modules if m['type'] == 'grid')
    n_img = sum(len(m['images']) for m in modules if m['type'] == 'grid')
    echo(f"  {blog['slug']}: {n_text} text modules, {n_grid} grids, {n_img} images "
         f"({html_path.name})")
    coverage = text_coverage(html_path, modules)
    if coverage < 0.95:
        echo(f"  ⚠️  {blog['slug']}: parsed text covers only {coverage:.0%} of the export's "
             "text; the parser is dropping paragraphs. Fix PortfolioParser before using this.")

    pool = load_pool(blog.get('trips', []))
    by_ts = defaultdict(list)
    for p in pool:
        if p['ts']:
            by_ts[p['ts']].append(p)

    # local file for each grid image + its EXIF
    infos = []
    for m in modules:
        if m['type'] != 'grid':
            continue
        for src in m['images']:
            name = Path(src.split('?')[0]).name
            local = files_dir / name if files_dir else None
            if local is None or not local.exists():
                infos.append({'name': name, 'local': None})
                continue
            info = read_exif(local) or {}
            info.update({'name': name, 'local': local})
            infos.append(info)

    offset = detect_offset([i for i in infos if i.get('ts')], by_ts)
    if offset:
        echo(f"    clock offset detected: {offset} (export EXIF → manifest time)")

    # match in document order
    matched = phone_orig = screenshots = missing = 0
    asset_dir = ASSET_ARCHIVE / blog['slug']
    idx = 0
    out_modules = []
    for m in modules:
        if m['type'] == 'text':
            out_modules.append(m)
            continue
        paths = []
        for _src in m['images']:
            info = infos[idx]
            idx += 1
            if info.get('local') is None:
                missing += 1
                echo(f"    ⚠️  image file missing in export: {info['name']}")
                continue
            rec = match_photo(info, by_ts, offset)
            if rec:
                matched += 1
                paths.append(rec['path'])
                continue
            # A phone photo (has EXIF) → reference the FULL-QUALITY original from the
            # phone dirs, not Adobe's downscaled export copy. build_blogs compresses it.
            if info.get('ts'):
                key = info['ts'].strftime('%Y%m%d_%H%M%S')
                orig = phone_index.get(key)
                if orig:
                    phone_orig += 1
                    paths.append(str(orig))
                    continue
            # Otherwise (map/satellite screenshot, or a phone photo whose original is
            # missing): store a pre-compressed webp — never the raw export image.
            if info.get('ts'):
                stem = info['ts'].strftime('%Y%m%d_%H%M%S')
                model = re.sub(r'[^A-Za-z0-9-]', '', (info.get('model') or '').replace(' ', ''))
                if model:
                    stem += f"-{model}"
            else:
                stem = f"s{idx:03d}-{info['name'].split('_')[0][:8]}"
            screenshots += 1
            dest = asset_dir / f'{stem}.webp'
            if not dest.exists() or force:
                compress_screenshot(info['local'], dest)
            paths.append(str(dest))
        if paths:
            out_modules.append({'type': 'grid', 'paths': paths})

    echo(f"    matched {matched}/{n_img} to trip photos; {phone_orig} phone originals; "
         f"{screenshots} screenshots (webp)" + (f"; ⚠️ {missing} files missing" if missing else ''))

    # cross-check against the photo-record CSV
    if blog.get('photo_record'):
        rec_names = csv_names(Path(blog['photo_record']))
        got = {Path(p['path']).stem for p in pool if p['used']}
        norm = lambda s: re.split(r'-Enhanced|-NR|-SAI', s)[0].lower()
        rec_n = {norm(x) for x in rec_names if not x.lower().startswith('screenshot')}
        got_n = {norm(x) for x in got}
        only_csv = rec_n - got_n
        only_match = got_n - rec_n
        echo(f"    photo-record cross-check: {len(rec_n & got_n)} agree, "
             f"{len(only_csv)} only-in-record, {len(only_match)} only-matched")
        if only_csv:
            echo(f"      only in record (sample): {sorted(only_csv)[:8]}")

    # emit source file
    lines = [f"### {blog['title']} {blog['year']}", '']
    for m in out_modules:
        if m['type'] == 'text':
            for kind, text in m['items']:
                if kind == 'title':
                    lines += [f'## {text}', '']
                else:
                    lines += [text, '']
        else:
            lines += m['paths'] + ['']
    src_md.parent.mkdir(parents=True, exist_ok=True)
    src_md.write_text('\n'.join(lines).rstrip() + '\n')
    echo(f"    ✓ wrote {src_md.relative_to(ROOT)}")


@click.command()
@click.option('--blog', 'only_slug', default=None)
@click.option('--force', is_flag=True, help='Overwrite an existing source file')
def main(only_slug, force):
    cfg = json.loads((ROOT / 'config' / 'blogs.json').read_text())
    blogs = cfg.get('blogs', [])
    phone_index = build_phone_index(cfg.get('phone_dirs', []))
    click.echo(f"Phone-photo index: {len(phone_index):,} originals "
               f"from {len(cfg.get('phone_dirs', []))} dir(s)")
    for blog in blogs:
        if only_slug and blog['slug'] != only_slug:
            continue
        if not blog.get('export'):
            continue
        reconstruct(blog, phone_index, force=force)


if __name__ == '__main__':
    main()
