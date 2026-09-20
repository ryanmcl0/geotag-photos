/* Site analytics, including unlocked galleries. Excludes auth values and owner tools. */
window.SiteAnalytics = (() => {
    'use strict';
    const website = 'f0c05e42-b349-433c-9552-e9ca6837c98a';
    const pending = [];
    let ready = false;

    function excluded() {
        const host = location.hostname;
        const local = !host.includes('.') || host.endsWith('.local') ||
            host.endsWith('.localhost') || host.includes(':') ||
            /^127\.|^10\.|^192\.168\.|^172\.(1[6-9]|2\d|3[01])\./.test(host);
        const preview = host.endsWith('.pages.dev') && host.split('.').length > 3;
        const ownerPath = /^\/(?:login|auth[^/]*|posts|people|plans|phone)(?:[/.]|$)/.test(location.pathname);
        const ownerSession = /(?:^|;\s*)posts_auth=[^;]+/.test(document.cookie);
        let optedOut = false;
        try { optedOut = !!localStorage.getItem('umami.disabled'); } catch (_) { /* storage unavailable */ }
        return local || preview || ownerPath || ownerSession || optedOut ||
            new URLSearchParams(location.search).get('library') === 'phone' ||
            navigator.doNotTrack === '1' || navigator.globalPrivacyControl === true;
    }

    function cleanUrl(value, referrer = false) {
        if (!value) return '';
        try {
            const url = new URL(value, location.origin);
            if (!/^https?:$/.test(url.protocol)) return '';
            if (referrer && url.origin !== location.origin) return url.origin;
            const query = new URLSearchParams();
            for (const key of ['trip', 'year', 'mode', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content']) {
                if (url.searchParams.has(key)) query.set(key, url.searchParams.get(key).slice(0, 150));
            }
            return url.pathname + (query.size ? '?' + query.toString() : '');
        } catch (_) { return ''; }
    }

    // Recheck on every send: owner mode and opt-out can change without reloading.
    window.siteAnalyticsBeforeSend = (type, payload) => {
        if (excluded()) return false;
        return { ...payload, url: cleanUrl(payload.url || location.href),
            referrer: cleanUrl(payload.referrer || '', true) };
    };

    function send(name, data) {
        if (excluded()) return;
        if (!ready) {
            if (pending.length < 50) pending.push([name, data]);
            return;
        }
        try {
            const result = window.umami.track(name, data);
            if (result && result.catch) result.catch(() => {});
        } catch (_) { /* Analytics must never interrupt browsing. */ }
    }

    function attachLightbox(gallery, source) {
        let lastIndex;
        const view = () => {
            const index = gallery.getCurrentIndex();
            if (index === lastIndex) return;
            lastIndex = index;
            const ref = gallery.currItem && gallery.currItem.ref;
            if (!ref || !ref.trip || !ref.id || ref.trip.startsWith('phone-')) return;
            send('photo_view', { photo: ref.trip + '/' + ref.id, source });
        };
        gallery.listen('afterChange', view);
        // Attach after init: count the opening slide once, then each navigation.
        view();
    }

    if (!excluded()) {
        const script = document.createElement('script');
        script.defer = true;
        script.src = 'https://cloud.umami.is/script.js';
        script.dataset.websiteId = website;
        script.dataset.beforeSend = 'siteAnalyticsBeforeSend';
        script.dataset.excludeHash = 'true';
        script.dataset.doNotTrack = 'true';
        script.onload = () => {
            ready = true;
            pending.splice(0).forEach(([name, data]) => send(name, data));
        };
        script.onerror = () => { pending.length = 0; };
        document.head.appendChild(script);

        document.addEventListener('click', event => {
            const el = event.target.closest && event.target.closest('a, button, .year-filter-option, .country-filter-option');
            if (!el || el.closest('.posts-overlay, #pw-overlay, #mobile-pw-overlay')) return;
            for (const key of ['year', 'country', 'filter', 'layer', 'kind']) {
                if (el.dataset[key] !== undefined) {
                    send('filter_click', { filter: key, value: el.dataset[key] || 'all' });
                    return;
                }
            }
            if (el.tagName !== 'A' || !el.getAttribute('href') || el.getAttribute('href').startsWith('#')) return;
            const url = new URL(el.href, location.href);
            if (!/^https?:$/.test(url.protocol) || /^\/(?:posts|people|plans|phone|login|auth[^/]*)(?:[/.]|$)/.test(url.pathname)) return;
            send(url.origin === location.origin ? 'navigation_click' : 'outbound_click', {
                destination: url.origin === location.origin ? cleanUrl(url.href) : url.origin + url.pathname
            });
        }, true);
    }
    return { track: send, attachLightbox };
})();
