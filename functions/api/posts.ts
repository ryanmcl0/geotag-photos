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
const MAX_BODY_BYTES = 512 * 1024;
const MAX_PHOTOS_PER_POST = 20;   // Instagram carousel limit

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
const tokenFor = async (secret: string) =>
    hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)));

interface PhotoRef { trip: string; id: string; ar?: number; blur?: boolean }
// Behind-the-scenes items from the local-only phone library: {trip, id}
// photos or {trip, file} videos. Uncapped (not part of the IG carousel).
interface PhoneRef { trip: string; id?: string; file?: string; ar?: number }
interface Post { id: string; name: string; created?: string; photos: PhotoRef[]; phone?: PhoneRef[] }
interface PostsDoc { version: number; updated: string | null; posts: Post[] }

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
            (ph.blur === undefined || typeof ph.blur === 'boolean')) &&
        ((p as Post).phone === undefined || (
            Array.isArray((p as Post).phone) &&
            (p as Post).phone!.length <= 200 &&
            (p as Post).phone!.every(ph =>
                ph && typeof ph === 'object' &&
                typeof ph.trip === 'string' && ph.trip.startsWith('phone-') &&
                (typeof ph.id === 'string' || typeof ph.file === 'string')))));
}

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket; CF_POSTS_PASSWORD: string }> = async (context) => {
    const pass = context.env.CF_POSTS_PASSWORD;
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('posts_auth='));
    const val = match ? match.split('=').slice(1).join('=') : null;
    if (!pass || val !== await tokenFor(pass)) {
        return new Response('Not found', { status: 404 });
    }

    const readDoc = async (): Promise<PostsDoc> => {
        const obj = await context.env.PHOTOS_BUCKET.get(STATE_KEY);
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
        let body: { baseVersion?: unknown; posts?: unknown };
        try {
            body = JSON.parse(raw);
        } catch {
            return new Response('Bad request', { status: 400 });
        }
        if (typeof body.baseVersion !== 'number' || !validPosts(body.posts)) {
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
        await context.env.PHOTOS_BUCKET.put(STATE_KEY, JSON.stringify(next), {
            httpMetadata: { contentType: 'application/json' }
        });
        return json({ version: next.version });
    }

    return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, PUT' } });
};
