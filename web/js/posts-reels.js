/*
 * Posts ▸ Reels (localhost only; the live site has no reels manifest, so the tab isn't there). Since 2026-11-14 (owner:
 * "it's getting quite convoluted and big … different sections") the tab is in sections, one shown at a time, picked in
 * a row of buttons under the tab bar, each with a small live count. The choice is remembered in this browser and kept
 * in the address as #reels/<section>, so a section can be linked (posts.html#reels/footage opens the tab on it).
 *
 *   In progress  (posts-progress.js): what the short-form work is doing this minute (the redo of old reels, the
 *            machine-only generator, the reels the AI planned and their renders, what was filed and held), each job
 *            tagged Machine / AI / You, and the vlog days with the vlog dashboard in a frame. It reads
 *            web/phone/reels/status.json (the video project's reel_status.py, once a minute). Count: jobs running now.
 *   Queue    (posts-queue.js panel('queue')): the bot's post queue (paste the paths of reels that can go out now; the
 *            slider's time frame; each line's status; the bot's schedule) and the shortlist (approved, not yet).
 *            Count: reels in the queue still to go out.
 *   Review   approvals, rejects and feedback in one place. To review (posts-review.js): the reels in reels.json with no
 *            decision yet, newest first, a page at a time, each with its player and Post / Shortlist / Reject… (the
 *            post queue's own lists, so one state), keys to go through them fast. Under it the Rejected box
 *            (posts-queue.js panel('rejects')): the reject list with reasons and Undo, and its paste box.
 *            Count: reels still to judge.
 *   Profile  (this file): the reels picked for posting, in posting order, with their captions and posted ticks, a
 *            phone preview of how the profile will look, and the 4K export. Count: reels picked.
 *   Music    (posts-music.js): the music pool new reels are cut to, Add / Remove, the review folders, rejected music.
 *            Count: songs in the pool.
 *   Footage  (posts-footage.js): how much of each trip's footage the reels use, from web/phone/reels/usage.json (the
 *            video project's footage_usage.py). Count: the share of drone clips used.
 *
 * Profile: reels are picked with "+ Post" on their cards (galleries and China pages, in posts mode). The list,
 * captions and ticks are kept by reels-site.js (web/phone/reels/state.json through tools/reels_local.py, or this
 * browser when that helper isn't running). "Export 4K" only queues a request (web/phone/reels/export_queue.json): the
 * render is the video project's tools/vlog/reel_export.py, run by hand, one reel at a time, to the NAS export folder.
 * Each picked reel also has "Reject…" (posts-queue.js keeps the reject list, by the reel's file path from reels.json):
 * a reel rejected here leaves this posting list too; one rejected elsewhere shows greyed with its reason and Undo.
 *
 * The counts follow the "reels:changed" event the parts fire on document when their data changes.
 */
