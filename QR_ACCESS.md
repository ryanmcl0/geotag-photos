# QR site access

The private QR code opens the website through the first, whole-site password
gate. It does not unlock the separate **See All** gate.

The QR contains a random invitation token, not `CF_SITE_PASSWORD`. When scanned,
the login page exchanges that token for the normal 30-day `site_auth` cookie and
removes the token from the visible URL. Treat the QR image as a password: anyone
with a copy can use it until its token is rotated or revoked.

The local files involved are:

- `site-access-qr.png` — the private QR image; ignored by Git.
- `.env.deploy` — contains `CF_QR_ACCESS_TOKEN`; ignored by Git.
- `tools/create_site_access_qr.py` — generates both of the above.

## Rotate the QR code

From the project root, generate a new token and QR image:

```bash
venv/bin/python tools/create_site_access_qr.py
```

This replaces `site-access-qr.png` and updates `CF_QR_ACCESS_TOKEN` in
`.env.deploy`. It does not change the secret on Cloudflare by itself.

Publish the replacement token:

```bash
source .env.deploy
printf '%s' "$CF_QR_ACCESS_TOKEN" | npx wrangler pages secret put \
  CF_QR_ACCESS_TOKEN --project-name "$CF_PAGES_PROJECT"
```

As soon as Cloudflare accepts the new secret, the old QR can no longer start new
sessions. No site deployment is required for a token-only rotation.

Test the replacement QR in a private browsing window or on a device that has not
recently visited the site. A normal browser window may already have a valid login
cookie, which can make an invalid QR appear to work.

## Revoke QR access without replacing it

Delete the Cloudflare secret:

```bash
source .env.deploy
npx wrangler pages secret delete CF_QR_ACCESS_TOKEN \
  --project-name "$CF_PAGES_PROJECT"
```

Confirm the deletion when Wrangler asks. New scans of every existing QR will then
fail, while the normal site password continues to work.

To keep local configuration consistent, remove the `CF_QR_ACCESS_TOKEN` line from
`.env.deploy`, or leave it empty:

```bash
export CF_QR_ACCESS_TOKEN=""
```

Do not run the QR generator again until QR access should be restored, because it
will create a new token and image locally.

## Already logged-in devices

Rotating or deleting the QR token blocks new QR logins. It does not sign out a
browser that already exchanged the QR for a `site_auth` cookie. That cookie lasts
up to 30 days.

To invalidate every existing first-gate session immediately, change
`CF_SITE_PASSWORD` and publish that secret. This also changes the password for
people who type it normally, so use it only when existing access must be removed:

```bash
source .env.deploy
printf '%s' "$CF_SITE_PASSWORD" | npx wrangler pages secret put \
  CF_SITE_PASSWORD --project-name "$CF_PAGES_PROJECT"
```

Make sure `CF_SITE_PASSWORD` has been changed in `.env.deploy` before running that
command. The QR code must then be regenerated and its new token published if QR
access should continue.

## Restore QR access after revocation

Generate and publish a fresh QR using the two rotation commands above. Share only
the newly generated `site-access-qr.png`; older copies remain invalid.
