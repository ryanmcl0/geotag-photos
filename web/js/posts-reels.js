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
 *   Profile  (this file): first "How the profile will look" (2026-11-15): the bot's schedule on a phone on top of his
 *            current posts, a slider day by day, and each cover's check (cover_check.json). Under it the older list:
 *            the reels picked with "+ Post", their captions and posted ticks, and the 4K export. Count: reels picked.
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
        ['review', 'Review', 'Reels you haven\'t judged yet'],
        ['reviewed', 'Reviewed', 'Reels you have judged: verdict, stars, notes'],
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
        // #reels/queue (the old Queue page, superseded by Reviewed on 2026-11-15) opens Reviewed
        const asked = m && (m[1] === 'queue' ? 'reviewed' : m[1]);
        if (asked && known(asked)) { lsSet(SECTION_KEY, asked); return asked; }
        const s = lsGet(SECTION_KEY, 'review');
        return known(s) ? s : 'review';
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
        if (id === 'reviewed') {
            const n = window.PostsReview ? PostsReview.reviewedCount() : null;
            return n === null ? null : { n, title: `${n} reel${n === 1 ? '' : 's'} judged` };
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

    // ── the schedule preview (owner 2026-11-15: "show how its gonna look with the schedule. and put a slider so like I
    // can see how it progresses over time. and determine if the cover photos are suitable or not") ──
    // The bot's schedule (PostsQueue's rows, else the cover check's copy) on top of his profile as it is now, as of the
    // day the slider is on. Covers and their verdicts: web/phone/reels/cover_check.json, from the video project's
    // tools/reelbot/cover_check.py (queue_sync runs it after each sync): the cover each post will really get (the pool's
    // cover photo, else Instagram's first frame), checked on the grid's 3:4 crop.
    const PV_DAY = 'reels_preview_day';
    const PV_VIEW = 'reels_preview_view';
    const LIVE_ROWS = ['planned', 'approved', 'posted'];
    let covers;                       // undefined: not asked yet, null: none
    let pv = { day: null, pick: null, timer: null };

    async function loadCovers() {
        try {
            const r = await fetch('/phone/reels/cover_check.json', { cache: 'no-store' });
            covers = r.ok ? await r.json() : null;
        } catch (e) { covers = null; }
        return covers;
    }

    const ukDay = t => new Date(t).toLocaleDateString('en-CA', { timeZone: 'Europe/London' });     // YYYY-MM-DD
    const fmtDay = d => new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
    const fmtTime = t => new Date(t).toLocaleTimeString('en-GB', { timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', hour12: false });

    function scheduleRows() {
        const fromQ = window.PostsQueue && PostsQueue.scheduleRows ? PostsQueue.scheduleRows() : null;
        const rows = (fromQ && fromQ.length ? fromQ : (covers && covers.schedule) || [])
            .filter(r => r.time && LIVE_ROWS.includes(r.status || 'planned'));
        // a reel the bot already posted is in the live list from the check's API read: don't show it twice
        const liveAt = covers && covers.live && covers.live.length ? covers.updated : null;
        return rows.filter(r => !(r.status === 'posted' && liveAt && r.time <= liveAt))
            .sort((a, b) => a.time < b.time ? -1 : 1);
    }

    function coverOf(key) { return (covers && covers.covers && covers.covers[key]) || null; }
    const imgUrl = rel => '/phone/reels/' + rel + (covers && covers.updated ? '?v=' + encodeURIComponent(covers.updated) : '');

    function schedulePreview() {
        const box = el('section', 'sp');
        const head = el('div', 'pq-h', 'How the profile will look');
        box.appendChild(head);
        if (covers === undefined) {
            box.appendChild(el('p', 'reels-how', 'Loading the schedule and the covers…'));
            loadCovers().then(() => box.replaceWith(schedulePreview()));
            return box;
        }
        const rows = scheduleRows();
        if (!rows.length) {
            box.appendChild(el('p', 'reels-how', 'No schedule yet: Send to NAS (Reviewed ▸ Posting schedule) gives the bot the queue, ' +
                'and the bot makes the schedule.'));
            return box;
        }
        const days = [...new Set(rows.map(r => ukDay(r.time)))].sort();
        let di = days.indexOf(pv.day || lsGet(PV_DAY, ''));
        if (di < 0) di = Math.max(0, days.indexOf(days.find(d => d >= ukDay(Date.now())) || days[0]));

        const cs = Object.values((covers && covers.covers) || {});
        const nBad = cs.filter(c => c.verdict === 'bad').length, nWarn = cs.filter(c => c.verdict === 'warn').length;
        box.appendChild(el('p', 'reels-how',
            'The bot\'s schedule on top of your profile as it is now. Drag the slider to see the grid day by day; the ' +
            'reels that go out that day have a blue edge. Tap a tile for its cover check. ' +
            (covers ? `Covers: ${cs.length - nBad - nWarn} fine, ${nWarn} to look at, ${nBad} bad ` +
                `(checked ${new Date(covers.updated).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}).`
                : 'No cover check yet (tools/reelbot/cover_check.py in the video project).')));

        // the slider
        const bar = el('div', 'sp-bar');
        const play = el('button', 'reels-mini', '▶');
        play.type = 'button';
        play.title = 'Play the schedule day by day';
        const range = el('input');
        range.type = 'range';
        range.min = '0';
        range.max = String(days.length - 1);
        range.value = String(di);
        range.setAttribute('aria-label', 'Day of the schedule');
        const label = el('div', 'sp-label');
        bar.append(play, range, label);
        box.appendChild(bar);

        const layout = el('div', 'sp-layout');
        const phoneBox = el('div', 'sp-phone');
        const side = el('div', 'sp-side');
        layout.append(phoneBox, side);
        box.appendChild(layout);

        const byKey = Object.fromEntries(rows.map(r => [r.key, r]));
        const pos = Object.fromEntries(rows.map((r, i) => [r.key, i + 1]));

        function draw() {
            const day = days[+range.value];
            pv.day = day;
            lsSet(PV_DAY, day);
            const upto = rows.filter(r => ukDay(r.time) <= day);
            const today = upto.filter(r => ukDay(r.time) === day);
            label.textContent = `${fmtDay(day)} · ${today.length} going out · ${upto.length} of ${rows.length} posted by the end of the day` +
                (+range.value === days.length - 1 ? ' (the last day)' : ` · ${rows.length - upto.length} to come`);
            phoneBox.replaceChildren(previewPhone(upto, new Set(today.map(r => r.key))));
            drawSide();
        }

        function previewPhone(upto, fresh) {
            const view = lsGet(PV_VIEW, covers && covers.share_to_feed === false ? 'reels' : 'grid');
            const wrap = el('div', 'reels-phone-wrap');
            const sw = el('div', 'reels-phone-switch');
            [['grid', 'Main grid (3:4)'], ['reels', 'Reels tab (9:16)']].forEach(([k, l]) => {
                const b = el('button', 'reel-chip' + (view === k ? ' active' : ''), l);
                b.type = 'button';
                b.addEventListener('click', () => { lsSet(PV_VIEW, k); draw(); });
                sw.appendChild(b);
            });
            wrap.appendChild(sw);
            const ph = el('div', 'reels-phone');
            const scr = el('div', 'reels-phone-screen');
            const live = ((covers && covers.live) || []).filter(m => view === 'grid' || /REEL|VIDEO/.test(m.type || ''));
            const hd = el('div', 'reels-phone-head');
            const av = el('div', 'reels-phone-avatar');
            if (live[0]) av.style.backgroundImage = `url("${imgUrl(live[0].img)}")`;
            const stats = el('div', 'reels-phone-stats');
            [[upto.length, 'new reels'], [live.length + (live.length >= 30 ? '+' : ''), 'already up']].forEach(([n, l]) => {
                const c = el('div', 'reels-phone-stat');
                c.append(el('b', '', String(n)), el('span', '', l));
                stats.appendChild(c);
            });
            hd.append(av, stats);
            scr.append(hd, el('div', 'reels-phone-name', 'As of ' + fmtDay(pv.day)));
            const tabs = el('div', 'reels-phone-tabs');
            tabs.append(el('span', view === 'grid' ? 'on' : '', '▦'), el('span', view === 'reels' ? 'on' : '', '▶'));
            scr.appendChild(tabs);
            const grid = el('div', 'reels-phone-grid ' + (view === 'grid' ? 'tall34' : 'tall916'));
            // the bot posts to the Reels tab only (share_to_feed false, owner 2026-11-15: "I don't want to show on main
            // grid"): on the main grid only what is up already
            const offGrid = view === 'grid' && covers && covers.share_to_feed === false;
            if (offGrid) {
                scr.appendChild(el('p', 'reels-how sp-offgrid', `The bot's reels go to the Reels tab only, so the main grid stays ` +
                    `as it is now. ${upto.length} new reel${upto.length === 1 ? '' : 's'} by this day: switch to Reels tab to see them.`));
            }
            (offGrid ? [] : upto.slice().reverse()).forEach(r => {
                const c = coverOf(r.key);
                const t = el('button', 'reels-phone-tile sp-tile' + (fresh.has(r.key) ? ' sp-new' : '') +
                    (pv.pick === r.key ? ' sp-pick' : ''));
                t.type = 'button';
                t.title = `${fmtDay(ukDay(r.time))} ${fmtTime(r.time)} · ${r.title || r.key}` +
                    (c && c.why && c.why.length ? '\n' + c.why.join('\n') : '');
                if (c && c.img) {
                    const img = el('img');
                    img.src = imgUrl(c.img);
                    img.loading = 'lazy';
                    img.alt = '';
                    t.appendChild(img);
                }
                if (c && c.verdict && c.verdict !== 'ok') t.appendChild(el('span', 'sp-dot ' + c.verdict, c.verdict === 'bad' ? '!' : '?'));
                t.addEventListener('click', () => { pv.pick = r.key; draw(); });
                grid.appendChild(t);
            });
            live.forEach(m => {
                const t = el('a', 'reels-phone-tile sp-live');
                t.href = m.permalink || '#';
                t.target = '_blank';
                t.rel = 'noopener';
                t.title = 'Already on the profile · ' + new Date(m.time).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
                const img = el('img');
                img.src = imgUrl(m.img);
                img.loading = 'lazy';
                img.alt = '';
                t.appendChild(img);
                grid.appendChild(t);
            });
            scr.appendChild(grid);
            ph.appendChild(scr);
            wrap.appendChild(ph);
            return wrap;
        }

        function jumpTo(key) {
            const r = byKey[key];
            if (!r) return;
            pv.pick = key;
            range.value = String(days.indexOf(ukDay(r.time)));
            draw();
        }

        function drawSide() {
            side.replaceChildren();
            const r = pv.pick && byKey[pv.pick];
            if (r) {
                const c = coverOf(r.key) || {};
                const card = el('div', 'sp-card');
                if (c.img) {
                    const img = el('img', 'sp-big');
                    img.src = imgUrl(c.img);
                    img.alt = '';
                    card.appendChild(img);
                }
                const info = el('div', 'sp-info');
                info.append(el('div', 'reel-title', r.title || r.key),
                    el('div', 'reel-meta', `#${pos[r.key]} · ${fmtDay(ukDay(r.time))} ${fmtTime(r.time)} UK · ` +
                        (c.src === 'frame' ? 'no cover: Instagram\'s first frame' : c.src !== 'cover' ? 'no cover'
                            : c.from === 'frame' ? 'a frame of the reel (no portrait photo of yours fits)'
                            : c.from ? `your ${c.from} photo` : 'a cover photo')));
                if (c.from_why) info.appendChild(el('div', 'reel-meta', c.from_why));
                const v = el('div', 'sp-verdict ' + (c.verdict || 'none'),
                    c.verdict === 'ok' ? 'Cover looks fine' : c.verdict === 'warn' ? 'Cover: have a look' : c.verdict === 'bad' ? 'Cover: not suitable' : 'Not checked yet');
                info.appendChild(v);
                if (c.why && c.why.length) {
                    const ul = el('ul', 'sp-why');
                    c.why.forEach(w => ul.appendChild(el('li', '', w)));
                    info.appendChild(ul);
                }
                if (c.same_as && c.same_as.length) {
                    info.appendChild(el('div', 'reel-meta', 'Same photo as:'));
                    const ul = el('ul', 'sp-why');
                    c.same_as.filter(k => byKey[k]).sort((a, b) => pos[a] - pos[b]).forEach(k => {
                        const li = el('li');
                        const a = el('button', 'sp-link', `#${pos[k]} ${byKey[k].title || k} (${fmtDay(ukDay(byKey[k].time))})`);
                        a.type = 'button';
                        a.addEventListener('click', () => jumpTo(k));
                        li.appendChild(a);
                        ul.appendChild(li);
                    });
                    info.appendChild(ul);
                }
                card.appendChild(info);
                side.appendChild(card);
            }
            // every cover that needs a look, in posting order, bad first
            const list = rows.filter(x => { const c = coverOf(x.key); return c && c.verdict && c.verdict !== 'ok'; })
                .sort((a, b) => (coverOf(a.key).verdict === 'bad' ? 0 : 1) - (coverOf(b.key).verdict === 'bad' ? 0 : 1) || pos[a.key] - pos[b.key]);
            const det = el('details', 'sp-fix');
            det.open = lsGet('reels_preview_fix_open', true);
            det.addEventListener('toggle', () => lsSet('reels_preview_fix_open', det.open));
            det.appendChild(el('summary', 'pq-h', `Covers to fix or look at (${list.length})`));
            list.forEach(x => {
                const c = coverOf(x.key);
                const b = el('button', 'sp-fix-row' + (pv.pick === x.key ? ' sp-pick' : ''));
                b.type = 'button';
                if (c.img) {
                    const img = el('img');
                    img.src = imgUrl(c.img);
                    img.loading = 'lazy';
                    img.alt = '';
                    b.appendChild(img);
                }
                const tx = el('div');
                tx.append(el('div', 'sp-fix-t', `#${pos[x.key]} ${x.title || x.key}`),
                    el('div', 'reel-meta', `${fmtDay(ukDay(x.time))} · ` + c.why.join(' · ')));
                b.append(tx, el('span', 'sp-dot ' + c.verdict, c.verdict === 'bad' ? '!' : '?'));
                b.addEventListener('click', () => jumpTo(x.key));
                det.appendChild(b);
            });
            if (!list.length) det.appendChild(el('p', 'reels-how', covers ? 'Every cover looks fine.' : 'No cover check yet.'));
            side.appendChild(det);
        }

        range.addEventListener('input', () => { pv.pick = null; draw(); });
        play.addEventListener('click', () => {
            if (pv.timer) { clearInterval(pv.timer); pv.timer = null; play.textContent = '▶'; return; }
            if (+range.value >= days.length - 1) range.value = '0';
            play.textContent = '❚❚';
            pv.timer = setInterval(() => {
                if (!box.isConnected || +range.value >= days.length - 1) {
                    clearInterval(pv.timer);
                    pv.timer = null;
                    play.textContent = '▶';
                    return;
                }
                range.value = String(+range.value + 1);
                pv.pick = null;
                draw();
            }, 450);
        });
        draw();
        return box;
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
        } else if (section === 'reviewed') {
            if (window.PostsReview) body.appendChild(PostsReview.reviewedPanel(doc));
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

        if (pv.timer) { clearInterval(pv.timer); pv.timer = null; }
        root.appendChild(schedulePreview());
        root.appendChild(el('div', 'pq-h sp-old-h', 'Picked with “+ Post” (the older list)'));

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
