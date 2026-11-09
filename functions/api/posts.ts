/**
 * GET/PUT /api/posts — owner-only Instagram post drafts, stored as a single
 * JSON object at R2 key _state/posts.json (the '_' prefix is reserved: the
 * /photos proxy 404s it and prune.py never treats it as a trip).
 *
 * Auth is self-contained (like the photos proxy): requires the posts_auth
 * cookie to match sha256(CF_POSTS_PASSWORD). Every failure mode is a 404 so
 * the endpoint doesn't reveal the feature exists.
 */

const STATE_KEY = '_state/posts.json';
// ?set=auto addresses a second, independent document holding machine-generated
// suggestions (tools/auto_curate_posts.py); the generator replaces it wholesale
// while manual drafts in the main document are never touched.
const AUTO_STATE_KEY = '_state/auto_posts.json';
const MAX_BODY_BYTES = 512 * 1024;
const MAX_PHOTOS_PER_POST = 20;   // Instagram carousel limit

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
// Same HMAC cookie scheme as functions/_middleware.ts (null without the key).
const tokenFor = async (key: string | undefined, tier: string, secret: string) => {
    if (!key) return null;
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key),
        { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(tier + '\n' + secret)));
};

// map: which location card ./post.py pull renders for this photo, if any.
// loc: the place-name label ./post.py pull bakes into the photo, and its style.
// route: ./post.py pull renders one map card of the whole trip's GPS route for it.
interface PhotoRef {
    trip: string; id: string; ar?: number; blur?: boolean;
    map?: 'route' | 'pin' | 'china'; loc?: 'A' | 'C'; route?: boolean;
}
const MAP_STYLES = ['route', 'pin', 'china'];
const LOC_STYLES = ['A', 'C'];
// Behind-the-scenes items from the local-only phone library: {trip, id}
// photos or {trip, file} videos. Uncapped (not part of the IG carousel).
interface PhoneRef { trip: string; id?: string; file?: string; ar?: number }
// Photos previously removed from the post (newest first), kept so the manager
// can offer them back; the client caps the list and drops re-added entries.
interface HistoryRef {
    trip: string; id?: string; file?: string; ar?: number; blur?: boolean;
    map?: 'route' | 'pin' | 'china'; loc?: 'A' | 'C'; route?: boolean; phone?: boolean; waitlist?: boolean; removed?: string;
}
const MAX_HISTORY_PER_POST = 100;
// Demoted photos: still attached to the post but outside the carousel, so they
// do NOT count toward the platform cap (unlike phone items on xhs).
const MAX_WAITLIST_PER_POST = 40;
// platform: 'ig' (default) or 'xhs'. On xhs the phone items are part of the
// carousel, so photos+phone together are capped at 20 (amber from 18 in the client); on ig the phone bucket
// stays behind-the-scenes and uncapped. posted marks published drafts.
// caption/song are free text pulled into <post>/caption.txt by ./post.py pull.
// num is a small stable human-facing number (#N on the card, ./post.py pull N);
// assigned once on creation and never reused, so it survives reorders.
// account marks which IG account the post is for (free string, client cycles
// through its known handles).
// noHighlight opts a draft out of the public Highlights page (/api/highlights).
// trial (xhs drafts, which ./post.py ig also posts to Instagram): a trial reel,
// 'auto' = shared to followers if it performs well, 'manual' = shared by hand.
interface Post {
    id: string; name: string; created?: string; photos: PhotoRef[]; phone?: PhoneRef[];
    platform?: 'ig' | 'xhs'; posted?: boolean; caption?: string; song?: string;
    num?: number; account?: string; history?: HistoryRef[]; waitlist?: PhotoRef[];
    noHighlight?: boolean; trial?: 'auto' | 'manual';
}
// Doc-level owner switches. highlightsEnabled is the Highlights page's master
// feature flag (absent/false = the page doesn't exist anywhere on the site);
// highlightsIg/highlightsXhs pick which platform's drafts feed it (absent = on).
// bridgesPreview opens the China Bridges page to locked visitors as a
// gallery-less preview (absent = on; false = the original locked tile).
interface Settings { highlightsEnabled?: boolean; highlightsIg?: boolean; highlightsXhs?: boolean; bridgesPreview?: boolean }
const SETTINGS_KEYS = ['highlightsEnabled', 'highlightsIg', 'highlightsXhs', 'bridgesPreview'];
// reel_queue: the Reels tab's post queue (localhost page): the paths of reels he
// approved, the slider's span, and what the video project's Mac helper
// (tools/reelbot/queue_sync.py) found for each line and the bot's schedule.
// Absent in a PUT = unchanged, like settings.
interface QueueItem {
    id?: string; path: string; added?: string; status?: string; why?: string; key?: string;
    title?: string; kind?: string; place?: string; frame?: string; dur?: number; thumb?: string;
    when?: string; approved?: boolean; checked?: string;
}
// shortlist: reels he approved but isn't posting yet (only the page reads it; the bot only gets items).
interface ShortlistItem { id?: string; path: string; added?: string }
interface ReelQueue {
    items: QueueItem[]; shortlist?: ShortlistItem[]; span_days?: number; span_set?: string; updated?: string;
    schedule?: Record<string, unknown>; synced?: string;
}
const QUEUE_STATUSES = ['pending', 'valid', 'invalid', 'scheduled', 'posted', 'skipped'];
const QUEUE_TEXT = ['id', 'added', 'why', 'key', 'title', 'kind', 'place', 'frame', 'thumb', 'when', 'checked'];
// reel_rejects: reels he rejected from his Short Form Content pool on the Reels tab, each with his reason. The video
// project's Mac helper (queue_sync.py) keeps the bot from ever offering them and logs each reason for the reel
// builders (Short Form Content/REJECTS.jsonl); noted = when it logged that line. Absent in a PUT = unchanged.
interface RejectItem { id?: string; path: string; reason: string; rejected?: string; noted?: string }
interface ReelRejects { items: RejectItem[]; updated?: string; synced?: string }
const MAX_REJECTS = 1000;
// reel_music_pool: the songs new reels are cut to (his active music pool, owner 2026-11-08). The page adds a line
// ("waiting") or moves one to removed; the video project's Mac helper (queue_sync.py) applies it to the pool file and
// marks it back: in / out / failed (why). Titles only: the song lists themselves stay in the local-only manifest
// (web/phone/reels/music.json), never here. Absent in a PUT = unchanged.
interface PoolLine {
    id: string; tid?: string; title?: string; label?: string; status?: string; why?: string; added?: string;
    removed?: string; applied?: string; source?: string;
}
// rejects: the "Rejected music" box (owner 2026-11-09): a reel whose SONG he never wants again, pasted as its file path
// with an optional reason ("reject"), or a rejected song's Undo ("undo"). The Mac (queue_sync.py) finds the reel's song,
// holds it everywhere and marks the line done / failed (why); song = its key on the Mac, title = its name, uses = the
// reels already cut to it. Kept inside reel_music_pool: one more list beside items and removed, so the save/merge/keep
// rules of the pool cover it with no new field.
interface RejectLine {
    id: string; action: 'reject' | 'undo'; reel?: string; reason?: string; added?: string; status?: string; why?: string;
    song?: string; title?: string; uses?: number; applied?: string;
}
interface ReelMusicPool { items: PoolLine[]; removed?: PoolLine[]; rejects?: RejectLine[]; updated?: string; synced?: string }
const MAX_REJECT_LINES = 300;
const REJECT_STATUSES = ['waiting', 'done', 'failed'];
const REJECT_TEXT: Record<string, number> = {
    id: 1000, reel: 1000, reason: 500, added: 40, why: 300, song: 400, title: 300, applied: 40
};
const MAX_POOL_LINES = 500;
const POOL_STATUSES = ['waiting', 'in', 'out', 'failed'];
const POOL_TEXT: Record<string, number> = {
    id: 1000, tid: 200, title: 300, label: 10, why: 300, added: 40, removed: 40, applied: 40, source: 20
};
interface PostsDoc {
    version: number; updated: string | null; posts: Post[]; settings?: Settings; reel_queue?: ReelQueue;
    reel_rejects?: ReelRejects; reel_music_pool?: ReelMusicPool;
}

