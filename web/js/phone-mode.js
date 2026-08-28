/**
 * Local-only nav entries: the "Phone" library tab and the face-recognition
 * pages.
 *
 * Everything under web/phone/ (the phone mirror from tools/build_phone_site.py,
 * the People cluster browser from local_browse/build_people_ui.py, the
 * photos-of-me review from local_browse/build_friend_review.py) is git-ignored
 * AND excluded from deploy, so it only exists on machines where it has been
 * built. Each entry is probed before its nav link is injected, which keeps the
 * committed code inert everywhere else (prod probes 404 and nothing renders).
 */
(function () {
    const active = new URLSearchParams(location.search).get('library') === 'phone';

    function injectNav() {
        document.querySelectorAll('.nav-more-menu').forEach(menu => {
            if (menu.querySelector('a[data-phone-lib]')) return;
            const a = document.createElement('a');
            // Galleries is the browsing view (year tabs + trip tiles); the map
            // is one click away from the pill once phone mode is on.
            a.href = '/galleries.html?library=phone';
            a.textContent = 'Phone';
            a.dataset.phoneLib = '1';
            menu.appendChild(a);
        });
    }

    // Face pages built by the local_browse tooling. Empty now: /people (injected by
    // posts.js) is the single People entry, and on localhost its document includes
    // the phone library, so there is nothing left for a second link to add. The old
    // browser under /phone/people/ is still built by local_browse/build_people_ui.py
    // if you want it directly — it just isn't in the nav twice.
    // Kept as a list because the probe machinery below is the pattern for adding a
    // local-only page back: a missing page under /phone/ does NOT 404 (the host
    // serves the site index with 200), so presence is confirmed from its <title>.
    const FACE_PAGES = [];

    function injectPage(page) {
        document.querySelectorAll('.nav-more-menu').forEach(menu => {
            if (menu.querySelector(`a[data-local-page="${page.key}"]`)) return;
            const a = document.createElement('a');
            a.href = page.href;
            a.textContent = page.label;
            a.dataset.localPage = page.key;
            menu.appendChild(a);
        });
    }

    function probeFacePages() {
        FACE_PAGES.forEach(page => {
            // Range-limited GET: enough bytes for the <title>, not the whole
            // page (the People browser is ~90KB).
            fetch(page.href, { headers: { Range: 'bytes=0-2047' } })
                .then(r => (r.ok ? r.text() : ''))
                .then(html => {
                    if (!html.includes(page.marker)) return;
                    const run = () => injectPage(page);
                    if (document.readyState === 'loading') {
                        document.addEventListener('DOMContentLoaded', run);
                    } else {
                        run();
                    }
                })
                .catch(() => {});
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

        // Switch between the two phone-library views without leaving the mode.
        const onMap = /\/map(\.html)?$/.test(location.pathname);
        const swap = document.createElement('a');
        swap.textContent = onMap ? 'Galleries' : 'Map';
        swap.href = (onMap ? '/galleries.html' : '/map.html') + '?library=phone';
        swap.title = onMap ? 'Browse phone trips by year' : 'See phone photos on the map';
        swap.style.cssText = 'color:#fff;text-decoration:none;font-weight:600;' +
            'border:1px solid rgba(255,255,255,.5);border-radius:99px;padding:2px 9px';

        const exit = document.createElement('a');
        exit.textContent = '✕';
        exit.href = '/map.html';
        exit.title = 'Back to camera library';
        exit.style.cssText = 'color:#fff;text-decoration:none;font-weight:700';
        pill.append(label, swap, exit);
        document.body.appendChild(pill);
    }

    // One reliable probe for "this machine has a local build": the trips index
    // is JSON, so the HTML fallback is filtered by content type. Only then are
    // the face pages checked, which keeps production at zero extra requests.
    fetch('/phone/trips/index.json', { method: 'HEAD' }).then(r => {
        // content-type guard: a SPA-style HTML fallback must not count as present
        if (!r.ok || !(r.headers.get('content-type') || '').includes('json')) return;
        const run = () => { injectNav(); addPill(); };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
        else run();
        probeFacePages();
    }).catch(() => {});
})();
