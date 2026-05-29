/**
 * GET /photos/* — proxy requests to the private R2 bucket.
 * e.g. /photos/2024-kyrgyzstan/thumbnails/photo.webp
 *   → R2 key: 2024-kyrgyzstan/thumbnails/photo.webp
 */

export const onRequest: PagesFunction<{ PHOTOS_BUCKET: R2Bucket }> = async (context) => {
    const parts = context.params.path as string[];
    const key = parts.join('/');

    const object = await context.env.PHOTOS_BUCKET.get(key);
    if (!object) {
        return new Response('Not found', { status: 404 });
    }

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('Cache-Control', 'public, max-age=31536000, immutable');

    return new Response(object.body as ReadableStream, { headers });
};
