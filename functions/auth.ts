/**
 * POST /auth — validate password and set auth cookie.
 */

const hex = (buf: ArrayBuffer) =>
    [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
// Same HMAC cookie scheme as functions/_middleware.ts (null without the key).
const tokenFor = async (key: string | undefined, tier: string, secret: string) => {
    if (!key) return null;
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key),
        { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(tier + '\n' + secret)));
};

export const onRequestPost: PagesFunction<{ CF_SITE_PASSWORD: string; CF_COOKIE_SECRET: string }> = async (context) => {
    const formData = await context.request.formData();
    const password = formData.get('password') as string;
    const correct = context.env.CF_SITE_PASSWORD;
    const token = correct && await tokenFor(context.env.CF_COOKIE_SECRET, 'site', correct);

    if (token && password && password === correct) {
        // Only mark the cookie Secure on HTTPS — over http (e.g. local LAN-IP
        // testing on a phone at http://192.168.x.x) browsers drop Secure cookies,
        // which would bounce the user straight back to /login. Mirrors /auth-all.
        const isSecure = new URL(context.request.url).protocol === 'https:';
        return new Response(null, {
            status: 302,
            headers: {
                'Location': '/',
                'Set-Cookie': `site_auth=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000${isSecure ? '; Secure' : ''}`
            }
        });
    }

    return new Response(null, {
        status: 302,
        headers: { 'Location': '/login.html?error=1' }
    });
};
