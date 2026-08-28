/**
 * Owner-only People browser.
 *
 * Shows every deployed camera photo the local face index reached, grouped into
 * the named people from config/people.json plus the clusters that are still
 * unnamed. Read-only: it is the place to SEE who appears where, so you can decide
 * who to switch off. Switching someone off happens in config/people.json and is
 * applied at build time by tools/people_index.py → photo_privacy.py.
 *
 * Gated behind the same posts_auth cookie as /posts (a separate owner-only
 * password, NOT See All): the document lives at R2 _state/people.json and is
 * served by /api/people, which 404s without the cookie.
 *
 * Photos are marked public or gated. Gated thumbnails come through the /photos
 * proxy and additionally need the See All cookie, so without it they render as
 * padlock placeholders rather than broken images. Blocked photos are not in the
 * document at all.
 */
window.People = (function () {
    const PAGE_SIZE = 120;   // photos rendered per person before "Show more"

    function unlocked() {
        return document.cookie.split(';').some(c => {
            const t = c.trim();
            return t.startsWith('posts_auth=') && t.length > 'posts_auth='.length;
        });
    }

    function renderPasswordForm(root) {
        root.innerHTML = `
            <div class="posts-login">
                <h1>People</h1>
                <input type="password" id="people-pw" placeholder="Password" autocomplete="current-password">
                <p class="posts-login-error" id="people-pw-error"></p>
                <button type="button" id="people-pw-submit">Enter</button>
            </div>`;
        const input = root.querySelector('#people-pw');
        const error = root.querySelector('#people-pw-error');
        async function submit() {
            const fd = new FormData();
            fd.append('password', input.value);
            try {
                const r = await fetch('/auth-posts', { method: 'POST', body: fd });
                if (r.ok) { location.reload(); return; }
            } catch (e) { /* fall through to the generic message */ }
            error.textContent = 'Nope.';
            input.value = '';
            input.focus();
        }
        root.querySelector('#people-pw-submit').addEventListener('click', submit);
        input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
        input.focus();
    }

    // ---------- Rendering ----------

    const HIDE_LABEL = {
        gated: 'Gated — off the public site, still visible with See All',
        blocked: 'Blocked — hidden from every tier',
    };

    function statusChip(person) {
        const chip = document.createElement('span');
        if (!person.hide) {
            chip.className = 'people-chip people-chip--on';
            chip.textContent = 'Visible';
        } else {
            chip.className = 'people-chip people-chip--' + person.hide;
            chip.textContent = person.hide === 'blocked' ? 'Blocked' : 'Gated';
            chip.title = HIDE_LABEL[person.hide];
        }
        return chip;
    }

    // g: 0 public · 1 gated (needs See All) · 2 local phone library (localhost only,
    // present only in the document serve.sh seeds — never in the deployed one).
    const PUBLIC = 0, GATED = 1, PHONE = 2;

    /** Photo refs the grid understands, filtered by the current view. */
    function visible(person, filter) {
        return person.photos.filter(p => filter === 'all'
            || (filter === 'public' && p.g === PUBLIC)
            || (filter === 'gated' && p.g === GATED)
            || (filter === 'phone' && p.g === PHONE));
    }

    function renderPerson(person, filter, unnamed) {
        const sec = document.createElement('section');
        sec.className = 'people-person';

        const head = document.createElement('div');
        head.className = 'people-head';
        const h = document.createElement('h2');
        h.textContent = person.label;
        const count = document.createElement('span');
        count.className = 'people-count';
        const shown = visible(person, filter);
        count.textContent = shown.length === person.n
            ? `${person.n} photos`
            : `${shown.length} of ${person.n} photos`;
        head.append(h, count);
        if (!unnamed) head.appendChild(statusChip(person));
        if (person.clusters && person.clusters.length) {
            const cl = document.createElement('span');
            cl.className = 'people-clusters';
            cl.textContent = person.clusters.join(' ');
            cl.title = 'Face clusters merged into this person';
            head.appendChild(cl);
        }
        if (unnamed) {
            const hint = document.createElement('span');
            hint.className = 'people-clusters';
            hint.textContent = 'unnamed';
            hint.title = 'Add this cluster id to a person in config/people.json to name it';
            head.appendChild(hint);
        }
        sec.appendChild(head);

        const grid = document.createElement('div');
        grid.className = 'people-grid';
        sec.appendChild(grid);

        let limit = PAGE_SIZE;
        const more = document.createElement('button');
        more.className = 'people-more';
        more.type = 'button';

        function draw() {
            const refs = shown.slice(0, limit).map(p => ({
                trip: p.t, id: p.i, gated: p.g === GATED, phone: p.g === PHONE
            }));
            grid.innerHTML = '';
            refs.forEach((ref, i) => {
                const cell = document.createElement('a');
                cell.className = 'people-cell'
                    + (ref.gated ? ' is-gated' : '') + (ref.phone ? ' is-phone' : '');
                // Gallery.photoUrl routes a 'phone-' prefixed trip to /phone/trips.
                cell.href = Gallery.photoUrl(ref, 'display');
                cell.target = '_blank';
                cell.rel = 'noopener';
                cell.title = `${ref.trip} / ${ref.id}`
                    + (ref.gated ? ' (gated)' : ref.phone ? ' (phone library, local only)' : '');
                const img = document.createElement('img');
                img.loading = 'lazy';
                img.alt = ref.id;
                img.src = Gallery.photoUrl(ref, 'thumbnails');
                img.addEventListener('error', () => {
                    // Phone-library thumbnails are symlinks onto the NAS, which over
                    // Tailscale SMB can take 10-20s and occasionally time out. That is
                    // slowness, not gating, so it must not show the padlock — retry
                    // once, then mark it as unreachable.
                    if (ref.phone && !img.dataset.retried) {
                        img.dataset.retried = '1';
                        img.src = Gallery.photoUrl(ref, 'thumbnails') + '?r=1';
                        return;
                    }
                    if (ref.phone) { cell.classList.add('is-slow'); img.remove(); return; }
                    // A gated photo 404s without the See All cookie — show the same
                    // padlock the rest of the site uses instead of a broken image.
                    Gallery.lockedCover(img);
                }, { once: false });
                const cap = document.createElement('span');
                cap.className = 'people-cap';
                cap.textContent = ref.trip;
                cell.append(img, cap);
                grid.appendChild(cell);
            });
            if (shown.length > limit) {
                more.textContent = `Show more (${shown.length - limit} left)`;
                more.style.display = '';
            } else {
                more.style.display = 'none';
            }
        }
        more.addEventListener('click', () => { limit += PAGE_SIZE * 4; draw(); });
        draw();
        sec.appendChild(more);
        if (!shown.length) sec.classList.add('is-empty');
        return sec;
    }

    function render(root, doc, filter) {
        root.innerHTML = '';

        const bar = document.createElement('div');
        bar.className = 'people-bar';
        const title = document.createElement('h1');
        title.textContent = 'People';
        bar.appendChild(title);

        const filters = document.createElement('div');
        filters.className = 'people-filters';
        const tabs = [['all', 'All'], ['public', 'Public only'], ['gated', 'Gated only']];
        // Only offered on localhost, where the seeded document carries the phone
        // library. The deployed document has no g=2 entries, so no tab appears.
        if (doc.has_phone) tabs.push(['phone', 'Phone library']);
        tabs.forEach(([v, label]) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.textContent = label;
            b.className = 'people-filter' + (v === filter ? ' is-on' : '');
            b.addEventListener('click', () => render(root, doc, v));
            filters.appendChild(b);
        });
        bar.appendChild(filters);
        root.appendChild(bar);

        const note = document.createElement('p');
        note.className = 'people-note';
        const named = doc.people.length, unnamedN = doc.unnamed.length;
        note.textContent = `${named} named ${named === 1 ? 'person' : 'people'}, `
            + `${unnamedN} unnamed ${unnamedN === 1 ? 'cluster' : 'clusters'}. `
            + 'Switching someone off only changes their PUBLIC photos — filter to Public only to see just those. '
            + 'Edit config/people.json and rebuild to hide or unhide.'
            + (doc.has_phone
                ? ' Running on localhost, so the local phone library is included; the deployed site shows only deployed photos.'
                : '');
        if (doc.n_blocked_hidden) {
            note.textContent += ` ${doc.n_blocked_hidden} blocked photos are not listed here.`;
        }
        root.appendChild(note);

        doc.people.forEach(p => root.appendChild(renderPerson(p, filter, false)));
        if (doc.unnamed.length) {
            const h = document.createElement('h2');
            h.className = 'people-sep';
            h.textContent = 'Unnamed clusters';
            root.appendChild(h);
            doc.unnamed.forEach(p => root.appendChild(renderPerson(p, filter, true)));
        }
    }

    function initPeoplePage() {
        const root = document.getElementById('people-app');
        if (!root) return;
        if (!unlocked()) { renderPasswordForm(root); return; }
        root.innerHTML = '<p class="people-note">Loading…</p>';
        fetch('/api/people', { cache: 'no-store' })
            .then(r => (r.ok ? r.json() : null))
            .then(doc => {
                if (!doc) {
                    root.innerHTML = '<p class="people-note">No people index published yet. '
                        + 'Run <code>tools/people_index.py</code> and deploy.</p>';
                    return;
                }
                render(root, doc, 'all');
            })
            .catch(() => {
                root.innerHTML = '<p class="people-note">Could not load the people index.</p>';
            });
    }

    return { initPeoplePage, unlocked };
})();
