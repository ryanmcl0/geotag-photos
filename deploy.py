#!/usr/bin/env python3
"""
Deploy the travel map to Cloudflare (Pages + R2).

Images are served privately via a Pages Function that proxies R2 —
the R2 bucket stays private (no public r2.dev URL needed).

Usage:
  ./deploy.py [--skip-images] [--skip-pages] [--dry-run] [--trip SLUG]

Environment variables (set in .env.deploy):
  CF_ACCOUNT_ID      Cloudflare account ID (32-char hex)
  CF_API_TOKEN       Cloudflare API token (R2:Edit + Pages:Edit)
  CF_R2_BUCKET       R2 bucket name
  CF_PAGES_PROJECT   Pages project name
  CF_R2_ENDPOINT     S3-compatible endpoint for uploads
  CF_SITE_PASSWORD   Password to protect the site (optional). Set to "" and
                     redeploy to remove the gate entirely (leave unset to
                     skip touching this secret).
  CF_QR_ACCESS_TOKEN Separate token embedded in the site-access QR code
                     (optional). Set to "" and redeploy to revoke QR access.
  CF_ALL_PASSWORD    Password to unlock all (non-public) trips (optional).
                     Same "" convention as CF_SITE_PASSWORD.
  CF_POSTS_PASSWORD  Password for the owner-only Posts feature (optional;
                     unset = feature off). Same "" convention as above.
  CF_PAGES_GIT_REPO  Path to the local git repo for the site (optional)
  CF_CONFIG_BACKUP_REPO  Path to a private git repo that source-controls
                         config/ (gitignored in this public repo) (optional)

CF_CDN_BASE_URL is auto-derived as <CF_SITE_URL>/photos (CF_SITE_URL defaults to
https://<pages-project>.pages.dev)
"""

import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from prune import prune_removed_trips
import blocklist

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("Error: boto3 not installed. Install with: pip install boto3")
    sys.exit(1)


# Per-trip files the site never reads that list more than the manifests show:
# exif_cache.json and source_state.json cover every source file, hidden and
# blocked photos included (name, time, often GPS); route.geojson.orig is the real
# track kept when photo_privacy blanks a publish-from-private trip's route.
LOCAL_ONLY_TRIP_FILES = ['exif_cache.json', 'source_state.json', 'route.geojson.orig']

# Places routes must never show (home etc.), in the git-ignored config/:
# [{"lat": .., "lon": .., "radius_km": ..}, ...]. Absent file = no zones.
PRIVACY_ZONES_FILE = Path('config/privacy_zones.json')


def apply_privacy_zones(trips_dir: Path):
    """Cut every route point inside a privacy zone out of the deployed
    route.geojson files (the local copies keep the full track). A line that
    passes through a zone is split there rather than joined straight across."""
    if not PRIVACY_ZONES_FILE.exists():
        return
    import math
    zones = json.loads(PRIVACY_ZONES_FILE.read_text())

    def inside(pt):
        lon, lat = pt[0], pt[1]
        for z in zones:
            dy = (lat - z['lat']) * 111.32
            dx = (lon - z['lon']) * 111.32 * math.cos(math.radians(z['lat']))
            if math.hypot(dx, dy) < z['radius_km']:
                return True
        return False

    def clip(line):
        runs, run = [], []
        for pt in line:
            if inside(pt):
                if len(run) > 1:
                    runs.append(run)
                run = []
            else:
                run.append(pt)
        if len(run) > 1:
            runs.append(run)
        return runs

    changed = 0
    for path in trips_dir.glob('*/route.geojson'):
        doc = json.loads(path.read_text())
        feats, hit = [], False
        for f in doc.get('features', []):
            g = f.get('geometry') or {}
            if g.get('type') == 'Point':
                if inside(g['coordinates']):
                    hit = True
                    continue
            elif g.get('type') in ('LineString', 'MultiLineString'):
                lines = [g['coordinates']] if g['type'] == 'LineString' else g['coordinates']
                runs = [r for line in lines for r in clip(line)]
                if sum(map(len, runs)) != sum(map(len, lines)):
                    hit = True
                    if not runs:
                        continue
                    f = {**f, 'geometry': {'type': 'MultiLineString', 'coordinates': runs}}
            feats.append(f)
        if hit:
            path.write_text(json.dumps({**doc, 'features': feats}))
            changed += 1
    if changed:
        print(f"    ✓ Privacy zones: trimmed {changed} routes")


class DeployConfig:
    def __init__(self):
        self.account_id = os.getenv('CF_ACCOUNT_ID')
        self.api_token = os.getenv('CF_API_TOKEN')
        self.r2_bucket = os.getenv('CF_R2_BUCKET')
        self.pages_project = os.getenv('CF_PAGES_PROJECT')
        self.r2_endpoint = os.getenv('CF_R2_ENDPOINT')
        self.r2_access_key_id = os.getenv('CF_R2_ACCESS_KEY_ID')
        self.r2_secret_key = os.getenv('CF_R2_SECRET_KEY')
        self.git_repo = os.getenv('CF_PAGES_GIT_REPO')
        # Public address of the live site (custom domain); pages.dev if unset.
        self.site_url = (os.getenv('CF_SITE_URL') or f"https://{self.pages_project}.pages.dev").rstrip('/')

        missing = [f"CF_{n.upper()}" for n in ['account_id', 'api_token', 'r2_bucket', 'pages_project', 'r2_endpoint', 'r2_access_key_id', 'r2_secret_key']
                   if not getattr(self, n)]
        if missing:
            print(f"Error: Missing environment variables: {', '.join(missing)}")
            sys.exit(1)

        # CDN base URL: images are served through the Pages proxy, not directly from R2.
        # Must be the canonical site address: the old pages.dev host redirects, which
        # would cost every image an extra round trip.
        self.cdn_base_url = f"{self.site_url}/photos"


