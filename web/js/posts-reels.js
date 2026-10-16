/*
 * Posts ▸ Reels: the video pipeline's reels with a posting schedule (localhost only).
 *
 * The reels stay where the pipeline delivered them on the external drive; nothing is copied. The video
 * project's tools/vlog/reel_schedule.py writes web/phone/reels/reels.json plus one symlink per source
 * folder (web/phone/ is git-ignored and never deployed, like the phone videos), and the local server
 * streams the files from there. On the live site the manifest doesn't exist, so the tab doesn't either.
 *
 * Groups: Schedule (one reel a day) | Earth zoom | Compilation | Vlog cut | One-shot (the earth zooms
 * without the globe). "Posted" ticks are kept in this browser (localStorage, keyed by the reel's id), so
 * regenerating the schedule keeps them.
 */
window.PostsReels = (function () {
    'use strict';

    const VIEW_KEY = 'reels_view';
    const POSTED_KEY = 'reels_posted';
    let manifestP = null;

    function load() {
        if (!manifestP) {
            manifestP = (window.Gallery && Gallery.isLocal)
                ? fetch('/phone/reels/reels.json', { cache: 'no-store' })
                    .then(r => (r.ok ? r.json() : null)).catch(() => null)
                : Promise.resolve(null);
        }
        return manifestP;
    }

    const store = {
        get(key, dflt) {
            try { const v = localStorage.getItem(key); return v === null ? dflt : JSON.parse(v); }
            catch (e) { return dflt; }
        },
        set(key, v) {
            try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* private mode */ }
        },
    };

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }

    function when(iso) {
        const d = new Date(iso);
        return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }) +
            ' · ' + iso.slice(11, 16);
    }

    function play(src, title) {
        if (window.Gallery) Gallery.openVideo(src, title);
        else window.open(src, '_blank');
    }

    // The thumbnail is the video itself, paused on an early frame (no poster files needed). Its source is set
    // only once it scrolls into view, and at most two at a time: the local dev server reads the files off the
    // external drive and stalls when a long list opens dozens of streams at once.
    const waiting = [];
    let loading = 0;
    function pump() {
        while (loading < 2 && waiting.length) {
            const v = waiting.shift();
            if (!v.isConnected) continue;
            loading++;
            let done = false;
            const finish = () => { if (done) return; done = true; loading--; pump(); };
            v.addEventListener('loadeddata', finish, { once: true });
            v.addEventListener('error', finish, { once: true });
            setTimeout(finish, 8000);
            v.src = v.dataset.src;
        }
    }
    const lazy = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
        entries.forEach(en => {
            if (!en.isIntersecting) return;
            lazy.unobserve(en.target);
            waiting.push(en.target);
        });
        pump();
    }, { rootMargin: '200px' }) : null;

    function thumb(reel) {
        const box = el('button', 'reels-thumb');
        box.type = 'button';
        box.title = 'Play';
        if (reel.cover) {
            const img = el('img');
            img.src = reel.cover;
            img.loading = 'lazy';
            box.appendChild(img);
        } else {
            const v = el('video');
            v.muted = true;
            v.playsInline = true;
            v.preload = 'metadata';
            v.dataset.src = reel.src + '#t=' + (reel.kind === 'earth_zoom' ? '10' : '1.5');   // earth zooms: past the globe intro
            if (lazy) lazy.observe(v); else v.src = v.dataset.src;
            box.appendChild(v);
        }
        box.appendChild(el('span', 'reels-play', '▶'));
        if (reel.dur) box.appendChild(el('span', 'reels-dur', Math.round(reel.dur) + ' s'));
        box.addEventListener('click', () => play(reel.src, reel.title));
        return box;
    }

    function card(doc, reel, rerender, opts) {
        const posted = store.get(POSTED_KEY, {});
        const c = el('div', 'reels-card' + (posted[reel.id] ? ' posted' : ''));
        c.appendChild(thumb(reel));
        const body = el('div', 'reels-body');
        if (opts.date && reel.date) {
            const due = !posted[reel.id] && reel.date.slice(0, 10) < new Date().toISOString().slice(0, 10);
            body.appendChild(el('div', 'reels-date' + (due ? ' due' : ''), when(reel.date) + (due ? ' · overdue' : '')));
        }
        const t = el('div', 'reels-title', reel.title);
        body.appendChild(t);
        const meta = el('div', 'reels-meta');
        meta.appendChild(el('span', 'reels-kind reels-kind-' + reel.kind, (doc.kinds || {})[reel.kind] || reel.kind));
        const where = [];
        if (reel.places && reel.places.length) where.push([...new Set(reel.places)].join(' → '));
        if (reel.trip) where.push(reel.trip.replace(/-/g, ' '));
        if (reel.days && reel.days.length) where.push('day ' + reel.days.join(', '));
        if (where.length) meta.appendChild(el('span', '', where.join(' · ')));
        if (reel.note) meta.appendChild(el('span', 'reels-note', reel.note));
        body.appendChild(meta);

        // alternatives: the sped-up twin, the same shot without the globe
        const alts = (reel.alts || []).slice();
        const plain = doc.reels.find(r => r.of === reel.id);
        if (plain) alts.push({ label: 'Without globe', src: plain.src, dur: plain.dur });
        if (reel.of) {
            const ez = doc.reels.find(r => r.id === reel.of);
            if (ez) alts.push({ label: 'With globe', src: ez.src, dur: ez.dur });
        }
        if (alts.length) {
            const row = el('div', 'reels-alts');
            alts.forEach(a => {
                const b = el('button', 'reels-alt', '▶ ' + a.label + (a.dur ? ` (${Math.round(a.dur)} s)` : ''));
                b.type = 'button';
                b.addEventListener('click', () => play(a.src, reel.title + ' — ' + a.label));
                row.appendChild(b);
            });
            body.appendChild(row);
        }

        if (reel.caption) {
            const det = el('details', 'reels-caption');
            const sum = el('summary', '', 'Caption');
            const copy = el('button', 'reels-copy', 'Copy');
            copy.type = 'button';
            copy.addEventListener('click', e => {
                e.preventDefault();
                navigator.clipboard.writeText(reel.caption).then(() => { copy.textContent = 'Copied'; });
            });
            sum.appendChild(copy);
            det.append(sum, el('pre', '', reel.caption));
            body.appendChild(det);
        }

        const tick = el('label', 'reels-posted');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = !!posted[reel.id];
        cb.addEventListener('change', () => {
            const cur = store.get(POSTED_KEY, {});
            if (cb.checked) cur[reel.id] = new Date().toISOString().slice(0, 10); else delete cur[reel.id];
            store.set(POSTED_KEY, cur);
            rerender();
        });
        tick.append(cb, document.createTextNode(posted[reel.id] ? ` Posted ${posted[reel.id]}` : ' Posted'));
        body.appendChild(tick);
        c.appendChild(body);
        return c;
    }

    function render(root, doc, rerender) {
        const byId = Object.fromEntries(doc.reels.map(r => [r.id, r]));
        const counts = {};
        doc.reels.forEach(r => { counts[r.kind] = (counts[r.kind] || 0) + 1; });
        let view = store.get(VIEW_KEY, 'schedule');
        if (view !== 'schedule' && !counts[view]) view = 'schedule';

        const chips = el('div', 'reels-chips');
        [['schedule', `Schedule (${doc.schedule.length})`],
         ...['earth_zoom', 'compilation', 'vlog_cut', 'one_shot']
             .filter(k => counts[k]).map(k => [k, `${(doc.kinds || {})[k] || k} (${counts[k]})`])]
            .forEach(([id, label]) => {
                const b = el('button', 'reels-chip' + (view === id ? ' active' : ''), label);
                b.type = 'button';
                b.addEventListener('click', () => { store.set(VIEW_KEY, id); rerender(); });
                chips.appendChild(b);
            });
        root.appendChild(chips);

        const posted = store.get(POSTED_KEY, {});
        if (view === 'schedule') {
            root.appendChild(el('p', 'reels-how',
                `One reel a day at ${doc.time}: three earth zooms, three vlog cuts and a compilation each week, ` +
                'the strongest first, vlog cuts in trip order, never the same place twice in a row. ' +
                'Everything here is a draft awaiting your verdict. Regenerated from the video project ' +
                `(tools/vlog/reel_schedule.py) — last ${doc.generated.replace('T', ' ')}.`));
            const todo = doc.schedule.map(id => byId[id]).filter(r => r && !posted[r.id]);
            const done = doc.schedule.map(id => byId[id]).filter(r => r && posted[r.id]);
            let week = null;
            todo.forEach(r => {
                const d = new Date(r.date);
                const monday = new Date(d);
                monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
                const wk = monday.toISOString().slice(0, 10);
                if (wk !== week) {
                    week = wk;
                    root.appendChild(el('h3', 'reels-week', 'Week of ' +
                        monday.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })));
                }
                root.appendChild(card(doc, r, rerender, { date: true }));
            });
            if (done.length) {
                const det = el('details', 'reels-done');
                det.appendChild(el('summary', '', `Posted (${done.length})`));
                done.forEach(r => det.appendChild(card(doc, r, rerender, { date: true })));
                root.appendChild(det);
            }
            return;
        }
        const grid = el('div', 'reels-grid');
        doc.reels.filter(r => r.kind === view).forEach(r => grid.appendChild(card(doc, r, rerender, { date: true })));
        root.appendChild(grid);
    }

    return { load, render };
})();
