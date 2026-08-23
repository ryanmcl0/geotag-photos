#!/usr/bin/env python3
"""
Pull Instagram post drafts (made on the website via the owner-only Posts
feature) down to the laptop: fetch the draft list from /api/posts, resolve
every {trip, id} to its full-size edited source file via the local trip
manifests, and copy the files into per-post folders in carousel order.

Usage:
  ./posts_pull.py [--dest DIR] [--post NAME] [--url URL] [--dry-run] [--list]

  --dest DIR   Destination root (default: /Volumes/RYAN/Edits/Posts). Each post
               becomes <dest>/<post name>/NN_<filename>, NN = carousel order.
               Warns and exits if the RYAN drive is not mounted.
  --post NAME  Only pull the named post (default: all).
  --url URL    Posts API URL (default: https://<CF_PAGES_PROJECT>.pages.dev/api/posts).
  --list       Just print the drafts and their resolved paths, copy nothing.
  --dry-run    Show what would be copied without copying.

Environment (or parsed from .env.deploy): CF_POSTS_PASSWORD (required),
CF_SITE_PASSWORD (if the site gate is on), CF_PAGES_PROJECT (for the URL).
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
WEB_TRIPS = ROOT / 'web' / 'trips'
SOURCE_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp')


def load_env():
    """os.environ, falling back to .env.deploy (a shell file: export KEY="value")."""
    import os
    env = dict(os.environ)
    env_file = ROOT / '.env.deploy'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            line = re.sub(r'^export\s+', '', line)
            if '=' not in line:
                continue
            key, _, val = line.partition('=')
            val = val.strip().strip('"').strip("'")
            env.setdefault(key.strip(), val)
    return env


def token_for(password):
    """Hex SHA-256, matching the server's tokenFor() cookie scheme."""
    return hashlib.sha256(password.encode()).hexdigest()