function validMusicPool(m: unknown): m is ReelMusicPool | undefined {
    if (m === undefined) return true;
    if (!m || typeof m !== 'object' || Array.isArray(m)) return false;
    const j = m as ReelMusicPool;
    const text = (v: unknown, max: number) => v === undefined || (typeof v === 'string' && v.length <= max);
    const line = (i: unknown) => {
        if (!i || typeof i !== 'object' || Array.isArray(i)) return false;
        const l = i as Record<string, unknown>;
        if (typeof l.id !== 'string' || !l.id.length) return false;
        if (l.status !== undefined && !POOL_STATUSES.includes(l.status as string)) return false;
        return Object.keys(l).every(k => k === 'status' || (k in POOL_TEXT && text(l[k], POOL_TEXT[k])));
    };
    if (!Array.isArray(j.items) || j.items.length > MAX_POOL_LINES) return false;
    if (j.removed !== undefined && (!Array.isArray(j.removed) || j.removed.length > MAX_POOL_LINES)) return false;
    if (!text(j.updated, 40) || !text(j.synced, 40)) return false;
    if (!Object.keys(j).every(k => ['items', 'removed', 'rejects', 'updated', 'synced'].includes(k))) return false;
    const reject = (i: unknown) => {
        if (!i || typeof i !== 'object' || Array.isArray(i)) return false;
        const l = i as Record<string, unknown>;
        if (typeof l.id !== 'string' || !l.id.length) return false;
        if (l.action !== 'reject' && l.action !== 'undo') return false;
        if (l.action === 'reject' && !(typeof l.reel === 'string' && l.reel.trim().length > 0)) return false;
        if (l.action === 'undo' && !(typeof l.song === 'string' && l.song.length > 0)) return false;
        if (l.status !== undefined && !REJECT_STATUSES.includes(l.status as string)) return false;
        if (l.uses !== undefined && !(Number.isInteger(l.uses) && (l.uses as number) >= 0 && (l.uses as number) <= 100000)) return false;
        return Object.keys(l).every(k => ['action', 'status', 'uses'].includes(k) ||
            (k in REJECT_TEXT && text(l[k], REJECT_TEXT[k])));
    };
    if (j.rejects !== undefined && (!Array.isArray(j.rejects) || j.rejects.length > MAX_REJECT_LINES ||
        !j.rejects.every(reject))) return false;
    return j.items.every(line) && (j.removed || []).every(line);
}

