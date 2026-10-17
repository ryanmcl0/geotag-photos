#!/bin/zsh
# Local attack suite for the site's access rules (functions/). Runs the real
# Functions under `wrangler pages dev` on 127.0.0.1 with a throwaway, simulated
# R2 bucket seeded with one dummy object per privacy class (picked from
# functions/photos/private_index.json and web/trips/index.json), then tries to
# reach each class the way an outsider would: no cookie, forged and old-style
# cookies, other host names, the site password only, See All, posts. Finally
# restarts without CF_COOKIE_SECRET and checks nobody can sign in at all.
#
#   zsh tests/security_local.sh        (deploy.py runs it before every deploy)
#
# Needs .env.deploy (the real passwords, so the login flows are the real ones).
# Never touches the real bucket or Cloudflare; uses 127.0.0.1, never localhost,
# so no test cookie lands on your own dev session.

set -u
cd "${0:A:h}/.."
[ -f .env.deploy ] || { echo "security_local: .env.deploy missing"; exit 1; }
source .env.deploy
PORT=${SECURITY_TEST_PORT:-8799}
B="http://127.0.0.1:$PORT"
TMP=$(mktemp -d)
# Stopping a server = everything started with this run's --persist-to: killing only the
# port's listener (workerd) left the npm and wrangler parents running after every
# run (2026-10-17: ~50 of them had piled up).
trap '{ pkill -f -- "--persist-to $TMP"; kill $(lsof -ti tcp:$PORT -sTCP:LISTEN); } 2>/dev/null; rm -rf "$TMP"' EXIT
pass=0; fail=0

# --- one key per privacy class -------------------------------------------------
python3 - "$TMP/keys" <<'EOF' || { echo "security_local: could not pick test keys"; exit 1; }
import json, sys
a = json.load(open('functions/photos/private_index.json'))
idx = json.load(open('web/trips/index.json'))['trips']
public = [t['id'] for t in idx if t.get('public') is not False and t['id'] not in a['private_trips']]
hidden = lambda s: set(a['private_photos'].get(s, [])) | set(a['blocked_photos'].get(s, [])) | set(a['force_public'].get(s, []))
keys = {}
keys['public'] = f"{public[0]}/thumbnails/SECTEST_PUBLIC.webp"
keys['private_trip'] = f"{a['private_trips'][0]}/thumbnails/SECTEST_PRIV.webp"
s = next(s for s, v in a['private_photos'].items() if v and s in public)
keys['private_in_public'] = f"{s}/thumbnails/{a['private_photos'][s][0]}.webp"
s = next(s for s, v in a['blocked_photos'].items() if v)
keys['blocked'] = f"{s}/display/{a['blocked_photos'][s][0]}.webp"
keys['private_suffix'] = f"{public[0]}-private/thumbnails/SECTEST.webp"
keys['unknown_slug'] = "sectest-unlisted-trip/thumbnails/SECTEST.webp"
keys['state'] = "_state/posts.json"
keys['private_trip_manifest'] = f"/trips/{a['private_trips'][0]}/manifest.json"
keys['public_manifest_all'] = f"/trips/{public[0]}/manifest.all.json"
with open(sys.argv[1], 'w') as f:
    for k, v in keys.items():
        f.write(f"{k} {v}\n")
EOF
key(){ awk -v c=$1 '$1==c{print $2}' "$TMP/keys"; }
enc(){ python3 -c "import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))" "$1"; }

printf 'x' > "$TMP/obj.webp"
for c in public private_trip private_in_public blocked private_suffix unknown_slug state; do
    npx wrangler r2 object put "$CF_R2_BUCKET/$(key $c)" --file="$TMP/obj.webp" \
        --local --persist-to "$TMP/state" >/dev/null 2>&1 || { echo "security_local: seeding $c failed"; exit 1; }
done

start(){   # start(with_secret)
    local extra=()
    [ "$1" = yes ] && extra=(--binding CF_COOKIE_SECRET="$CF_COOKIE_SECRET")
    (npx wrangler pages dev web --compatibility-date=2026-05-29 --ip 127.0.0.1 --port "$PORT" \
        --persist-to "$TMP/state" \
        --binding CF_SITE_PASSWORD="$CF_SITE_PASSWORD" --binding CF_QR_ACCESS_TOKEN="$CF_QR_ACCESS_TOKEN" \
        --binding CF_ALL_PASSWORD="$CF_ALL_PASSWORD" --binding CF_POSTS_PASSWORD="$CF_POSTS_PASSWORD" \
        $extra > "$TMP/wrangler.log" 2>&1 &)
    for i in $(seq 1 60); do curl -s -o /dev/null "$B/robots.txt" && return 0; sleep 1; done
    echo "security_local: wrangler did not start"; tail -20 "$TMP/wrangler.log"; exit 1
}
stop(){ { pkill -f -- "--persist-to $TMP"; kill $(lsof -ti tcp:$PORT -sTCP:LISTEN); } 2>/dev/null; sleep 2; }

# expect <label> <allowed codes, e.g. 404|302> curl-args...
expect(){
    local label=$1 want=$2; shift 2
    local got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@")
    if [[ "$got" =~ ^($want)$ ]]; then pass=$((pass+1))
    else fail=$((fail+1)); printf '  ✗ %-55s got %s, want %s\n' "$label" "$got" "$want"; fi
}
photo(){ echo "$B/photos/$(enc "$(key $1)" | sed 's#%2F#/#g')"; }