def sync_public_flags(dry_run: bool = False):
    """Sync public flags into web/trips/index.json.

    Reads trips.json and matches each processed trip by manifest source.photos_path
    against the trip's edits path. Sets public=True/False accordingly.
    """
    trips_config_path = Path('config/trips.json')
    index_path = Path('web/trips/index.json')

    if not trips_config_path.exists():
        print("    ⚠️  trips.json not found, skipping")
        return

    import re as _re

    def _slugify(name: str) -> str:
        s = name.lower()
        s = _re.sub(r'[^a-z0-9]+', '-', s)
        return s.strip('-')

    trips_config = json.loads(trips_config_path.read_text())
    # Placeholder ("pending") trips have no edits path — skip them here.
    public_edits_paths = set(t['edits'] for t in trips_config.get('public', []) if t.get('edits'))

    # "wip" = editing in progress → tile shows a "More photos coming…" note. Config flag
    # on the trip ("wip": true), stamped onto the index by slug like the public flag.
    wip_slugs = {_slugify(t['name'])
                 for t in (trips_config.get('public', []) + trips_config.get('private', []))
                 if t.get('wip')}

    # Explicit private slugs — trips in the private block, keyed by slug.
    # These always win over path matching (handles shared edits paths like
    # "2024 China (March)" sharing /Edits/2024 China with the public Xinjiang trip).
    explicit_private_slugs = {_slugify(t['name']) for t in trips_config.get('private', [])}

    # publish-from-private: trips that stay in the private block but expose an allowlist
    # of photos publicly (config/public_from_private.json). They must read as public so
    # the map shows them; photo_privacy keeps everything but the allowlist gated. Single
    # switch — remove the entry and the trip reverts to fully private.
    pfp_path = Path('config/public_from_private.json')
    pfp_slugs = set()
    if pfp_path.exists():
        try:
            pfp_slugs = set(json.loads(pfp_path.read_text()).get('trips', {}))
        except (OSError, json.JSONDecodeError):
            pass

    # Build slug → source Edits path from each trip's manifest
    slug_to_source: dict[str, str] = {}
    for manifest_file in sorted(Path('web/trips').rglob('manifest.json')):
        slug = manifest_file.parent.name
        try:
            manifest = json.loads(manifest_file.read_text())
            source_path = manifest.get('source', {}).get('photos_path', '')
            if source_path:
                slug_to_source[slug] = source_path
        except Exception:
            pass

    index = json.loads(index_path.read_text())
    changed = 0
    for trip in index.get('trips', []):
        # Placeholder ("pending") trips have no manifest source — their public flag is set
        # by placeholder_trips.apply_placeholders; don't let the source-path match below
        # (which would see an empty path and force private) override it.
        if trip.get('pending'):
            continue
        source_path = slug_to_source.get(trip['id'], '')
        # Priority order:
        # 1. Slugs ending in '-private' → always private (off-route splits)
        # 2. Slug in public_from_private → public (partial publish from a private trip)
        # 3. Slug appears in trips.json private block → private
        # 4. source.photos_path matches a public edits path → public
        # 5. Otherwise → private
        if trip['id'].endswith('-private'):
            is_public = False
        elif trip['id'] in pfp_slugs:
            is_public = True
        elif trip['id'] in explicit_private_slugs:
            is_public = False
        else:
            is_public = source_path in public_edits_paths
        if trip.get('public') != is_public:
            trip['public'] = is_public
            changed += 1
        want_wip = trip['id'] in wip_slugs
        if bool(trip.get('wip')) != want_wip:
            if want_wip:
                trip['wip'] = True
            else:
                trip.pop('wip', None)
            changed += 1

    if dry_run:
        print(f"    [dry-run] would update public flags ({changed} changes)")
        return

    index_path.write_text(json.dumps(index, indent=2) + '\n')
    if changed:
        print(f"    ✓ Updated public flags for {changed} trips")
    else:
        print(f"    ✓ Public flags up to date")


def sync_config_backup(dry_run: bool = False):
    """Source-control config/ in a separate private repo.

    config/*.json are gitignored in this (public) repo because they expose
    private trip data — drive paths, building coordinates, classifications.
    This mirrors config/ into the private repo at CF_CONFIG_BACKUP_REPO and
    commits, so the source-of-truth files stay version-controlled somewhere.

    Caches (.classify_cache.json, .dims_cache.json), .bak backups and
    .DS_Store are excluded — they're regenerable / noise.
    """
    target = os.getenv('CF_CONFIG_BACKUP_REPO')
    if not target:
        print("  ⚠️  CF_CONFIG_BACKUP_REPO not set, skipping config backup")
        return
    target_path = Path(target)
    if not target_path.exists():
        print(f"  ✗ Config backup repo path does not exist: {target_path}")
        return
    if not (target_path / '.git').exists():
        print(f"  ✗ Not a git repo (run `git init`): {target_path}")
        return

    src = Path('config')
    if dry_run:
        print(f"    [dry-run] would rsync {src}/ to {target_path}/ and commit")
        return

    try:
        subprocess.run([
            'rsync', '-av', '--delete',
            '--exclude', '.git',
            '--exclude', '.DS_Store',
            '--exclude', '.classify_cache.json',
            '--exclude', '.dims_cache.json',
            '--exclude', '*.bak',
            # local_browse/ + plan doc + expeditions/ are synced separately
            # below: protect them from this rsync's --delete
            '--exclude', 'local_browse',
            '--exclude', '/expeditions',
            '--exclude', 'PHONE_PHOTOS_PLAN.md',
            str(src) + '/', str(target_path) + '/'
        ], check=True, capture_output=True)
        print(f"    ✓ Synced config/ → {target_path}")
    except subprocess.CalledProcessError as e:
        print(f"    ✗ Config sync failed: {e.stderr.decode()}")
        return

    # Local-only phone/face tooling and curation (git-ignored in the main
    # repo, but worth private tracking): scripts, people labels, cluster
    # export, plan doc. The 70MB+ face_index.sqlite is excluded — it's
    # regenerable and would bloat the backup repo's history.
    local_browse = Path('local_browse')
    if local_browse.is_dir():
        try:
            subprocess.run([
                'rsync', '-av', '--delete',
                '--exclude', '.DS_Store',
                '--exclude', 'face_index.sqlite',
                '--exclude', '__pycache__',
                '--exclude', '*.log',
                str(local_browse) + '/', str(target_path / 'local_browse') + '/'
            ], check=True, capture_output=True)
            for extra in (Path('PHONE_PHOTOS_PLAN.md'), Path('docs/phone_trip_sizes.tsv')):
                if extra.exists():
                    subprocess.run(['rsync', '-a', str(extra),
                                    str(target_path / 'local_browse') + '/'],
                                   check=True, capture_output=True)
            print(f"    ✓ Synced local_browse/ (phone + face tooling) → {target_path}/local_browse")
        except subprocess.CalledProcessError as e:
            print(f"    ✗ local_browse sync failed: {e.stderr.decode()}")

    # Skills kept out of the public repo (they name friends' folders and the
    # face-recognition workflow), but still worth private tracking.
    private_skills = [p for p in (Path('.claude/skills/photos-of-me'),) if p.is_dir()]
    if private_skills:
        try:
            dest = target_path / 'claude_skills'
            dest.mkdir(parents=True, exist_ok=True)
            for skill in private_skills:
                subprocess.run([
                    'rsync', '-av', '--delete', '--exclude', '.DS_Store',
                    str(skill) + '/', str(dest / skill.name) + '/'
                ], check=True, capture_output=True)
            print(f"    ✓ Synced private skills ({', '.join(p.name for p in private_skills)}) "
                  f"→ {target_path}/claude_skills")
        except subprocess.CalledProcessError as e:
            print(f"    ✗ private skills sync failed: {e.stderr.decode()}")

    backup_expeditions_source(target_path)

    try:
        status = subprocess.run(['git', 'status', '--porcelain'],
                                cwd=str(target_path), capture_output=True, text=True)
        if not status.stdout.strip():
            print("    ✓ No config changes to commit")
            return
        subprocess.run(['git', 'add', '.'], cwd=str(target_path), check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'Sync config from geotag-photos'],
                       cwd=str(target_path), check=True, capture_output=True)
        push = subprocess.run(['git', 'push'], cwd=str(target_path), capture_output=True)
        if push.returncode == 0:
            print("    ✓ Committed and pushed config backup")
        else:
            print("    ✓ Committed config changes (push failed — push manually)")
    except subprocess.CalledProcessError as e:
        print(f"    ✗ Config commit failed: {e.stderr.decode()}")


