#!/bin/bash
# Photo Map (local) launcher: starts serve.sh (wrangler pages dev) if it isn't
# already up, then opens the site. Same idea as video_browse/launcher.sh, but for
# the main site, so People/Posts and the other localhost-only features work.
# The repo is resolved from this script's own location, so the .app wrapper and
# the checkout can live anywhere.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8788
URL="http://localhost:$PORT/"

# Finder launches a .app with a bare PATH (/usr/bin:/bin:...), which has no node,
# so npx/wrangler would not be found. Add the usual Homebrew locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

alert() {   # non-blocking notification; falls back to nothing on failure
  osascript -e "display notification \"$1\" with title \"Photo Map (local)\"" >/dev/null 2>&1 || true
}

cd "$REPO" || exit 1
mkdir -p local_browse
LOG=local_browse/serve_site.log

if ! curl -s -o /dev/null --max-time 2 "$URL"; then
  alert "Starting the local server…"
  nohup ./serve.sh >>"$LOG" 2>&1 &
  # wrangler takes a while on a cold start (bundling the middleware + Functions)
  for _ in $(seq 1 120); do
    if curl -s -o /dev/null --max-time 1 "$URL"; then break; fi
    sleep 0.5
  done
  if ! curl -s -o /dev/null --max-time 2 "$URL"; then
    alert "Server did not start — see local_browse/serve_site.log"
    open -a Console "$REPO/$LOG" 2>/dev/null || open "$REPO/$LOG"
    exit 1
  fi
fi

open "$URL"

# People and Posts serve phone photos through symlinks into the Tailscale drive.
# Without it those pages load but every phone thumbnail 404s, so flag it — after
# opening the site, and with a bounded wait, because a stale SMB mount makes even
# a stat hang and that must never hold up the launch.
( [ -e /Volumes/RYAN/phone_browse ] ) & probe=$!
for _ in $(seq 1 8); do kill -0 "$probe" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$probe" 2>/dev/null; then
  kill "$probe" 2>/dev/null
  alert "RYAN drive is not responding (stale mount?) — People/Posts photos will not load."
elif ! wait "$probe"; then
  alert "RYAN drive not mounted — People/Posts photos will not load."
fi
