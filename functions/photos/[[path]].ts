/**
 * GET /photos/* — proxy requests to the private R2 bucket.
 * e.g. /photos/2024-kyrgyzstan/thumbnails/photo.webp
 *   → R2 key: 2024-kyrgyzstan/thumbnails/photo.webp
 */

import ACCESS_INDEX from './private_index.json';

const ACCESS = ACCESS_INDEX as {
    private_trips: string[];
    private_photos: Record<string, string[]>;
    blocked_photos?: Record<string, string[]>;
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

interface Env {
    PHOTOS_BUCKET: R2Bucket;
    CF_ALL_PASSWORD: string;
    CF_POSTS_PASSWORD: string;
}

export const onRequest: PagesFunction<Env> = async (context) => {
    const parts = (context.params.path as string[]).map(decode);
    const key = parts.join('/');
    const slug = parts[0] || '';

    // Underscore prefixes are reserved for site state (e.g. _state/posts.json),
    // never photos — without this they'd be served publicly with immutable caching.
    if (slug.startsWith('_')) {
        return new Response('Not found', { status: 404 });
    }
    const stem = (parts[parts.length - 1] || '').replace(/\.[a-z0-9]+$/i, '');

    // Blocked photos (a person switched off at the strict tier) are refused before
    // any allow-list or cookie is consulted: there is no tier that sees these, and
    // force_public must not be able to rescue one. The object stays in R2.
    if (((ACCESS.blocked_photos || {})[slug] || []).includes(stem)) {
        return new Response('Not found', { status: 404 });
    }

    const forced = (ACCESS.force_public[slug] || []).includes(stem);
    const restricted = !forced && (
        ACCESS.private_trips.includes(slug) ||
        (ACCESS.private_photos[slug] || []).includes(stem));

    // Restricted photos need the all-access cookie, or the owner's posts cookie:
    // the Posts manager is owner-only, so a draft's photos must never show locked
    // there just because See All happens to be off.
    if (restricted) {
        const cookies = context.request.headers.get('Cookie') || '';
        const cookieVal = (name: string) => {
            const m = cookies.split(';').map(c => c.trim()).find(c => c.startsWith(name + '='));
            return m ? m.split('=').slice(1).join('=') : null;
        };
        const holds = async (name: string, secret: string | undefined) =>
            !!secret && cookieVal(name) === await tokenFor(secret);
        if (!(await holds('all_access', context.env.CF_ALL_PASSWORD) ||
              await holds('posts_auth', context.env.CF_POSTS_PASSWORD))) {
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
