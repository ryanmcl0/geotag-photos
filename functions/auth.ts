/**
 * POST /auth — validate password and set auth cookie.
 */

export const onRequestPost: PagesFunction<{ CF_SITE_PASSWORD: string }> = async (context) => {
    const formData = await context.request.formData();
    const password = formData.get('password') as string;
    const correct = context.env.CF_SITE_PASSWORD;

    if (password && password === correct) {
        return new Response(null, {
            status: 302,
            headers: {
                'Location': '/',
                'Set-Cookie': `site_auth=${password}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000`
            }
        });
    }

    return new Response(null, {
        status: 302,
        headers: { 'Location': '/login.html?error=1' }
    });
};
