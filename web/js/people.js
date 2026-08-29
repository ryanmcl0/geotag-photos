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

    // Country display names, matching web/js/app.js (which isn't loaded here).
    const CUSTOM_COUNTRY_NAMES = { SCT: 'Scotland', WAL: 'Wales', ENG: 'England' };
    const _regionNames = new Intl.DisplayNames(['en'], { type: 'region' });
    function countryName(cc) {
        if (!cc) return 'Unknown';
        if (CUSTOM_COUNTRY_NAMES[cc]) return CUSTOM_COUNTRY_NAMES[cc];
        try { return _regionNames.of(cc); } catch (e) { return cc; }
    }

    // g: 0 public · 1 gated (needs See All) · 2 local phone library (localhost only,
    // present only in the document serve.sh seeds — never in the deployed one).
    const PUBLIC = 0, GATED = 1, PHONE = 2;

    /** Photo refs the grid understands, filtered by the current view.
     *  `f` is {kind, country, year} — country '' / year '' mean no constraint,
     *  and country '?' selects the photos with no country at all. */
    function visible(person, f) {
        return person.photos.filter(p => {
            const kindOk = f.kind === 'all'
                || (f.kind === 'public' && p.g === PUBLIC)
                || (f.kind === 'gated' && p.g === GATED)
                || (f.kind === 'phone' && p.g === PHONE);
            if (!kindOk) return false;
            if (f.country === '?' ? p.c : (f.country && p.c !== f.country)) return false;
            if (f.year && String(p.d || '').slice(0, 4) !== f.year) return false;
            return true;
        });
    }

    // ---------- Two levels: an index of people, then one person's gallery ----------
    //
    // Everything used to render inline as 180 open sections, which meant scrolling
    // past thousands of thumbnails to reach anyone. The index is now a compact list
    // and the photos live one click in, where the filters also live.

    function photoCell(ref, i, refs) {
        const cell = document.createElement('div');
        // 'photo-cell' + data-trip/data-id is the contract posts.js select mode
        // looks for, so a set can be turned into a post straight from here.
        cell.className = 'photo-cell people-cell'
            + (ref.g === GATED ? ' is-gated' : '') + (ref.g === PHONE ? ' is-phone' : '');
        cell.dataset.trip = ref.t;
        cell.dataset.id = ref.i;
        cell.title = `${ref.t} / ${ref.i}`
            + (ref.g === GATED ? ' (gated)' : ref.g === PHONE ? ' (phone library, local only)' : '');
        const img = document.createElement('img');
        img.loading = 'lazy';
        img.alt = ref.i;
        const gref = { trip: ref.t, id: ref.i };
        img.src = Gallery.photoUrl(gref, 'thumbnails');
        img.addEventListener('error', () => {
            // Phone-library thumbnails are symlinks onto the NAS, which over
            // Tailscale SMB can take 10-20s and occasionally time out. That is
            // slowness, not gating, so it must not show the padlock.
            if (ref.g === PHONE && !img.dataset.retried) {
                img.dataset.retried = '1';
                img.src = Gallery.photoUrl(gref, 'thumbnails') + '?r=1';
                return;
            }
            if (ref.g === PHONE) { cell.classList.add('is-slow'); img.remove(); return; }
            Gallery.lockedCover(img);
        }, { once: false });
        const cap = document.createElement('span');
        cap.className = 'people-cap';
        cap.textContent = ref.t;
        cell.append(img, cap);
        // Opens the site's PhotoSwipe viewer, not a new tab. Posts-mode select
        // owns the click while it is active.
        cell.addEventListener('click', e => {
            const bar = document.getElementById('posts-actionbar');
            if (cell.classList.contains('sel')
                || (bar && getComputedStyle(bar).display !== 'none')) return;
            e.preventDefault();
            Gallery.openLightbox(refs.map(r => ({ trip: r.t, id: r.i })), i);
        });
        return cell;
    }

    function personKey(p) { return p.key || p.label; }

    /** Cover thumbnail for an index row: first photo that isn't gated, else first. */
    function coverRef(person) {
        return person.photos.find(p => p.g === PUBLIC) || person.photos[0];
    }

    function renderIndex(root, doc) {
        root.innerHTML = '';
        const bar = document.createElement('div');
        bar.className = 'people-bar';
        const title = document.createElement('h1');
        title.textContent = 'People';
        bar.appendChild(title);
        root.appendChild(bar);

        const note = document.createElement('p');
        note.className = 'people-note';
        const named = doc.people.length, unnamedN = doc.unnamed.length;
        note.textContent = `${named} named ${named === 1 ? 'person' : 'people'}, `
            + `${unnamedN} unnamed ${unnamedN === 1 ? 'cluster' : 'clusters'}. `
            + 'Click anyone to open their photos, where you can filter by country and year. '
            + 'Edit config/people.json and rebuild to hide or unhide.'
            + (doc.has_phone
                ? ' Running on localhost, so the local phone library is included; '
                  + 'the deployed site shows only deployed photos.'
                : '');
        if (doc.n_blocked_hidden) {
            note.textContent += ` ${doc.n_blocked_hidden} blocked photos are not listed here.`;
        }
        root.appendChild(note);

        function group(heading, list, unnamed) {
            if (!list.length) return;
            if (heading) {
                const h = document.createElement('h2');
                h.className = 'people-sep';
                h.textContent = heading;
                root.appendChild(h);
            }
            const wrap = document.createElement('div');
            wrap.className = 'people-index';
            list.forEach(person => {
                const row = document.createElement('button');
                row.type = 'button';
                row.className = 'people-row';
                row.addEventListener('click', () => {
                    location.hash = '#p=' + encodeURIComponent(personKey(person));
                });
                const cover = coverRef(person);
                const img = document.createElement('img');
                img.loading = 'lazy';
                img.alt = '';
                if (cover) {
                    img.src = Gallery.photoUrl({ trip: cover.t, id: cover.i }, 'thumbnails');
                    img.addEventListener('error', () => img.classList.add('is-blank'), { once: true });
                }
                const meta = document.createElement('span');
                meta.className = 'people-row-meta';
                const nm = document.createElement('span');
                nm.className = 'people-row-name';
                nm.textContent = person.label;
                const ct = document.createElement('span');
                ct.className = 'people-row-count';
                ct.textContent = `${person.n} photo${person.n === 1 ? '' : 's'}`;
                meta.append(nm, ct);
                row.append(img, meta);
                if (person.curated_set) {
                    const chip = document.createElement('span');
                    chip.className = 'people-chip people-chip--curated';
                    chip.textContent = 'Curated';
                    row.appendChild(chip);
                } else if (!unnamed) {
                    row.appendChild(statusChip(person));
                }
                wrap.appendChild(row);
            });
            root.appendChild(wrap);
        }

        group('', doc.people, false);
        group('Unnamed clusters', doc.unnamed, true);
    }

    function renderPerson(root, doc, person, filter) {
        root.innerHTML = '';

        const back = document.createElement('button');
        back.type = 'button';
        back.className = 'people-back';
        back.textContent = '‹ All people';
        back.addEventListener('click', () => { location.hash = ''; });
        root.appendChild(back);

        const bar = document.createElement('div');
        bar.className = 'people-bar';
        const title = document.createElement('h1');
        title.textContent = person.label;
        bar.appendChild(title);
        if (person.curated_set) {
            const chip = document.createElement('span');
            chip.className = 'people-chip people-chip--curated';
            chip.textContent = 'Curated';
            bar.appendChild(chip);
        } else {
            bar.appendChild(statusChip(person));
        }

        const filters = document.createElement('div');
        filters.className = 'people-filters';
        const tabs = [['all', 'All'], ['public', 'Public only'], ['gated', 'Gated only']];
        if (doc.has_phone) tabs.push(['phone', 'Phone library']);
        tabs.forEach(([v, label]) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.textContent = label;
            b.className = 'people-filter' + (v === filter.kind ? ' is-on' : '');
            b.addEventListener('click', () => draw({ ...filter, kind: v }));
            filters.appendChild(b);
        });
        bar.appendChild(filters);

        // Options are counted within THIS person and the current kind filter, so a
        // dropdown can never offer a combination that comes back empty.
        const inScope = visible(person, { ...filter, country: '', year: '' });
        const cCounts = new Map(), yCounts = new Map();
        inScope.forEach(p => {
            const c = p.c || '?';
            cCounts.set(c, (cCounts.get(c) || 0) + 1);
            const y = String(p.d || '').slice(0, 4);
            if (y) yCounts.set(y, (yCounts.get(y) || 0) + 1);
        });

        function selector(label, value, options, onPick) {
            const wrap = document.createElement('label');
            wrap.className = 'people-select';
            wrap.textContent = label;
            const sel = document.createElement('select');
            options.forEach(([v, t]) => {
                const o = document.createElement('option');
                o.value = v; o.textContent = t; o.selected = v === value;
                sel.appendChild(o);
            });
            sel.addEventListener('change', () => onPick(sel.value));
            wrap.appendChild(sel);
            return wrap;
        }

        bar.appendChild(selector('Country', filter.country,
            [['', `All countries (${inScope.length})`]].concat(
                [...cCounts.entries()].sort((a, b) => b[1] - a[1])
                    .map(([cc, n]) => [cc, `${cc === '?' ? 'Unknown' : countryName(cc)} (${n})`])),
            v => draw({ ...filter, country: v })));
        bar.appendChild(selector('Year', filter.year,
            [['', 'All years']].concat(
                [...yCounts.entries()].sort((a, b) => b[0].localeCompare(a[0]))
                    .map(([y, n]) => [y, `${y} (${n})`])),
            v => draw({ ...filter, year: v })));

        if (filter.country || filter.year) {
            const clear = document.createElement('button');
            clear.type = 'button';
            clear.className = 'people-filter';
            clear.textContent = 'Clear';
            clear.addEventListener('click', () => draw({ ...filter, country: '', year: '' }));
            bar.appendChild(clear);
        }
        root.appendChild(bar);

        const count = document.createElement('p');
        count.className = 'people-note';
        root.appendChild(count);

        const grid = document.createElement('div');
        grid.className = 'people-grid';
        root.appendChild(grid);

        const more = document.createElement('button');
        more.className = 'people-more';
        more.type = 'button';
        root.appendChild(more);

        // The document is already newest-first; a curated set keeps its ranked
        // order instead, because that ranking is the whole point of it.
        const shown = visible(person, filter);
        let limit = PAGE_SIZE;
        count.textContent = shown.length === person.n
            ? `${person.n} photos`
            : `${shown.length} of ${person.n} photos`;

        function paint() {
            const refs = shown.slice(0, limit);
            grid.innerHTML = '';
            refs.forEach((ref, i) => grid.appendChild(photoCell(ref, i, refs)));
            if (window.Posts && Posts.enabled && Posts.onGridRender) {
                try { Posts.onGridRender(grid, refs.map(r => ({ trip: r.t, id: r.i }))); }
                catch (e) { /* posts mode is optional here */ }
            }
            more.textContent = `Show more (${shown.length - limit} left)`;
            more.style.display = shown.length > limit ? '' : 'none';
        }
        more.addEventListener('click', () => { limit += PAGE_SIZE * 4; paint(); });
        paint();

        function draw(f) { renderPerson(root, doc, person, f); }
    }

    function route(root, doc) {
        const m = /#p=([^&]+)/.exec(location.hash || '');
        if (!m) { renderIndex(root, doc); window.scrollTo(0, 0); return; }
        const key = decodeURIComponent(m[1]);
        const person = doc.people.concat(doc.unnamed).find(p => personKey(p) === key);
        if (!person) { renderIndex(root, doc); return; }
        renderPerson(root, doc, person, { kind: 'all', country: '', year: '' });
        window.scrollTo(0, 0);
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
                route(root, doc);
                // Hash routing so back/forward and a bookmarked person both work.
                window.addEventListener('hashchange', () => route(root, doc));
            })
            .catch(() => {
                root.innerHTML = '<p class="people-note">Could not load the people index.</p>';
            });
    }

    return { initPeoplePage, unlocked };
})();