EXPEDITIONS = Path('expeditions')


def backup_expeditions_source(target_path: Path):
    """Keep the private expeditions/ app version-controlled away from this repo.

    expeditions/ is its own nested git repo, excluded from this (public) one via
    .git/info/exclude. Two private copies are kept:
      - a snapshot of its working tree (exactly the files its git sees: tracked
        plus untracked-not-ignored, so uncommitted edits too) in the config
        backup repo under expeditions/, committed with the config sync;
      - its commit history (branches already tracking an origin branch), pushed
        to its own origin, but only after GitHub confirms that remote is private.
    """
    if not (EXPEDITIONS / '.git').exists():
        return
    try:
        ls = subprocess.run(['git', 'ls-files', '-co', '--exclude-standard', '-z'],
                            cwd=EXPEDITIONS, check=True, capture_output=True, text=True)
        files = sorted({f for f in ls.stdout.split('\0') if f and (EXPEDITIONS / f).is_file()})
        dest = target_path / 'expeditions'
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(['rsync', '-a', '--files-from=-', str(EXPEDITIONS) + '/', str(dest) + '/'],
                       input='\n'.join(files), text=True, check=True, capture_output=True)
        # --files-from never deletes: prune what the working tree no longer has
        keep = set(files)
        for p in sorted(dest.rglob('*'), reverse=True):
            rel = p.relative_to(dest).as_posix()
            if p.is_file() and rel not in keep:
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        print(f"    ✓ Synced expeditions/ source ({len(files)} files) → {dest}")
    except subprocess.CalledProcessError as e:
        print(f"    ✗ expeditions snapshot failed: {e.stderr}")

    origin = subprocess.run(['git', 'remote', 'get-url', 'origin'],
                            cwd=EXPEDITIONS, capture_output=True, text=True).stdout.strip()
    if not origin:
        print("    ⚠️  expeditions/ has no origin remote; history stays local only")
        return
    vis = subprocess.run(['gh', 'repo', 'view', origin, '--json', 'visibility', '-q', '.visibility'],
                         capture_output=True, text=True)
    if vis.returncode != 0 or vis.stdout.strip() != 'PRIVATE':
        print(f"    ✗ Not pushing expeditions/ history: couldn't confirm {origin} is private "
              f"({(vis.stdout or vis.stderr).strip() or 'gh unavailable'})")
        return
    # Only branches that already track an origin branch and have new commits:
    # `push --all` would resurrect merged-and-deleted branches on the remote, and
    # fail on a stale local main that is merely behind.
    refs = subprocess.run(['git', 'for-each-ref', '--format=%(refname:short) %(upstream:short)', 'refs/heads'],
                          cwd=EXPEDITIONS, capture_output=True, text=True).stdout.split('\n')
    ahead = []
    for line in filter(None, refs):
        branch, _, upstream = line.partition(' ')
        if upstream != f'origin/{branch}':
            continue
        n = subprocess.run(['git', 'rev-list', '--count', f'{upstream}..{branch}'],
                           cwd=EXPEDITIONS, capture_output=True, text=True).stdout.strip()
        if n not in ('', '0'):
            ahead.append(branch)
    if not ahead:
        print("    ✓ expeditions/ history already on its private origin")
        return
    push = subprocess.run(['git', 'push', 'origin', *ahead], cwd=EXPEDITIONS, capture_output=True, text=True)
    if push.returncode == 0:
        print(f"    ✓ Pushed expeditions/ history ({', '.join(ahead)}) → {origin} (private)")
    else:
        print(f"    ✗ expeditions/ history push failed (push manually): {push.stderr.strip()}")


