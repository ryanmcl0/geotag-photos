/**
 * POST /auth — validate password and set auth cookie.
 */

export const onRequestPost: PagesFunction<{ CF_SITE_PASSWORD: string }> = async (context) => {
    const formData = await context.request.formData();
    const password = formData.get('password') as string;
    const correct = context.env.CF_SITE_PASSWORD;

    if (password && password === correct) {
        // Only mark the cookie Secure on HTTPS — over http (e.g. local LAN-IP
        // testing on a phone at http://192.168.x.x) browsers drop Secure cookies,
        // which would bounce the user straight back to /login. Mirrors /auth-all.
        const isSecure = new URL(context.request.url).protocol === 'https:';
        return new Response(null, {
            status: 302,
            headers: {
                'Location': '/',
                'Set-Cookie': `site_auth=${password}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000${isSecure ? '; Secure' : ''}`
            }
        });
    }

    return new Response(null, {
        status: 302,
        headers: { 'Location': '/login.html?error=1' }
    });
};
