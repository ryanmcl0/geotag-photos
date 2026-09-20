const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('web/js/analytics.js', 'utf8');

function setup({ url = 'https://photos.example.com/gallery?trip=albania', cookie = '', disabled = false, privacy = false } = {}) {
    const scripts = [], events = [], handlers = {};
    const document = {
        cookie,
        head: { appendChild: script => scripts.push(script) },
        createElement: () => ({ dataset: {} }),
        addEventListener: (name, fn) => { handlers[name] = fn; }
    };
    const context = vm.createContext({ document, location: new URL(url), URL, URLSearchParams,
        navigator: { globalPrivacyControl: privacy },
        localStorage: { getItem: () => disabled ? '1' : null } });
    context.window = context;
    vm.runInContext(code, context);
    context.umami = { track: (name, data) => { events.push({ name, data }); } };
    return { context, scripts, events, handlers, document, load: () => scripts[0].onload() };
}

test('local previews, owner pages, owner sessions and opt-outs never load Umami', () => {
    for (const url of ['http://localhost:8000/', 'http://192.168.1.10/', 'http://[::1]/',
        'https://preview.photo-map-travel.pages.dev/', 'https://photos.example.com/posts',
        'https://photos.example.com/plans/', 'https://photos.example.com/gallery?library=phone']) {
        assert.equal(setup({ url }).scripts.length, 0, url);
    }
    for (const options of [{ cookie: 'posts_auth=abc' }, { disabled: true }, { privacy: true }]) {
        assert.equal(setup(options).scripts.length, 0);
    }
});

test('opening slide and swipes count once per index change, never preloads', () => {
    const env = setup();
    const callbacks = {};
    let index = 0;
    const gallery = { currItem: { ref: { trip: 'albania', id: 'photo1' } },
        getCurrentIndex: () => index, listen: (event, fn) => { callbacks[event] = fn; } };
    env.context.SiteAnalytics.attachLightbox(gallery, 'map');
    assert.equal(env.events.length, 0, 'early events queued until script loads');
    env.load();
    assert.equal(env.events.length, 1);
    callbacks.afterChange();
    assert.equal(env.events.length, 1);
    index = 1;
    gallery.currItem = { ref: { trip: 'albania', id: 'photo2' } };
    callbacks.afterChange();
    assert.equal(env.events.length, 2);
    assert.equal(env.events[1].data.photo, 'albania/photo2');
    env.document.cookie = 'all_access=unlocked';
    index = 2;
    callbacks.afterChange();
    assert.equal(env.events.length, 3, 'private gallery navigation remains tracked after unlocking');
});

test('payloads strip arbitrary query strings and hashes while keeping trip and campaign attribution', () => {
    const { context } = setup();
    const payload = context.siteAnalyticsBeforeSend('event', {
        url: '/gallery?trip=albania&token=SECRET&utm_source=instagram#SECRET',
        referrer: 'https://search.example.com/find?secret=SECRET'
    });
    assert.equal(payload.url, '/gallery?trip=albania&utm_source=instagram');
    assert.equal(payload.referrer, 'https://search.example.com');
    assert.equal(context.siteAnalyticsBeforeSend('event', { referrer: '' }).referrer, '');
});

test('owner mode cancels automatic pageviews and queued events', () => {
    const env = setup();
    env.context.SiteAnalytics.track('photo_view', { photo: 'trip/photo' });
    env.document.cookie = 'posts_auth=owner';
    env.load();
    assert.equal(env.events.length, 0);
    assert.equal(env.context.siteAnalyticsBeforeSend('event', { url: '/posts' }), false);
});

test('blocked or failing analytics does not interrupt the site', () => {
    const env = setup();
    env.scripts[0].onerror();
    assert.doesNotThrow(() => env.context.SiteAnalytics.track('photo_view', {}));
    env.load();
    env.context.umami.track = () => { throw Error('blocked'); };
    assert.doesNotThrow(() => env.context.SiteAnalytics.track('photo_view', {}));
});

test('filter and outbound clicks have bounded metadata; owner controls are ignored', () => {
    const env = setup();
    env.load();
    const click = el => env.handlers.click({ target: { closest: () => el } });
    click({ closest: () => false, dataset: { year: '2026' }, tagName: 'BUTTON' });
    assert.equal(env.events[0].name, 'filter_click');
    click({ closest: () => true });
    assert.equal(env.events.length, 1);
    click({ closest: () => false, dataset: {}, tagName: 'A',
        href: 'https://example.com/page?secret=hidden', getAttribute: () => 'https://example.com/page?secret=hidden' });
    assert.equal(env.events[1].data.destination, 'https://example.com/page');
});


test('private galleries and existing See All sessions load and send analytics', () => {
    for (const path of ['/gallery?trip=private-trip', '/rooftopping', '/videos']) {
        const env = setup({ url: 'https://photos.example.com' + path, cookie: 'all_access=secret' });
        assert.equal(env.scripts.length, 1);
        env.load();
        env.context.SiteAnalytics.track('photo_view', { photo: 'private-trip/photo' });
        assert.equal(env.events.length, 1);
        const payload = env.context.siteAnalyticsBeforeSend('event', { url: path });
        assert.ok(payload);
        assert.equal(JSON.stringify(payload).includes('secret'), false);
    }
});
