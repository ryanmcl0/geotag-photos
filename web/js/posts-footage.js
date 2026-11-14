/*
 * Posts ▸ Reels ▸ Footage (localhost only, owner 2026-11-14: "ultimately I want to exhaust my footage … per trip, what
 * % of the footage has been used, how many number of clips have been used, how much is left … is there any trips
 * that I don't have any reels from? … an even distribution").
 *
 * A tracker of how much of each trip's footage the reels use. It only reads web/phone/reels/usage.json (local-only,
 * like reels.json and music.json; written by the video project's tools/vlog/footage_usage.py, run by hand). No report
 * yet → one line saying so. The report's shape (version 1):
 *   totals: clips, used, pct, hours, hours_used, by_device {drone|camera|action|360|phone: {clips, used, hours,
 *           hours_used}}, reels, reels_unresolved, drafts, private_clips, trips, trips_without_reels
 *   trips[]: key, name, folder, year, start, end, status ("private" = no public clips), the same figures,
 *           reels_by_group, drafts, drafts_only_clips, posted, parts_kind ("day" | "folder") and parts[] (key, label,
 *           day, regions, date, clips, used, pct, by_device {…: {clips, used}}, reels, drafts)
 *   unmatched_clips [{reel, clip}], unresolved_reels [path]
 * "clips" are public clips only (private ones never go into reels and are only counted); "used" = in at least one kept
 * reel; drafts are machine-made reels not kept yet.
 *
 * On the page: a summary strip (reels made, drone clips used, all clips used, trips with no reels: a button that
 * filters the table, when the report was made); a camera filter (which figures the bars show, remembered in this
 * browser); one table, one row per trip (reels, used / total with a bar and the %, the other cameras, hours, clips
 * left), sortable by each column (first: the least used first, so the gaps are on top); a row opens to its days (or
 * shoot folders) and its drafts line; the private trips folded into one line at the bottom; the reels that couldn't be
 * traced to their clips in one quiet line under it. Almost every reel is cut from drone clips, so Drone is the
 * default.
 */
