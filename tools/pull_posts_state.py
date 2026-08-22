#!/usr/bin/env python3
"""Mirror the production Posts drafts into the local wrangler dev state.

The deployed site keeps post drafts in R2 (_state/posts.json via /api/posts);
`wrangler pages dev` uses its own local, initially-empty bucket. Local-only
features that operate on posts (e.g. the phone-photos companion) need the
real drafts locally, so this pulls prod -> local dev.

Usage: source .env.deploy && python3 tools/pull_posts_state.py
       (the local dev server must be running on :8788)

One-way: never writes to production.
"""
import hashlib
import json
import os
import sys
import urllib.request

PROJECT = os.environ.get("CF_PAGES_PROJECT")
LOCAL = "http://localhost:8788"


def token(var):
    pw = os.environ.get(var)
    if not pw:
        sys.exit(f"{var} not set — source .env.deploy first")
    return hashlib.sha256(pw.encode()).hexdigest()


def api(base, cookies, method="GET", body=None):
    req = urllib.request.Request(f"{base}/api/posts", method=method,
                                 data=json.dumps(body).encode() if body else None)
    req.add_header("Cookie", cookies)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    if not PROJECT:
        sys.exit("CF_PAGES_PROJECT not set — source .env.deploy first")
    cookies = (f"posts_auth={token('CF_POSTS_PASSWORD')}; "
               f"site_auth={token('CF_SITE_PASSWORD')}")
    prod = api(f"https://{PROJECT}.pages.dev", cookies)
    local = api(LOCAL, cookies)
    api(LOCAL, cookies, "PUT", {"baseVersion": local["version"], "posts": prod["posts"]})
    names = ", ".join(p["name"] for p in prod["posts"]) or "(none)"
    print(f"mirrored {len(prod['posts'])} post(s) from prod v{prod['version']} "
          f"into local dev: {names}")


if __name__ == "__main__":
    main()
