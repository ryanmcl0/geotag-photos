#!/usr/bin/env python3
"""Take superseded Pages deployments offline and unbind R2 from previews.

Every Pages deployment stays online at <id>.photo-map-travel.pages.dev, bound
to the live R2 bucket but running the Functions (and privacy index) it shipped
with, so an old one keeps serving photos made private after it was built.
This deletes every production deployment except the live one, detaches the R2
bucket from the preview environment (which has none of the passwords), then
probes each deleted address to confirm it no longer serves.

Never touched: the live deployment (the project's canonical one), anything
with an alias (custom domain or branch address), and preview deployments
(the feature/qr-site-access placeholder must stay, see cloudflare-infra).

  source .env.deploy && python3 tools/purge_old_deployments.py         # list only
  source .env.deploy && python3 tools/purge_old_deployments.py --yes   # do it
"""

import json
import os
import sys
import urllib.error
import urllib.request

PROJECT = os.environ.get('CF_PAGES_PROJECT', 'photo-map-travel')
ACCOUNT = os.environ.get('CF_ACCOUNT_ID')
TOKEN = os.environ.get('CF_API_TOKEN')
API = f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/pages/projects/{PROJECT}'


def call(method, path='', body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {TOKEN}',
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b'{}') or {'success': False, 'errors': [str(e)]}


def status(url):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        return urllib.request.build_opener(NoRedirect).open(url, timeout=20).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def main():
    if not (ACCOUNT and TOKEN):
        sys.exit('CF_ACCOUNT_ID / CF_API_TOKEN not set: source .env.deploy first')
    apply = '--yes' in sys.argv

    project = call('GET')['result']
    live = project['canonical_deployment']['id']
    deps, page = [], 1
    while True:
        res = call('GET', f'/deployments?per_page=25&page={page}').get('result') or []
        deps += res
        if len(res) < 25:
            break
        page += 1

    doomed = [d for d in deps if d['environment'] == 'production'
              and d['id'] != live and not d.get('aliases')]
    print(f'Live deployment (kept): {project["canonical_deployment"]["url"]} '
          f'{project["canonical_deployment"].get("aliases")}')
    print(f'Kept as well: {len(deps) - len(doomed) - 1} preview/aliased deployment(s)')
    print(f'To delete: {len(doomed)} superseded production deployments, '
          f'{doomed[-1]["created_on"][:10] if doomed else "-"} to {doomed[0]["created_on"][:10] if doomed else "-"}')
    preview_r2 = (project['deployment_configs'].get('preview') or {}).get('r2_buckets') or {}
    print(f'Preview environment R2 bindings to remove: {sorted(preview_r2) or "none"}')
    if not apply:
        print('\nNothing changed. Re-run with --yes to apply.')
        return

    failed = []
    for d in doomed:
        r = call('DELETE', f'/deployments/{d["id"]}')
        if not r.get('success'):
            failed.append((d['url'], r.get('errors')))
    print(f'Deleted {len(doomed) - len(failed)}/{len(doomed)}')
    for url, err in failed:
        print(f'  ✗ {url}: {err}')

    if preview_r2:
        r = call('PATCH', '', {'deployment_configs': {'preview': {
            'r2_buckets': {name: None for name in preview_r2}}}})
        left = ((r.get('result') or {}).get('deployment_configs', {}).get('preview') or {}).get('r2_buckets')
        print('Preview R2 binding removed' if r.get('success') and not left
              else f'✗ preview R2 still bound: {left or r.get("errors")}')

    # A deleted address should stop answering. Anything that still serves the
    # site (200) or its login redirect (302) is a copy that is still online.
    alive = [(d['url'], s) for d in doomed for s in [status(d['url'] + '/')] if s in (200, 301, 302, 308)]
    if alive:
        print(f'✗ {len(alive)} deleted address(es) still answer:')
        for url, s in alive:
            print(f'  {s} {url}')
        sys.exit(1)
    print(f'✓ none of the {len(doomed)} deleted addresses serve anything')


if __name__ == '__main__':
    main()
