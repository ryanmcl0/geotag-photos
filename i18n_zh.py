#!/usr/bin/env python3
"""
Chinese text for the site's data (trip names, places, buildings, bridges, regions,
descriptions): the build half of the zh/en language layer (web/js/i18n.js).

Source of truth: config/i18n_zh_data.json, a private {English: Chinese} table
(config/ is gitignored and backed up to the private configs repo, since it names
private trips and places). This script never edits data JSON in place: page code
compares some fields against English. Instead it writes a sidecar next to each
data file,

    web/trips/<slug>/manifest.json   →  web/trips/<slug>/manifest.zh.json
    web/collections/china.json       →  web/collections/china.zh.json
    web/blogs/<slug>.json            →  web/blogs/<slug>.zh.json

holding only the entries for strings that file contains. i18n.js fetches the
sidecar alongside the JSON in Chinese mode. The middleware gates a sidecar exactly
like the file it describes, so a locked visitor never receives the Chinese for a
private trip. Every data file gets a sidecar (possibly {}), so a fetch never falls
through to an HTML fallback page.

Strings joined with " · " are looked up part by part, matching i18n.js.

Usage:
  ./i18n_zh.py                 # write all sidecars + add the script to generated pages
  ./i18n_zh.py --check-ui      # dashes / placeholder mismatches in js/i18n-zh.js
  ./i18n_zh.py --missing       # list data strings that have no Chinese yet
  ./i18n_zh.py --merge a.json b.json ...   # merge translation tables into the config
"""

import argparse
import glob
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
WEB = ROOT / 'web'
DATA_DICT = ROOT / 'config' / 'i18n_zh_data.json'

# Data files a page may fetch (relative to web/).
SIDECAR_GLOBS = ['trips/index.json', 'trips/*/manifest*.json',
                 'collections/*.json', 'blogs/*.json']

# Values that are never shown as text.
_SKIP_VALUE = re.compile(r'^(https?:|/|[\w.-]+\.(webp|jpe?g|png|json|gpx|geojson)$)|^(RM|DJI_|IMG_|PXL_)\w+$|^[a-z0-9_-]+$')
_LETTER = re.compile(r'[A-Za-z]')
# Keys whose values are ids, paths or blog text (blog prose has its own zh source).
_SKIP_KEYS = {'id', 'source_filename', 'thumbnail', 'display', 'timestamp', 'path', 'src',
              'photo_ids', 'u', 'm', 'renders', 'keys', 'nearest_photo_id', 'b', 'source',
              'bz', 'section', 'email', 'name_zh', 'x', 'cover', 'url', 'href', 'file', 'route'}

# Files any visitor can fetch (mirrors PUBLIC_COLLECTIONS in functions/_middleware.ts,
# plus the trip index and blog tile metadata). In these, locked entries' strings are
# kept out of the sidecar and go to LOCKED_SIDECAR, which only all-access visitors
# can fetch (any /collections/ path off the whitelist is gated).
LOCKED_SIDECAR = 'collections/locked_names.zh.json'


def public_files():
    mw = (ROOT / 'functions' / '_middleware.ts').read_text()
    m = re.search(r'PUBLIC_COLLECTIONS\s*=\s*\[(.*?)\]', mw, re.S)
    cols = re.findall(r"'/(collections/[^']+)'", m.group(1)) if m else []
    return set(cols) | {'trips/index.json'} | {
        str(p.relative_to(WEB)) for p in WEB.glob('blogs/*.json') if not p.name.endswith('.zh.json')}

_table = None


def table():
    """English → Chinese: the config table plus the name_zh pairs already in the
    China data (those are authoritative)."""
    global _table
    if _table is None:
        _table = {}
        if DATA_DICT.exists():
            _table.update({k: v for k, v in json.loads(DATA_DICT.read_text()).items()
                           if isinstance(v, str) and v and v != k})
    return _table


def _strings(obj, out, known, hidden=None):
    """Collect display strings into `out`. With `hidden` (a set), strings inside an
    object marked "public": false go there instead: used for publicly served files
    that also list locked items (trips/index.json names every private trip)."""
    if isinstance(obj, dict):
        if hidden is not None and obj.get('public') is False:
            _strings(obj, hidden, known)
            return
        zh = obj.get('name_zh')
        if isinstance(zh, str) and zh:
            for k in ('title', 'name', 't'):
                if isinstance(obj.get(k), str):
                    known[obj[k].strip()] = zh
        for k, v in obj.items():
            if k not in _SKIP_KEYS:
                _strings(v, out, known, hidden)
    elif isinstance(obj, list):
        for v in obj:
            _strings(v, out, known, hidden)
    elif isinstance(obj, str):
        s = obj.strip()
        if s and _LETTER.search(s) and not _SKIP_VALUE.match(s):
            out.add(s)


def _atoms(s):
    """The string itself, plus its parts when it is a " · " / ", " list."""
    yield s
    for sep in (' · ', ', '):
        if sep in s:
            for p in s.split(sep):
                p = p.strip()
                if p:
                    yield from _atoms(p)


_DATED = re.compile(r'^(\d{4}(?::\d{2})?)\s+(.+)$')


def entries_for(strings, extra=None):
    tbl = table()
    out = {}
    for s in strings:
        for a in _atoms(s):
            zh = (extra or {}).get(a) or tbl.get(a)
            if zh and zh != a:
                out[a] = zh
                # Blog titles are also shown without their "Deepest China: " series.
                if a.startswith('Deepest ') and ': ' in a and '：' in zh:
                    out.setdefault(a.split(': ', 1)[1], zh.split('：', 1)[1])
                # Trip names are also shown without their "2024:01 " prefix.
                m, mz = _DATED.match(a), _DATED.match(zh)
                if m and mz and m.group(1) == mz.group(1):
                    out.setdefault(m.group(2), mz.group(2))
    return out


