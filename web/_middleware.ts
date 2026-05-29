/**
 * Cloudflare Pages middleware — cookie-based auth.
 * Set CF_SITE_PASSWORD as a Pages secret to enable protection.
 * Set CF_ALL_PASSWORD to enable the "See All" private trips unlock.
 */

export const onRequest: PagesFunction<{
    CF_SITE_PASSWORD: string;
    CF_ALL_PASSWORD: string;
}> = async (context) => {
    const url = new URL(context.request.url);

    // POST /auth-all — validate "see all" password, set all_access cookie
    if (url.pathname === '/auth-all' && context.request.method === 'POST') {
        const allPassword = context.env.CF_ALL_PASSWORD;
        const formData = await context.request.formData();
        const submitted = formData.get('password') as string;

        if (!allPassword || (submitted && submitted === allPassword)) {
            const isSecure = url.protocol === 'https:';
            return new Response(JSON.stringify({ ok: true }), {
                status: 200,
                headers: {
                    'Content-Type': 'application/json',
                    'Set-Cookie': `all_access=1; SameSite=Strict; Path=/; Max-Age=2592000${isSecure ? '; Secure' : ''}`
                }
            });
        }
        return new Response(JSON.stringify({ ok: false }), {
            status: 401,
            headers: { 'Content-Type': 'application/json' }
        });
    }

    const sitePassword = context.env.CF_SITE_PASSWORD;
    if (!sitePassword) return context.next();

    // Always allow the login page and auth endpoints through.
    // CF Pages strips .html extensions (308 /login.html → /login), so check both.
    if (['/login', '/login.html', '/auth', '/auth-all'].includes(url.pathname)) {
        return context.next();
    }

    // Check site auth cookie
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('site_auth='));
    if (match && match.split('=').slice(1).join('=') === sitePassword) {
        return context.next();
    }

    // Redirect to extensionless /login (CF Pages' canonical form)
    return Response.redirect(new URL('/login', context.request.url), 302);
};