start yes
J="$TMP/jar"
OLD_SITE=$(printf %s "$CF_SITE_PASSWORD" | shasum -a 256 | cut -d' ' -f1)
OLD_ALL=$(printf %s "$CF_ALL_PASSWORD" | shasum -a 256 | cut -d' ' -f1)

# No cookie: everything is behind the login.
for c in public private_trip private_in_public blocked private_suffix unknown_slug state; do
    expect "no cookie: /photos $c" '302' "$(photo $c)"
done
expect "no cookie: home page" '302' "$B/"
expect "no cookie: /api/posts" '302|404' "$B/api/posts"

# Other host names (old deployment addresses, branch aliases, anything else).
expect "host <id>.photo-map-travel.pages.dev" '404' -H 'Host: 1a2b3c4d.photo-map-travel.pages.dev' "$B/"
expect "host branch alias" '404' -H 'Host: some-branch.photo-map-travel.pages.dev' "$(photo public)"
expect "host evil.example" '404' -H 'Host: evil.example' "$(photo public)"
expect "host photo-map-travel.pages.dev redirects" '301' -H 'Host: photo-map-travel.pages.dev' "$B/"

# Forged / pre-HMAC cookies.
expect "old sha256 site cookie" '302' -H "Cookie: site_auth=$OLD_SITE" "$B/"
expect "old sha256 all cookie" '302' -H "Cookie: site_auth=$OLD_SITE; all_access=$OLD_ALL" "$(photo private_trip)"
expect "empty site cookie" '302' -H 'Cookie: site_auth=' "$B/"
expect "ui twin alone unlocks nothing" '302' -H 'Cookie: all_access_ui=1; posts_ui=1' "$(photo private_trip)"

# Wrong passwords.
expect "wrong site password" '302' -X POST -d password=wrong "$B/auth"
expect "wrong all password" '401' -X POST -d password=wrong "$B/auth-all"
expect "wrong posts password" '404' -X POST -d password=wrong "$B/auth-posts"

# Site password only.
curl -s -o /dev/null -c "$J" -X POST --data-urlencode "password=$CF_SITE_PASSWORD" "$B/auth"
expect "site: home page" '200' -b "$J" "$B/"
expect "site: public photo" '200' -b "$J" "$(photo public)"
for c in private_trip private_in_public blocked private_suffix unknown_slug state; do
    expect "site: /photos $c" '404' -b "$J" "$(photo $c)"
done
expect "site: encoded _state" '404' -b "$J" "$B/photos/%5Fstate/posts.json"
expect "site: private trip manifest" '404' -b "$J" "$B$(key private_trip_manifest)"
expect "site: manifest.all.json" '404' -b "$J" "$B$(key public_manifest_all)"
expect "site: gated collection" '404' -b "$J" "$B/collections/search.all.json"
for p in /rooftopping /plans/ /videos; do expect "site: $p" '302' -b "$J" "$B$p"; done
expect "site: /expeditions/" '404' -b "$J" "$B/expeditions/"
for p in /api/posts /api/people /api/source-index; do expect "site: $p" '404' -b "$J" "$B$p"; done

# See All.
curl -s -o /dev/null -b "$J" -c "$J" -X POST --data-urlencode "password=$CF_ALL_PASSWORD" "$B/auth-all"
if grep -q '#HttpOnly_127.0.0.1.*all_access' "$J"; then pass=$((pass+1)); else fail=$((fail+1)); echo "  ✗ all_access cookie is not HttpOnly"; fi
for c in private_trip private_in_public private_suffix unknown_slug; do
    expect "all: /photos $c" '200' -b "$J" "$(photo $c)"
done
expect "all: blocked stays hidden" '404' -b "$J" "$(photo blocked)"
expect "all: _state stays hidden" '404' -b "$J" "$(photo state)"
expect "all: /rooftopping" '200' -b "$J" "$B/rooftopping"
expect "all: /api/posts still owner-only" '404' -b "$J" "$B/api/posts"
curl -s -o /dev/null -b "$J" -c "$J" -X POST -d tier=all "$B/auth-lock"
expect "after lock: private trip" '404' -b "$J" "$(photo private_trip)"

# Posts (owner).
curl -s -o /dev/null -b "$J" -c "$J" -X POST --data-urlencode "password=$CF_POSTS_PASSWORD" "$B/auth-posts"
expect "posts: /api/posts" '200' -b "$J" "$B/api/posts"
expect "posts: blocked stays hidden" '404' -b "$J" "$(photo blocked)"
curl -s -o /dev/null -b "$J" -c "$J" -X POST -d tier=posts "$B/auth-lock"
expect "after exit: /api/posts" '404' -b "$J" "$B/api/posts"

# Without the cookie key nobody can sign in, and a missing cookie never matches.
stop; start no
rm -f "$J"
expect "no key: correct site password issues nothing" '302' -c "$J" -X POST --data-urlencode "password=$CF_SITE_PASSWORD" "$B/auth"
if grep -q site_auth "$J" 2>/dev/null; then fail=$((fail+1)); echo "  ✗ no key: a site_auth cookie was issued"; else pass=$((pass+1)); fi
expect "no key: home page without cookie" '302' "$B/"
expect "no key: all password" '401' -X POST --data-urlencode "password=$CF_ALL_PASSWORD" "$B/auth-all"
expect "no key: posts password" '404' -X POST --data-urlencode "password=$CF_POSTS_PASSWORD" "$B/auth-posts"

if [ $fail -gt 0 ]; then echo "security_local: $fail failed, $pass passed"; exit 1; fi
echo "security_local: all $pass checks passed"