def sidecar_for(path: Path, hidden=None):
    data = json.loads(path.read_text())
    strings, known = set(), {}
    _strings(data, strings, known, hidden)
    return entries_for(strings, known)


def data_files():
    seen = []
    for g in SIDECAR_GLOBS:
        for p in sorted(WEB.glob(g)):
            # manifest.full.json is a local-only stash (never deployed), so it
            # gets no sidecar either.
            if p.name.endswith('.zh.json') or p.name == 'manifest.full.json' or not p.is_file():
                continue
            seen.append(p)
    return seen


def _write(out: Path, tbl):
    text = json.dumps(tbl, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    if not out.exists() or out.read_text() != text:
        out.write_text(text)


def write_sidecars(echo=print):
    pub = public_files()
    locked, n, filled = set(), 0, 0
    tables = {}
    for p in data_files():
        rel = str(p.relative_to(WEB))
        hidden = set() if rel in pub else None
        try:
            tbl = sidecar_for(p, hidden)
        except (OSError, json.JSONDecodeError):
            tbl = {}
        if hidden:
            locked |= hidden
        tables[rel] = tbl
        n += 1
        filled += bool(tbl)
    # china.json is swapped for china.preview.json for locked visitors, but the
    # page still asks for china.zh.json: cover the preview's strings too.
    if 'collections/china.json' in tables and 'collections/china.preview.json' in tables:
        tables['collections/china.json'].update(tables['collections/china.preview.json'])
    for rel, tbl in tables.items():
        _write(WEB / (rel[:-5] + '.zh.json'), tbl)
    # Locked entries named in public files, for all-access visitors only. Anything
    # also shown publicly is already in a public sidecar.
    public_atoms = set()
    for rel in pub:
        public_atoms |= set(tables.get(rel, {}))
    lk = {k: v for k, v in entries_for(locked).items() if k not in public_atoms}
    _write(WEB / LOCKED_SIDECAR, lk)
    echo(f"  ✓ zh sidecars: {n} data file(s), {filled} with Chinese; "
         f"{len(lk)} locked-only name(s) in {LOCKED_SIDECAR}")


def missing():
    tbl = table()
    miss = set()
    for p in data_files():
        if p.name == 'locked_names.json':
            continue
        strings, known = set(), {}
        _strings(json.loads(p.read_text()), strings, known)
        for s in strings:
            atoms = list(_atoms(s))
            if any(a in tbl or a in known for a in atoms):
                continue
            if re.fullmatch(r'[~\d.,\s]+(m|km|h|min)?( \([\d.,]+ m crane\))?', s):
                continue
            miss.add(s)
    return sorted(miss)


def page_script(strings):
    """<script> for a generated page's <head>: the Chinese for text baked into its
    HTML (blog titles, video titles). Empty string when there is nothing to add."""
    tbl = entries_for(strings)
    if not tbl:
        return ''
    js = json.dumps(tbl, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    return f'    <script>window.I18N_PAGE={js};</script>\n'


I18N_TAG = '<script src="/js/i18n.js"></script>'


def head_tag():
    """The language script, first thing in <head> on every page."""
    return f'    {I18N_TAG}\n'


def tag_pages(echo=print):
    """Put the language script into generated pages written before it existed (or by
    a generator that doesn't add it). Idempotent."""
    n = 0
    for p in WEB.rglob('*.html'):
        rel = p.relative_to(WEB).parts
        if rel[0] in ('vendor', 'expeditions', 'phone'):
            continue
        s = p.read_text()
        if I18N_TAG in s:
            continue
        m = re.search(r'\n([ \t]*)<meta name="viewport"[^>]*>', s)
        if not m:
            continue
        p.write_text(s[:m.end()] + f'\n{m.group(1)}{I18N_TAG}' + s[m.end():])
        n += 1
    echo(f"  ✓ language script added to {n} generated page(s)")


def check_ui():
    """Problems in js/i18n-zh.js: em/en dashes, mismatched placeholders."""
    src = (WEB / 'js' / 'i18n-zh.js').read_text()
    body = src[src.index('window.I18N_ZH ='):]
    tbl = json.loads(body[body.index('{'):body.rindex('}') + 1])
    bad = 0
    for k, v in tbl.items():
        if '—' in v or '–' in v:
            print(f"dash: {k!r}"); bad += 1
        for t in 'nsx':
            if k.count('{%s}' % t) != v.count('{%s}' % t):
                print(f"placeholder {{{t}}}: {k!r} → {v!r}"); bad += 1
    print(f"{len(tbl)} entries, {bad} problem(s)")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--missing', action='store_true')
    ap.add_argument('--merge', nargs='+')
    ap.add_argument('--check-ui', action='store_true')
    a = ap.parse_args()
    if a.check_ui:
        raise SystemExit(1 if check_ui() else 0)
    if a.merge:
        cur = json.loads(DATA_DICT.read_text()) if DATA_DICT.exists() else {}
        before = len(cur)
        for f in a.merge:
            for k, v in json.loads(Path(f).read_text()).items():
                if isinstance(v, str) and v and v != k:
                    cur[html.unescape(k).strip()] = v.strip()
        DATA_DICT.write_text(json.dumps(dict(sorted(cur.items())), ensure_ascii=False, indent=1))
        print(f"  ✓ {DATA_DICT.relative_to(ROOT)}: {before} → {len(cur)} entries")
        return
    if a.missing:
        m = missing()
        for s in m:
            print(s)
        print(f"{len(m)} string(s) without Chinese")
        return
    write_sidecars()
    tag_pages()


if __name__ == '__main__':
    main()
