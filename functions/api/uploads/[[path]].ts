/**
 * /api/uploads/<id>/<part> — owner-only photos added to a post draft straight
 * from a device ("＋ From device" on the Posts page): exports that were never
 * processed onto the site. Each upload is three R2 objects under the reserved
 * _uploads/ prefix (the /photos proxy 404s '_' keys, prune.py and deploy never
 * touch them):
 *   original    the file as chosen (what ./post.py pull copies into the post)
 *   display     a WebP the page made for the lightbox
 *   thumbnails  a WebP the page made for the card
 * A draft refers to one as {trip: 'device', id}; js/gallery.js photoUrl maps
 * that here.
 *
 * GET / HEAD read a part, PUT writes one. Same auth as /api/posts (posts_auth
 * cookie) and every failure is a 404, so the endpoint doesn't reveal itself.
 */

interface Env { PHOTOS_BUCKET: R2Bucket; CF_POSTS_PASSWORD: string; CF_COOKIE_SECRET: string }

const PREFIX = '_uploads/';
const PARTS = ['original', 'display', 'thumbnails'];
const ID_RE = /^d[0-9a-z]{8,40}$/;
const MAX_BYTES: Record<string, number> = { original: 95 * 1024 * 1024, display: 8 * 1024 * 1024, thumbnails: 2 * 1024 * 1024 };
const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif', 'image/tiff', 'image/avif'];

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
// Same HMAC cookie scheme as functions/_middleware.ts (null without the key).
const tokenFor = async (key: string | undefined, tier: string, secret: string) => {
    if (!key) return null;
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key),
        { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(tier + '\n' + secret)));
};

// A filename safe to keep as metadata and hand back in a header.
const cleanName = (s: string | null) =>
    (s || '').replace(/[^\w .()+-]/g, '_').replace(/^[ .]+/, '').slice(0, 200);

const notFound = () => new Response('Not found', { status: 404 });

export const onRequest: PagesFunction<Env> = async (context) => {
    const pass = context.env.CF_POSTS_PASSWORD;
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('posts_auth='));
    const val = match ? match.split('=').slice(1).join('=') : null;
    if (!pass || !val || val !== await tokenFor(context.env.CF_COOKIE_SECRET, 'posts', pass)) {
        return notFound();
    }

    const segs = ([] as string[]).concat(context.params.path || []);
    if (segs.length !== 2) return notFound();
    const [id, part] = segs;
    if (!ID_RE.test(id) || !PARTS.includes(part)) return notFound();
    const key = `${PREFIX}${id}/${part}`;
    const method = context.request.method;

    if (method === 'GET' || method === 'HEAD') {
        const obj = method === 'GET'
            ? await context.env.PHOTOS_BUCKET.get(key)
            : await context.env.PHOTOS_BUCKET.head(key);
        if (!obj) return notFound();
        const headers = new Headers();
        headers.set('Content-Type', obj.httpMetadata?.contentType || 'application/octet-stream');
        headers.set('Content-Length', String(obj.size));
        // Browser-only: never cached at the edge (owner-only content).
        headers.set('Cache-Control', 'private, max-age=86400');
        headers.set('X-Content-Type-Options', 'nosniff');
        const name = obj.customMetadata?.name;
        if (name) headers.set('X-Filename', name);
        return new Response(method === 'GET' ? (obj as R2ObjectBody).body : null, { headers });
    }

    if (method === 'PUT') {
        const type = (context.request.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
        if (part === 'original' ? !IMAGE_TYPES.includes(type) : type !== 'image/webp') {
            return new Response('Unsupported type', { status: 415 });
        }
        const len = Number(context.request.headers.get('Content-Length') || 0);
        if (len > MAX_BYTES[part]) return new Response('Payload too large', { status: 413 });
        const body = await context.request.arrayBuffer();
        if (!body.byteLength || body.byteLength > MAX_BYTES[part]) {
            return new Response('Bad size', { status: body.byteLength ? 413 : 400 });
        }
        const name = cleanName(context.request.headers.get('X-Filename'));
        await context.env.PHOTOS_BUCKET.put(key, body, {
            httpMetadata: { contentType: type },
            customMetadata: name ? { name } : undefined,
        });
        return new Response(JSON.stringify({ ok: true }), {
            headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
        });
    }

    return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, HEAD, PUT' } });
};
