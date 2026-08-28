#!/bin/bash
# Local development server using Wrangler to support middleware & auth.

# Load environment variables for passwords
if [ -f .env.deploy ]; then
    source .env.deploy
else
    echo "Warning: .env.deploy not found. Site may be unprotected locally."
fi

# Get local IP for convenience
IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)
PORT=8788

echo "----------------------------------------------------"
echo "🚀 Starting Wrangler Dev Server"
echo "📍 Local:  http://localhost:$PORT"
echo "📱 Mobile: http://$IP:$PORT"
echo "----------------------------------------------------"

# Seed the LOCAL (simulated) R2 bucket with the owner-only state documents, so the
# posts-gated /api/* endpoints work here instead of 404ing on a missing binding.
# Local R2 is a miniflare simulation under .wrangler/state — it never touches the
# real bucket, and only these small JSON docs are put there (no photos).
# people_local.json is the same document plus the local phone library, so on
# localhost the People page shows a whole person; the deployed site gets
# people_site.json, which has no phone references in it at all.
PEOPLE_DOC=config/people_local.json
[ -f "$PEOPLE_DOC" ] || PEOPLE_DOC=config/people_site.json
if [ -f "$PEOPLE_DOC" ]; then
    npx wrangler r2 object put "$CF_R2_BUCKET/_state/people.json" \
        --file="$PEOPLE_DOC" --content-type=application/json --local >/dev/null 2>&1 \
        && echo "✓ seeded local R2 with _state/people.json (from $PEOPLE_DOC)"
fi

# Run wrangler with bindings for site and private trip passwords
# --ip 0.0.0.0 allows mobile access on the local network
# --live-reload reloads the browser on file changes; the middleware also sends
# Cache-Control: no-store on localhost, so the dev server is never stale.
# The PHOTOS_BUCKET R2 binding that /api/people + /api/posts read comes from
# wrangler.toml — do NOT pass --r2 PHOTOS_BUCKET, which overrides it with a
# bucket literally named "PHOTOS_BUCKET" that the r2 CLI then refuses to write to.
npx wrangler pages dev web --ip 0.0.0.0 \
    --compatibility-date=2026-06-10 \
    --live-reload \
    --binding CF_SITE_PASSWORD="$CF_SITE_PASSWORD" \
    --binding CF_ALL_PASSWORD="$CF_ALL_PASSWORD" \
    --binding CF_POSTS_PASSWORD="$CF_POSTS_PASSWORD"
