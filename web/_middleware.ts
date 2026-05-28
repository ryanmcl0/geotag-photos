/**
 * Cloudflare Pages middleware — cookie-based auth.
 * Set CF_SITE_PASSWORD as a Pages secret to enable protection.
 */

export const onRequest: PagesFunction<{ CF_SITE_PASSWORD: string }> = async (context) => {
    const password = context.env.CF_SITE_PASSWORD;
    if (!password) return context.next();

    const url = new URL(context.request.url);
    // Always allow the login page and the auth POST endpoint through
    if (url.pathname === '/login.html' || url.pathname === '/auth') {
        return context.next();
    }

    // Check auth cookie
    const cookies = context.request.headers.get('Cookie') || '';
    const match = cookies.split(';').map(c => c.trim()).find(c => c.startsWith('site_auth='));
    if (match && match.split('=').slice(1).join('=') === password) {
        return context.next();
    }

    return Response.redirect(new URL('/login.html', context.request.url), 302);
};
