#!/bin/zsh
# Local attack suite for the site's access rules (functions/). Runs the real
# Functions under `wrangler pages dev` on 127.0.0.1 with a throwaway, simulated
# R2 bucket seeded with one dummy object per privacy class (picked from
# functions/photos/private_index.json and web/trips/index.json), then tries to
# reach each class the way an outsider would: no cookie, forged and old-style
# cookies, other host names, See All, posts. Finally restarts without
# CF_COOKIE_SECRET and checks nobody can sign in at all.
#
# The whole attack runs twice: with the site password gate on, and with it off
# (CF_SITE_PASSWORD unset, the public map). Off, anyone gets the public map and
# photos, and every private class must still 404. Both runs happen whichever
# mode .env.deploy is in, so switching the gate either way is tested before it
# happens.
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
# The runtime version production uses (deploy.py writes it into wrangler.toml).
COMPAT_DATE=$(sed -nE "s/^COMPATIBILITY_DATE = '(.+)'$/\\1/p" deploy.py)
[ -n "$COMPAT_DATE" ] || { echo "security_local: COMPATIBILITY_DATE not found in deploy.py"; exit 1; }
PORT=${SECURITY_TEST_PORT:-8799}
B="http://127.0.0.1:$PORT"
TMP=$(mktemp -d)
# Stopping a server = everything started with this run's --persist-to: killing only the
# port's listener (workerd) left the npm and wrangler parents running after every
# run (2026-10-17: ~50 of them had piled up).
trap '{ pkill -f -- "--persist-to $TMP"; kill $(lsof -ti tcp:$PORT -sTCP:LISTEN); } 2>/dev/null; rm -rf "$TMP"' EXIT
pass=0; fail=0

# The site password the gate-on run signs in with: the live one, or the one kept
# in .env.deploy while the gate is off.
SITE_PW=${CF_SITE_PASSWORD:-${CF_SITE_PASSWORD_SAVED:-}}
QR=${CF_QR_ACCESS_TOKEN:-}

# --- one key per privacy class -------------------------------------------------
python3 - "$TMP/keys" <<'PY' || { echo "security_local: could not pick test keys"; exit 1; }
import json, sys
a = json.load(open('functions/photos/private_index.json'))
idx = json.load(open('web/trips/index.json'))['trips']
public = [t['id'] for t in idx if t.get('public') is not False and t['id'] not in a['private_trips']]
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
PY
key(){ awk -v c=$1 '$1==c{print $2}' "$TMP/keys"; }
enc(){ python3 -c "import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))" "$1"; }
PRIVATE_CLASSES=(private_trip private_in_public blocked private_suffix unknown_slug state)

printf 'x' > "$TMP/obj.webp"
for c in public $PRIVATE_CLASSES; do
    npx wrangler r2 object put "$CF_R2_BUCKET/$(key $c)" --file="$TMP/obj.webp" \
        --local --persist-to "$TMP/state" >/dev/null 2>&1 || { echo "security_local: seeding $c failed"; exit 1; }
done

