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
    CF_QR_ACCESS_TOKEN: string;
    CF_ALL_PASSWORD: string;
    CF_POSTS_PASSWORD: string;
    CF_COOKIE_SECRET: string;
    ASSETS: Fetcher;
    PHOTOS_BUCKET: R2Bucket;
}

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
// A tier's cookie value: HMAC-SHA256 keyed by CF_COOKIE_SECRET over the tier name
// and its password. A bare hash of the password could be cracked back to the
// password offline from one stolen cookie; this can't without the key. Changing
// the password, or rotating CF_COOKIE_SECRET, signs everyone out. No key = null,
// which holds() below never accepts (nor a missing cookie), so it fails closed.
const tokenFor = async (key: string | undefined, tier: string, secret: string) => {
    if (!key) return null;
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key),
        { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(tier + '\n' + secret)));
};

// The real tier cookies are HttpOnly so page scripts (or an injected one) can't
// read them. Pages only need to know whether a tier is unlocked, so each comes
// with a valueless *_ui twin the scripts check instead.
const cookie = (name: string, value: string, url: URL, httpOnly: boolean, maxAge = 2592000) =>
    `${name}=${value};${httpOnly ? ' HttpOnly;' : ''} SameSite=Strict; Path=/; Max-Age=${maxAge}` +
    (url.protocol === 'https:' ? '; Secure' : '');

// Local dev hosts (wrangler pages dev, incl. a phone on the LAN), as in the /photos proxy.
const isLocalHost = (h: string) =>
    ['localhost', '127.0.0.1', '[::1]'].includes(h) || h.endsWith('.local') ||
    /^10\./.test(h) || /^192\.168\./.test(h) || /^172\.(1[6-9]|2\d|3[01])\./.test(h);

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
const SETTINGS_TTL_MS = 5 * 60_000;
type OwnerSettings = { highlightsEnabled?: boolean; bridgesPreview?: boolean };
// null = the doc couldn't be read; each flag decides what that means for it.
let settingsCache: { at: number; data: Promise<OwnerSettings | null> } | null = null;

function ownerSettings(context: EventContext<Env, string, unknown>): Promise<OwnerSettings | null> {
    if (!settingsCache || Date.now() - settingsCache.at > SETTINGS_TTL_MS) {
        const data = (async () => {
            try {
                const obj = await context.env.PHOTOS_BUCKET.get('_state/posts.json');
                if (!obj) return {};
                const doc = await obj.json() as { settings?: OwnerSettings };
                return doc.settings || {};
            } catch {
                settingsCache = null;
                return null;
            }
        })();
        settingsCache = { at: Date.now(), data };
    }
    return settingsCache.data;
}

async function highlightsEnabled(context: EventContext<Env, string, unknown>): Promise<boolean> {
    return (await ownerSettings(context))?.highlightsEnabled === true;
}

// Public Bridges preview (China hub): settings.bridgesPreview, ON unless set to
// false. On, locked visitors get collections/china.preview.json in place of
// china.json: the Bridges page opens as a gallery-less list, one public thumbnail
// per bridge. Off (or the doc unreadable), the original locked tile is served and
// the preview's copied phone thumbnails 404, exactly as before the feature.
async function bridgesPreviewEnabled(context: EventContext<Env, string, unknown>): Promise<boolean> {
    const s = await ownerSettings(context);
    return s !== null && s.bridgesPreview !== false;
}

// gallery_highlights.json is id lists only — the gallery page renders just the ids
// present in the manifest the visitor could load, and images stay behind the proxy.
// trip_blogs.json maps trips to blog slugs + titles, which blogs.html already shows.
// gallery_descriptions.json and gallery_regions.json only ever hold public trips'
// text (private trips' regions sit in gallery_regions.all.json, gated like the rest).
// search.json is the public search index (build_search.py builds it from public
// sources only); its full twin search.all.json stays gated like every other file.
const PUBLIC_COLLECTIONS = ['/collections/china.json', '/collections/mountains.json', '/collections/site_stats.json',
    '/collections/gallery_covers.json', '/collections/gallery_highlights.json',
    '/collections/trip_blogs.json', '/collections/gallery_descriptions.json',
    '/collections/gallery_regions.json', '/collections/search.json'];

