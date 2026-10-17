/**
 * Shared "See All" unlock: cookie check, password modal, lock badges.
 * Pages include this once; elements with [data-gated] prompt for the password
 * when locked, and .lock-badge elements hide once unlocked.
 */
window.Unlock = (function () {
    function unlocked() {
        return document.cookie.split(';').some(c => {
            const t = c.trim();
            return t.startsWith('all_access_ui=') && t.length > 'all_access_ui='.length;
        });
    }

    let pending = null; // { href, onSuccess }

    function ensureModal() {
        if (document.getElementById('pw-overlay')) return;
        const overlay = document.createElement('div');
        overlay.className = 'pw-overlay';
        overlay.id = 'pw-overlay';
        overlay.innerHTML = `
            <div class="pw-modal">
                <h3>🚧 Under Construction</h3>
                <p class="pw-note">This section is still being built. Check back soon.</p>
                <input type="password" id="pw-input" placeholder="Password" autocomplete="current-password">
                <p class="pw-error" id="pw-error"></p>
                <div class="pw-actions">
                    <button class="pw-cancel" id="pw-cancel" type="button">Cancel</button>
                    <button class="pw-submit" id="pw-submit" type="button">Unlock</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        document.getElementById('pw-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
        document.getElementById('pw-input').addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
        document.getElementById('pw-submit').addEventListener('click', submit);
    }

    function open(opts) {
        pending = opts || {};
        ensureModal();
        document.getElementById('pw-overlay').classList.add('visible');
        document.getElementById('pw-input').focus();
    }

    function close() {
        const overlay = document.getElementById('pw-overlay');
        if (!overlay) return;
        overlay.classList.remove('visible');
        document.getElementById('pw-error').textContent = '';
        document.getElementById('pw-input').value = '';
    }

    async function submit() {
        const fd = new FormData();
        fd.append('password', document.getElementById('pw-input').value);
        try {
            const r = await fetch('/auth-all', { method: 'POST', body: fd });
            const j = await r.json();
            if (j.ok) {
                close();
                refreshLocks();
                if (pending && pending.href) location.href = pending.href;
                else if (pending && pending.onSuccess) pending.onSuccess();
                else location.reload();
            } else {
                document.getElementById('pw-error').textContent = 'Incorrect password';
            }
        } catch (err) {
            document.getElementById('pw-error').textContent = 'Error: ' + err.message;
        }
    }

    function refreshLocks() {
        const u = unlocked();
        document.querySelectorAll('.lock-badge').forEach(l => { l.style.display = u ? 'none' : ''; });
        // Static pages (e.g. Blogs) hard-code `tile--locked` in markup; once unlocked,
        // drop it so the cover un-dims and the padlock overlay disappears. (Locked
        // visitors keep it; re-locking reloads the page, restoring the markup.)
        if (u) document.querySelectorAll('.tile--locked').forEach(t => t.classList.remove('tile--locked'));
    }

    // The all_access cookie is HttpOnly: only the server can clear it.
    function lock() {
        fetch('/auth-lock', { method: 'POST', body: new URLSearchParams({ tier: 'all' }) }).finally(() => location.reload());
    }

    function bindNavMore() {
        document.querySelectorAll('.nav-more').forEach(nm => {
            const toggle = nm.querySelector('.nav-more-toggle');
            if (!toggle) return;
            toggle.addEventListener('click', e => {
                e.stopPropagation();
                const isOpen = nm.classList.contains('open');
                document.querySelectorAll('.nav-more.open').forEach(o => { if (o !== nm) o.classList.remove('open'); });
                nm.classList.toggle('open', !isOpen);
            });
        });
        document.addEventListener('click', () => {
            document.querySelectorAll('.nav-more.open').forEach(o => o.classList.remove('open'));
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') document.querySelectorAll('.nav-more.open').forEach(o => o.classList.remove('open'));
        });
    }

    // Site search: a magnifier beside See All on every page with the top nav, and
    // "/" from anywhere opens it (search.html handles "/" itself, to focus the box).
    function bindSearch() {
        const seeall = document.getElementById('seeall-link');
        const onSearch = /\/search(\.html)?$/.test(location.pathname);
        if (seeall && !document.querySelector('.nav-search')) {
            const a = document.createElement('a');
            a.className = 'nav-search';
            a.href = '/search.html';
            a.title = 'Search (/)';
            a.setAttribute('aria-label', 'Search');
            a.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/></svg>';
            if (onSearch) a.classList.add('active');
            seeall.before(a);
        }
        if (onSearch) return;
        document.addEventListener('keydown', e => {
            if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return;
            const t = document.activeElement;
            if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
            e.preventDefault();
            location.href = '/search.html';
        });
    }

    // Phones: the top bar slides away while scrolling down and comes back on any
    // scroll up (styles in site.css). --topnav-h lets things that stick under the
    // bar (galleries year headings, the blog day rail) sit just below it.
    function bindAutoHide() {
        const nav = document.querySelector('.topnav');
        if (!nav) return;
        const phone = window.matchMedia('(max-width: 760px)');
        const measure = () => document.documentElement.style.setProperty('--topnav-h', nav.offsetHeight + 'px');
        measure();
        window.addEventListener('resize', measure);
        let lastY = window.scrollY;
        window.addEventListener('scroll', () => {
            const y = window.scrollY;
            if (Math.abs(y - lastY) < 6) return;   // ignore jitter and iOS bounce
            const away = phone.matches && y > lastY && y > nav.offsetHeight &&
                !nav.querySelector('.nav-more.open');
            nav.classList.toggle('topnav--away', away);
            document.documentElement.classList.toggle('nav-away', away);
            lastY = y;
        }, { passive: true });
    }

    function bind() {
        bindNavMore();
        bindSearch();
        bindAutoHide();
        document.querySelectorAll('[data-gated]').forEach(t => {
            t.addEventListener('click', e => {
                if (unlocked()) return;
                e.preventDefault();
                open({ href: t.getAttribute('href') });
            });
        });
        // See All / Lock toggle in the nav — present on every page, so the user can
        // switch back to public-only from anywhere once unlocked.
        const seeall = document.getElementById('seeall-link');
        if (seeall) {
            seeall.textContent = unlocked() ? 'Lock' : 'See All';
            seeall.addEventListener('click', e => {
                e.preventDefault();
                if (unlocked()) lock(); else open({});
            });
        }
        refreshLocks();
        if (new URLSearchParams(location.search).get('unlock') && !unlocked()) open({});
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
    else bind();

    return { unlocked, open, lock, refreshLocks };
})();