start(){   # start <cookie key: yes|no> <site gate: on|off>
    local extra=()
    [ "$1" = yes ] && extra+=(--binding CF_COOKIE_SECRET="$CF_COOKIE_SECRET")
    [ "$2" = on ] && extra+=(--binding CF_SITE_PASSWORD="$SITE_PW")
    [ -n "$QR" ] && extra+=(--binding CF_QR_ACCESS_TOKEN="$QR")
    (npx wrangler pages dev web --compatibility-date="$COMPAT_DATE" --ip 127.0.0.1 --port "$PORT" \
        --persist-to "$TMP/state" \
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
    else fail=$((fail+1)); printf '  ✗ %-62s got %s, want %s\n' "$label" "$got" "$want"; fi
}
# no_cookie <label> <jar>: the jar must not hold a site_auth cookie
no_cookie(){
    if grep -q site_auth "$2" 2>/dev/null; then fail=$((fail+1)); echo "  ✗ $1: a site_auth cookie was issued"
    else pass=$((pass+1)); fi
}
photo(){ echo "$B/photos/$(enc "$(key $1)" | sed 's#%2F#/#g')"; }

suite(){   # suite on|off
    local g=$1 M="[gate $1]" J="$TMP/jar-$1"
    local OLD_SITE=$(printf %s "$SITE_PW" | shasum -a 256 | cut -d' ' -f1)
    local OLD_ALL=$(printf %s "$CF_ALL_PASSWORD" | shasum -a 256 | cut -d' ' -f1)
    rm -f "$J"; : > "$J"
    start yes $g

    # Other host names (old deployment addresses, branch aliases, anything else).
    expect "$M host <id>.photo-map-travel.pages.dev" '404' -H 'Host: 1a2b3c4d.photo-map-travel.pages.dev' "$B/"
    expect "$M host branch alias" '404' -H 'Host: some-branch.photo-map-travel.pages.dev' "$(photo public)"
    expect "$M host evil.example" '404' -H 'Host: evil.example' "$(photo public)"
    expect "$M host photo-map-travel.pages.dev redirects" '301' -H 'Host: photo-map-travel.pages.dev' "$B/"

    if [ $g = on ]; then
        # No cookie: everything is behind the login.
        for c in public $PRIVATE_CLASSES; do
            expect "$M no cookie: /photos $c" '302' "$(photo $c)"
        done
        expect "$M no cookie: home page" '302' "$B/"
        expect "$M no cookie: /api/posts" '302|404' "$B/api/posts"
        # Forged / pre-HMAC cookies.
        expect "$M old sha256 site cookie" '302' -H "Cookie: site_auth=$OLD_SITE" "$B/"
        expect "$M old sha256 all cookie" '302' -H "Cookie: site_auth=$OLD_SITE; all_access=$OLD_ALL" "$(photo private_trip)"
        expect "$M empty site cookie" '302' -H 'Cookie: site_auth=' "$B/"
        expect "$M ui twin alone unlocks nothing" '302' -H 'Cookie: all_access_ui=1; posts_ui=1' "$(photo private_trip)"
        expect "$M wrong site password" '302' -X POST -d password=wrong "$B/auth"
        # Signed in with the site password: the visitor below.
        curl -s -o /dev/null -c "$J" -X POST --data-urlencode "password=$SITE_PW" "$B/auth"
        expect "$M site: home page" '200' -b "$J" "$B/"
    else
        # No site password: the map is open to everyone, and nothing else is.
        expect "$M home page" '200' "$B/"
        expect "$M /login goes to the map" '302' "$B/login"
        expect "$M old sha256 all cookie" '404' -H "Cookie: all_access=$OLD_ALL" "$(photo private_trip)"
        expect "$M empty all cookie" '404' -H 'Cookie: all_access=' "$(photo private_trip)"
        expect "$M ui twin alone unlocks nothing" '404' -H 'Cookie: all_access_ui=1; posts_ui=1' "$(photo private_trip)"
        # The site login signs nobody in: not the old site password, not a QR invite.
        curl -s -o /dev/null -c "$TMP/j-auth" -X POST --data-urlencode "password=${SITE_PW:-anything}" "$B/auth"
        no_cookie "$M site password" "$TMP/j-auth"
        [ -n "$QR" ] && expect "$M QR token" '401' -X POST --data-urlencode "token=$QR" "$B/auth-qr"
    fi

    # The visitor: signed in to the site (gate on) or anyone at all (gate off).
    # Public photos load; no private class, gated page, gated file or API does.
    local V="$M visitor"
    expect "$V: public photo" '200' -b "$J" "$(photo public)"
    for c in $PRIVATE_CLASSES; do
        expect "$V: /photos $c" '404' -b "$J" "$(photo $c)"
    done
    expect "$V: encoded _state" '404' -b "$J" "$B/photos/%5Fstate/posts.json"
    expect "$V: private trip manifest" '404' -b "$J" "$B$(key private_trip_manifest)"
    expect "$V: manifest.all.json" '404' -b "$J" "$B$(key public_manifest_all)"
    expect "$V: gated collection" '404' -b "$J" "$B/collections/search.all.json"
    expect "$V: manifest.full sidecar" '404' -b "$J" "$B/trips/$(key public_manifest_all | cut -d/ -f3)/manifest.full.zh.json"
    # The asset server decodes escapes and merges slashes before its lookup, so
    # every spelling of a gated file must hit the same gate.
    for p in "$(key private_trip_manifest)" "$(key public_manifest_all)" /collections/search.all.json \
             /plans/ /videos /rooftopping /expeditions/ "$(python3 -c "import json;print(json.load(open('functions/photos/private_index.json'))['private_pages'][0]+'.json')")"; do
        for v in $(python3 - "$p" <<'PY'
import sys
p = sys.argv[1]; segs = p.split('/')
first = segs[1]
print('/' + p)                                                  # //x/y
print(p.replace('/', '//', 2).replace('//', '/', 1))            # /x//y
print('/%' + format(ord(first[0]), '02X') + p[2:])              # first letter escaped
if p.count('/') > 1 and not p.endswith('/'):
    i = p.rindex('/'); print(p[:i] + '%2F' + p[i+1:])           # last slash escaped
if '.' in segs[-1]:
    i = p.rindex('.'); print(p[:i] + '%2E' + p[i+1:])           # extension dot escaped
PY
); do
            expect "$V: spelled $v" '302|404' -b "$J" "$B$v"
        done
    done
    for p in /rooftopping /plans/ /videos; do expect "$V: $p" '302' -b "$J" "$B$p"; done
    expect "$V: /expeditions/" '404' -b "$J" "$B/expeditions/"
    for p in /api/posts /api/people /api/source-index; do expect "$V: $p" '404' -b "$J" "$B$p"; done
    expect "$V: wrong all password" '401' -b "$J" -X POST -d password=wrong "$B/auth-all"
    expect "$V: wrong posts password" '404' -b "$J" -X POST -d password=wrong "$B/auth-posts"

    # See All.
    curl -s -o /dev/null -b "$J" -c "$J" -X POST --data-urlencode "password=$CF_ALL_PASSWORD" "$B/auth-all"
    if grep -q '#HttpOnly_127.0.0.1.*all_access' "$J"; then pass=$((pass+1)); else fail=$((fail+1)); echo "  ✗ $M all_access cookie is not HttpOnly"; fi
    for c in private_trip private_in_public private_suffix unknown_slug; do
        expect "$M all: /photos $c" '200' -b "$J" "$(photo $c)"
    done
    expect "$M all: blocked stays hidden" '404' -b "$J" "$(photo blocked)"
    expect "$M all: _state stays hidden" '404' -b "$J" "$(photo state)"
    expect "$M all: /rooftopping" '200' -b "$J" "$B/rooftopping"
    expect "$M all: /api/posts still owner-only" '404' -b "$J" "$B/api/posts"
    curl -s -o /dev/null -b "$J" -c "$J" -X POST -d tier=all "$B/auth-lock"
    expect "$M after lock: private trip" '404' -b "$J" "$(photo private_trip)"

    # Posts (owner).
    curl -s -o /dev/null -b "$J" -c "$J" -X POST --data-urlencode "password=$CF_POSTS_PASSWORD" "$B/auth-posts"
    expect "$M posts: /api/posts" '200' -b "$J" "$B/api/posts"
    expect "$M posts: blocked stays hidden" '404' -b "$J" "$(photo blocked)"
    curl -s -o /dev/null -b "$J" -c "$J" -X POST -d tier=posts "$B/auth-lock"
    expect "$M after exit: /api/posts" '404' -b "$J" "$B/api/posts"

    # Without the cookie key nobody can sign in, and a missing cookie never matches.
    stop; start no $g
    local K="$TMP/jar-$g-nokey"; rm -f "$K"
    if [ $g = on ]; then
        expect "$M no key: correct site password issues nothing" '302' -c "$K" -X POST --data-urlencode "password=$SITE_PW" "$B/auth"
        no_cookie "$M no key" "$K"
        expect "$M no key: home page without cookie" '302' "$B/"
    else
        expect "$M no key: home page" '200' "$B/"
        expect "$M no key: public photo" '200' "$(photo public)"
        expect "$M no key: private trip" '404' "$(photo private_trip)"
    fi
    expect "$M no key: all password" '401' -X POST --data-urlencode "password=$CF_ALL_PASSWORD" "$B/auth-all"
    expect "$M no key: posts password" '404' -X POST --data-urlencode "password=$CF_POSTS_PASSWORD" "$B/auth-posts"
    expect "$M no key: private trip after both" "$([ $g = on ] && echo 302 || echo 404)" -c "$K" -b "$K" "$(photo private_trip)"
    stop
}

modes=(off)
if [ -n "$SITE_PW" ]; then modes=(on off)
else echo "security_local: no site password in .env.deploy (CF_SITE_PASSWORD or CF_SITE_PASSWORD_SAVED): gate-on run skipped"; fi
for m in $modes; do suite $m; done

if [ $fail -gt 0 ]; then echo "security_local: $fail failed, $pass passed (gate ${(j:, :)modes})"; exit 1; fi
echo "security_local: all $pass checks passed (gate ${(j:, :)modes})"
