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
    ASSETS: Fetcher;
}

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
const tokenFor = async (secret: string) =>
    hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)));

let tripFlagsCache: Promise<Record<string, boolean>> | null = null;

function tripFlags(context: EventContext<Env, string, unknown>): Promise<Record<string, boolean>> {
    if (!tripFlagsCache) {
        tripFlagsCache = (async () => {
            try {
                const res = await context.env.ASSETS.fetch(new URL('/trips/index.json', context.request.url));
                const idx = await res.json() as { trips?: { id: string; public?: boolean }[] };
                const map: Record<string, boolean> = {};
                for (const t of idx.trips || []) map[t.id] = t.public !== false;
                return map;
            } catch {
                tripFlagsCache = null;
                return {};
            }
        })();
    }
    return tripFlagsCache;
}

const PUBLIC_COLLECTIONS = ['/collections/china.json', '/collections/site_stats.json',
    '/collections/gallery_covers.json'];

async function needsAllAccess(path: string, context: EventContext<Env, string, unknown>): Promise<boolean> {
    if (['/rooftopping', '/rooftopping.html'].includes(path)) return true;
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

    if (context.request.method === 'POST' && (path === '/auth' || path === '/auth-all')) {
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

    const sitePassword = context.env.CF_SITE_PASSWORD;
    const allPassword = context.env.CF_ALL_PASSWORD;
    const isAuthPath = ['/login', '/login.html', '/auth', '/auth-all'].includes(path);

    // CF Pages strips .html (308 /login.html → /login).
    if (sitePassword && !isAuthPath) {
        const authed = cookieVal('site_auth') === sitePassword;
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

    const response = await context.next();

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