def fetch_posts(url, site_password, posts_password):
    cookies = [f'posts_auth={token_for(posts_password)}']
    if site_password:
        cookies.insert(0, f'site_auth={token_for(site_password)}')
    # Cloudflare's bot rules 403 the default Python-urllib user agent
    req = urllib.request.Request(url, headers={
        'Cookie': '; '.join(cookies),
        'User-Agent': 'posts-pull/1.0',
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit("❌ /api/posts returned 404 — wrong CF_POSTS_PASSWORD, "
                     "or the feature is not deployed/enabled.")
        sys.exit(f"❌ Fetch failed: HTTP {e.code}")
    except urllib.error.URLError as e:
        sys.exit(f"❌ Fetch failed: {e.reason}")


_manifest_cache = {}


def trip_manifest_photos(trip):
    """id → photo record for a trip, from manifest.json overlaid with
    manifest.all.json (private photos only exist in the latter). Also returns
    the trip's source photos_path."""
    if trip in _manifest_cache:
        return _manifest_cache[trip]
    photos, photos_path = {}, None
    for name in ('manifest.json', 'manifest.all.json'):
        p = WEB_TRIPS / trip / name
        if not p.exists():
            continue
        m = json.loads(p.read_text())
        photos_path = (m.get('source') or {}).get('photos_path') or photos_path
        for ph in m.get('photos', []):
            photos.setdefault(ph['id'], ph)
    _manifest_cache[trip] = (photos, photos_path)
    return photos, photos_path


def normalize_stem(stem):
    # Same suffix handling as build_collections.build_id_index
    return re.split(r'-Enhanced|-NR|-SAI|-2$', stem)[0]


def resolve_source(trip, photo_id):
    """{trip, id} → absolute Path of the edited source file, or (None, why)."""
    photos, photos_path = trip_manifest_photos(trip)
    ph = photos.get(photo_id)
    if ph is None:
        return None, f'id not in {trip} manifests'
    if not photos_path:
        return None, f'{trip} manifest has no source.photos_path'
    base = Path(photos_path)
    if not base.exists():
        # Distinguish "drive not mounted" from "folder renamed"
        mount = Path(*base.parts[:3]) if str(base).startswith('/Volumes/') else base
        if not mount.exists():
            sys.exit(f"❌ {mount} is not available. Is the RYAN drive mounted?")
        return None, f'source dir missing: {base}'
    exact = base / ph.get('source_filename', f'{photo_id}.jpg')
    if exact.exists():
        return exact, None
    # Fall back to a recursive stem search (re-edits can change the suffix)
    norm = normalize_stem(photo_id)
    candidates = [f for f in base.rglob('*')
                  if f.suffix.lower() in SOURCE_EXTS
                  and normalize_stem(f.stem) == norm]
    if candidates:
        # Prefer the longest stem (most-processed edit), then newest
        best = sorted(candidates, key=lambda f: (len(f.stem), f.stat().st_mtime))[-1]
        return best, None
    return None, f'no file matching {photo_id} under {base}'


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip() or 'untitled'


def sync_post_dir(dest_dir, plan):
    """Bring dest_dir in line with the plan. Reorders done on the site are
    applied by renaming the existing local NN_ files (in-place edits kept);
    only genuinely new photos are copied from the drive. Returns (copied,
    renamed)."""
    planned = {dst.name for _, _, _, dst in plan}

    # Local NN_<file>s that are no longer at their planned name, keyed by base
    # filename. Two-phase (park under a temp name, then place) so swapped
    # order numbers can't collide mid-rename.
    parked, parked_n = {}, 0
    for f in sorted(dest_dir.iterdir()):
        m = re.match(r'^\d{2,3}_(.+)$', f.name)
        if not m or not f.is_file() or f.name in planned:
            continue
        tmp = dest_dir / f'.reorder_{parked_n}_{m.group(1)}'
        parked_n += 1
        f.rename(tmp)
        parked.setdefault(m.group(1), []).append(tmp)

    copied = renamed = 0
    for i, ref, src, dst in plan:
        if dst.exists():   # right place already; local edits are never clobbered
            continue
        if parked.get(src.name):
            parked[src.name].pop(0).rename(dst)
            renamed += 1
            continue
        shutil.copy2(src, dst)
        copied += 1

    # Whatever is still parked was removed from the post on the site.
    for tmps in parked.values():
        for tmp in tmps:
            orig = tmp.name.split('_', 2)[2]
            tmp.rename(dest_dir / f'removed_{orig}')
            print(f'   ⚠️  {orig} is no longer in this post, kept as removed_{orig}')
    return copied, renamed


PHONE_MANIFESTS = Path('/Volumes/RYAN/phone_browse/manifests')
_phone_cache = {}


def phone_sources(slug):
    """(name -> original src Path, Phone dir root) for one phone trip slug."""
    if slug in _phone_cache:
        return _phone_cache[slug]
    mpath = PHONE_MANIFESTS / f'{slug}.jsonl'
    names, phone_root = {}, None
    if mpath.exists():
        for line in mpath.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            names[row['name']] = Path(row['src'])
            if phone_root is None:
                d = Path(row['src']).parent
                while d.name.lower() != 'phone' and d != d.parent:
                    d = d.parent
                phone_root = d
    _phone_cache[slug] = (names, phone_root)
    return _phone_cache[slug]


def resolve_phone(ref):
    """Original NAS file for a {trip, id} phone photo or {trip, file} video."""
    slug = ref['trip'].removeprefix('phone-')
    names, phone_root = phone_sources(slug)
    if ref.get('id'):
        src = names.get(ref['id'])
        return (src, None) if src and src.exists() else (None, 'not in phone manifest')
    if phone_root is None:
        return None, 'phone trip manifest missing'
    src = phone_root / ref['file']
    return (src, None) if src.exists() else (None, f'missing: {src}')


def sync_phone_dir(phone_dir, plan):
    """Mirror the plan into <post>/Phone/: copy new, drop unselected copies."""
    phone_dir.mkdir(parents=True, exist_ok=True)
    wanted = {dst.name for _, _, _, dst in plan}
    removed = 0
    for f in sorted(phone_dir.iterdir()):
        if f.is_file() and re.match(r'\d+_', f.name) and f.name not in wanted:
            f.unlink()
            removed += 1
    copied = 0
    for _, _, src, dst in plan:
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        shutil.copy2(src, dst)
        copied += 1
    return copied, removed


def main():
    ap = argparse.ArgumentParser(description='Pull post drafts and copy their source files.')
    ap.add_argument('--dest', default='/Volumes/RYAN/Edits/Posts',
                    help='Destination root (default: /Volumes/RYAN/Edits/Posts)')
    ap.add_argument('--post', help='Only this post name')
    ap.add_argument('--url', help='Posts API URL')
    ap.add_argument('--local', action='store_true',
                    help='Pull from the local dev server (http://localhost:8788) — '
                         'phone-library selections only exist in local state')
    ap.add_argument('--dry-run', action='store_true', help='Print the copy plan only')
    ap.add_argument('--list', action='store_true', help='List drafts and resolved paths only')
    args = ap.parse_args()

    # The dest (and the source files) live on the network drive: fail fast
    # with a clear message if it isn't mounted.
    dest_root = Path(args.dest)
    if str(dest_root).startswith('/Volumes/'):
        mount = Path(*dest_root.parts[:3])
        if not mount.exists():
            sys.exit(f"⚠️  {mount} is not mounted. Connect the RYAN drive first — "
                     "the post folders and their source files both live on it.")

    env = load_env()
    posts_password = env.get('CF_POSTS_PASSWORD')
    if not posts_password:
        sys.exit('❌ CF_POSTS_PASSWORD not set (environment or .env.deploy).')
    url = args.url
    if not url and args.local:
        url = 'http://localhost:8788/api/posts'
    if not url:
        project = env.get('CF_PAGES_PROJECT')
        if not project:
            sys.exit('❌ Need --url or CF_PAGES_PROJECT to build the API URL.')
        url = f'https://{project}.pages.dev/api/posts'

    doc = fetch_posts(url, env.get('CF_SITE_PASSWORD'), posts_password)
    posts = doc.get('posts', [])
    if args.post:
        posts = [p for p in posts if p['name'] == args.post]
        if not posts:
            sys.exit(f'❌ No post named "{args.post}". '
                     f'Available: {", ".join(p["name"] for p in doc.get("posts", []))or "none"}')
    if not posts:
        print('No post drafts yet.')
        return

    print(f"📥 {len(posts)} post draft(s) (state v{doc.get('version')})\n")
    unresolved = 0
    for post in posts:
        name = sanitize(post['name'])
        dest_dir = Path(args.dest) / name
        print(f"── {post['name']} ({len(post['photos'])} photos) → {dest_dir}")
        plan = []
        for i, ref in enumerate(post['photos'], 1):
            src, why = resolve_source(ref['trip'], ref['id'])
            if src is None:
                print(f"   ⚠️  {i:02d} {ref['trip']}/{ref['id']}: {why}")
                unresolved += 1
                continue
            plan.append((i, ref, src, dest_dir / f'{i:02d}_{src.name}'))

        if args.list or args.dry_run:
            for i, ref, src, dst in plan:
                print(f"   {i:02d} {src}  →  {dst}")
            for i, ref in enumerate(post.get('phone') or [], 1):
                src, why = resolve_phone(ref)
                print(f"   Phone {i:02d} {src or why}  →  {dest_dir / 'Phone'}")
            print()
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        copied, renamed = sync_post_dir(dest_dir, plan)
        manifest = {
            'name': post['name'],
            'version': doc.get('version'),
            'photos': [{'order': i, 'trip': ref['trip'], 'id': ref['id'],
                        'source': str(src), 'copied_to': str(dst)}
                       for i, ref, src, dst in plan],
        }
        (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        parts = [f'{copied} copied']
        if renamed:
            parts.append(f'{renamed} reordered')
        parts.append(f'{len(plan) - copied - renamed} already up to date')
        print(f"   ✓ {', '.join(parts)}")

        # Behind-the-scenes phone selections -> <post>/Phone/ (originals,
        # photos and videos alike, resolved via the phone_browse manifests).
        phone_refs = post.get('phone') or []
        if phone_refs:
            phone_plan = []
            for i, ref in enumerate(phone_refs, 1):
                src, why = resolve_phone(ref)
                label = ref.get('id') or ref.get('file')
                if src is None:
                    print(f"   ⚠️  Phone {i:02d} {ref['trip']}/{label}: {why}")
                    unresolved += 1
                    continue
                phone_plan.append((i, ref, src, dest_dir / 'Phone' / f'{i:02d}_{src.name}'))
            pcopied, premoved = sync_phone_dir(dest_dir / 'Phone', phone_plan)
            manifest['phone'] = [
                {'order': i, 'trip': ref['trip'],
                 'id': ref.get('id'), 'file': ref.get('file'),
                 'source': str(src), 'copied_to': str(dst)}
                for i, ref, src, dst in phone_plan]
            (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            pparts = [f'{pcopied} copied']
            if premoved:
                pparts.append(f'{premoved} removed')
            pparts.append(f'{len(phone_plan) - pcopied} already up to date')
            print(f"   ✓ Phone/ ({len(phone_plan)} items): {', '.join(pparts)}")

        # Lightroom smart collection matching these photos (filename + capture
        # date), so the post is one import away in the LR Collections panel.
        try:
            sys.path.insert(0, str(ROOT / 'tools'))
            from lr_smart_collection import write_lrsmcol
            lr_out = dest_dir / f'{name}.lrsmcol'
            write_lrsmcol([dst for _, _, _, dst in plan], post['name'], lr_out)
            print(f"   ✓ Lightroom smart collection → {lr_out}\n")
        except Exception as e:
            print(f"   ⚠️  Lightroom smart collection not written: {e}\n")

    if unresolved:
        sys.exit(f'⚠️  {unresolved} photo(s) could not be resolved (see above).')


if __name__ == '__main__':
    main()
