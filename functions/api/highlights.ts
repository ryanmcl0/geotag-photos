/**
 * GET /api/highlights — read-only feed for the public Highlights page, built
 * from the owner's post drafts (_state/posts.json). Unlike /api/posts this is
 * open to every site visitor, so the privacy filtering happens HERE, server
 * side, against the same private_index the photos proxy enforces:
 *
 *  - blocked photos never appear, for anyone
 *  - without a valid all_access cookie only public photos are returned, and a
 *    post left with no public photos is omitted entirely (its name included —
 *    a title alone can reveal a private trip)
 *  - with all_access the full carousels come back in their curated order
 *
 * Locked visitors therefore never even receive private {trip, id} references;
 * and if one ever leaked, the /photos proxy would still 404 the image itself.
 *
 * The owner curates the feed from the posts page: a per-post noHighlight flag
 * and doc-level settings (highlightsIg / highlightsXhs, absent = on).
 */

import ACCESS_INDEX from '../photos/private_index.json';

const ACCESS = ACCESS_INDEX as {
    private_trips: string[];
    private_photos: Record<string, string[]>;
    blocked_photos?: Record<string, string[]>;
    force_public: Record<string, string[]>;
};

const STATE_KEY = '_state/posts.json';

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
const tokenFor = async (secret: string) =>
    hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)));

interface PhotoRef { trip: string; id: string; ar?: number }
interface Post {
    id: string; name: string; photos: PhotoRef[];
    platform?: 'ig' | 'xhs'; noHighlight?: boolean;
}
interface PostsDoc {
    posts?: Post[];
    settings?: { highlightsEnabled?: boolean; highlightsIg?: boolean; highlightsXhs?: boolean };
}

// Same precedence as functions/photos/[[path]].ts: blocked beats everything,
// force_public rescues, otherwise private trip / private photo restricts.
// Returns 'blocked' | 'private' | 'public'.
function classify(trip: string, id: string): 'blocked' | 'private' | 'public' {
    if (((ACCESS.blocked_photos || {})[trip] || []).includes(id)) return 'blocked';
    const fp = ACCESS.force_public[trip] || [];
    if (fp.includes('*') || fp.includes(id)) return 'public';
    if (ACCESS.private_trips.includes(trip)) return 'private';
    if ((ACCESS.private_photos[trip] || []).includes(id)) return 'private';
    return 'public';
}

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket; CF_ALL_PASSWORD: string }> = async (context) => {
    if (context.request.method !== 'GET') {
        return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET' } });
    }

    // all_access check mirrors the photos proxy: fails closed when the
    // password is unset, so a misconfigured deploy never exposes privates.
    const pass = context.env.CF_ALL_PASSWORD;
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('all_access='));
    const val = match ? match.split('=').slice(1).join('=') : null;
    const unlocked = !!pass && val === await tokenFor(pass);

    let doc: PostsDoc = {};
    const obj = await context.env.PHOTOS_BUCKET.get(STATE_KEY);
    if (obj) {
        try { doc = await obj.json() as PostsDoc; } catch { doc = {}; }
    }

    const settings = doc.settings || {};
    // Owner feature flag (flipped from the posts manager): while off, this
    // endpoint doesn't exist — same 404 the middleware gives /highlights.
    if (settings.highlightsEnabled !== true) {
        return new Response('Not found', { status: 404 });
    }
    const platformOn = {
        ig: settings.highlightsIg !== false,
        xhs: settings.highlightsXhs !== false,
    };

    const out = [];
    for (const post of doc.posts || []) {
        if (post.noHighlight) continue;
        if (!platformOn[post.platform === 'xhs' ? 'xhs' : 'ig']) continue;
        const photos = [];
        for (const ph of post.photos || []) {
            if (!ph || typeof ph.trip !== 'string' || typeof ph.id !== 'string') continue;
            if (ph.trip.startsWith('phone-')) continue;   // local-only library, never on R2
            const level = classify(ph.trip, ph.id);
            if (level === 'blocked') continue;
            if (level === 'private' && !unlocked) continue;
            const ref: PhotoRef = { trip: ph.trip, id: ph.id };
            if (typeof ph.ar === 'number') ref.ar = ph.ar;
            photos.push(ref);
        }
        if (!photos.length) continue;
        out.push({ id: post.id, title: post.name, photos });
    }

    return new Response(JSON.stringify({ unlocked, posts: out }), {
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
    });
};
