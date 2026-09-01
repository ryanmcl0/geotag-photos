#!/bin/bash
# Photo Map (local) launcher: opens the local site, cold-starting serve.sh
# (wrangler pages dev) first if it isn't already up.
#
# This script is the source of truth. The Desktop "Photo Map (Local).app" is an
# AppleScript applet (built by make_photomap_app.sh) that does nothing but run
# it, so edits here take effect on the next launch with no rebuild or re-sign.
# The applet must stay a compiled applet: an app whose executable is a bare
# shell script runs as /bin/bash, and tccd silently denies Documents access to
# platform binaries instead of showing the Allow prompt — serve.sh then dies
# with no output at all (this bit us on 2026-09-01).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8788
URL="http://localhost:$PORT/"
# Outside Documents so launches are diagnosable even when the TCC grant is
# missing — local_browse/ is unreachable in exactly that failure mode.
LOG="$HOME/Library/Logs/PhotoMapLocal.log"
# Finder/applet launches get a bare PATH (/usr/bin:/bin:...), which has no
# node, so npx/wrangler would not be found. Add the usual Homebrew locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

say() { echo "$(date '+%F %T') $1" >>"$LOG"; }
alert() { osascript -e "display notification \"$1\" with title \"Photo Map (local)\"" >/dev/null 2>&1 || true; }

say "launched"
if curl -s -o /dev/null --max-time 2 "$URL"; then
  open "$URL" && say "server up — opened new tab" || { say "open failed"; alert "Could not open the browser"; }
  exit 0
fi

say "server down — cold start via $REPO"
alert "Starting the local server…"
if ! cd "$REPO" 2>>"$LOG"; then
  say "cd failed"
  alert "Repo not reachable at $REPO"
  exit 1
fi
mkdir -p local_browse 2>>"$LOG"
# Touch the log first: this is the call that hits the Documents TCC check, so a
# denial is caught and reported here instead of serve.sh dying silently.
if ! { : >>local_browse/serve_site.log; } 2>>"$LOG"; then
  say "cannot write local_browse/serve_site.log (Documents permission denied?)"
  alert "No access to Documents — allow Photo Map (Local) in System Settings > Privacy & Security > Files & Folders"
  exit 1
fi
nohup ./serve.sh >>local_browse/serve_site.log 2>&1 &
# wrangler takes a while on a cold start (bundling the middleware + Functions)
for _ in $(seq 1 120); do
  curl -s -o /dev/null --max-time 1 "$URL" && break
  sleep 0.5
done
if ! curl -s -o /dev/null --max-time 2 "$URL"; then
  say "server did not start"
  alert "Server did not start — see local_browse/serve_site.log"
  open -a Console "$REPO/local_browse/serve_site.log" 2>/dev/null || open "$REPO/local_browse/serve_site.log"
  exit 1
fi
open "$URL" && say "cold start ok — opened new tab"

# People and Posts serve phone photos through symlinks into the Tailscale
# drive. Without it those pages load but every phone thumbnail 404s, so flag
# it — after opening the site, and with a bounded wait, because a stale SMB
# mount makes even a stat hang and that must never hold up the launch.
( [ -e /Volumes/RYAN/phone_browse ] ) & probe=$!
for _ in $(seq 1 8); do kill -0 "$probe" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$probe" 2>/dev/null; then
  kill "$probe" 2>/dev/null
  alert "RYAN drive is not responding (stale mount?) — People/Posts photos will not load."
elif ! wait "$probe"; then
  alert "RYAN drive not mounted — People/Posts photos will not load."
fi
