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
// Same HMAC cookie scheme as functions/_middleware.ts (null without the key).
const tokenFor = async (key: string | undefined, tier: string, secret: string) => {
    if (!key) return null;
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key),
        { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(tier + '\n' + secret)));
};

// Pages hands back the path segments still percent-encoded, but R2 keys hold the
// raw filename — so "IMG_0624 (2).webp" arrives as "IMG_0624%20(2).webp" and misses
// the object (404 → 🔒 placeholder tile). Decode every segment before it's used,
// for the bucket lookup AND for the privacy check, which otherwise compares an
// encoded stem against the raw names in private_photos and fails open.
const decode = (s: string) => { try { return decodeURIComponent(s); } catch { return s; } };

// Local dev hosts (wrangler pages dev, incl. a phone on the LAN), as in js/gallery.js.
const isLocalHost = (h: string) =>
    ['localhost', '127.0.0.1', '[::1]'].includes(h) || h.endsWith('.local') ||
    /^10\./.test(h) || /^192\.168\./.test(h) || /^172\.(1[6-9]|2\d|3[01])\./.test(h);

interface Env {
    PHOTOS_BUCKET: R2Bucket;
    CF_ALL_PASSWORD: string;
    CF_POSTS_PASSWORD: string;
    CF_COOKIE_SECRET: string;
    ASSETS: Fetcher;
}

// Which trips this deployment knows as public, from its own trips/index.json
// (cached like the middleware's copy). Anything else in the bucket counts as
// private: a trip added after this deploy, a leftover *-private prefix from the
// retired split-trip model, or everything at once if the index can't be read.
const TRIP_FLAGS_TTL_MS = 5 * 60_000;
let tripFlagsCache: { at: number; data: Promise<Record<string, boolean>> } | null = null;

function tripFlags(context: EventContext<Env, string, unknown>): Promise<Record<string, boolean>> {
    if (!tripFlagsCache || Date.now() - tripFlagsCache.at > TRIP_FLAGS_TTL_MS) {
        const data = (async () => {
            try {
                const res = await context.env.ASSETS.fetch(new URL('/trips/index.json', context.request.url));
                const idx = await res.json() as { trips?: { id: string; public?: boolean }[] };
                const map: Record<string, boolean> = {};
                for (const t of idx.trips || []) map[t.id] = t.public !== false;
                if (!Object.keys(map).length) tripFlagsCache = null;
                return map;
            } catch {
                tripFlagsCache = null;
                return {};
            }
        })();
        tripFlagsCache = { at: Date.now(), data };
    }
    return tripFlagsCache.data;
}

export const onRequest: PagesFunction<Env> = async (context) => {
    // Only the live site answers (see the host lock in _middleware.ts, which runs
    // first); repeated here so the bucket is never one middleware edit away.
    const host = new URL(context.request.url).hostname;
    if (host !== 'ryanmcl.com' && !isLocalHost(host)) {
        return new Response('Not found', { status: 404 });
    }
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

    // Blog image folders (blog-<slug>) aren't trips, so they aren't in the index;
    // a private blog's folder ends in -private and is in private_trips.
    const forced = (ACCESS.force_public[slug] || []).includes(stem);
    const known = slug.startsWith('blog-') || (await tripFlags(context))[slug] === true;
    const restricted = !forced && (
        !known || slug.endsWith('-private') ||
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
        const holds = async (name: string, tier: string, secret: string | undefined) => {
            const v = cookieVal(name);
            return !!secret && !!v && v === await tokenFor(context.env.CF_COOKIE_SECRET, tier, secret);
        };
        if (!(await holds('all_access', 'all', context.env.CF_ALL_PASSWORD) ||
              await holds('posts_auth', 'posts', context.env.CF_POSTS_PASSWORD))) {
            return new Response('Not found', { status: 404 });
        }
    }

    const object = await context.env.PHOTOS_BUCKET.get(key);
    if (!object) {
        // wrangler pages dev simulates an empty bucket. Locally, serve the same file
        // from the web/trips/<trip>/{display,thumbnails} symlinks into hosted-photos
        // (after the checks above), so prebuilt pages that use /photos URLs, like
        // Expedition Tours, render on localhost too.
        if (isLocalHost(new URL(context.request.url).hostname)) {
            const local = await context.env.ASSETS.fetch(new URL('/trips/' + parts.map(encodeURIComponent).join('/'), context.request.url));
            if (local.ok && (local.headers.get('Content-Type') || '').startsWith('image/')) return local;
        }
        return new Response('Not found', { status: 404 });
    }

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    // Never "public": the zone's edge cache would serve that copy to anyone (see the
    // onRequest wrapper in _middleware.ts). Browsers may still keep it.
    headers.set('Cache-Control', restricted ? 'private, max-age=3600' : 'private, max-age=31536000, immutable');

    return new Response(object.body as ReadableStream, { headers });
};
