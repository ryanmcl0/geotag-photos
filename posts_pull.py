#!/usr/bin/env python3
"""
Pull Instagram post drafts (made on the website via the owner-only Posts
feature) down to the laptop: fetch the draft list from /api/posts, resolve
every {trip, id} to its full-size edited source file via the local trip
manifests, and copy the files into per-post folders in carousel order.

Usage:
  ./posts_pull.py [--dest DIR] [--post NAME] [--url URL] [--dry-run] [--list]

  --dest DIR   Destination root (default: ./Posts). Each post becomes
               <dest>/<post name>/NN_<filename>, NN = carousel order.
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
    req = urllib.request.Request(url, headers={'Cookie': '; '.join(cookies)})
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


def main():
    ap = argparse.ArgumentParser(description='Pull post drafts and copy their source files.')
    ap.add_argument('--dest', default='Posts', help='Destination root (default: ./Posts)')
    ap.add_argument('--post', help='Only this post name')
    ap.add_argument('--url', help='Posts API URL')
    ap.add_argument('--dry-run', action='store_true', help='Print the copy plan only')
    ap.add_argument('--list', action='store_true', help='List drafts and resolved paths only')
    args = ap.parse_args()

    env = load_env()
    posts_password = env.get('CF_POSTS_PASSWORD')
    if not posts_password:
        sys.exit('❌ CF_POSTS_PASSWORD not set (environment or .env.deploy).')
    url = args.url
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
            print()
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for i, ref, src, dst in plan:
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            shutil.copy2(src, dst)
            copied += 1
        manifest = {
            'name': post['name'],
            'version': doc.get('version'),
            'photos': [{'order': i, 'trip': ref['trip'], 'id': ref['id'],
                        'source': str(src), 'copied_to': str(dst)}
                       for i, ref, src, dst in plan],
        }
        (dest_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f"   ✓ {copied} copied, {len(plan) - copied} already up to date\n")

    if unresolved:
        sys.exit(f'⚠️  {unresolved} photo(s) could not be resolved (see above).')


if __name__ == '__main__':
    main()
