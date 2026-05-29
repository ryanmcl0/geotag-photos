export async function onRequest(context) {
    if (context.request.method !== 'POST') {
        return new Response('Method Not Allowed', { status: 405 });
    }
    const formData = await context.request.formData();
    const password = formData.get('password');
    const correct = context.env.CF_ALL_PASSWORD;
    if (!correct || password === correct) {
        const isSecure = new URL(context.request.url).protocol === 'https:';
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