def build_expeditions(dry_run: bool = False) -> bool:
    """Build the Expedition Tours app (expeditions/, a nested Next.js static
    export with basePath /expeditions) into web/expeditions/.

    Photos aren't bundled: the pages point at the site's own /photos R2 proxy,
    and their trips' webps are already uploaded by the normal image sync.
    Gating (all-access only, 404 otherwise) is in functions/_middleware.ts.
    """
    out, dest = EXPEDITIONS / 'out', Path('web/expeditions')
    if dry_run:
        print(f"    [dry-run] would build {EXPEDITIONS}/ and rsync {out}/ to {dest}/")
        return True
    # serve.sh --local symlinks the whole hosted-photos tree in here for dev;
    # building with it present would copy every photo into the export.
    dev_photos = EXPEDITIONS / 'public' / 'photos'
    if dev_photos.is_symlink():
        dev_photos.unlink()
    elif dev_photos.exists():
        print(f"    ✗ {dev_photos} is a real directory; remove it (it would be bundled)")
        return False
    env = {k: v for k, v in os.environ.items() if k != 'NEXT_PUBLIC_PHOTO_BASE'}
    env['GEOTAG'] = str(Path.cwd())
    try:
        if not (EXPEDITIONS / 'node_modules').is_dir():
            subprocess.run(['npm', 'ci'], cwd=EXPEDITIONS, env=env, check=True)
        subprocess.run(['npm', 'run', 'build'], cwd=EXPEDITIONS, env=env, check=True,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"    ✗ expeditions build failed:\n{(e.stdout or '')[-2000:]}{(e.stderr or '')[-2000:]}")
        return False
    if not (out / 'index.html').is_file():
        print(f"    ✗ {out}/index.html missing after build")
        return False
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(['rsync', '-a', '--delete', '--exclude', '.DS_Store',
                    str(out) + '/', str(dest) + '/'], check=True)
    n = sum(1 for p in dest.rglob('*') if p.is_file())
    print(f"    ✓ Built expeditions/ → {dest}/ ({n} files)")
    return True


def write_wrangler_toml(config: DeployConfig):
    """Generate wrangler.toml with R2 binding so Pages Functions can access the bucket."""
    content = f"""name = "{config.pages_project}"
pages_build_output_dir = "web"

[[r2_buckets]]
binding = "PHOTOS_BUCKET"
bucket_name = "{config.r2_bucket}"
"""
    Path('wrangler.toml').write_text(content)
    print(f"    ✓ wrangler.toml written (bucket: {config.r2_bucket})")


class R2Uploader:
    def __init__(self, config: DeployConfig):
        self.config = config
        self.s3 = boto3.client(
            's3',
            endpoint_url=config.r2_endpoint,
            aws_access_key_id=config.r2_access_key_id,
            aws_secret_access_key=config.r2_secret_key,
            region_name='auto'
        )
        # Photos that must never be uploaded; see blocklist.py
        self.blocklist = blocklist.load()

    def upload_trip(self, trip_slug: str, dry_run: bool = False) -> dict:
        hosted_dir = Path('hosted-photos') / trip_slug
        if not hosted_dir.exists():
            print(f"  ⚠️  hosted-photos/{trip_slug} not found, skipping")
            return {'skipped': True}

        stats = {'uploaded': 0, 'skipped_existing': 0, 'deleted': 0, 'errors': 0,
                 'bytes': 0, 'blocked': 0}

        # Map existing R2 objects to their size, so we re-upload files whose CONTENT
        # changed (e.g. a quality re-encode keeps the same key but a different size) and
        # skip only genuinely-unchanged files. Size-based, not just key-presence.
        existing = {}
        try:
            paginator = self.s3.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=self.config.r2_bucket, Prefix=f"{trip_slug}/"):
                for obj in page.get('Contents', []):
                    existing[obj['Key']] = obj['Size']
        except Exception:
            pass  # If listing fails, upload everything

        local_keys = set()
        to_upload = []  # (img_file, s3_key, local_size) for files whose content changed
        for img_file in sorted(hosted_dir.rglob('*.webp')):
            s3_key = str(img_file.relative_to('hosted-photos'))
            # Hard block (blocklist.py): never upload these, and leave them OUT
            # of local_keys so the stale sweep below deletes any copy a previous
            # deploy already put in R2.
            if self.blocklist.is_blocked(trip_slug, img_file.stem):
                stats['blocked'] += 1
                print(f"    ⛔ blocked, not uploading: {s3_key} "
                      f"[{self.blocklist.why(trip_slug, img_file.stem)}]")
                continue
            local_keys.add(s3_key)
            local_size = img_file.stat().st_size
            unchanged = existing.get(s3_key) == local_size

            if dry_run:
                status = "(unchanged)" if unchanged else ("(changed)" if s3_key in existing else "(new)")
                print(f"    [dry-run] {s3_key} {status}")
                continue

            if unchanged:
                stats['skipped_existing'] += 1
            else:
                to_upload.append((img_file, s3_key, local_size))

        # Upload changed files concurrently — the job is latency-bound (many small PUTs),
        # so a thread pool cuts wall-time ~10x. boto3 clients are thread-safe; ex.map
        # yields results back on this thread so stat updates stay single-threaded.
        if to_upload and not dry_run:
            def _put(item):
                img_file, s3_key, local_size = item
                try:
                    self.s3.upload_file(str(img_file), self.config.r2_bucket, s3_key)
                    return (s3_key, local_size, None)
                except ClientError as e:
                    return (s3_key, 0, e)
            with ThreadPoolExecutor(max_workers=32) as ex:
                for s3_key, size, err in ex.map(_put, to_upload):
                    if err:
                        stats['errors'] += 1
                        print(f"    ✗ {s3_key}: {err}")
                    else:
                        stats['uploaded'] += 1
                        stats['bytes'] += size

        # Sync deletes: remove R2 objects under this trip that no longer exist locally
        # (photos removed by reclustering / private-split / orphan cleanup). Keeps R2 a
        # mirror of local hosted-photos so freed space is actually reclaimed.
        stale = [k for k in existing if k not in local_keys]
        if stale:
            if dry_run:
                for k in stale:
                    print(f"    [dry-run] {k} (delete — no local file)")
            else:
                try:
                    for i in range(0, len(stale), 1000):
                        self.s3.delete_objects(
                            Bucket=self.config.r2_bucket,
                            Delete={'Objects': [{'Key': k} for k in stale[i:i + 1000]]})
                    stats['deleted'] = len(stale)
                except ClientError as e:
                    stats['errors'] += 1
                    print(f"    ✗ delete stale: {e}")

        if not dry_run:
            msg = (f"    ✓ {trip_slug}: {stats['uploaded']} uploaded, "
                   f"{stats['skipped_existing']} unchanged, {stats['deleted']} deleted")
            if stats['blocked']:
                msg += f", {stats['blocked']} BLOCKED"
            if stats['errors']:
                msg += f", {stats['errors']} errors"
            print(msg)

        return stats


