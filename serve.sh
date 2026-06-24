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

# Run wrangler with bindings for site and private trip passwords
# --ip 0.0.0.0 allows mobile access on the local network
# --live-reload reloads the browser on file changes; the middleware also sends
# Cache-Control: no-store on localhost, so the dev server is never stale.
npx wrangler pages dev web --ip 0.0.0.0 \
    --compatibility-date=2026-06-10 \
    --live-reload \
    --binding CF_SITE_PASSWORD="$CF_SITE_PASSWORD" \
    --binding CF_ALL_PASSWORD="$CF_ALL_PASSWORD"