async function needsAllAccess(path: string, context: EventContext<Env, string, unknown>): Promise<boolean> {
    // A Chinese sidecar (x.zh.json, written by i18n_zh.py next to x.json) is gated
    // exactly like the file it translates.
    path = path.replace(/\.zh\.json$/, '.json');
    if (['/rooftopping', '/rooftopping.html'].includes(path)) return true;
    // Private trip plans: gate the whole /plans/ section (hub, plan pages, their
    // JSON/GPX/JS/CSS) behind the all-access password, like Urbex/Videos.
    if (path === '/plans' || path === '/plans.html' || path.startsWith('/plans/')) return true;
    // Private blog posts: /blogs/<slug> (+ .html / the tile-metadata .json)
    if ((ACCESS.private_pages || []).some(p => path === p || path.startsWith(p + '.'))) return true;
    if (path.startsWith('/collections/')) return !PUBLIC_COLLECTIONS.includes(path);
    const m = path.match(/^\/trips\/([^/]+)\/(.*)$/);
    if (m) {
        // Only manifest.json (and its .zh twin) is public; every variant
        // (manifest.all.json, a manifest.full stash's sidecar) holds hidden entries.
        if (/\/manifest\.[^/]+\.json$/.test(path)) return true;
        const slug = m[1];
        const stem = (m[2].split('/').pop() || '').replace(/\.[a-z0-9]+$/i, '');
        const fp = ACCESS.force_public[slug] || [];
        if (fp.includes('*') || fp.includes(stem)) return false;
        if ((ACCESS.private_photos[slug] || []).includes(stem)) return true;
        if (slug.endsWith('-private')) return true;
        // Checked before the index.json flags, which read as {} when that fetch
        // fails: a private trip's files must not open up on a transient error.
        if (ACCESS.private_trips.includes(slug)) return true;
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

// The site's home is ryanmcl.com. The original pages.dev address and the www host
// send everyone there, keeping the path, query (and the #qr= fragment, which the
// browser carries across the redirect).
//
// Every other host 404s, the per-deploy ones (<id>.photo-map-travel.pages.dev and
// branch aliases) included. Pages keeps every deployment online forever at its own
// address, still bound to the live R2 bucket but running the code and privacy
// index it shipped with, so an old deploy would keep serving any photo made
// private (or any trip added) after it. Since this check ships inside each
// deployment, a deploy stops answering anywhere the moment it is superseded.
const CANONICAL_HOST = 'ryanmcl.com';
const REDIRECT_HOSTS = ['photo-map-travel.pages.dev', 'www.ryanmcl.com'];

// The asset server looks a file up by its decoded path, with repeated slashes
// merged, so every rule below has to see that same form: the raw pathname keeps
// percent-escapes ("%70lans" is /plans) and doubled slashes. Null for a path the
// rules can't reason about (bad escape, backslash, dot segment): it 404s.
function canonicalPath(raw: string): string | null {
    let p: string;
    try { p = decodeURIComponent(raw); } catch { return null; }
    p = p.replace(/\/{2,}/g, '/');
    if (p.includes('\\') || /(^|\/)\.{1,2}(\/|$)/.test(p)) return null;
    return p;
}

const handle: PagesFunction<Env> = async (context) => {
    const url = new URL(context.request.url);
    const canonical = canonicalPath(url.pathname);

    if (REDIRECT_HOSTS.includes(url.hostname)) {
        url.hostname = CANONICAL_HOST;
        url.protocol = 'https:';
        url.port = '';
        // 301 for page loads; 308 keeps the method and body for anything else (e.g. POST /auth).
        const method = context.request.method;
        return Response.redirect(url.toString(), method === 'GET' || method === 'HEAD' ? 301 : 308);
    }
    if (url.hostname !== CANONICAL_HOST && !isLocalHost(url.hostname)) {
        return new Response('Not found', { status: 404 });
    }
    if (canonical === null) return new Response('Not found', { status: 404 });
    const path = canonical;
    const cookieSecret = context.env.CF_COOKIE_SECRET;
    const cookies = context.request.headers.get('Cookie') || '';
    const cookieVal = (name: string) => {
        const m = cookies.split(';').map(c => c.trim()).find(c => c.startsWith(name + '='));
        return m ? m.split('=').slice(1).join('=') : null;
    };
    const holds = async (name: string, tier: string, secret: string) => {
        const v = cookieVal(name);
        return !!v && v === await tokenFor(cookieSecret, tier, secret);
    };

    if (context.request.method === 'POST' && ['/auth', '/auth-qr', '/auth-all', '/auth-posts'].includes(path)) {
        const ip = context.request.headers.get('CF-Connecting-IP') || 'local';
        const retry = authRetryAfter(ip);
        if (retry) {
            return new Response('Too many attempts. Try again later.', {
                status: 429,
                headers: { 'Retry-After': String(retry) }
            });
        }
    }

    // QR invitations use a separate, revocable bearer token. The browser reads it
    // from the URL fragment on /login (fragments never reach the server), sends it
    // here in a POST body, then replaces the URL with /. The real site password is
    // therefore never embedded in the QR code or exposed to an invited visitor.
    if (path === '/auth-qr' && context.request.method === 'POST') {
        const sitePassword = context.env.CF_SITE_PASSWORD;
        const qrToken = context.env.CF_QR_ACCESS_TOKEN;
        const formData = await context.request.formData();
        const submitted = formData.get('token') as string;

        const token = sitePassword && await tokenFor(cookieSecret, 'site', sitePassword);
        if (token && qrToken && submitted && submitted === qrToken) {
            return new Response(JSON.stringify({ ok: true }), {
                status: 200,
                headers: {
                    'Content-Type': 'application/json',
                    'Cache-Control': 'no-store',
                    'Set-Cookie': cookie('site_auth', token, url, true)
                }
            });
        }
        return new Response(JSON.stringify({ ok: false }), {
            status: 401,
            headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
        });
    }

    if (path === '/auth-all' && context.request.method === 'POST') {
        const allPassword = context.env.CF_ALL_PASSWORD;
        const formData = await context.request.formData();
        const submitted = formData.get('password') as string;

        if (!allPassword || (submitted && submitted === allPassword)) {
            const token = allPassword ? await tokenFor(cookieSecret, 'all', allPassword) : '1';
            if (token) {
                const headers = new Headers({ 'Content-Type': 'application/json' });
                headers.append('Set-Cookie', cookie('all_access', token, url, true));
                headers.append('Set-Cookie', cookie('all_access_ui', '1', url, false));
                return new Response(JSON.stringify({ ok: true }), { status: 200, headers });
            }
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

        const token = postsPassword && await tokenFor(cookieSecret, 'posts', postsPassword);
        if (token && submitted && submitted === postsPassword) {
            const headers = new Headers({ 'Content-Type': 'application/json' });
            headers.append('Set-Cookie', cookie('posts_auth', token, url, true));
            headers.append('Set-Cookie', cookie('posts_ui', '1', url, false));
            return new Response(JSON.stringify({ ok: true }), { status: 200, headers });
        }
        return new Response('Not found', { status: 404 });
    }

    // Lock / exit posts mode. The tier cookies are HttpOnly, so the page can't
    // clear them itself: it posts the tier here, which expires the cookie and its
    // *_ui twin. Clearing cookies needs no auth.
    if (path === '/auth-lock' && context.request.method === 'POST') {
        const tier = (await context.request.formData()).get('tier');
        const names = tier === 'all' ? ['all_access', 'all_access_ui']
            : tier === 'posts' ? ['posts_auth', 'posts_ui'] : [];
        const headers = new Headers({ 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        for (const n of names) headers.append('Set-Cookie', cookie(n, '', url, !n.endsWith('_ui'), 0));
        return new Response(JSON.stringify({ ok: names.length > 0 }), { status: names.length ? 200 : 400, headers });
    }

    const sitePassword = context.env.CF_SITE_PASSWORD;
    const allPassword = context.env.CF_ALL_PASSWORD;
    const isAuthPath = ['/login', '/login.html', '/auth', '/auth-qr', '/auth-all', '/auth-posts'].includes(path);
    // The tab icon is needed on the login page itself, before anyone is signed in;
    // robots.txt must reach crawlers, which never sign in. So is the language layer
    // (interface wording only, the same text as the public repo).
    if (path === '/icons/favicon.svg' || path === '/robots.txt' ||
        path === '/js/i18n.js' || path === '/js/i18n-zh.js') return context.next();

    // With the site gate off there is nothing to sign in to: old links and QR
    // invites (/login#qr=...) land on the map instead of a login form.
    if (!sitePassword && (path === '/login' || path === '/login.html')) {
        return Response.redirect(new URL('/', context.request.url), 302);
    }

    // CF Pages strips .html (308 /login.html → /login).
    if (sitePassword && !isAuthPath) {
        // The cookie holds a hash of the password (set by /auth), not the password.
        const authed = await holds('site_auth', 'site', sitePassword);
        if (!authed) return Response.redirect(new URL('/login', context.request.url), 302);
    }

    const allAccess = allPassword !== undefined && allPassword !== ''
        && await holds('all_access', 'all', allPassword);

    // Expedition Tours: the DeepEast app, built into web/expeditions/ by deploy.py
    // from the private expeditions/ checkout. It exists only for all-access visitors.
    // Locked, every path under it (pages, chunks, RSC payloads, media) 404s as if it
    // was never deployed, rather than offering the unlock prompt like Urbex does,
    // and the nav links to it are stripped from the HTML below.
    if ((path === '/expeditions' || path.startsWith('/expeditions/')) && !allAccess) {
        return new Response('Not found', { status: 404 });
    }

    if (await needsAllAccess(path, context)) {
        // The owner's posts cookie also opens private photo files (only the image
        // files, not manifests or pages), matching the /photos proxy: on localhost
        // the Posts manager loads its thumbnails from these /trips paths.
        const postsPassword = context.env.CF_POSTS_PASSWORD;
        const ownerPhoto = /^\/trips\/[^/]+\/(display|thumbnails)\//.test(path)
            && !!postsPassword && await holds('posts_auth', 'posts', postsPassword);
        if (!allAccess && !ownerPhoto) {
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

    if (path === '/collections/china.json' && await bridgesPreviewEnabled(context)) {
        const preview = await context.env.ASSETS.fetch(new URL('/collections/china.preview.json', url));
        if (preview.ok) {
            const res = new Response(preview.body, preview);
            res.headers.set('Cache-Control', 'no-store');
            return res;
        }
    }
    if (path.startsWith('/previews/bridges/') && !(await bridgesPreviewEnabled(context))) {
        return new Response('Not found', { status: 404 });
    }

    let response = await context.next();

    // While Highlights is off, strip its nav links out of every HTML page so the
    // site looks exactly as it did before the feature existed. The links stay in
    // the static files; turning the flag on simply stops removing them. Links
    // marked data-unlocked-only (Expedition Tours) are likewise removed for anyone
    // without the all-access cookie.
    const contentType = response.headers.get('Content-Type') || '';
    if (contentType.includes('text/html')) {
        const stripHighlights = !(await highlightsEnabled(context));
        if (stripHighlights || !allAccess) {
            let rewriter = new HTMLRewriter();
            if (stripHighlights) rewriter = rewriter.on('a[href$="highlights.html"]', { element(el) { el.remove(); } });
            if (!allAccess) rewriter = rewriter.on('a[data-unlocked-only]', { element(el) { el.remove(); } });
            response = rewriter.transform(response);
        }
        // The same file now renders differently per cookie, so the browser must not
        // revalidate its copy with an ETag: the asset server would answer 304 to an
        // unlocked reload and the locked page (no Expedition Tours link) would stick.
        response = new Response(response.body, response);
        response.headers.delete('ETag');
        response.headers.set('Cache-Control', 'no-cache');
        response.headers.append('Vary', 'Cookie');
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

// The ryanmcl.com zone's edge cache sits in front of this middleware: a stored
// copy is served without running it, to anyone, cookie or not. A photo cached
// as public (immutable, for a year) bypassed the site password, and kept being
// served after it was made private; a 404 cached for a locked visitor hid a
// photo from See All. So nothing leaves here shared-cacheable: every response
// is marked private (browsers still cache it), "public" is dropped, and the
// edge never stores a copy.
export const onRequest: PagesFunction<Env> = async (context) => {
    const res = await handle(context);
    const cc = res.headers.get('Cache-Control') || '';
    if (/\b(private|no-store)\b/.test(cc)) return res;
    const out = new Response(res.body, res);
    const rest = cc.replace(/\bpublic\b\s*,?\s*/, '').trim();
    out.headers.set('Cache-Control', rest ? 'private, ' + rest : 'private');
    return out;
};