window.PostsFootage = (function () {
    'use strict';

    const SRC = '/phone/reels/usage.json';
    const CAM_KEY = 'reels_footage_camera';
    const CAMERAS = [['all', 'All cameras'], ['drone', 'Drone'], ['camera', 'Camera'], ['action', 'Action'],
        ['360', '360'], ['phone', 'Phone']];
    const LOW = 10;                   // under this % a bar is drawn as a gap (dashed), whatever the colour
    let report;                       // undefined: not read yet; null: no report
    let loadP = null;
    let camera = lsGet(CAM_KEY, 'drone');
    let sort = null;                  // {col, dir}; null = the least used first
    let onlyNoReels = false;
    const open = new Set();
    let panelEl = null;

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }
    function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
    function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } }

    // ── the report ──
    function valid(d) { return !!d && typeof d === 'object' && d.version === 1 && Array.isArray(d.trips); }

    function load(force) {
        if (!loadP || force) {
            // a missing file comes back as the site's HTML page (the dev server's fallback), so parse it carefully
            loadP = fetch(SRC, { cache: 'no-store' })
                .then(r => (r.ok ? r.text() : null))
                .then(t => { try { const d = t ? JSON.parse(t) : null; return valid(d) ? d : null; } catch (e) { return null; } })
                .catch(() => null)
                .then(d => {
                    report = d;
                    try { document.dispatchEvent(new CustomEvent('reels:changed', { detail: 'footage' })); } catch (e) { /* no DOM */ }
                    return d;
                });
        }
        return loadP;
    }

    // ── the figures ──
    const num = v => (typeof v === 'number' && isFinite(v) ? v : 0);

    // one camera's (or all cameras') figures of a trip, a part or the totals
    function figs(o, cam) {
        if (!o) return { clips: 0, used: 0, hours: 0, hours_used: 0 };
        const src = cam === 'all' ? o : ((o.by_device || {})[cam] || {});
        return { clips: num(src.clips), used: num(src.used), hours: num(src.hours), hours_used: num(src.hours_used) };
    }

    // the column beside the bars: the drone when the bars show all cameras, else every other camera
    function sideFigs(o, cam) {
        if (cam === 'all') return figs(o, 'drone');
        const a = figs(o, 'all');
        const m = figs(o, cam);
        return { clips: Math.max(0, a.clips - m.clips), used: Math.max(0, a.used - m.used),
            hours: Math.max(0, a.hours - m.hours), hours_used: Math.max(0, a.hours_used - m.hours_used) };
    }

    function pct(f) { return f.clips ? (100 * f.used) / f.clips : null; }

    function isPrivate(t) { return t.status === 'private'; }

    // trips with footage and not one reel (public footage only)
    function noReels(t) { return !isPrivate(t) && num(t.clips) > 0 && !num(t.reels); }

    function summary(rep) {
        const tt = (rep && rep.totals) || {};
        const trips = (rep && rep.trips) || [];
        const drone = figs(tt, 'drone');
        const all = figs(tt, 'all');
        return {
            reels: num(tt.reels), drafts: num(tt.drafts), drone, dronePct: pct(drone), all, allPct: pct(all),
            noReels: trips.filter(noReels).length, made: rep ? rep.made : null,
            unresolved: ((rep && rep.unresolved_reels) || []).length
        };
    }

    // the sort keys: what each column sorts by, and which way first
    const COLS = {
        trip: { first: 'asc', key: t => `${t.year || 0} ${t.name || t.key}`, text: true },
        reels: { first: 'desc', key: t => num(t.reels) },
        main: { first: 'asc', key: (t, cam) => pct(figs(t, cam)) },
        side: { first: 'asc', key: (t, cam) => pct(sideFigs(t, cam)) },
        hours: { first: 'desc', key: (t, cam) => figs(t, cam).hours },
        left: { first: 'desc', key: (t, cam) => { const f = figs(t, cam); return f.clips - f.used; } }
    };

    // the table's rows: the listed (public) trips, sorted, and the private ones apart
    function tripRows(rep, cam, how, onlyEmpty) {
        const trips = (rep && rep.trips) || [];
        const priv = trips.filter(isPrivate).sort((a, b) => (b.year || 0) - (a.year || 0) || String(a.name).localeCompare(String(b.name)));
        let listed = trips.filter(t => !isPrivate(t));
        if (onlyEmpty) listed = listed.filter(noReels);
        const col = COLS[(how && how.col) || 'main'] || COLS.main;
        const dir = (how && how.dir) || col.first;
        const sign = dir === 'desc' ? -1 : 1;
        listed = listed.slice().sort((a, b) => {
            const ka = col.key(a, cam);
            const kb = col.key(b, cam);
            // a trip with nothing to count (no clips from this camera) always goes last
            if (ka === null || kb === null) {
                if (ka !== kb) return ka === null ? 1 : -1;
            } else {
                const c = col.text ? String(ka).localeCompare(String(kb)) : ka - kb;
                if (c) return sign * c;
            }
            // ties: the bigger trip first (more left to use), then by name
            const fa = figs(a, cam);
            const fb = figs(b, cam);
            return (fb.clips - fb.used) - (fa.clips - fa.used) || String(a.name).localeCompare(String(b.name));
        });
        return { listed, priv };
    }

    // ── formatting ──
    const int = n => Math.round(num(n)).toLocaleString('en-GB');
    function fmtPct(p) {
        if (p === null) return '—';
        if (p > 0 && p < 1) return '<1%';
        return `${Math.round(p)}%`;
    }
    function fmtHours(h) { return h ? (h < 10 ? h.toFixed(1) : int(h)) + ' h' : '—'; }
    function fmtMade(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d)) return String(iso);
        return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
    }
    const camWord = cam => (CAMERAS.find(c => c[0] === cam) || [cam, cam])[1];

    // used / total, a thin bar (its fill is the share used; a gap under 10 % is dashed) and the %
    function usedCell(f, withBar) {
        const td = el('td', 'fu-num fu-used');
        const p = pct(f);
        if (!f.clips) { td.appendChild(el('span', 'fu-none', '—')); return td; }
        const wrap = el('div', 'fu-usedwrap');
        wrap.appendChild(el('span', 'fu-frac', `${int(f.used)} / ${int(f.clips)}`));
        if (withBar) {
            const bar = el('span', 'fu-bar' + (p === 0 ? ' empty' : p < LOW ? ' low' : ''));
            const fill = el('span', 'fu-fill');
            fill.style.width = `${Math.min(100, Math.max(p > 0 ? 2 : 0, p))}%`;
            bar.appendChild(fill);
            bar.setAttribute('role', 'img');
            bar.setAttribute('aria-label', `${fmtPct(p)} used`);
            wrap.appendChild(bar);
            wrap.appendChild(el('span', 'fu-pct', fmtPct(p)));
        }
        td.appendChild(wrap);
        return td;
    }
    function fracCell(f) {
        const td = el('td', 'fu-num fu-opt');
        td.textContent = f.clips ? `${int(f.used)} / ${int(f.clips)}` : '—';
        return td;
    }

    // ── drawing ──
    function summaryStrip(s) {
        const strip = el('div', 'fu-summary');
        const tile = (big, label, sub) => {
            const t = el('div', 'fu-tile');
            t.appendChild(el('div', 'fu-big', big));
            t.appendChild(el('div', 'fu-label', label));
            if (sub) t.appendChild(el('div', 'fu-sub', sub));
            strip.appendChild(t);
            return t;
        };
        tile(int(s.reels), 'reels made', s.drafts ? `and ${int(s.drafts)} machine-made drafts` : null);
        tile(fmtPct(s.dronePct), 'of drone clips used', `${int(s.drone.used)} of ${int(s.drone.clips)}`);
        tile(fmtPct(s.allPct), 'of all clips used', `${int(s.all.used)} of ${int(s.all.clips)}`);
        const nr = el('button', 'fu-tile fu-tile-btn' + (onlyNoReels ? ' active' : ''));
        nr.type = 'button';
        nr.title = onlyNoReels ? 'Show every trip again' : 'Show only the trips with no reels';
        nr.append(el('div', 'fu-big', int(s.noReels)), el('div', 'fu-label', s.noReels === 1 ? 'trip with no reels' : 'trips with no reels'),
            el('div', 'fu-sub', onlyNoReels ? 'Showing only these · show all' : 'Show only these'));
        nr.disabled = !s.noReels && !onlyNoReels;
        nr.addEventListener('click', () => { onlyNoReels = !onlyNoReels; redraw(); });
        strip.appendChild(nr);
        tile(fmtMade(s.made) || '—', 'report made', 'Run footage_usage.py again to update');
        return strip;
    }

    function cameraChips() {
        const bar = el('div', 'reel-chips fu-cams');
        bar.appendChild(el('span', 'fu-chips-label', 'Bars show'));
        CAMERAS.forEach(([k, label]) => {
            const c = el('button', 'reel-chip' + (k === camera ? ' active' : ''), label);
            c.type = 'button';
            c.addEventListener('click', () => { camera = k; lsSet(CAM_KEY, k); redraw(); });
            bar.appendChild(c);
        });
        return bar;
    }

    function headCell(tr, col, label, cls) {
        const th = el('th', cls || '');
        const cur = sort || { col: 'main', dir: COLS.main.first };
        const on = cur.col === col;
        const b = el('button', 'fu-sort' + (on ? ' on' : ''), label + (on ? (cur.dir === 'asc' ? ' ↑' : ' ↓') : ''));
        b.type = 'button';
        b.title = 'Sort by this column';
        b.addEventListener('click', () => {
            sort = on ? { col, dir: cur.dir === 'asc' ? 'desc' : 'asc' } : { col, dir: COLS[col].first };
            redraw();
        });
        th.appendChild(b);
        if (on) th.setAttribute('aria-sort', cur.dir === 'asc' ? 'ascending' : 'descending');
        tr.appendChild(th);
    }

    function partsBlock(t) {
        const box = el('div', 'fu-partsbox');
        const notes = [];
        if (num(t.drafts)) {
            notes.push(`${int(t.drafts)} machine-made draft${t.drafts === 1 ? '' : 's'}` +
                (num(t.drafts_only_clips) ? ` use ${int(t.drafts_only_clips)} more clip${t.drafts_only_clips === 1 ? '' : 's'}`
                    : ' use no clips the kept reels don\'t') + ' (not kept yet).');
        }
        if (typeof t.posted === 'number') notes.push(`${int(t.posted)} posted.`);
        if (num(t.private_clips)) notes.push(`${int(t.private_clips)} private clips, never used for reels.`);
        const groups = Object.entries(t.reels_by_group || {}).filter(([, n]) => n);
        if (groups.length) notes.push('Reels by folder: ' + groups.map(([g, n]) => `${g.replace(/^\d+\s+/, '')} ${n}`).join(', ') + '.');
        if (t.start) notes.push(t.end && t.end !== t.start ? `${t.start} to ${t.end}.` : `${t.start}.`);
        if (notes.length) box.appendChild(el('p', 'fu-note', notes.join(' ')));
        const parts = t.parts || [];
        if (!parts.length) {
            box.appendChild(el('p', 'fu-note', 'No day or shoot folders for this trip.'));
            return box;
        }
        const tbl = el('table', 'fu-table fu-parts');
        const head = el('tr');
        [[t.parts_kind === 'folder' ? 'Shoot folder' : 'Day', ''], ['Reels', 'fu-num'], [camWord(camera) + ' used', 'fu-num'],
            [camera === 'all' ? 'Drone' : 'Other cameras', 'fu-num fu-opt'], ['Left', 'fu-num']].forEach(([h, c]) => head.appendChild(el('th', c, h)));
        tbl.appendChild(head);
        parts.forEach(p => {
            const main = figs(p, camera);
            const tr = el('tr', noReels(Object.assign({ status: t.status }, p)) ? 'fu-noreels' : '');
            const name = el('td', 'fu-name');
            name.appendChild(el('span', '', p.label || p.key));
            if (p.regions && !(p.label || '').includes(p.regions)) name.appendChild(el('span', 'fu-year', p.regions));
            if (noReels(Object.assign({ status: t.status }, p))) name.appendChild(el('span', 'fu-tag', 'No reels yet'));
            tr.appendChild(name);
            tr.appendChild(el('td', 'fu-num', int(p.reels)));
            tr.appendChild(usedCell(main, true));
            tr.appendChild(fracCell(sideFigs(p, camera)));
            tr.appendChild(el('td', 'fu-num', main.clips ? int(main.clips - main.used) : '—'));
            tbl.appendChild(tr);
        });
        const scroll = el('div', 'fu-scroll');
        scroll.appendChild(tbl);
        box.appendChild(scroll);
        return box;
    }

    function tripTable(rep) {
        const { listed, priv } = tripRows(rep, camera, sort, onlyNoReels);
        const tbl = el('table', 'fu-table');
        const head = el('tr');
        headCell(head, 'trip', 'Trip');
        headCell(head, 'reels', 'Reels', 'fu-num');
        headCell(head, 'main', camWord(camera) + ' clips used', 'fu-num');
        headCell(head, 'side', camera === 'all' ? 'Drone' : 'Other cameras', 'fu-num fu-opt');
        headCell(head, 'hours', 'Hours', 'fu-num fu-opt');
        headCell(head, 'left', 'Left', 'fu-num');
        const thead = el('thead');
        thead.appendChild(head);
        tbl.appendChild(thead);
        const body = el('tbody');
        if (!listed.length) {
            const tr = el('tr');
            const td = el('td', 'reels-empty', onlyNoReels ? 'Every trip has at least one reel.' : 'No trips in the report.');
            td.colSpan = 6;
            tr.appendChild(td);
            body.appendChild(tr);
        }
        listed.forEach(t => {
            const main = figs(t, camera);
            const isOpen = open.has(t.key);
            const tr = el('tr', 'fu-trip' + (noReels(t) ? ' fu-noreels' : '') + (isOpen ? ' open' : ''));
            const name = el('td', 'fu-name');
            const tog = el('button', 'fu-open', `${isOpen ? '▾' : '▸'} ${t.name || t.key}`);
            tog.type = 'button';
            tog.setAttribute('aria-expanded', String(isOpen));
            tog.title = isOpen ? 'Close' : (t.parts_kind === 'folder' ? 'Open its shoot folders' : 'Open its days');
            tog.addEventListener('click', () => { if (open.has(t.key)) open.delete(t.key); else open.add(t.key); redraw(); });
            name.appendChild(tog);
            if (t.year) name.appendChild(el('span', 'fu-year', String(t.year)));
            if (noReels(t)) name.appendChild(el('span', 'fu-tag', 'No reels yet'));
            tr.appendChild(name);
            const reels = el('td', 'fu-num', int(t.reels));
            if (num(t.reels_unresolved)) reels.title = `${t.reels_unresolved} of them could not be traced to their clips`;
            tr.appendChild(reels);
            tr.appendChild(usedCell(main, true));
            tr.appendChild(fracCell(sideFigs(t, camera)));
            tr.appendChild(el('td', 'fu-num fu-opt', fmtHours(main.hours)));
            tr.appendChild(el('td', 'fu-num', main.clips ? int(main.clips - main.used) : '—'));
            body.appendChild(tr);
            if (isOpen) {
                const pr = el('tr', 'fu-partsrow');
                const td = el('td');
                td.colSpan = 6;
                td.appendChild(partsBlock(t));
                pr.appendChild(td);
                body.appendChild(pr);
            }
        });
        if (priv.length && !onlyNoReels) {
            const tr = el('tr', 'fu-private');
            const td = el('td');
            td.colSpan = 6;
            const d = el('details');
            d.appendChild(el('summary', '', `${priv.length} private trip${priv.length === 1 ? '' : 's'}, not used for reels`));
            const ul = el('ul', 'fu-privlist');
            priv.forEach(t => ul.appendChild(el('li', '', [t.name || t.key, t.year,
                num(t.private_clips) ? `${int(t.private_clips)} clips` : null].filter(Boolean).join(' · '))));
            d.appendChild(ul);
            td.appendChild(d);
            tr.appendChild(td);
            body.appendChild(tr);
        }
        tbl.appendChild(body);
        const scroll = el('div', 'fu-scroll');
        scroll.appendChild(tbl);
        return scroll;
    }

    function quietList(items, one, many, fmt) {
        const d = el('details', 'fu-quiet');
        d.appendChild(el('summary', '', items.length === 1 ? one : `${int(items.length)} ${many}`));
        const ul = el('ul');
        items.slice(0, 200).forEach(x => ul.appendChild(el('li', '', fmt(x))));
        if (items.length > 200) ul.appendChild(el('li', '', `… and ${int(items.length - 200)} more`));
        d.appendChild(ul);
        return d;
    }

    function draw() {
        const sec = el('section', 'pq fu');
        const h = el('div', 'fu-headline');
        h.appendChild(el('div', 'pq-h', 'Footage used for reels'));
        const re = el('button', 'reels-mini', 'Reload');
        re.type = 'button';
        re.title = 'Read the report again (after running footage_usage.py)';
        re.addEventListener('click', () => load(true).then(redraw));
        h.appendChild(re);
        sec.appendChild(h);
        if (report === undefined) {
            sec.appendChild(el('p', 'reels-how', 'Loading the footage report…'));
            return sec;
        }
        if (!report) {
            sec.appendChild(el('p', 'reels-empty', 'No footage report yet. Run the video project\'s footage_usage.py.'));
            return sec;
        }
        sec.appendChild(el('p', 'reels-how',
            'How much of each trip\'s footage the reels use. A clip counts as used once a reel you kept has it. Only ' +
            'public footage counts: private trips never go into reels. The least used trips are on top; open one for ' +
            'its days.'));
        sec.appendChild(summaryStrip(summary(report)));
        sec.appendChild(cameraChips());
        if (onlyNoReels) {
            const f = el('p', 'fu-filter');
            f.appendChild(document.createTextNode('Only the trips with footage and no reels. '));
            const all = el('button', 'reels-mini', 'Show all trips');
            all.type = 'button';
            all.addEventListener('click', () => { onlyNoReels = false; redraw(); });
            f.appendChild(all);
            sec.appendChild(f);
        }
        sec.appendChild(tripTable(report));
        const unresolved = report.unresolved_reels || [];
        if (unresolved.length) {
            sec.appendChild(quietList(unresolved, '1 reel could not be traced to its clips',
                'reels could not be traced to their clips', p => String(p)));
        }
        const unmatched = report.unmatched_clips || [];
        if (unmatched.length) {
            sec.appendChild(quietList(unmatched, '1 clip a reel names is not in the footage index',
                'clips that reels name are not in the footage index', x => `${x.reel}: ${x.clip}`));
        }
        return sec;
    }

    function redraw() {
        if (!panelEl || !panelEl.isConnected) return;
        const y = window.scrollY;
        panelEl.replaceChildren(draw());
        window.scrollTo(0, y);                          // a sort or a filter keeps the page where it was
    }

    function panel() {
        panelEl = el('div', 'fu-wrap');
        panelEl.appendChild(draw());
        load(report === undefined ? false : true).then(redraw);
        return panelEl;
    }

    // the section button's number: the share of drone clips used, or null without a report
    function count() {
        if (!report) return null;
        const s = summary(report);
        return s.dronePct === null ? null : fmtPct(s.dronePct);
    }

    return { panel, load, count, summary, tripRows, figs, sideFigs, pct, fmtPct, noReels };
})();
