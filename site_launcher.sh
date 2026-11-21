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
# Posts: every launch syncs with the live site, also when the server was already
# up (it outlives the browser tab, so a re-launch used to just open a tab on
# whatever the last session left). The sync runs in ./post.py mirror; if it has
# died, start it again (only one ever runs: it holds 127.0.0.1:8790).
sync_posts() {
  if curl -s -o /dev/null --max-time 20 -X POST "http://127.0.0.1:8790/sync"; then
    say "posts synced"
  else
    say "posts sync not running — starting it"
    # the venv: the system python3 may lack numpy/OpenCV (the place-name lookups need them)
    local py=python3
    [ -x "$REPO/venv/bin/python" ] && "$REPO/venv/bin/python" -c 'import numpy' 2>/dev/null && py="$REPO/venv/bin/python"
    ( cd "$REPO" && nohup "$py" post.py mirror --port "$PORT" >>local_browse/serve_site.log 2>&1 & ) </dev/null >/dev/null 2>&1
  fi
}
# Every background helper below is cut off from the launcher's stdin/stdout/stderr (the trailing
# redirects): otherwise the subshell that waits on it keeps the app's pipe open and the desktop app
# never finishes, so the next double-click only re-activates that hung copy.
# Posts ▸ Reels helper (tools/reels_local.py): start it if a server that was
# already up predates it.
reels_helper() {
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:8791/reels/state" && return 0
  ( cd "$REPO" && nohup python3 tools/reels_local.py --port 8791 >>local_browse/serve_site.log 2>&1 & ) </dev/null >/dev/null 2>&1
  say "reels helper started"
}
# The vlog pipeline's dashboard (the video project's tools/vlog/status.py serve, port 8770): Posts ▸ Reels ▸ In
# progress shows it in a frame. Started only when the video project and the work disk are there; never holds
# up the launch.
vlog_dashboard() {
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:8770/" && return 0
  local ve="$HOME/Documents/Projects/video-editor-r4"
  [ -d "$ve" ] || ve="$HOME/Documents/Projects/video-editor"
  local trip="/Volumes/Crucial X9/editor/vlog/china-summer-2026/trip.json"
  [ -x "$ve/.venv/bin/python" ] && [ -f "$trip" ] || return 0
  ( cd "$ve" && nohup .venv/bin/python tools/vlog/status.py serve "$trip" --port 8770 >>"$REPO/local_browse/serve_site.log" 2>&1 & ) </dev/null >/dev/null 2>&1
  say "vlog dashboard started"
}
# Only one launcher at a time: an older copy still waiting (on a server that
# never came up, say) would race this one. It is ours to stop.
mine=" $$ "; a=$PPID
while [ -n "$a" ] && [ "$a" -gt 1 ]; do mine+="$a "; a=$(ps -o ppid= -p "$a" | tr -d ' '); done
for pid in $(pgrep -f 'site_launcher\.sh'); do
  case "$mine" in *" $pid "*) continue ;; esac   # this launch (and the app's shell running it)
  kill "$pid" 2>/dev/null && say "stopped an older launcher ($pid)"
done

# A stuck server: something still holds the port but no longer answers (seen
# after days of uptime). serve.sh can't start while it does, so stop it, the
# whole tree that started it (serve.sh, npm exec, wrangler, workerd) and the
# posts mirror, then cold-start below. Only our own dev server is stopped:
# anything else on the port is named in a notification instead.
answers() { curl -s -o /dev/null --max-time "${1:-5}" "${URL}robots.txt"; }
stop_tree() {   # TERM the pid and its dev-server ancestors, KILL what's left after 5 s
  local pid=$1 pids=() cmd
  while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null) || break
    case "$cmd" in
      *workerd*|*wrangler*|*serve.sh*|*"npm exec"*) pids+=("$pid") ;;
      *) break ;;
    esac
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
  done
  [ ${#pids[@]} -eq 0 ] && return 1
  kill "${pids[@]}" 2>/dev/null
  for _ in $(seq 1 10); do kill -0 "${pids[@]}" 2>/dev/null || return 0; sleep 0.5; done
  kill -9 "${pids[@]}" 2>/dev/null
  return 0
}
holder=$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
if [ -n "$holder" ] && ! answers 5 && ! answers 5; then
  what=$(ps -o command= -p "$holder" 2>/dev/null | cut -c1-120)
  if stop_tree "$holder"; then
    say "server on :$PORT stuck (pid $holder) — stopped it, restarting"
    alert "The local server had frozen. Restarting it…"
    pkill -f 'post.py mirror' 2>/dev/null
    for _ in $(seq 1 10); do lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.5; done
  else
    say "port $PORT held by something else: $what"
    alert "Port $PORT is in use by another program ($(basename "${what%% *}")). Quit it, then open Photo Map again."
    exit 1
  fi
fi

if answers 2; then
  sync_posts
  reels_helper
  vlog_dashboard
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
nohup ./serve.sh >>local_browse/serve_site.log 2>&1 </dev/null &
serve_pid=$!
# wrangler takes a while on a cold start (bundling the middleware + Functions).
# If serve.sh dies (port taken, wrangler error) stop waiting and say so at once.
for _ in $(seq 1 120); do
  curl -s -o /dev/null --max-time 1 "$URL" && break
  kill -0 "$serve_pid" 2>/dev/null || { say "serve.sh exited early"; break; }
  sleep 0.5
done
if ! curl -s -o /dev/null --max-time 2 "$URL"; then
  say "server did not start"
  alert "Server did not start. The log is opening: send it to Claude if it isn't obvious."
  open -a Console "$REPO/local_browse/serve_site.log" 2>/dev/null || open "$REPO/local_browse/serve_site.log"
  exit 1
fi
open "$URL" && say "cold start ok — opened new tab"
vlog_dashboard

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