def upload_source_index(config: 'DeployConfig', dry_run: bool = False):
    """Build and upload the photo source index to R2 (_state/source_index.json,
    served by the posts-gated /api/source-index). It maps every {trip, id} to
    its source path + filename, so the NAS posts-puller can resolve drafts to
    files without this repo's web/trips manifests. Reads the UNPATCHED local
    manifests (run before ManifestPatcher)."""
    index = {}
    for trip_dir in sorted(Path('web/trips').iterdir()):
        photos, photos_path = {}, None
        for name in ('manifest.json', 'manifest.all.json'):
            p = trip_dir / name
            if not p.exists():
                continue
            m = json.loads(p.read_text())
            photos_path = (m.get('source') or {}).get('photos_path') or photos_path
            for ph in m.get('photos', []):
                photos.setdefault(ph['id'], ph.get('source_filename', f"{ph['id']}.jpg"))
        if photos and photos_path:
            index[trip_dir.name] = {'photos_path': photos_path, 'photos': photos}
    body = json.dumps(index, separators=(',', ':')).encode()
    n = sum(len(t['photos']) for t in index.values())
    if dry_run:
        print(f"    [dry-run] would upload _state/source_index.json "
              f"({len(index)} trips, {n} photos, {len(body) / 1024:.0f} KB)")
        return
    R2Uploader(config).s3.put_object(
        Bucket=config.r2_bucket, Key='_state/source_index.json', Body=body,
        ContentType='application/json')
    print(f"    ✓ _state/source_index.json: {len(index)} trips, {n} photos, "
          f"{len(body) / 1024:.0f} KB")


def upload_people_index(config: 'DeployConfig', dry_run: bool = False):
    """Upload the People page document to R2 (_state/people.json, served by the
    posts-gated /api/people). Built by tools/people_index.py from the local-only
    face clusters, so the site never sees face data — just photo ids per person.

    Skipped silently when the file doesn't exist: the roster is optional, and a
    site without one simply has no People page."""
    src = Path('config/people_site.json')
    if not src.exists():
        return
    body = src.read_bytes()
    doc = json.loads(body)
    # people_site.json is the camera-only variant; the one carrying the local phone
    # library is config/people_local.json, which only serve.sh reads. Refuse rather
    # than publish a document naming photos that exist on no host but this one.
    groups = doc.get('people', []) + doc.get('unnamed', [])
    phone = [ph['t'] for g in groups for ph in g.get('photos', [])
             if ph.get('g') == 2 or str(ph.get('t', '')).startswith('phone-')]
    if phone:
        raise SystemExit(f"✗ config/people_site.json contains {len(phone)} local phone-library "
                         f"references (e.g. {phone[0]}) — refusing to upload. Re-run "
                         "tools/people_index.py, which writes the phone variant to "
                         "config/people_local.json instead.")
    n = sum(p['n'] for p in doc.get('people', [])) + sum(u['n'] for u in doc.get('unnamed', []))
    if dry_run:
        print(f"    [dry-run] would upload _state/people.json "
              f"({len(doc.get('people', []))} people, {n} photos, {len(body) / 1024:.0f} KB)")
        return
    R2Uploader(config).s3.put_object(
        Bucket=config.r2_bucket, Key='_state/people.json', Body=body,
        ContentType='application/json')
    print(f"    ✓ _state/people.json: {len(doc.get('people', []))} people, "
          f"{len(doc.get('unnamed', []))} unnamed clusters, {n} photos")


class ManifestPatcher:
    """Patch manifest.json files with CDN URLs for deployment.
    Saves originals and restores them after Pages deploy so local dev is unaffected."""

    def __init__(self, config: DeployConfig):
        self.config = config
        self._originals: dict[Path, str] = {}

    def patch_all(self, dry_run: bool = False):
        # manifest.json + manifest.all.json (the gated full variant of split trips).
        # manifest.full.json is skipped: it is the local-only pre-strip stash for the
        # blocked-people tier, never deployed, and baking CDN URLs into it would leave
        # those URLs behind when photo_privacy restores from it.
        for manifest_file in sorted(Path('web/trips').rglob('manifest*.json')):
            if manifest_file.name == 'manifest.full.json':
                continue
            trip_slug = manifest_file.parent.name
            original = manifest_file.read_text()
            manifest = json.loads(original)

            if dry_run:
                print(f"    [dry-run] {trip_slug}: {len(manifest.get('photos', []))} photos → CDN URLs")
                continue

            self._originals[manifest_file] = original

            for photo in manifest.get('photos', []):
                photo['thumbnail'] = f"{self.config.cdn_base_url}/{trip_slug}/{photo['thumbnail']}"
                photo['display'] = f"{self.config.cdn_base_url}/{trip_slug}/{photo['display']}"

            manifest_file.write_text(json.dumps(manifest, indent=2))
            print(f"    ✓ {trip_slug}")

    def restore_all(self):
        """Restore original manifests (relative paths) after deploy."""
        for manifest_file, original in self._originals.items():
            manifest_file.write_text(original)
        if self._originals:
            print(f"    ✓ Restored {len(self._originals)} local manifests")


