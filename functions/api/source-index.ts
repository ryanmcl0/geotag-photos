/**
 * GET /api/source-index — the photo source index for the NAS posts-puller:
 * {trip: {photos_path, photos: {id: source_filename}}}, uploaded by deploy.py
 * to R2 key _state/source_index.json on every deploy.
 *
 * Same self-contained auth as /api/posts (posts_auth cookie must match
 * sha256(CF_POSTS_PASSWORD)); every failure mode is a 404 so the endpoint
 * doesn't reveal the feature exists. Read-only.
 */

const STATE_KEY = '_state/source_index.json';

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
// Same HMAC cookie scheme as functions/_middleware.ts (null without the key).
const tokenFor = async (key: string | undefined, tier: string, secret: string) => {
    if (!key) return null;
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key),
        { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(tier + '\n' + secret)));
};

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket; CF_POSTS_PASSWORD: string; CF_COOKIE_SECRET: string }> = async (context) => {
    const pass = context.env.CF_POSTS_PASSWORD;
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('posts_auth='));
    const val = match ? match.split('=').slice(1).join('=') : null;
    if (!pass || !val || val !== await tokenFor(context.env.CF_COOKIE_SECRET, 'posts', pass)) {
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