function validRejects(r: unknown): r is ReelRejects | undefined {
    if (r === undefined) return true;
    if (!r || typeof r !== 'object' || Array.isArray(r)) return false;
    const j = r as ReelRejects;
    const text = (v: unknown, max: number) => v === undefined || (typeof v === 'string' && v.length <= max);
    if (!Array.isArray(j.items) || j.items.length > MAX_REJECTS) return false;
    if (!text(j.updated, 40) || !text(j.synced, 40)) return false;
    return j.items.every(i =>
        i && typeof i === 'object' &&
        typeof i.path === 'string' && i.path.length > 0 && i.path.length <= 1000 &&
        typeof i.reason === 'string' && i.reason.trim().length > 0 && i.reason.length <= 500 &&
        text(i.id, 1000) && text(i.rejected, 40) && text(i.noted, 40) &&
        Object.keys(i).every(k => ['id', 'path', 'reason', 'rejected', 'noted'].includes(k)));
}

function validQueue(q: unknown): q is ReelQueue | undefined {
    if (q === undefined) return true;
    if (!q || typeof q !== 'object' || Array.isArray(q)) return false;
    const r = q as ReelQueue;
    const text = (v: unknown, max: number) => v === undefined || (typeof v === 'string' && v.length <= max);
    if (!Array.isArray(r.items) || r.items.length > 500) return false;
    if (r.span_days !== undefined &&
        !(Number.isInteger(r.span_days) && r.span_days >= 1 && r.span_days <= 90)) return false;
    if (!text(r.span_set, 40) || !text(r.updated, 40) || !text(r.synced, 40)) return false;
    if (r.schedule !== undefined && (!r.schedule || typeof r.schedule !== 'object' || Array.isArray(r.schedule) ||
        JSON.stringify(r.schedule).length > 200 * 1024)) return false;
    if (r.shortlist !== undefined && !(Array.isArray(r.shortlist) && r.shortlist.length <= 500 &&
        r.shortlist.every(i => i && typeof i === 'object' &&
            typeof i.path === 'string' && i.path.length <= 1000 && text(i.id, 1000) && text(i.added, 40)))) return false;
    return r.items.every(i =>
        i && typeof i === 'object' &&
        typeof i.path === 'string' && i.path.length <= 1000 &&
        (i.status === undefined || QUEUE_STATUSES.includes(i.status)) &&
        QUEUE_TEXT.every(k => text((i as unknown as Record<string, unknown>)[k], k === 'id' ? 1000 : 400)) &&
        (i.dur === undefined || (typeof i.dur === 'number' && i.dur >= 0 && i.dur < 86400)) &&
        (i.approved === undefined || typeof i.approved === 'boolean'));
}