class PagesDeployer:
    def __init__(self, config: DeployConfig):
        self.config = config

    def set_secret(self, name: str, value: str, dry_run: bool = False) -> bool:
        if dry_run:
            print(f"    [dry-run] would set Pages secret: {name}")
            return True
        result = subprocess.run(
            ['npx', 'wrangler', 'pages', 'secret', 'put', name,
             '--project-name', self.config.pages_project],
            input=value, text=True, capture_output=True
        )
        if result.returncode == 0:
            print(f"    ✓ Secret {name} set")
            return True
        print(f"    ✗ Failed to set {name}: {result.stderr.strip()}")
        return False

    def delete_secret(self, name: str, dry_run: bool = False) -> bool:
        if dry_run:
            print(f"    [dry-run] would delete Pages secret: {name}")
            return True
        result = subprocess.run(
            ['npx', 'wrangler', 'pages', 'secret', 'delete', name,
             '--project-name', self.config.pages_project],
            input='y', text=True, capture_output=True
        )
        if result.returncode == 0:
            print(f"    ✓ Secret {name} deleted")
            return True
        # Deleting a secret that was never set isn't an error for our purposes.
        if 'not found' in result.stderr.lower():
            print(f"    ✓ Secret {name} already unset")
            return True
        print(f"    ✗ Failed to delete {name}: {result.stderr.strip()}")
        return False

    def deploy(self, dry_run: bool = False) -> bool:
        if dry_run:
            print("    [dry-run] would run: wrangler pages deploy web/")
            return True
        try:
            result = subprocess.run(
                ['npx', 'wrangler', 'pages', 'deploy', 'web/',
                 '--project-name', self.config.pages_project],
                capture_output=True, text=True, check=True
            )
            # Extract deployment URL from output
            for line in result.stdout.splitlines() + result.stderr.splitlines():
                if 'pages.dev' in line:
                    print(f"    {line.strip()}")
                    break
            print(f"    ✓ Deployed")
            return True
        except subprocess.CalledProcessError as e:
            print(f"    ✗ Deployment failed:\n{e.stderr}")
            return False


