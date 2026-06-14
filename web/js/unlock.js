/**
 * Shared "See All" unlock: cookie check, password modal, lock badges.
 * Pages include this once; elements with [data-gated] prompt for the password
 * when locked, and .lock-badge elements hide once unlocked.
 */
window.Unlock = (function () {
    function unlocked() {
        return document.cookie.split(';').some(c => {
            const t = c.trim();
            return t.startsWith('all_access=') && t.length > 'all_access='.length;
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
                <h3>🔓 See All</h3>
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

    function lock() {
        document.cookie = 'all_access=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
        location.reload();
    }

    function bind() {
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
