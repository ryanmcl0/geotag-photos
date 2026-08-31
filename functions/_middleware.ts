/** Cloudflare Pages middleware — cookie-based auth. */

import ACCESS_INDEX from './photos/private_index.json';

const ACCESS = ACCESS_INDEX as {
    private_trips: string[];
    private_photos: Record<string, string[]>;
    force_public: Record<string, string[]>;
    private_pages?: string[];
};

interface Env {
    CF_SITE_PASSWORD: string;
    CF_ALL_PASSWORD: string;
    CF_POSTS_PASSWORD: string;
    ASSETS: Fetcher;
    PHOTOS_BUCKET: R2Bucket;
}

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
const tokenFor = async (secret: string) =>
    hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)));

// Short TTL: an isolate can live long past a deploy, and a trip flipped to
// private must not keep serving under pre-deploy flags until the isolate recycles.
const TRIP_FLAGS_TTL_MS = 5 * 60_000;
let tripFlagsCache: { at: number; data: Promise<Record<string, boolean>> } | null = null;

function tripFlags(context: EventContext<Env, string, unknown>): Promise<Record<string, boolean>> {
    if (!tripFlagsCache || Date.now() - tripFlagsCache.at > TRIP_FLAGS_TTL_MS) {
        const data = (async () => {
            try {
                const res = await context.env.ASSETS.fetch(new URL('/trips/index.json', context.request.url));
                const idx = await res.json() as { trips?: { id: string; public?: boolean }[] };
                const trips = idx.trips || [];
                if (!trips.length) {   // parsed-but-empty is as suspect as an error — don't cache it
                    tripFlagsCache = null;
                    return {};
                }
                const map: Record<string, boolean> = {};
                for (const t of trips) map[t.id] = t.public !== false;
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

// The Highlights page is an owner-flagged feature: settings.highlightsEnabled in
// the posts doc (_state/posts.json), flipped from the posts manager. While off the
// feature must not exist at all — /highlights 404s and the nav links baked into
// the static HTML are stripped — so the flag is read here, cached like tripFlags
// (an isolate may live long past a flip). Missing doc / read error = off.
const HIGHLIGHTS_TTL_MS = 5 * 60_000;
let highlightsCache: { at: number; data: Promise<boolean> } | null = null;

function highlightsEnabled(context: EventContext<Env, string, unknown>): Promise<boolean> {
    if (!highlightsCache || Date.now() - highlightsCache.at > HIGHLIGHTS_TTL_MS) {
        const data = (async () => {
            try {
                const obj = await context.env.PHOTOS_BUCKET.get('_state/posts.json');
                if (!obj) return false;
                const doc = await obj.json() as { settings?: { highlightsEnabled?: boolean } };
                return doc.settings?.highlightsEnabled === true;
            } catch {
                highlightsCache = null;
                return false;
            }
        })();
        highlightsCache = { at: Date.now(), data };
    }
    return highlightsCache.data;
}

// gallery_highlights.json is id lists only — the gallery page renders just the ids
// present in the manifest the visitor could load, and images stay behind the proxy.
const PUBLIC_COLLECTIONS = ['/collections/china.json', '/collections/site_stats.json',
    '/collections/gallery_covers.json', '/collections/gallery_highlights.json'];

async function needsAllAccess(path: string, context: EventContext<Env, string, unknown>): Promise<boolean> {
    if (['/rooftopping', '/rooftopping.html'].includes(path)) return true;
    // Private trip plans: gate the whole /plans/ section (hub, plan pages, their
    // JSON/GPX/JS/CSS) behind the all-access password, like Urbex/Videos.
    if (path === '/plans' || path === '/plans.html' || path.startsWith('/plans/')) return true;
    // Private blog posts: /blogs/<slug> (+ .html / the tile-metadata .json)
    if ((ACCESS.private_pages || []).some(p => path === p || path.startsWith(p + '.'))) return true;
    if (path.startsWith('/collections/')) return !PUBLIC_COLLECTIONS.includes(path);
    const m = path.match(/^\/trips\/([^/]+)\/(.*)$/);
    if (m) {
        if (path.endsWith('/manifest.all.json')) return true;
        const slug = m[1];
        const stem = decodeURIComponent(m[2].split('/').pop() || '').replace(/\.[a-z0-9]+$/i, '');
        const fp = ACCESS.force_public[slug] || [];
        if (fp.includes('*') || fp.includes(stem)) return false;
        if ((ACCESS.private_photos[slug] || []).includes(stem)) return true;
        if (slug.endsWith('-private')) return true;
        const flags = await tripFlags(context);
        if (flags[slug] === false) return true;
    }
    return false;
}

const AUTH_WINDOW_MS = 60_000;   // sliding window length
const AUTH_MAX_HITS = 10;        // auth POSTs per IP per window before 429
const authHits = new Map<string, number[]>();

function authRetryAfter(ip: string): number {
    const now = Date.now();
    const hits = (authHits.get(ip) || []).filter(t => now - t < AUTH_WINDOW_MS);
    hits.push(now);
    authHits.set(ip, hits);
    if (authHits.size > 5000) {   // opportunistic cleanup so the map can't grow unbounded
        for (const [k, v] of authHits) if (v.every(t => now - t >= AUTH_WINDOW_MS)) authHits.delete(k);
    }
    return hits.length > AUTH_MAX_HITS ? Math.ceil((AUTH_WINDOW_MS - (now - hits[0])) / 1000) : 0;
}

export const onRequest: PagesFunction<Env> = async (context) => {
    const url = new URL(context.request.url);
    const path = url.pathname;
    const cookies = context.request.headers.get('Cookie') || '';
    const cookieVal = (name: string) => {
        const m = cookies.split(';').map(c => c.trim()).find(c => c.startsWith(name + '='));
        return m ? m.split('=').slice(1).join('=') : null;
    };

    if (context.request.method === 'POST' && ['/auth', '/auth-all', '/auth-posts'].includes(path)) {
        const ip = context.request.headers.get('CF-Connecting-IP') || 'local';
        const retry = authRetryAfter(ip);
        if (retry) {
            return new Response('Too many attempts. Try again later.', {
                status: 429,
                headers: { 'Retry-After': String(retry) }
            });
        }
    }

    if (path === '/auth-all' && context.request.method === 'POST') {
        const allPassword = context.env.CF_ALL_PASSWORD;
        const formData = await context.request.formData();
        const submitted = formData.get('password') as string;

        if (!allPassword || (submitted && submitted === allPassword)) {
            const isSecure = url.protocol === 'https:';
            const token = allPassword ? await tokenFor(allPassword) : '1';
            return new Response(JSON.stringify({ ok: true }), {
                status: 200,
                headers: {
                    'Content-Type': 'application/json',
                    'Set-Cookie': `all_access=${token}; SameSite=Strict; Path=/; Max-Age=2592000${isSecure ? '; Secure' : ''}`
                }
            });
        }
        return new Response(JSON.stringify({ ok: false }), {
            status: 401,
            headers: { 'Content-Type': 'application/json' }
        });
    }

    // Owner-only posts unlock. Unlike /auth-all there is no open-when-unset
    // fallback and failures are 404s: with no CF_POSTS_PASSWORD configured, or
    // a wrong guess, the feature doesn't visibly exist.
    if (path === '/auth-posts' && context.request.method === 'POST') {
        const postsPassword = context.env.CF_POSTS_PASSWORD;
        const formData = await context.request.formData();
        const submitted = formData.get('password') as string;

        if (postsPassword && submitted && submitted === postsPassword) {
            const isSecure = url.protocol === 'https:';
            const token = await tokenFor(postsPassword);
            return new Response(JSON.stringify({ ok: true }), {
                status: 200,
                headers: {
                    'Content-Type': 'application/json',
                    'Set-Cookie': `posts_auth=${token}; SameSite=Strict; Path=/; Max-Age=2592000${isSecure ? '; Secure' : ''}`
                }
            });
        }
        return new Response('Not found', { status: 404 });
    }

    const sitePassword = context.env.CF_SITE_PASSWORD;
    const allPassword = context.env.CF_ALL_PASSWORD;
    const isAuthPath = ['/login', '/login.html', '/auth', '/auth-all', '/auth-posts'].includes(path);

    // CF Pages strips .html (308 /login.html → /login).
    if (sitePassword && !isAuthPath) {
        // The cookie holds a hash of the password (set by /auth), not the password.
        const authed = cookieVal('site_auth') === await tokenFor(sitePassword);
        if (!authed) return Response.redirect(new URL('/login', context.request.url), 302);
    }

    if (await needsAllAccess(path, context)) {
        const expected = allPassword ? await tokenFor(allPassword) : null;
        const ok = expected !== null && cookieVal('all_access') === expected;
        if (!ok) {
            const isData = /\.(json|geojson)$/.test(path);
            return isData
                ? new Response('Not found', { status: 404 })
                : Response.redirect(new URL('/?unlock=1', context.request.url), 302);
        }
    }

    // Highlights feature flag: while off the page never existed — its JS/CSS
    // included, so even a guessed asset URL reveals nothing. (/api/highlights
    // makes the same check itself and 404s too.)
    if (path === '/highlights' || path === '/highlights.html' ||
        path === '/js/highlights.js' || path === '/css/highlights.css') {
        if (!(await highlightsEnabled(context))) {
            return new Response('Not found', { status: 404 });
        }
    }

    let response = await context.next();

    // While Highlights is off, strip its nav links out of every HTML page so the
    // site looks exactly as it did before the feature existed. The links stay in
    // the static files; turning the flag on simply stops removing them.
    const contentType = response.headers.get('Content-Type') || '';
    if (contentType.includes('text/html') && !(await highlightsEnabled(context))) {
        response = new HTMLRewriter()
            .on('a[href$="highlights.html"]', { element(el) { el.remove(); } })
            .transform(response);
    }

    // Local dev (serve.sh): never serve anything the browser cached, so edits to
    // HTML/CSS/JS/images always show on reload. Production keeps its real caching.
    if (url.hostname === 'localhost' || url.hostname === '127.0.0.1' || url.hostname === '[::1]') {
        const fresh = new Response(response.body, response);
        fresh.headers.set('Cache-Control', 'no-store, must-revalidate');
        fresh.headers.delete('ETag');
        return fresh;
    }
    return response;
};
