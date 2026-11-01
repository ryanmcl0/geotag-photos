/*
 * Reels on the site (localhost only): the video pipeline's reel drafts placed with the photos they belong to —
 * per trip and day in the galleries, per place on the China pages, and in Posts ▸ Reels for posting.
 *
 * The manifest is web/phone/reels/reels.json, written by the video project's tools/vlog/reel_site.py (web/phone/ is
 * git-ignored and never deployed; web/phone/reels/files/ holds one link per delivery folder). On the live site
 * the manifest doesn't exist, so nothing here renders.
 *
 * Posting state (which reels are queued to post, in what order, their captions, posted ticks) and the 4K export
 * queue live in web/phone/reels/state.json and export_queue.json, written by the small local helper
 * tools/reels_local.py (127.0.0.1:8791, started by serve.sh). When the helper isn't running the state is kept in
 * this browser (localStorage) and the page says so.
 */
window.ReelsSite = (function () {
    'use strict';

    const HOST = location.hostname;
    const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(HOST) || HOST.endsWith('.local') ||
        /^10\./.test(HOST) || /^192\.168\./.test(HOST) || /^172\.(1[6-9]|2\d|3[01])\./.test(HOST);
    const API = `http://${HOST === 'localhost' ? '127.0.0.1' : HOST}:8791`;
    const LS_KEY = 'reels_site_state';

    let docP = null;
    function load() {
        if (!docP) {
            docP = LOCAL
                ? fetch('/phone/reels/reels.json', { cache: 'no-store' })
                    .then(r => (r.ok ? r.json() : null))
                    .then(d => (d && d.schema === 'reel-site.v1' ? d : null))
                    .catch(() => null)
                : Promise.resolve(null);
        }
        return docP;
    }

    // ── posting state ─────────────────────────────────────────────────────────────────────────────────────────────
    const blank = () => ({ queue: [], posted: {}, captions: {} });
    let state = null;
    let stateWhere = 'none';            // 'disk' | 'browser'
    const listeners = new Set();

    function lsGet() {
        try { const v = localStorage.getItem(LS_KEY); return v ? JSON.parse(v) : null; } catch (e) { return null; }
    }
    function lsSet(v) {
        try { localStorage.setItem(LS_KEY, JSON.stringify(v)); } catch (e) { /* private mode */ }
    }
    function norm(s) {
        s = Object.assign(blank(), s || {});
        s.queue = Array.isArray(s.queue) ? s.queue.filter(x => typeof x === 'string') : [];
        return s;
    }

    let stateP = null;
    function loadState() {
        if (!stateP) {
            stateP = fetch(`${API}/reels/state`, { cache: 'no-store' })
                .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
                .then(s => {
                    stateWhere = 'disk';
                    // anything saved in this browser while the helper was down is merged in once
                    const ls = lsGet();
                    state = norm(s);
                    if (ls && ls.unsynced) {
                        const l = norm(ls);
                        l.queue.forEach(id => { if (!state.queue.includes(id)) state.queue.push(id); });
                        Object.assign(state.posted, l.posted);
                        Object.assign(state.captions, l.captions);
                        save();
                    }
                    return state;
                })
                .catch(() => {
                    stateWhere = 'browser';
                    state = norm(lsGet());
                    return state;
                });
        }
        return stateP;
    }

    let saveTimer = null;
    function save() {
        lsSet(Object.assign({}, state, { unsynced: stateWhere !== 'disk' }));
        listeners.forEach(fn => { try { fn(state); } catch (e) { /* listener gone */ } });
        if (stateWhere !== 'disk') return;
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            fetch(`${API}/reels/state`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ queue: state.queue, posted: state.posted, captions: state.captions })
            }).then(r => {
                if (r.ok) lsSet(Object.assign({}, state, { unsynced: false }));
            }).catch(() => {
                stateWhere = 'browser';
                lsSet(Object.assign({}, state, { unsynced: true }));
                listeners.forEach(fn => fn(state));
            });
        }, 300);
    }

    function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
    function queued(id) { return !!state && state.queue.includes(id); }
    function toggleQueued(id, on) {
        if (!state) return;
        const has = state.queue.includes(id);
        if (on === undefined) on = !has;
        if (on && !has) state.queue.push(id);
        if (!on && has) state.queue = state.queue.filter(x => x !== id);
        save();
    }
    function moveQueued(id, delta) {
        const i = state.queue.indexOf(id);
        const j = i + delta;
        if (i < 0 || j < 0 || j >= state.queue.length) return;
        state.queue.splice(i, 1);
        state.queue.splice(j, 0, id);
        save();
    }
    function setPosted(id, on) {
        if (on) state.posted[id] = new Date().toISOString().slice(0, 10); else delete state.posted[id];
        save();
    }
    function caption(reel) {
        return state && Object.prototype.hasOwnProperty.call(state.captions, reel.id)
            ? state.captions[reel.id] : (reel.caption || '');
    }
    function setCaption(id, text) { state.captions[id] = text; save(); }

    async function requestExport(ids) {
        const r = await fetch(`${API}/reels/export`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids, captions: Object.fromEntries(ids.map(id => [id, state.captions[id]])) })
        });
        if (!r.ok) throw new Error(`export request failed (${r.status})`);
        return r.json();
    }
    async function exportQueue() {
        try {
            const r = await fetch(`${API}/reels/export`, { cache: 'no-store' });
            return r.ok ? r.json() : null;
        } catch (e) { return null; }
    }

    // ── cards ─────────────────────────────────────────────────────────────────────────────────────────────────────
    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }

    function fmtDur(s) {
        if (!s && s !== 0) return '';
        s = Math.round(s);
        return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : `${s} s`;
    }

    let playing = null;
    function stopPlaying() {
        if (!playing) return;
        const { v, box } = playing;
        v.pause();
        v.removeAttribute('src');
        v.load();
        v.remove();
        box.classList.remove('playing');
        playing = null;
    }

    /** The phone-shaped screen: cover (16:9 letterboxed on black), plays in place on click. */
    function screen(reel) {
        const box = el('div', 'reel-screen' + (reel.aspect === '16:9' ? ' wide' : ''));
        box.tabIndex = 0;
        box.setAttribute('role', 'button');
        box.title = 'Play';
        if (reel.cover) {
            const img = el('img');
            img.src = reel.cover;
            img.loading = 'lazy';
            img.alt = reel.title || reel.id;
            box.appendChild(img);
        }
        box.appendChild(el('span', 'reel-play', '▶'));
        if (reel.dur) box.appendChild(el('span', 'reel-dur', fmtDur(reel.dur)));
        if (reel.aspect === '16:9') box.appendChild(el('span', 'reel-aspect', '16:9'));
        const start = () => {
            if (playing && playing.box === box) return;
            stopPlaying();
            const v = el('video');
            v.src = reel.src;
            v.controls = true;
            v.autoplay = true;
            v.playsInline = true;
            v.addEventListener('ended', stopPlaying);
            box.appendChild(v);
            box.classList.add('playing');
            playing = { v, box };
        };
        box.addEventListener('click', e => { if (e.target.tagName !== 'VIDEO') start(); });
        box.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); start(); } });
        return box;
    }

    /** A reel card: the screen, its title, kind · places, and (in posts mode) the "+ Post" toggle. */
    function card(reel, opts) {
        opts = opts || {};
        const c = el('div', 'reel-card');
        c.dataset.id = reel.id;
        c.appendChild(screen(reel));
        const body = el('div', 'reel-body');
        body.appendChild(el('div', 'reel-title', reel.title || reel.id));
        const meta = [];
        if (opts.showKind !== false) meta.push(reel.kind_label);
        if (opts.showDay && reel.day) meta.push(`Day ${reel.day}`);
        if (opts.showPlaces !== false && reel.places && reel.places.length && reel.places_from !== 'day') {
            meta.push(reel.places.slice(0, 3).join(', ') + (reel.places.length > 3 ? '…' : ''));
        }
        if (meta.length) body.appendChild(el('div', 'reel-meta', meta.join(' · ')));
        c.appendChild(body);
        if (window.Posts && Posts.enabled && opts.postable !== false) {
            const b = el('button', 'reel-post-btn');
            b.type = 'button';
            const paint = () => {
                const on = queued(reel.id);
                const posted = state && state.posted[reel.id];
                b.textContent = posted ? '✓ Posted' : on ? '✓ In Reels' : '+ Post';
                b.classList.toggle('on', on || !!posted);
                b.title = on ? 'Queued in Posts ▸ Reels (click to remove)' : 'Add to Posts ▸ Reels';
            };
            loadState().then(paint);
            onChange(paint);
            b.addEventListener('click', e => { e.stopPropagation(); toggleQueued(reel.id); });
            c.appendChild(b);
        }
        return c;
    }

    function grid(reels, opts) {
        const g = el('div', 'reel-grid');
        reels.forEach(r => g.appendChild(card(r, opts)));
        return g;
    }

    const KIND_ORDER = { day_montage: 0, shoot_reel: 1, vlog_cut: 2, trip_montage: 3, hook_reel: 4, other: 5 };
    function sortReels(list) {
        return list.slice().sort((a, b) =>
            (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) ||
            (a.shoot || 0) - (b.shoot || 0) ||
            (a.aspect === '9:16' ? 0 : 1) - (b.aspect === '9:16' ? 0 : 1) ||
            a.id.localeCompare(b.id));
    }

    /** Kind chips (All · Day montages · Shoot reels · …) over a list; onPick(kind|'all'). */
    function kindChips(reels, onPick) {
        const counts = {};
        reels.forEach(r => { counts[r.kind] = (counts[r.kind] || 0) + 1; });
        const kinds = Object.keys(counts).sort((a, b) => (KIND_ORDER[a] ?? 9) - (KIND_ORDER[b] ?? 9));
        const bar = el('div', 'reel-chips');
        if (kinds.length < 2) return bar;
        const LABEL = { day_montage: 'Day montages', shoot_reel: 'Shoot reels', vlog_cut: 'Vlog cuts',
            trip_montage: 'Whole-trip montages', hook_reel: 'Hook reels', other: 'Other' };
        let cur = 'all';
        [['all', `All (${reels.length})`], ...kinds.map(k => [k, `${LABEL[k] || k} (${counts[k]})`])].forEach(([k, label]) => {
            const b = el('button', 'reel-chip' + (k === cur ? ' active' : ''), label);
            b.type = 'button';
            b.addEventListener('click', () => {
                cur = k;
                bar.querySelectorAll('.reel-chip').forEach(x => x.classList.toggle('active', x === b));
                onPick(k);
            });
            bar.appendChild(b);
        });
        return bar;
    }

    /** A collapsible "Reels" master section: <details> with a summary line and a body filled on first open. */
    function master(title, count, fill, opts) {
        opts = opts || {};
        const d = el('details', 'reels-master');
        if (opts.open) d.open = true;
        const s = el('summary', 'reels-master-head');
        s.appendChild(el('span', 'reels-master-title', title));
        s.appendChild(el('span', 'reels-master-count', `${count} reel${count === 1 ? '' : 's'}`));
        if (opts.note) s.appendChild(el('span', 'reels-master-note', opts.note));
        d.appendChild(s);
        const body = el('div', 'reels-master-body');
        d.appendChild(body);
        let filled = false;
        const go = () => { if (d.open && !filled) { filled = true; fill(body); } if (!d.open) stopPlaying(); };
        d.addEventListener('toggle', go);
        go();
        return d;
    }

    function fmtDay(iso) {
        if (!iso) return '';
        try {
            return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-GB', {
                weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
        } catch (e) { return iso; }
    }

    /**
     * Gallery: one collapsed "Reels" section for a trip — day by day (that day's montage, its shoot reels, vlog
     * cuts), then "Whole trip" (whole-trip montages and hook reels). Returns null when the trip has none.
     */
    async function tripSection(tripId) {
        const doc = await load();
        if (!doc) return null;
        const reels = doc.reels.filter(r => r.trip === tripId);
        if (!reels.length) return null;
        loadState();
        return master('Reels', reels.length, body => {
            const flow = el('div');
            const paint = kind => {
                stopPlaying();
                flow.replaceChildren();
                const shown = reels.filter(r => kind === 'all' || r.kind === kind);
                const days = [...new Set(shown.filter(r => r.day).map(r => r.day))].sort((a, b) => a - b);
                days.forEach(n => {
                    const list = sortReels(shown.filter(r => r.day === n));
                    const first = list[0];
                    flow.appendChild(sectionHead(first.day_name || `Day ${n}`,
                        [fmtDay(first.day_date), (first.provinces || []).join(', ')].filter(Boolean).join(' · '),
                        list.length, `reels-day-${n}`));
                    flow.appendChild(grid(list, { showPlaces: true }));
                });
                const whole = sortReels(shown.filter(r => !r.day));
                if (whole.length) {
                    flow.appendChild(sectionHead('Whole trip', 'Whole-trip montages and hook reels', whole.length));
                    flow.appendChild(grid(whole, {}));
                }
            };
            body.appendChild(kindChips(reels, paint));
            body.appendChild(flow);
            paint('all');
        }, { note: 'drafts · localhost only' });
    }

    function sectionHead(label, sub, count, id) {
        const h = el('div', 'reels-day-head');
        if (id) h.id = id;
        h.appendChild(el('span', 'reels-day-label', label));
        if (sub) h.appendChild(el('span', 'reels-day-sub', sub));
        h.appendChild(el('span', 'reels-day-count', `${count} reel${count === 1 ? '' : 's'}`));
        return h;
    }

    /** Reels on a place page: those whose GPS/places put them on page key 'facet/sub' (reel_site.py "pages"). */
    async function forPage(key) {
        const doc = await load();
        if (!doc) return [];
        return doc.reels.filter(r => (r.pages || []).includes(key));
    }
    async function forFacet(facet) {
        const doc = await load();
        if (!doc) return [];
        return doc.reels.filter(r => (r.pages || []).some(p => p.startsWith(facet + '/')));
    }

    return {
        load, loadState, onChange, queued, toggleQueued, moveQueued, setPosted, caption, setCaption,
        requestExport, exportQueue, where: () => stateWhere, state: () => state,
        card, grid, screen, master, sectionHead, kindChips, sortReels, stopPlaying, tripSection, forPage, forFacet,
        fmtDur, fmtDay, isLocal: LOCAL
    };
})();
