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
PORT=${PORT:-8788}

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

# Post drafts live in the real R2 bucket, but `wrangler pages dev` simulates its
# own local one, so the two sides start out unrelated: a post made on the live
# site (or on your phone) is invisible here until it is copied down. ./post.py
# mirror waits for this server, copies the drafts down — keeping any that only
# exist on this laptop — and then pushes local edits back up every few seconds.
# That used to happen only when the server was started through ./post.py serve.
# Set POSTS_MIRROR=0 to skip it (./post.py serve does, since it mirrors itself).
MIRROR_PID=""
WRANGLER_PID=""

# Stop the mirror first and give it a moment: its final push needs the dev server
# it reads from to still be alive. Then stop wrangler. Killing this script has to
# take the whole thing down, or a background mirror is left running against a dead
# server.
# npx leaves a tree behind (npm exec -> node wrangler -> cli.js -> workerd) and
# killing the pid we launched does not take the rest with it. Walk it instead of
# matching on the command line: `pkill -f wrangler...` also matches any other
# shell that happens to mention it, including the one running this script.
kill_tree() {
    local pid=$1 child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        kill_tree "$child"
    done
    kill "$pid" 2>/dev/null
}

shutdown() {
    if [ -n "$MIRROR_PID" ]; then
        kill "$MIRROR_PID" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$MIRROR_PID" 2>/dev/null || break
            sleep 0.5
        done
    fi
    [ -n "$WRANGLER_PID" ] && kill_tree "$WRANGLER_PID"
    return 0
}
trap shutdown EXIT INT TERM

if [ "${POSTS_MIRROR:-1}" != "0" ] && [ -n "$CF_POSTS_PASSWORD" ] && [ -n "$CF_PAGES_PROJECT" ]; then
    ./post.py mirror --port "$PORT" &
    MIRROR_PID=$!
elif [ "${POSTS_MIRROR:-1}" != "0" ]; then
    echo "⚠️  posts not synced: CF_POSTS_PASSWORD / CF_PAGES_PROJECT missing from .env.deploy"
fi

# Run wrangler with bindings for site and private trip passwords
# --ip 0.0.0.0 allows mobile access on the local network
# --live-reload reloads the browser on file changes; the middleware also sends
# Cache-Control: no-store on localhost, so the dev server is never stale.
# The PHOTOS_BUCKET R2 binding that /api/people + /api/posts read comes from
# wrangler.toml — do NOT pass --r2 PHOTOS_BUCKET, which overrides it with a
# bucket literally named "PHOTOS_BUCKET" that the r2 CLI then refuses to write to.
# Backgrounded and waited on rather than run in the foreground: a signal reaches
# a `wait` immediately, so the trap above can shut both halves down. In the
# foreground the shell would sit on wrangler until it exited on its own.
npx wrangler pages dev web --ip 0.0.0.0 --port "$PORT" \
    --compatibility-date=2026-06-10 \
    --live-reload \
    --binding CF_SITE_PASSWORD="$CF_SITE_PASSWORD" \
    --binding CF_ALL_PASSWORD="$CF_ALL_PASSWORD" \
    --binding CF_POSTS_PASSWORD="$CF_POSTS_PASSWORD" &
WRANGLER_PID=$!
wait "$WRANGLER_PID"
