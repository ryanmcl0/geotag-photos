/**
 * Local-only "Phone" library tab.
 *
 * The phone mirror dataset (web/phone/, built by tools/build_phone_site.py
 * from the NAS phone_browse library) is git-ignored and excluded from deploy,
 * so it only exists on machines where it has been built. This script probes
 * for it and only then injects the nav entry, which keeps the committed code
 * inert everywhere else (prod probes 404 and nothing renders).
 */
(function () {
    const active = new URLSearchParams(location.search).get('library') === 'phone';

    function injectNav() {
        document.querySelectorAll('.nav-more-menu').forEach(menu => {
            if (menu.querySelector('a[data-phone-lib]')) return;
            const a = document.createElement('a');
            a.href = '/map.html?library=phone';
            a.textContent = 'Phone';
            a.dataset.phoneLib = '1';
            menu.appendChild(a);
        });
    }

    // Mode pill (same idea as the Posts pill): shows the active library and
    // an exit back to the camera map.
    function addPill() {
        if (!active || document.getElementById('phone-pill')) return;
        const pill = document.createElement('div');
        pill.id = 'phone-pill';
        pill.style.cssText = 'position:fixed;bottom:14px;right:14px;z-index:1200;' +
            'background:#1d4ed8;color:#fff;font:12px/1 -apple-system,sans-serif;' +
            'padding:8px 12px;border-radius:999px;display:flex;gap:10px;' +
            'align-items:center;opacity:.92';
        const label = document.createElement('span');
        label.textContent = 'Phone library';
        const exit = document.createElement('a');
        exit.textContent = '✕';
        exit.href = '/map.html';
        exit.title = 'Back to camera library';
        exit.style.cssText = 'color:#fff;text-decoration:none;font-weight:700';
        pill.append(label, exit);
        document.body.appendChild(pill);
    }

    fetch('/phone/trips/index.json', { method: 'HEAD' }).then(r => {
        // content-type guard: a SPA-style HTML fallback must not count as present
        if (!r.ok || !(r.headers.get('content-type') || '').includes('json')) return;
        const run = () => { injectNav(); addPill(); };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
        else run();
    }).catch(() => {});
})();