class GitSyncer:
    """Sync site files to a local git repository for deployment via GitHub."""

    def __init__(self, config: DeployConfig):
        self.config = config
        self.target_path = Path(config.git_repo) if config.git_repo else None

    def sync(self, dry_run: bool = False):
        if not self.target_path:
            print("  ⚠️  CF_PAGES_GIT_REPO not set, skipping git sync")
            return

        if not self.target_path.exists():
            print(f"  ✗ Target repo path does not exist: {self.target_path}")
            return

        print(f"📂 Syncing to git repo: {self.target_path}")

        # 1. Copy web contents to root of target repo.
        web_src = Path('web')
        if dry_run:
            print(f"    [dry-run] would rsync {web_src}/* to {self.target_path}/")
        else:
            try:
                subprocess.run([
                    'rsync', '-av', '--delete',
                    '--exclude', '.git',
                    '--exclude', '.gitignore',
                    '--exclude', '.DS_Store',
                    '--exclude', '_middleware.ts',
                    '--exclude', 'functions',
                    '--exclude', 'wrangler.toml',
                    # web/plans is a symlink into the git-excluded private_planning/
                    # dir. As a raw symlink it dangles in the mirror and CF Pages
                    # fails the build ("build output directory contains links to
                    # files that can't be accessed"). It is instead dereferenced
                    # into the (private) mirror by the dedicated step below.
                    '--exclude', 'plans',
                    '--exclude', 'trips/*/thumbnails',
                    '--exclude', 'trips/*/display',
                    # Local-only pre-strip copy of a manifest whose blocked photos were
                    # removed (photo_privacy.unblocked_manifest). Deploying it would
                    # publish exactly the entries the blocked tier just took out.
                    '--exclude', 'trips/*/manifest.full.json',
                    *(arg for name in LOCAL_ONLY_TRIP_FILES for arg in ('--exclude', f'trips/*/{name}')),
                    # local-only phone library mirror — never deployed
                    '--exclude', 'phone',
                    str(web_src) + '/', str(self.target_path) + '/'
                ], check=True, capture_output=True)
                print("    ✓ Synced web/ contents")
            except subprocess.CalledProcessError as e:
                print(f"    ✗ Sync failed: {e.stderr.decode()}")
                return False
            # An --exclude also shields the mirror's copy from --delete, so copies
            # deployed before a file was excluded have to be removed by hand.
            stale = [f for name in LOCAL_ONLY_TRIP_FILES
                     for f in self.target_path.glob(f'trips/*/{name}')]
            for f in stale:
                f.unlink()
            if stale:
                print(f"    ✓ Removed {len(stale)} local-only trip files from the mirror")
            apply_privacy_zones(self.target_path / 'trips')

        # 1b. Plans section (private): web/plans symlinks into private_planning/page,
        # which is git-excluded from the public repo. Dereference (-L) the real files
        # into the mirror (a PRIVATE repo) so /plans works on the deployed site. It is
        # gated behind the all-access password by functions/_middleware.ts (the whole
        # /plans/ prefix), same as Urbex/Videos.
        plans_src = Path('web/plans')
        if plans_src.exists():   # follows the symlink → True only if the target is present
            plans_dst = self.target_path / 'plans'
            if dry_run:
                print(f"    [dry-run] would rsync (deref) {plans_src}/ to {plans_dst}/")
            else:
                plans_dst.mkdir(parents=True, exist_ok=True)
                try:
                    subprocess.run([
                        'rsync', '-aL', '--delete',
                        '--exclude', '.DS_Store',
                        str(plans_src) + '/', str(plans_dst) + '/'
                    ], check=True, capture_output=True)
                    print("    ✓ Synced plans/ (dereferenced, gated)")
                except subprocess.CalledProcessError as e:
                    print(f"    ✗ Plans sync failed: {e.stderr.decode()}")
                    return False
        else:
            print("    ⚠️  web/plans target missing — skipping plans/ (private_planning not mounted?)")

        # 2. Copy functions/ (middleware + the R2 photo proxy and its access index)
        func_src = Path('functions')
        target_functions = self.target_path / 'functions'
        if dry_run:
            print(f"    [dry-run] would rsync {func_src}/ to {target_functions}/")
        else:
            target_functions.mkdir(parents=True, exist_ok=True)
            if func_src.exists():
                try:
                    subprocess.run([
                        'rsync', '-av', '--delete',
                        '--exclude', '.git',
                        str(func_src) + '/', str(target_functions) + '/'
                    ], check=True, capture_output=True)
                    print("    ✓ Synced functions/")
                except subprocess.CalledProcessError as e:
                    print(f"    ✗ Functions sync failed: {e.stderr.decode()}")
                    return False

        # 3. Write wrangler.toml for the git repo (pages_build_output_dir = ".")
        if dry_run:
            print(f"    [dry-run] would update {self.target_path}/wrangler.toml")
        else:
            wrangler_content = f"""name = "{self.config.pages_project}"
pages_build_output_dir = "."

[[r2_buckets]]
binding = "PHOTOS_BUCKET"
bucket_name = "{self.config.r2_bucket}"
"""
            (self.target_path / 'wrangler.toml').write_text(wrangler_content)
            print("    ✓ Updated wrangler.toml in target repo")

        # 4. Git add and commit
        if dry_run:
            print(f"    [dry-run] would git commit in {self.target_path}")
        else:
            try:
                # Check if there are changes
                status = subprocess.run(['git', 'status', '--porcelain'], cwd=str(self.target_path), capture_output=True, text=True)
                if not status.stdout.strip():
                    print("    ✓ No changes to commit in target repo")
                    return True

                subprocess.run(['git', 'add', '.'], cwd=str(self.target_path), check=True, capture_output=True)
                subprocess.run(['git', 'commit', '-m', 'Sync site from geotag-photos'], cwd=str(self.target_path), check=True, capture_output=True)
                print("    ✓ Committed changes in target repo (remember to push!)")
            except subprocess.CalledProcessError as e:
                print(f"    ✗ Git commit failed: {e.stderr.decode()}")
                return False

        return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Deploy travel map to Cloudflare Pages + R2')
    parser.add_argument('--skip-images', action='store_true', help='Skip the R2 image sync (deploy code/manifests only)')
    parser.add_argument('--skip-pages', action='store_true', help='Skip Pages deployment')
    parser.add_argument('--no-prune', action='store_true', help='Do not remove trips that are no longer in config/trips.json')
    parser.add_argument('--skip-config-backup', action='store_true', help='Skip syncing config/ (and the expeditions/ source) to the private backup repo (CF_CONFIG_BACKUP_REPO)')
    parser.add_argument('--skip-expeditions', action='store_true', help='Do not rebuild Expedition Tours; ship web/expeditions/ as it is')
    parser.add_argument('--prune-force', action='store_true', help='Allow pruning even when many trips would be removed (overrides the safety guard)')
    parser.add_argument('--dry-run', action='store_true', help='Preview without making changes')
    parser.add_argument('--trip', help='Upload only a specific trip slug')
    args = parser.parse_args()

    config = DeployConfig()
    password = os.getenv('CF_SITE_PASSWORD')
    qr_access_token = os.getenv('CF_QR_ACCESS_TOKEN')
    all_password = os.getenv('CF_ALL_PASSWORD')
    posts_password = os.getenv('CF_POSTS_PASSWORD')

    print(f"🚀 Deploying to Cloudflare")
    print(f"   Account:  {config.account_id[:8]}...")
    print(f"   Bucket:   {config.r2_bucket}")
    print(f"   Project:  {config.pages_project}")
    print(f"   Site URL: {config.site_url}")
    print(f"   Photos:   {config.cdn_base_url}")
    if config.git_repo:
        print(f"   Git Repo: {config.git_repo}")
    print(f"   Auth:     {'password protected' if password else 'none'}")
    print(f"   QR access: {'enabled' if qr_access_token else 'off'}")
    print(f"   All-access: {'password protected' if all_password else 'none'}")
    print(f"   Posts:    {'password protected' if posts_password else 'off'}")
    if args.dry_run:
        print(f"   Mode:     DRY RUN")
    print()

    # Step 0: Back up config/ to the private repo (gitignored here for privacy)
    if not args.skip_config_backup:
        print("🗄️  Backing up config/ to private repo...")
        sync_config_backup(dry_run=args.dry_run)
        print()

    # Step 1: Sync public flags from public.json → index.json
    print("🏷️  Syncing public flags...")
    sync_public_flags(dry_run=args.dry_run)
    print()

    # Step 1a0: Re-resolve the people roster against the face clusters, so a roster
    # edit takes effect on this deploy. Only possible where the local-only face data
    # lives; elsewhere photo_privacy's digest check is what catches a stale index.
    if Path('local_browse/clusters.json').exists() and Path('config/people.json').exists():
        print("👤 Resolving people roster...")
        subprocess.run([sys.executable, 'tools/people_index.py'], check=True)
        print()

    # Step 1a: Per-photo privacy — split public-trip manifests and refresh the
    # image-proxy access index, so a deploy never ships an unsplit manifest.
    print("🔒 Syncing photo privacy...")
    import photo_privacy
    photo_privacy.sync(dry_run=args.dry_run)
    print()

    # Step 1a-ii: Hard blocklist. Unlike force_private (gated but still
    # uploaded), these are never sent to R2 at all — and are stripped from the
    # manifests here so a blocked photo can't leave a broken tile in a gallery.
    _blocked = blocklist.load()
    if _blocked:
        print(f"⛔ Blocklist active: {len(_blocked)} photo(s) "
              f"[{', '.join(_blocked.sources)}]")
        n = blocklist.strip_manifests(_blocked, dry_run=args.dry_run)
        hits = blocklist.local_hits(_blocked)
        print(f"    {n} manifest entr(ies) stripped, "
              f"{len(hits)} blocked file(s) present in hosted-photos/ "
              f"(not uploaded; any R2 copy is deleted)")
        print()

    # Step 1b: Prune trips removed from config/trips.json (index, web/trips,
    # hosted-photos, R2). On by default; config is the source of truth.
    if not args.no_prune:
        print("🧹 Pruning trips removed from config...")
        prune_s3 = None if (args.skip_images or args.dry_run) else R2Uploader(config).s3
        prune_removed_trips(s3=prune_s3, r2_bucket=config.r2_bucket,
                            dry_run=args.dry_run, force=args.prune_force)
        print()

    # Step 1c: (Re-)assert "Photos pending" placeholder trips into the index so every
    # deploy ships them, even if the index was regenerated. Idempotent.
    if not args.dry_run:
        print("📍 Applying placeholder trips...")
        from placeholder_trips import apply_placeholders
        apply_placeholders(Path('web/trips/index.json'))
        print()

    # Step 1c-ii: Rebuild the public coverage file: plain pins showing where the
    # gated trips/photos are, without exposing them. Runs after the privacy sync +
    # placeholders so it reflects exactly what this deploy hides.
    print("📌 Building private coverage pins...")
    from private_coverage import build as build_private_coverage
    build_private_coverage(dry_run=args.dry_run)
    print()

    # Step 1d: Refresh the landing-page stats AFTER the privacy sync/prune/
    # placeholders have settled the index and manifests, so the homepage numbers
    # always match what this deploy actually ships (they're otherwise only
    # regenerated when build_collections happens to run).
    if not args.dry_run:
        print("📊 Refreshing site stats...")
        from build_collections import emit_site_stats
        emit_site_stats(print)
        print()

    # Step 1e: Build Expedition Tours into web/expeditions/ (only where the private
    # expeditions/ checkout exists). A failed build stops the deploy here, before
    # R2 or Pages are touched.
    if not args.skip_pages and not args.skip_expeditions and (EXPEDITIONS / 'package.json').exists():
        print("🧭 Building Expedition Tours...")
        if not build_expeditions(dry_run=args.dry_run):
            print("❌ Expeditions build failed; nothing deployed "
                  "(fix it, or --skip-expeditions to ship the existing web/expeditions/)")
            sys.exit(1)
        print()

    # Step 2: Sync images to R2 (size-aware: skips unchanged, re-uploads changed,
    # deletes orphans). On by default; --skip-images for a code/manifest-only deploy.
    if not args.skip_images:
        print("📤 Syncing images to R2...")
        uploader = R2Uploader(config)
        if args.trip:
            uploader.upload_trip(args.trip, dry_run=args.dry_run)
        else:
            total_bytes = 0
            for trip_dir in sorted(Path('hosted-photos').iterdir()):
                if trip_dir.is_dir():
                    stats = uploader.upload_trip(trip_dir.name, dry_run=args.dry_run)
                    total_bytes += stats.get('bytes', 0)
            if not args.dry_run and total_bytes:
                print(f"   Total uploaded: {total_bytes / 1e9:.2f} GB")
        print()

    # Step 2a: Photo source index for the NAS posts-puller. Must run on the
    # UNPATCHED manifests (before Step 2's CDN rewrite). Uploaded on every
    # deploy, including --skip-images (it's one small JSON).
    print("🗂️  Uploading photo source index...")
    upload_source_index(config, dry_run=args.dry_run)
    upload_people_index(config, dry_run=args.dry_run)
    print()

    # Step 2: Patch manifests with CDN URLs
    print("📝 Patching manifests with CDN URLs...")
    patcher = ManifestPatcher(config)
    patcher.patch_all(dry_run=args.dry_run)
    print()

    # Step 3: Write wrangler.toml with R2 binding
    print("⚙️  Writing wrangler.toml...")
    if not args.dry_run:
        write_wrangler_toml(config)
    else:
        print(f"    [dry-run] would write wrangler.toml (bucket: {config.r2_bucket})")
    print()

    deployer = PagesDeployer(config)

    # Step 5: Set/clear password secrets. A var that's unset in the environment is left
    # alone; a var that's explicitly set to "" deletes the secret (removes the gate).
    if not args.skip_pages:
        site_password_in_env = 'CF_SITE_PASSWORD' in os.environ
        qr_access_token_in_env = 'CF_QR_ACCESS_TOKEN' in os.environ
        all_password_in_env = 'CF_ALL_PASSWORD' in os.environ
        posts_password_in_env = 'CF_POSTS_PASSWORD' in os.environ
        if (password or qr_access_token or all_password or posts_password
                or site_password_in_env or qr_access_token_in_env
                or all_password_in_env or posts_password_in_env):
            print("🔐 Setting password secrets...")
            if password:
                deployer.set_secret('CF_SITE_PASSWORD', password, dry_run=args.dry_run)
            elif site_password_in_env:
                deployer.delete_secret('CF_SITE_PASSWORD', dry_run=args.dry_run)
            if qr_access_token:
                deployer.set_secret('CF_QR_ACCESS_TOKEN', qr_access_token, dry_run=args.dry_run)
            elif qr_access_token_in_env:
                deployer.delete_secret('CF_QR_ACCESS_TOKEN', dry_run=args.dry_run)
            if all_password:
                deployer.set_secret('CF_ALL_PASSWORD', all_password, dry_run=args.dry_run)
            elif all_password_in_env:
                deployer.delete_secret('CF_ALL_PASSWORD', dry_run=args.dry_run)
            if posts_password:
                deployer.set_secret('CF_POSTS_PASSWORD', posts_password, dry_run=args.dry_run)
            elif posts_password_in_env:
                deployer.delete_secret('CF_POSTS_PASSWORD', dry_run=args.dry_run)
            print()

    # Step 6: Deploy to Pages
    success = True
    if not args.skip_pages:
        if config.git_repo:
            print("🌐 Syncing to Git repository...")
            syncer = GitSyncer(config)
            success = syncer.sync(dry_run=args.dry_run)
            if success and not args.dry_run:
                # Push to remote so CF Pages auto-deploys. This must not fail
                # silently: R2 has already been synced (stale objects deleted),
                # so an un-pushed site keeps serving old manifests that point at
                # keys which no longer exist.
                import subprocess as _sp
                push = _sp.run(['git', 'push', 'origin', 'main'],
                               cwd=config.git_repo, check=False, capture_output=True, text=True)
                if push.returncode != 0:
                    print("    ✗ git push failed — R2 is already updated but Pages was NOT "
                          "redeployed; the live site may reference removed images. "
                          "Fix the push and re-run deploy.")
                    print(f"      {(push.stderr or push.stdout).strip()}")
                    success = False
                else:
                    print("    ✓ Pushed to origin/main — CF Pages will auto-deploy")
        else:
            print("🌐 Deploying to Cloudflare Pages (Direct)...")
            success = deployer.deploy(dry_run=args.dry_run)

    # Always restore local manifests — even on --skip-pages or failure,
    # so local paths are never left in CDN-patched state.
    if not args.dry_run:
        print("\n♻️  Restoring local manifests...")
        patcher.restore_all()

    if not args.skip_pages:
        if success:
            print()
            if config.git_repo:
                print(f"✅ Done! {config.site_url}")
            else:
                print(f"✅ Done! {config.site_url}")
        else:
            print()
            print("❌ Deployment/Sync failed")
            sys.exit(1)

    if args.dry_run:
        print("\n(Dry run — no changes made)")


if __name__ == '__main__':
    main()
