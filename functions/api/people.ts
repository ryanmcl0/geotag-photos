/**
 * GET /api/people — the owner-only People index: every deployed camera photo the
 * face index reached, grouped into roster people plus still-unnamed clusters.
 * Built by tools/people_index.py and uploaded by deploy.py to R2 key
 * _state/people.json.
 *
 * Same self-contained auth as /api/posts and /api/source-index (posts_auth cookie
 * must match sha256(CF_POSTS_PASSWORD)); every failure mode is a 404 so the
 * endpoint doesn't reveal the feature exists. Read-only — who is switched on or
 * off is decided in config/people.json and applied at build time, never here.
 *
 * Note this document lists photo ids only. The images themselves still go through
 * the /photos proxy, so gated ones need the See All cookie too and blocked ones
 * are absent from this document entirely.
 */

const STATE_KEY = '_state/people.json';

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
const tokenFor = async (secret: string) =>
    hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)));

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket; CF_POSTS_PASSWORD: string }> = async (context) => {
    const pass = context.env.CF_POSTS_PASSWORD;
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('posts_auth='));
    const val = match ? match.split('=').slice(1).join('=') : null;
    if (!pass || val !== await tokenFor(pass)) {
        return new Response('Not found', { status: 404 });
    }
    if (context.request.method !== 'GET') {
        return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET' } });
    }
    const obj = await context.env.PHOTOS_BUCKET.get(STATE_KEY);
    if (!obj) return new Response('Not found', { status: 404 });
    return new Response(obj.body, {
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
    });
};
