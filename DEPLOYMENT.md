# Deploying to Cloudflare (Free)

Complete guide to hosting your travel maps on Cloudflare Pages + R2 for free.

## Overview

- **Cloudflare Pages** — hosts HTML/JS/JSON metadata (unlimited bandwidth, 25k file limit)
- **Cloudflare R2** — stores compressed photos (10 GB free/month)
- **Cost** — $0 for your library size (~3.2 GB photos + 8.6k files)

## Prerequisites

### 1. Install tools

```bash
# For R2 uploads
pip install boto3 requests

# For Pages deployment
npm install -g wrangler

# Verify
wrangler --version
```

### 2. Create Cloudflare accounts & buckets

1. **Sign up**: https://dash.cloudflare.com/sign-up
2. **Create R2 bucket**: https://dash.cloudflare.com/?to=/:account/r2
   - Name: `travel-photos` (or anything you prefer)
   - Region: auto
3. **Create Pages project**: https://dash.cloudflare.com/?to=/:account/pages
   - Name: `travel-maps` (or anything you prefer)
   - Don't connect to git yet; we'll deploy directly

### 3. Get API credentials

**Find your Account ID:**
- Visit https://dash.cloudflare.com/?to=/:account/r2
- Copy the Account ID from the right side

**Create API Token:**
- Visit https://dash.cloudflare.com/profile/api-tokens
- Click "Create Token"
- Use "Custom token" template
- Grant permissions:
  - `Account.R2` — read + write
  - `Pages` — read + write
  - Copy the token (you'll only see it once!)

### 4. Get R2 connection details

**Find your R2 endpoint:**
1. Go to https://dash.cloudflare.com/?to=/:account/r2
2. Click your bucket name
3. Look for "S3 API" section
4. Copy the "Endpoint" URL (looks like `https://bucket-name.account-id.r2.cloudflarestorage.com`)

## Environment Setup

Create a file `.env.deploy` in the project root:

```bash
# Required
export CF_ACCOUNT_ID="your-account-id"
export CF_API_TOKEN="your-api-token"
export CF_R2_BUCKET="travel-photos"
export CF_PAGES_PROJECT="travel-maps"
export CF_R2_ENDPOINT="https://travel-photos.account-id.r2.cloudflarestorage.com"
export CF_CDN_BASE_URL="https://travel-photos.account-id.r2.cloudflarestorage.com"

# Optional: Password protect the site
export CF_SITE_PASSWORD="your-secret-password"
```

Then load it:

```bash
source .env.deploy
```

**Security:** Never commit `.env.deploy` to git. Add it to `.gitignore`:

```bash
echo ".env.deploy" >> .gitignore
```

## First Deployment

### 1. Dry run (preview without uploading)

```bash
python deploy.py --dry-run
```

This shows what would happen without making any changes.

### 2. Full deployment

```bash
python deploy.py
```

This will:
- ✅ Upload all compressed images to R2
- ✅ Update manifest.json files with CDN URLs
- ✅ Deploy the site to Pages
- ✅ Set the site password (if provided)

Takes 5-15 minutes depending on library size.

### 3. Set the password (one-time, after first deploy)

If you provided `CF_SITE_PASSWORD`, the deploy script will output a command to run:

```bash
echo 'your-secret-password' | wrangler pages secret create CF_SITE_PASSWORD --project-name travel-maps
```

Run this to enable password protection.

### 4. Verify it works

Visit: **https://travel-maps.pages.dev**

If password-protected, you'll see a login page. Enter your password.

## Updating trips

After processing a new trip with `process_trip.py`, just deploy the new trip:

```bash
# Deploy just one trip (faster)
python deploy.py --trip 2024-japan

# Or deploy everything
python deploy.py
```

This only uploads new/changed images, so it's fast (~1-2 min per trip).

## Custom domain (optional)

If you want `photos.yourname.com` instead of `travel-maps.pages.dev`:

1. Point your domain's DNS to Cloudflare
2. Go to https://dash.cloudflare.com/?to=/:account/pages
3. Click your project → Settings → Custom domain
4. Add your domain

Takes ~5 min to propagate.

## Tips & Troubleshooting

### "wrangler: command not found"

```bash
npm install -g wrangler
```

### "Failed to upload to R2"

Check that your R2 endpoint URL matches your bucket name. Find it here:
https://dash.cloudflare.com/?to=/:account/r2 → click your bucket → S3 API section

### "Pages deployment failed"

Check your wrangler auth:

```bash
wrangler login
```

Then try again.

### Password protection not working

Make sure you ran the `wrangler pages secret create` command output by the deploy script.

### Want to change the password later?

```bash
echo 'new-password' | wrangler pages secret create CF_SITE_PASSWORD --project-name travel-maps
```

Then redeploy:

```bash
python deploy.py --skip-images  # Skip image upload, just redeploy Pages
```

### Want to remove password protection?

Delete the secret:

```bash
wrangler pages secret delete CF_SITE_PASSWORD --project-name travel-maps
```

Then redeploy:

```bash
python deploy.py --skip-images
```

## Pricing

You're safely within free tier for life (assuming <10 GB storage):

| Resource | Your Usage | Free Limit | Cost |
|---|---:|---:|---|
| **Pages storage** | ~8.6k files | Unlimited | $0 |
| **Pages bandwidth** | 1-10k requests/mo | Unlimited | $0 |
| **R2 storage** | ~3.2 GB | 10 GB/mo | $0 |
| **R2 reads** | ~1k-10k/mo | 1M/mo | $0 |

## Next steps

1. Fill in `.env.deploy` with your Cloudflare credentials
2. Run `python deploy.py --dry-run` to preview
3. Run `python deploy.py` to deploy
4. Share the link!

Questions? Check [Cloudflare docs](https://developers.cloudflare.com/).
