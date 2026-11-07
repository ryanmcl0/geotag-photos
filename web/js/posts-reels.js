/*
 * Posts ▸ Reels (localhost only): the reels picked for posting, in posting order, with their captions and posted
 * ticks, a phone preview of how the profile will look, and the 4K export.
 *
 * At the top, the post queue (posts-queue.js): the paths of reels he approved, spread by the bot on the NAS over the
 * slider's time frame and approved in Telegram once a day.
 *
 * Reels are picked with "+ Post" on their cards (galleries and China pages, in posts mode). The list, captions and
 * ticks are kept by reels-site.js (web/phone/reels/state.json through tools/reels_local.py, or this browser when that
 * helper isn't running). "Export 4K" only queues a request (web/phone/reels/export_queue.json): the render is the
 * video project's tools/vlog/reel_export.py, run by hand, one reel at a time, to the NAS export folder.
 */
window.PostsReels = (function () {
    'use strict';

    const VIEW_KEY = 'reels_profile_view';
    let selected = new Set();
    let exportQ = null;

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
        return doc;
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
        const c = el('div', 'reels-row' + (st.posted[r.id] ? ' posted' : ''));
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
        c.appendChild(body);
        return c;
    }

    function render(root, doc, rerender) {
        const st = ReelsSite.state();
        const byId = Object.fromEntries(doc.reels.map(r => [r.id, r]));
        const list = st.queue.map(id => byId[id]).filter(Boolean);
        selected = new Set([...selected].filter(id => st.queue.includes(id)));

        // the post queue (posts-queue.js): paste approved reels' paths, the slider, the bot's schedule
        if (window.PostsQueue) root.appendChild(PostsQueue.panel());

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

    return { load, render, count };
})();
