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

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket; CF_ALL_PASSWORD: string }> = async (context) => {
    const parts = context.params.path as string[];
    const key = parts.join('/');
    const slug = parts[0] || '';
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