window.PostsReels = (function () {
    'use strict';

    const VIEW_KEY = 'reels_profile_view';
    const SECTION_KEY = 'reels_section';
    const SECTIONS = [
        ['progress', 'In progress', 'Jobs running right now'],
        ['queue', 'Queue', 'Reels in the post queue still to go out'],
        ['review', 'Review', 'Reels you haven\'t judged yet'],
        ['profile', 'Profile', 'Reels picked for posting, in order'],
        ['music', 'Music', 'Songs in the music pool'],
        ['footage', 'Footage', 'Share of drone clips used in reels']
    ];
    let selected = new Set();
    let exportQ = null;
    let redrawRows = null;           // the latest render's rerender, for a reject list that changed elsewhere
    let listening = false;
    let section = null;

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }
    const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
    const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } };

    async function load() {
        if (!window.ReelsSite) return null;
        const doc = await ReelsSite.load();
        if (!doc) return null;
        await ReelsSite.loadState();
        exportQ = await ReelsSite.exportQueue();
        // what the section counts (and the Profile rows' rejects) need, whichever section is showing first
        if (window.PostsReview) PostsReview.setDoc(doc);
        if (window.PostsQueue) PostsQueue.ready();
        if (window.PostsMusic) PostsMusic.ready();
        if (window.PostsFootage) PostsFootage.load();
        if (window.PostsProgress) PostsProgress.load();
        return doc;
    }

    // ── the sections ──
    const known = id => SECTIONS.some(s => s[0] === id);

    // from the address (#reels/<id>) first, else the one picked last in this browser
    function currentSection() {
        const m = /^#reels\/([\w-]+)/.exec(location.hash || '');
        if (m && known(m[1])) { lsSet(SECTION_KEY, m[1]); return m[1]; }
        const s = lsGet(SECTION_KEY, 'queue');
        return known(s) ? s : 'queue';
    }

    function showInAddress(id) {
        const want = '#reels/' + id;
        if (location.hash !== want) {
            try { history.replaceState(null, '', location.pathname + location.search + want); } catch (e) { /* file: */ }
        }
    }

    function sectionCount(id) {
        if (id === 'queue') {
            if (!window.PostsQueue) return null;
            const c = PostsQueue.counts();
            return c.loaded ? { n: c.queue, title: `${c.queue} in the post queue, ${c.shortlist} on the shortlist` } : null;
        }
        if (id === 'review') {
            const n = window.PostsReview ? PostsReview.count() : null;
            return n === null ? null : { n, title: `${n} reel${n === 1 ? '' : 's'} to judge` };
        }
        if (id === 'profile') return { n: count(), title: 'Reels picked for posting' };
        if (id === 'music') {
            const n = window.PostsMusic ? PostsMusic.count() : null;
            return n === null ? null : { n, title: `${n} song${n === 1 ? '' : 's'} in the pool` };
        }
        if (id === 'progress') {
            const n = window.PostsProgress ? PostsProgress.count() : null;
            return n === null ? null : { n, title: n ? `${n} job${n === 1 ? '' : 's'} running right now` : 'Nothing is running right now' };
        }
        if (id === 'footage') {
            const p = window.PostsFootage ? PostsFootage.count() : null;
            return p === null ? null : { n: p, title: `${p} of the drone clips are in a reel` };
        }
        return null;
    }

    function paintCounts() {
        const bar = document.getElementById('reels-sections');
        if (!bar) return;
        bar.querySelectorAll('[data-section]').forEach(b => {
            const c = sectionCount(b.dataset.section);
            const span = b.querySelector('.reels-section-n');
            span.textContent = c ? String(c.n) : '';
            span.hidden = !c;
            b.title = c ? c.title : (SECTIONS.find(s => s[0] === b.dataset.section) || [])[2] || '';
        });
    }

    function sectionBar(rerender) {
        const bar = el('div', 'reels-sections');
        bar.id = 'reels-sections';
        bar.setAttribute('role', 'tablist');
        bar.setAttribute('aria-label', 'Reels sections');
        SECTIONS.forEach(([id, label]) => {
            const b = el('button', 'reels-section' + (id === section ? ' active' : ''));
            b.type = 'button';
            b.dataset.section = id;
            b.setAttribute('role', 'tab');
            b.setAttribute('aria-selected', String(id === section));
            b.append(el('span', '', label), el('span', 'reels-section-n'));
            b.addEventListener('click', () => {
                if (id === section) return;
                lsSet(SECTION_KEY, id);
                showInAddress(id);
                if (window.ReelsSite) ReelsSite.stopPlaying();
                rerender();
            });
            bar.appendChild(b);
        });
        return bar;
    }

    function count() {
        const s = window.ReelsSite && ReelsSite.state();
        return s ? s.queue.length : 0;
    }

    function exportStatus(id) {
        const items = ((exportQ && exportQ.items) || []).filter(it => it.id === id);
        return items.length ? items[items.length - 1] : null;
    }

    function fmtMB(bytes) {
        return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${Math.round(bytes / 1e6)} MB`;
    }

    // ── the phone: an Instagram-like profile of the posted + planned reels ──
    function phone(doc, list, onTile) {
        const st = ReelsSite.state();
        const view = lsGet(VIEW_KEY, 'reels');
        const wrap = el('div', 'reels-phone-wrap');
        const bar = el('div', 'reels-phone-switch');
        [['reels', 'Reels tab (9:16)'], ['grid', 'Main grid (3:4)']].forEach(([k, label]) => {
            const b = el('button', 'reel-chip' + (view === k ? ' active' : ''), label);
            b.type = 'button';
            b.addEventListener('click', () => { lsSet(VIEW_KEY, k); wrap.replaceWith(phone(doc, list, onTile)); });
            bar.appendChild(b);
        });
        wrap.appendChild(bar);

        const ph = el('div', 'reels-phone');
        const screenEl = el('div', 'reels-phone-screen');
        const head = el('div', 'reels-phone-head');
        const avatar = el('div', 'reels-phone-avatar');
        const first = list.find(r => r.cover);
        if (first) avatar.style.backgroundImage = `url("${first.cover}")`;
        const stats = el('div', 'reels-phone-stats');
        const posted = list.filter(r => st.posted[r.id]).length;
        [[list.length, 'reels'], [posted, 'posted'], [list.length - posted, 'planned']].forEach(([n, l]) => {
            const c = el('div', 'reels-phone-stat');
            c.append(el('b', '', String(n)), el('span', '', l));
            stats.appendChild(c);
        });
        head.append(avatar, stats);
        screenEl.appendChild(head);
        screenEl.appendChild(el('div', 'reels-phone-name', 'Profile preview'));
        const tabs = el('div', 'reels-phone-tabs');
        tabs.append(el('span', view === 'grid' ? 'on' : '', '▦'), el('span', view === 'reels' ? 'on' : '', '▶'));
        screenEl.appendChild(tabs);

        // Instagram shows the newest first: the last reel in posting order sits top-left.
        const grid = el('div', 'reels-phone-grid ' + (view === 'grid' ? 'tall34' : 'tall916'));
        const order = list.slice().reverse();
        order.forEach(r => {
            const n = list.indexOf(r) + 1;
            const t = el('button', 'reels-phone-tile' + (st.posted[r.id] ? ' posted' : ''));
            t.type = 'button';
            t.title = `${n}. ${r.title}` + (st.posted[r.id] ? ` (posted ${st.posted[r.id]})` : ' (planned)');
            if (r.cover) {
                const img = el('img');
                img.src = r.cover;
                img.loading = 'lazy';
                img.alt = '';
                t.appendChild(img);
            }
            t.appendChild(el('span', 'reels-phone-n', st.posted[r.id] ? '✓' : `#${n}`));
            if (r.dur) t.appendChild(el('span', 'reels-phone-dur', '▶ ' + ReelsSite.fmtDur(r.dur)));
            t.addEventListener('click', () => onTile(r));
            grid.appendChild(t);
        });
        if (!order.length) grid.appendChild(el('p', 'reels-phone-empty', 'No reels picked yet.'));
        screenEl.appendChild(grid);
        ph.appendChild(screenEl);
        wrap.appendChild(ph);
        wrap.appendChild(el('p', 'reels-how', 'Newest first, as on the profile: #1 is the first to post. ' +
            'Covers are cropped the way the grid crops them.'));
        return wrap;
    }

    // ── one row per picked reel ──
    function row(doc, r, i, n, rerender) {
        const st = ReelsSite.state();
        const rejected = window.PostsQueue && r.path ? PostsQueue.rejectOf(r.path) : null;
        const c = el('div', 'reels-row' + (st.posted[r.id] ? ' posted' : '') + (rejected ? ' rejected' : ''));
        c.id = `reel-row-${r.id}`;
        const side = el('div', 'reels-row-side');
        const pick = el('input');
        pick.type = 'checkbox';
        pick.title = 'Select for the 4K export';
        pick.checked = selected.has(r.id);
        pick.addEventListener('change', () => {
            if (pick.checked) selected.add(r.id); else selected.delete(r.id);
            rerender();
        });
        side.append(el('div', 'reels-row-n', `#${i + 1}`), pick);
        c.appendChild(side);
        const scr = ReelsSite.screen(r);
        scr.classList.add('reels-row-screen');
        c.appendChild(scr);

        const body = el('div', 'reels-row-body');
        body.appendChild(el('div', 'reel-title', r.title));
        const meta = [r.kind_label, r.day_name || (r.day ? `Day ${r.day}` : 'Whole trip'),
            r.aspect, r.dur ? ReelsSite.fmtDur(r.dur) : null, r.song ? '♪ ' + r.song : null].filter(Boolean);
        body.appendChild(el('div', 'reel-meta', meta.join(' · ')));

        const ta = el('textarea', 'reels-caption');
        ta.rows = 4;
        ta.placeholder = 'Caption for this reel…';
        ta.value = ReelsSite.caption(r);
        let t = null;
        ta.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => ReelsSite.setCaption(r.id, ta.value), 400);
        });
        body.appendChild(ta);

        const acts = el('div', 'reels-row-acts');
        const tick = el('label', 'reels-posted');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = !!st.posted[r.id];
        cb.addEventListener('change', () => { ReelsSite.setPosted(r.id, cb.checked); rerender(); });
        tick.append(cb, document.createTextNode(st.posted[r.id] ? ` Posted ${st.posted[r.id]}` : ' Posted'));
        acts.appendChild(tick);
        const btn = (label, title, fn, dis) => {
            const b = el('button', 'reels-mini', label);
            b.type = 'button';
            b.title = title;
            b.disabled = !!dis;
            b.addEventListener('click', fn);
            acts.appendChild(b);
        };
        btn('↑', 'Post earlier', () => { ReelsSite.moveQueued(r.id, -1); rerender(); }, i === 0);
        btn('↓', 'Post later', () => { ReelsSite.moveQueued(r.id, +1); rerender(); }, i === n - 1);
        btn('Copy', 'Copy the caption', () => navigator.clipboard.writeText(ta.value));
        btn('Remove', 'Take it off the posting list', () => {
            ReelsSite.toggleQueued(r.id, false);
            selected.delete(r.id);
            rerender();
        });
        const ex = exportStatus(r.id);
        if (ex) {
            const txt = ex.status === 'done'
                ? `4K ready${ex.size ? ' · ' + fmtMB(ex.size) : ''}`
                : ex.status === 'failed' ? '4K export failed' : `4K ${ex.status}`;
            const badge = el('span', 'reels-export-badge ' + ex.status, txt);
            if (ex.out) badge.title = ex.out;
            if (ex.error) badge.title = ex.error;
            acts.appendChild(badge);
        }
        body.appendChild(acts);
        if (window.PostsQueue && r.path) {
            // rejected from here: off this posting list as well (Undo under Rejected doesn't put it back)
            body.appendChild(PostsQueue.rejectControl(r.path, () => {
                if (!rejected && PostsQueue.rejectOf(r.path) && !st.posted[r.id]) {
                    ReelsSite.toggleQueued(r.id, false);
                    selected.delete(r.id);
                }
                rerender();
            }));
        }
        c.appendChild(body);
        return c;
    }

    // The tab: the section buttons, then the section picked.
    function render(root, doc, rerender) {
        section = currentSection();
        showInAddress(section);
        redrawRows = rerender;
        if (!listening) {
            listening = true;
            // the Profile rows show the reject list: redraw them when it changed elsewhere (the other sections' parts
            // redraw themselves)
            if (window.PostsQueue) {
                PostsQueue.onRejects(() => {
                    if (redrawRows && section === 'profile' && document.getElementById('reels-sections')) redrawRows();
                });
            }
            document.addEventListener('reels:changed', paintCounts);
            if (window.ReelsSite) ReelsSite.onChange(paintCounts);
        }
        root.appendChild(sectionBar(rerender));
        const body = el('div', 'reels-section-body');
        body.id = 'reels-section-' + section;
        root.appendChild(body);
        if (section === 'queue') {
            if (window.PostsQueue) body.appendChild(PostsQueue.panel('queue'));
        } else if (section === 'review') {
            if (window.PostsReview) body.appendChild(PostsReview.panel(doc));
            if (window.PostsQueue) body.appendChild(PostsQueue.panel('rejects'));
        } else if (section === 'music') {
            if (window.PostsMusic) body.appendChild(PostsMusic.panel());
        } else if (section === 'footage') {
            if (window.PostsFootage) body.appendChild(PostsFootage.panel());
        } else if (section === 'progress') {
            if (window.PostsProgress) body.appendChild(PostsProgress.panel());
        } else {
            renderProfile(body, doc, rerender);
        }
        paintCounts();
    }

    // The Profile section: the picked reels in posting order, the 4K export and the phone preview.
    function renderProfile(root, doc, rerender) {
        const st = ReelsSite.state();
        const byId = Object.fromEntries(doc.reels.map(r => [r.id, r]));
        const list = st.queue.map(id => byId[id]).filter(Boolean);
        selected = new Set([...selected].filter(id => st.queue.includes(id)));

        const where = ReelsSite.where() === 'disk'
            ? 'Saved on this Mac (web/phone/reels/state.json).'
            : 'Saved in this browser only: the local helper (tools/reels_local.py, started by serve.sh) is not running.';
        root.appendChild(el('p', 'reels-how',
            'Reels you picked with “+ Post” in a gallery or on the China pages, in posting order. ' + where));

        const layout = el('div', 'reels-layout');
        const left = el('div', 'reels-list');
        const right = el('div', 'reels-side');

        const tools = el('div', 'reels-tools');
        const all = el('button', 'reels-mini', selected.size === list.length && list.length ? 'Select none' : 'Select all');
        all.type = 'button';
        all.disabled = !list.length;
        all.addEventListener('click', () => {
            selected = selected.size === list.length ? new Set() : new Set(list.map(r => r.id));
            rerender();
        });
        const exp = el('button', 'reels-export-btn', `Export 4K (${selected.size})`);
        exp.type = 'button';
        exp.disabled = !selected.size || ReelsSite.where() !== 'disk';
        exp.title = ReelsSite.where() !== 'disk' ? 'Needs the local helper (tools/reels_local.py)'
            : 'Queue a full-quality 4K render of the selected reels';
        exp.addEventListener('click', async () => {
            exp.disabled = true;
            try {
                const res = await ReelsSite.requestExport([...selected]);
                exportQ = res.queue;
                selected = new Set();
                rerender();
            } catch (e) {
                alert(e.message);
                exp.disabled = false;
            }
        });
        const refresh = el('button', 'reels-mini', 'Refresh export status');
        refresh.type = 'button';
        refresh.addEventListener('click', async () => { exportQ = await ReelsSite.exportQueue(); rerender(); });
        tools.append(all, exp, refresh);
        left.appendChild(tools);
        const exportDir = Object.values(doc.trips || {}).map(t => t.export_dir).filter(Boolean)[0];
        const pending = ((exportQ && exportQ.items) || []).filter(it => it.status === 'queued' || it.status === 'running').length;
        left.appendChild(el('p', 'reels-how',
            'Export 4K queues the selected reels. The render runs in the video project, one at a time ' +
            '(tools/vlog/reel_export.py), into ' + (exportDir || 'the NAS reels folder') +
            ', each with its caption as a .txt beside it.' + (pending ? ` ${pending} waiting.` : '')));

        if (!list.length) {
            left.appendChild(el('p', 'reels-empty',
                'Nothing picked yet. Open a trip gallery (or a China page), open its Reels section and press “+ Post” on a reel.'));
        }
        list.forEach((r, i) => left.appendChild(row(doc, r, i, list.length, rerender)));

        right.appendChild(phone(doc, list, r => {
            const target = document.getElementById(`reel-row-${r.id}`);
            if (target) {
                target.scrollIntoView({ behavior: 'smooth', block: 'center' });
                target.classList.add('flash');
                setTimeout(() => target.classList.remove('flash'), 1200);
            }
        }));
        layout.append(left, right);
        root.appendChild(layout);
    }

    return { load, render, count, sections: SECTIONS.map(s => s[0]) };
})();