function validSettings(s: unknown): s is Settings | undefined {
    if (s === undefined) return true;
    if (!s || typeof s !== 'object' || Array.isArray(s)) return false;
    return Object.entries(s).every(([k, v]) =>
        SETTINGS_KEYS.includes(k) && typeof v === 'boolean');
}

function validPosts(posts: unknown): posts is Post[] {
    if (!Array.isArray(posts) || posts.length > 200) return false;
    return posts.every(p =>
        p && typeof p === 'object' &&
        typeof (p as Post).id === 'string' &&
        typeof (p as Post).name === 'string' &&
        Array.isArray((p as Post).photos) &&
        (p as Post).photos.length <= MAX_PHOTOS_PER_POST &&
        (p as Post).photos.every(ph =>
            ph && typeof ph === 'object' &&
            typeof ph.trip === 'string' && typeof ph.id === 'string' &&
            (ph.blur === undefined || typeof ph.blur === 'boolean') &&
            (ph.map === undefined || MAP_STYLES.includes(ph.map)) &&
            (ph.loc === undefined || LOC_STYLES.includes(ph.loc)) &&
            (ph.route === undefined || typeof ph.route === 'boolean')) &&
        ((p as Post).platform === undefined || ['ig', 'xhs'].includes((p as Post).platform!)) &&
        ((p as Post).posted === undefined || typeof (p as Post).posted === 'boolean') &&
        ((p as Post).noHighlight === undefined || typeof (p as Post).noHighlight === 'boolean') &&
        ((p as Post).trial === undefined || ['auto', 'manual'].includes((p as Post).trial!)) &&
        ((p as Post).caption === undefined ||
            (typeof (p as Post).caption === 'string' && (p as Post).caption!.length <= 4000)) &&
        ((p as Post).song === undefined ||
            (typeof (p as Post).song === 'string' && (p as Post).song!.length <= 300)) &&
        ((p as Post).num === undefined ||
            (Number.isInteger((p as Post).num) && (p as Post).num! >= 1 && (p as Post).num! <= 1000000)) &&
        ((p as Post).account === undefined ||
            (typeof (p as Post).account === 'string' && (p as Post).account!.length <= 40)) &&
        ((p as Post).platform !== 'xhs' ||
            (p as Post).photos.length + ((p as Post).phone?.length || 0) <= 20) &&
        ((p as Post).phone === undefined || (
            Array.isArray((p as Post).phone) &&
            (p as Post).phone!.length <= 200 &&
            (p as Post).phone!.every(ph =>
                ph && typeof ph === 'object' &&
                typeof ph.trip === 'string' && ph.trip.startsWith('phone-') &&
                (typeof ph.id === 'string' || typeof ph.file === 'string')))) &&
        ((p as Post).waitlist === undefined || (
            Array.isArray((p as Post).waitlist) &&
            (p as Post).waitlist!.length <= MAX_WAITLIST_PER_POST &&
            (p as Post).waitlist!.every(ph =>
                ph && typeof ph === 'object' &&
                typeof ph.trip === 'string' && typeof ph.id === 'string' &&
                (ph.blur === undefined || typeof ph.blur === 'boolean') &&
                (ph.map === undefined || MAP_STYLES.includes(ph.map)) &&
            (ph.loc === undefined || LOC_STYLES.includes(ph.loc)) &&
            (ph.route === undefined || typeof ph.route === 'boolean')))) &&
        ((p as Post).history === undefined || (
            Array.isArray((p as Post).history) &&
            (p as Post).history!.length <= MAX_HISTORY_PER_POST &&
            (p as Post).history!.every(h =>
                h && typeof h === 'object' &&
                typeof h.trip === 'string' &&
                (typeof h.id === 'string' || typeof h.file === 'string') &&
                (h.blur === undefined || typeof h.blur === 'boolean') &&
                (h.map === undefined || MAP_STYLES.includes(h.map)) &&
                (h.loc === undefined || LOC_STYLES.includes(h.loc)) &&
                (h.route === undefined || typeof h.route === 'boolean') &&
                (h.phone === undefined || typeof h.phone === 'boolean') &&
                (h.waitlist === undefined || typeof h.waitlist === 'boolean') &&
                (h.removed === undefined ||
                    (typeof h.removed === 'string' && h.removed.length <= 40))))));
}

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket; CF_POSTS_PASSWORD: string; CF_COOKIE_SECRET: string }> = async (context) => {
    const pass = context.env.CF_POSTS_PASSWORD;
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('posts_auth='));
    const val = match ? match.split('=').slice(1).join('=') : null;
    if (!pass || !val || val !== await tokenFor(context.env.CF_COOKIE_SECRET, 'posts', pass)) {
        return new Response('Not found', { status: 404 });
    }

    const stateKey = new URL(context.request.url).searchParams.get('set') === 'auto'
        ? AUTO_STATE_KEY : STATE_KEY;

    const readDoc = async (): Promise<PostsDoc> => {
        const obj = await context.env.PHOTOS_BUCKET.get(stateKey);
        if (!obj) return { version: 0, updated: null, posts: [] };
        try {
            return await obj.json() as PostsDoc;
        } catch {
            return { version: 0, updated: null, posts: [] };
        }
    };
    const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
        });

    if (context.request.method === 'GET') {
        return json(await readDoc());
    }

    if (context.request.method === 'PUT') {
        const raw = await context.request.text();
        if (raw.length > MAX_BODY_BYTES) return new Response('Payload too large', { status: 413 });
        let body: {
            baseVersion?: unknown; posts?: unknown; settings?: unknown; reel_queue?: unknown; reel_rejects?: unknown;
            reel_music_pool?: unknown;
        };
        try {
            body = JSON.parse(raw);
        } catch {
            return new Response('Bad request', { status: 400 });
        }
        if (typeof body.baseVersion !== 'number' || !validPosts(body.posts) ||
            !validSettings(body.settings) || !validQueue(body.reel_queue) ||
            !validRejects(body.reel_rejects) || !validMusicPool(body.reel_music_pool)) {
            return new Response('Bad request', { status: 400 });
        }
        const current = await readDoc();
        if (current.version !== body.baseVersion) {
            return json(current, 409);
        }
        const next: PostsDoc = {
            version: current.version + 1,
            updated: new Date().toISOString(),
            posts: body.posts
        };
        // A PUT without settings (post.py mirror, older clients) must not wipe
        // the stored switches — absent means "unchanged", not "reset".
        const settings = body.settings !== undefined ? body.settings : current.settings;
        if (settings && Object.keys(settings).length) next.settings = settings;
        // The same for the Reels tab's post queue: a drafts-only save keeps it.
        const queue = body.reel_queue !== undefined ? body.reel_queue as ReelQueue : current.reel_queue;
        if (queue) next.reel_queue = queue;
        // And the reject list: a save that leaves it out (post.py mirror, older pages) keeps it.
        const rejects = body.reel_rejects !== undefined ? body.reel_rejects as ReelRejects : current.reel_rejects;
        if (rejects) next.reel_rejects = rejects;
        // And the music pool, the same way.
        const pool = body.reel_music_pool !== undefined ? body.reel_music_pool as ReelMusicPool : current.reel_music_pool;
        if (pool) next.reel_music_pool = pool;
        await context.env.PHOTOS_BUCKET.put(stateKey, JSON.stringify(next), {
            httpMetadata: { contentType: 'application/json' }
        });
        return json({ version: next.version });
    }

    return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, PUT' } });
};
