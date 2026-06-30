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
  CF_ALL_PASSWORD    Password to unlock all (non-public) trips (optional).
                     Same "" convention as CF_SITE_PASSWORD.
  CF_PAGES_GIT_REPO  Path to the local git repo for the site (optional)
  CF_CONFIG_BACKUP_REPO  Path to a private git repo that source-controls
                         config/ (gitignored in this public repo) (optional)

CF_CDN_BASE_URL is auto-derived as https://<pages-project>.pages.dev/photos
"""

import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from prune import prune_removed_trips

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("Error: boto3 not installed. Install with: pip install boto3")
    sys.exit(1)


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

        missing = [f"CF_{n.upper()}" for n in ['account_id', 'api_token', 'r2_bucket', 'pages_project', 'r2_endpoint', 'r2_access_key_id', 'r2_secret_key']
                   if not getattr(self, n)]
        if missing:
            print(f"Error: Missing environment variables: {', '.join(missing)}")
            sys.exit(1)

        # CDN base URL: images are served through Pages proxy, not directly from R2
        self.cdn_base_url = f"https://{self.pages_project}.pages.dev/photos"


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
            str(src) + '/', str(target_path) + '/'
        ], check=True, capture_output=True)
        print(f"    ✓ Synced config/ → {target_path}")
    except subprocess.CalledProcessError as e:
        print(f"    ✗ Config sync failed: {e.stderr.decode()}")
        return

    try:
        status = subprocess.run(['git', 'status', '--porcelain'],
                                cwd=str(target_path), capture_output=True, text=True)
        if not status.stdout.strip():
            print("    ✓ No config changes to commit")
            return
        subprocess.run(['git', 'add', '.'], cwd=str(target_path), check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'Sync config from geotag-photos'],
                       cwd=str(target_path), check=True, capture_output=True)
        print("    ✓ Committed config changes (remember to push!)")
    except subprocess.CalledProcessError as e:
        print(f"    ✗ Config commit failed: {e.stderr.decode()}")


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

    def upload_trip(self, trip_slug: str, dry_run: bool = False) -> dict:
        hosted_dir = Path('hosted-photos') / trip_slug
        if not hosted_dir.exists():
            print(f"  ⚠️  hosted-photos/{trip_slug} not found, skipping")
            return {'skipped': True}

        stats = {'uploaded': 0, 'skipped_existing': 0, 'deleted': 0, 'errors': 0, 'bytes': 0}

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
            if stats['errors']:
                msg += f", {stats['errors']} errors"
            print(msg)

        return stats


class ManifestPatcher:
    """Patch manifest.json files with CDN URLs for deployment.
    Saves originals and restores them after Pages deploy so local dev is unaffected."""

    def __init__(self, config: DeployConfig):
        self.config = config
        self._originals: dict[Path, str] = {}

    def patch_all(self, dry_run: bool = False):
        # manifest.json + manifest.all.json (the gated full variant of split trips)
        for manifest_file in sorted(Path('web/trips').rglob('manifest*.json')):
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
                    '--exclude', 'trips/*/thumbnails',
                    '--exclude', 'trips/*/display',
                    str(web_src) + '/', str(self.target_path) + '/'
                ], check=True, capture_output=True)
                print("    ✓ Synced web/ contents")
            except subprocess.CalledProcessError as e:
                print(f"    ✗ Sync failed: {e.stderr.decode()}")
                return False

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
    parser.add_argument('--skip-config-backup', action='store_true', help='Skip syncing config/ to the private backup repo (CF_CONFIG_BACKUP_REPO)')
    parser.add_argument('--prune-force', action='store_true', help='Allow pruning even when many trips would be removed (overrides the safety guard)')
    parser.add_argument('--dry-run', action='store_true', help='Preview without making changes')
    parser.add_argument('--trip', help='Upload only a specific trip slug')
    args = parser.parse_args()

    config = DeployConfig()
    password = os.getenv('CF_SITE_PASSWORD')
    all_password = os.getenv('CF_ALL_PASSWORD')

    print(f"🚀 Deploying to Cloudflare")
    print(f"   Account:  {config.account_id[:8]}...")
    print(f"   Bucket:   {config.r2_bucket}")
    print(f"   Project:  {config.pages_project}")
    print(f"   Site URL: https://{config.pages_project}.pages.dev")
    print(f"   Photos:   {config.cdn_base_url}")
    if config.git_repo:
        print(f"   Git Repo: {config.git_repo}")
    print(f"   Auth:     {'password protected' if password else 'none'}")
    print(f"   All-access: {'password protected' if all_password else 'none'}")
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

    # Step 1a: Per-photo privacy — split public-trip manifests and refresh the
    # image-proxy access index, so a deploy never ships an unsplit manifest.
    print("🔒 Syncing photo privacy...")
    import photo_privacy
    photo_privacy.sync(dry_run=args.dry_run)
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
        all_password_in_env = 'CF_ALL_PASSWORD' in os.environ
        if password or all_password or site_password_in_env or all_password_in_env:
            print("🔐 Setting password secrets...")
            if password:
                deployer.set_secret('CF_SITE_PASSWORD', password, dry_run=args.dry_run)
            elif site_password_in_env:
                deployer.delete_secret('CF_SITE_PASSWORD', dry_run=args.dry_run)
            if all_password:
                deployer.set_secret('CF_ALL_PASSWORD', all_password, dry_run=args.dry_run)
            elif all_password_in_env:
                deployer.delete_secret('CF_ALL_PASSWORD', dry_run=args.dry_run)
            print()

    # Step 6: Deploy to Pages
    success = True
    if not args.skip_pages:
        if config.git_repo:
            print("🌐 Syncing to Git repository...")
            syncer = GitSyncer(config)
            success = syncer.sync(dry_run=args.dry_run)
            if success and not args.dry_run:
                # Push to remote so CF Pages auto-deploys
                import subprocess as _sp
                _sp.run(['git', 'push', 'origin', 'main'],
                        cwd=config.git_repo, check=False, capture_output=True)
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
                print(f"✅ Done! https://{config.pages_project}.pages.dev")
            else:
                print(f"✅ Done! https://{config.pages_project}.pages.dev")
        else:
            print()
            print("❌ Deployment/Sync failed")
            sys.exit(1)

    if args.dry_run:
        print("\n(Dry run — no changes made)")


if __name__ == '__main__':
    main()
