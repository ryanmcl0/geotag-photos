/**
 * GET /photos/* — proxy requests to the private R2 bucket.
 * e.g. /photos/2024-kyrgyzstan/thumbnails/photo.webp
 *   → R2 key: 2024-kyrgyzstan/thumbnails/photo.webp
 */

import ACCESS_INDEX from './private_index.json';

const ACCESS = ACCESS_INDEX as {
    private_trips: string[];
    private_photos: Record<string, string[]>;
    force_public: Record<string, string[]>;
};

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
const tokenFor = async (secret: string) =>
    hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)));

// Pages hands back the path segments still percent-encoded, but R2 keys hold the
// raw filename — so "IMG_0624 (2).webp" arrives as "IMG_0624%20(2).webp" and misses
// the object (404 → 🔒 placeholder tile). Decode every segment before it's used,
// for the bucket lookup AND for the privacy check, which otherwise compares an
// encoded stem against the raw names in private_photos and fails open.
const decode = (s: string) => { try { return decodeURIComponent(s); } catch { return s; } };

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket; CF_ALL_PASSWORD: string }> = async (context) => {
    const parts = (context.params.path as string[]).map(decode);
    const key = parts.join('/');
    const slug = parts[0] || '';

    // Underscore prefixes are reserved for site state (e.g. _state/posts.json),
    // never photos — without this they'd be served publicly with immutable caching.
    if (slug.startsWith('_')) {
        return new Response('Not found', { status: 404 });
    }
    const stem = (parts[parts.length - 1] || '').replace(/\.[a-z0-9]+$/i, '');

    const forced = (ACCESS.force_public[slug] || []).includes(stem);
    const restricted = !forced && (
        ACCESS.private_trips.includes(slug) ||
        (ACCESS.private_photos[slug] || []).includes(stem));

    if (restricted) {
        const pass = context.env.CF_ALL_PASSWORD;
        const cookies = context.request.headers.get('Cookie') || '';
        const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('all_access='));
        const val = match ? match.split('=').slice(1).join('=') : null;
        const expected = pass ? await tokenFor(pass) : null;
        if (expected === null || val !== expected) {
            return new Response('Not found', { status: 404 });
        }
    }

    const object = await context.env.PHOTOS_BUCKET.get(key);
    if (!object) {
        return new Response('Not found', { status: 404 });
    }

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('Cache-Control', restricted ? 'private, max-age=3600' : 'public, max-age=31536000, immutable');

    return new Response(object.body as ReadableStream, { headers });
};
