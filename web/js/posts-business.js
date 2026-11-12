/*
 * Posts ▸ Business (localhost only, owner 2026-11-12): ready-made posts (reels and carousels) for a second Instagram
 * account, and their posting schedule.
 *
 * The posts, their files and captions come from a content folder outside this repo, named in
 * ~/.config/content-engine/business/config.json. tools/reels_local.py reads it (GET /business), serves its files and
 * writes its state.json (PUT /business/state): the posting order (queue), the first day (start), one post every N days
 * (every), what is posted (date, link, by whom) and caption edits. The tool that made the posts can post one from the
 * Mac, and the reel bot on the NAS offers one a day in Telegram (Approve posts it); both mark what they post in the
 * same state.json, so it shows here, and a Posted tick here keeps them from posting it again.
 *
 * The day each queued post goes out (the same rule in all three): the first slot is the latest of today, start and the
 * last posted date + every; each later unposted post in the queue is `every` days after the one before.
 *
 * Every change is a function re-applied to the newest state after a 409 (written elsewhere meanwhile), like posts.js.
 */
window.PostsBusiness = (function () {
    'use strict';

    const API = 'http://127.0.0.1:8791';
    const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
    let doc = null;                  // {ok, folder, account, content, state} from the helper
    let chain = Promise.resolve();
    let redraw = null;
    let listening = false;
    let statusMsg = '';

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }
    const pad = n => String(n).padStart(2, '0');
    const isDay = s => typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s);
    function today() {
        const d = new Date();
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }
    function addDays(s, n) {
        const d = new Date(s + 'T12:00:00Z');
        d.setUTCDate(d.getUTCDate() + n);
        return d.toISOString().slice(0, 10);
    }
    const fmtDay = s => new Date(s + 'T12:00:00Z').toLocaleDateString('en-GB',
        { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
    const fmtDur = s => `${Math.floor(s / 60)}:${pad(Math.round(s % 60))}`;
    const fileURL = rel => `${API}/business/files/${rel.split('/').map(encodeURIComponent).join('/')}`;

    async function load() {
        if (!LOCAL) return null;
        try {
            const r = await fetch(`${API}/business`, { cache: 'no-store' });
            if (!r.ok) return null;
            const d = await r.json();
            if (!d.ok && d.reason === 'not set up') return null;   // no business folder on this Mac: no tab
            doc = d;
            return d;
        } catch (e) {
            return null;                                          // the helper isn't running
        }
    }

    const items = () => (doc && doc.ok && doc.content.items) || [];
    const state = () => (doc && doc.ok && doc.state) || {};

    function schedule(st) {
        const ids = new Set(items().map(it => it.id));
        const posted = st.posted || {};
        const every = Math.max(1, parseInt(st.every, 10) || 1);
        const start = isDay(st.start) ? st.start : today();
        const done = Object.values(posted).map(p => p && p.date).filter(isDay).sort();
        const last = done.length ? done[done.length - 1] : null;
        let slot = [today(), start, last ? addDays(last, every) : start].sort().pop();
        const out = [];
        const seen = new Set();
        (st.queue || []).forEach(id => {
            if (!ids.has(id) || posted[id] || seen.has(id)) return;
            seen.add(id);
            out.push([id, slot]);
            slot = addDays(slot, every);
        });
        return out;
    }

    function count() {
        return doc && doc.ok ? schedule(state()).length : 0;
    }

    function label(it) {
        return it.kind === 'reel' ? `Reel ${pad(it.n)}: ${it.title}` : `Carousel: ${it.title}`;
    }
    function kindText(it) {
        if (it.kind === 'reel') return 'Reel' + (it.duration ? ' · ' + fmtDur(it.duration) : '');
        return `Carousel · ${it.slides || (it.files || []).length} slides`;
    }
    const captionOf = it => ((state().captions || {})[it.id] || it.caption || '');

    // ── saving: fn changes a copy of the newest state; a 409 brings the state written elsewhere, and fn runs again ──
    function mutate(fn) {
        chain = chain.then(async () => {
            for (let i = 0; i < 4; i++) {
                const st = JSON.parse(JSON.stringify(state()));
                st.rev = st.rev || 0;
                st.queue = st.queue || [];
                st.posted = st.posted || {};
                st.captions = st.captions || {};
                fn(st);
                const r = await fetch(`${API}/business/state`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(st),
                });
                const body = await r.json().catch(() => ({}));
                if (r.ok) {
                    doc.state = body;
                    statusMsg = '';
                    return true;
                }
                if (r.status === 409 && body.state) {
                    doc.state = body.state;
                    continue;
                }
                throw new Error(body.error || `HTTP ${r.status}`);
            }
            throw new Error('it kept changing elsewhere; try again');
        }).catch(e => {
            statusMsg = 'Not saved: ' + e.message;
            return false;
        });
        return chain;
    }
    const change = fn => mutate(fn).then(() => redraw && redraw());

    function move(id, by) {
        return change(st => {
            const sched = schedule(st).map(([x]) => x);
            const i = sched.indexOf(id);
            const j = i + by;
            if (i < 0 || j < 0 || j >= sched.length) return;
            [sched[i], sched[j]] = [sched[j], sched[i]];
            // posted ids keep their places; the unposted ones take the new order
            let k = 0;
            st.queue = st.queue.map(x => (sched.includes(x) ? sched[k++] : x));
        });
    }

    // ── one post ──
    function preview(it) {
        const box = el('div', 'biz-screen' + (it.kind === 'reel' ? '' : ' feed'));
        if (it.kind === 'reel') {
            const v = el('video');
            v.controls = true;
            v.preload = 'none';
            v.playsInline = true;
            if (it.cover) v.poster = fileURL(it.cover);
            v.src = fileURL(it.file);
            box.appendChild(v);
        } else {
            const img = el('img');
            img.loading = 'lazy';
            img.alt = '';
            img.src = fileURL(it.cover || (it.files || [])[0] || '');
            box.appendChild(img);
            box.appendChild(el('span', 'biz-badge', `1/${(it.files || []).length}`));
        }
        return box;
    }

    function slides(it, box) {
        const strip = el('div', 'biz-slides');
        (it.files || []).forEach((rel, i) => {
            const t = el('img', i === 0 ? 'on' : '');
            t.loading = 'lazy';
            t.alt = '';
            t.title = `Slide ${i + 1}`;
            t.src = fileURL(rel);
            t.addEventListener('click', () => {
                box.querySelector('img').src = fileURL(rel);
                box.querySelector('.biz-badge').textContent = `${i + 1}/${it.files.length}`;
                strip.querySelectorAll('img').forEach(x => x.classList.toggle('on', x === t));
            });
            strip.appendChild(t);
        });
        return strip;
    }

    function row(it, where, ctx) {
        const st = state();
        const p = (st.posted || {})[it.id];
        const c = el('div', 'reels-row' + (p ? ' posted' : ''));
        c.id = `biz-row-${it.id}`;
        const side = el('div', 'reels-row-side');
        side.appendChild(el('div', 'reels-row-n', where === 'scheduled' ? `#${ctx.n}` : where === 'posted' ? '✓' : '·'));
        c.appendChild(side);
        const box = preview(it);
        c.appendChild(box);

        const body = el('div', 'reels-row-body');
        const head = where === 'scheduled' ? `${fmtDay(ctx.date)} · ` : where === 'posted' ? `${fmtDay(p.date)} · ` : '';
        body.appendChild(el('div', 'reel-title', head + label(it)));
        body.appendChild(el('div', 'reel-meta',
            [kindText(it), it.section, it.pin ? `pin ${it.pin} on the profile` : null].filter(Boolean).join(' · ')));
        if (it.kind === 'carousel') body.appendChild(slides(it, box));
        if (it.kind === 'reel' && it.sound) body.appendChild(el('div', 'biz-sound', '♪ Sound to pick in the app: ' + it.sound));
        if (it.kind === 'reel' && it.audio === 'reference' && !p) {
            body.appendChild(el('div', 'biz-note', 'Its soundtrack is for review only (not licensed): by hand, ' +
                'pick a sound in the app; from the API or the bot, it goes out silent or as mixed, your choice.'));
        }

        if (p) {
            const line = el('div', 'reels-row-acts');
            if (p.permalink) {
                const a = el('a', 'biz-link', 'Open the post ↗');
                a.href = p.permalink;
                a.target = '_blank';
                a.rel = 'noopener';
                line.appendChild(a);
            }
            line.appendChild(el('span', 'reels-posted', `Posted ${p.date}` + (p.via ? ` (${p.via === 'hand' ? 'by hand' : p.via})` : '')));
            if (p.via === 'hand') {
                const undo = el('button', 'reels-mini', 'Undo');
                undo.type = 'button';
                undo.title = 'Not posted after all: back in its place in the schedule';
                undo.addEventListener('click', () => change(s => { delete s.posted[it.id]; }));
                line.appendChild(undo);
            }
            body.appendChild(line);
            body.appendChild(el('div', 'biz-caption-done', captionOf(it)));
            c.appendChild(body);
            return c;
        }

        const ta = el('textarea', 'reels-caption');
        ta.rows = 4;
        ta.value = captionOf(it);
        const edited = el('span', 'biz-edited', (st.captions || {})[it.id] ? 'caption edited here' : '');
        let t = null;
        ta.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => {
                const v = ta.value;
                mutate(s => {
                    if (!v.trim() || v.trim() === (it.caption || '').trim()) delete s.captions[it.id];
                    else s.captions[it.id] = v;
                }).then(ok => {
                    edited.textContent = ok ? ((state().captions || {})[it.id] ? 'caption edited here · saved' : 'saved')
                        : statusMsg;
                });
            }, 600);
        });
        body.appendChild(ta);

        const acts = el('div', 'reels-row-acts');
        const btn = (text, title, fn, dis) => {
            const b = el('button', 'reels-mini', text);
            b.type = 'button';
            b.title = title;
            b.disabled = !!dis;
            b.addEventListener('click', fn);
            acts.appendChild(b);
        };
        const tick = el('label', 'reels-posted');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.addEventListener('change', () => change(s => {
            s.posted[it.id] = { date: today(), via: 'hand' };
        }));
        tick.append(cb, document.createTextNode(' Posted'));
        tick.title = 'Posted by hand today: the bot and the post command leave it alone from now on';
        acts.appendChild(tick);
        if (where === 'scheduled') {
            btn('↑', 'Post earlier', () => move(it.id, -1), ctx.i === 0);
            btn('↓', 'Post later', () => move(it.id, +1), ctx.i === ctx.total - 1);
            btn('To backlog', 'Take it off the schedule', () => change(s => {
                s.queue = s.queue.filter(x => x !== it.id);
            }));
        } else {
            btn('+ Schedule', 'Add it to the end of the schedule', () => change(s => {
                s.queue = s.queue.filter(x => x !== it.id).concat([it.id]);
            }));
        }
        btn('Copy caption', 'Copy the caption', () => navigator.clipboard.writeText(ta.value));
        const cmd = doc.content.post_command;
        if (cmd) {
            const ref = it.kind === 'reel' ? String(it.n) : (it.ref || it.id);
            const line = cmd.replace('{ref}', ref);
            btn('Copy command', line, () => navigator.clipboard.writeText(line));
        }
        if ((st.captions || {})[it.id]) {
            btn('Reset caption', 'Back to the caption it came with', () => change(s => { delete s.captions[it.id]; }));
        }
        acts.appendChild(edited);
        body.appendChild(acts);
        c.appendChild(body);
        return c;
    }

    // ── the phone: the profile grid as it will look, newest first ──
    function phone(order, posted, onTile) {
        const wrap = el('div', 'reels-phone-wrap');
        const ph = el('div', 'reels-phone');
        const scr = el('div', 'reels-phone-screen');
        const head = el('div', 'reels-phone-head');
        const avatar = el('div', 'reels-phone-avatar');
        const first = order.find(it => it.cover);
        if (first) avatar.style.backgroundImage = `url("${fileURL(first.cover)}")`;
        const stats = el('div', 'reels-phone-stats');
        [[order.length, 'posts'], [posted, 'posted'], [order.length - posted, 'planned']].forEach(([n, l]) => {
            const s = el('div', 'reels-phone-stat');
            s.append(el('b', '', String(n)), el('span', '', l));
            stats.appendChild(s);
        });
        head.append(avatar, stats);
        scr.appendChild(head);
        scr.appendChild(el('div', 'reels-phone-name', doc.account ? '@' + doc.account : 'Profile preview'));
        const tabs = el('div', 'reels-phone-tabs');
        tabs.append(el('span', 'on', '▦'), el('span', '', '▶'));
        scr.appendChild(tabs);
        const grid = el('div', 'reels-phone-grid tall34');
        order.slice().reverse().forEach(it => {
            const n = order.indexOf(it) + 1;
            const done = !!(state().posted || {})[it.id];
            const t = el('button', 'reels-phone-tile' + (done ? ' posted' : ''));
            t.type = 'button';
            t.title = `${n}. ${label(it)}` + (done ? ' (posted)' : ' (planned)');
            if (it.cover) {
                const img = el('img');
                img.src = fileURL(it.cover);
                img.loading = 'lazy';
                img.alt = '';
                t.appendChild(img);
            }
            t.appendChild(el('span', 'reels-phone-n', done ? '✓' : `#${n - posted}`));
            t.appendChild(el('span', 'reels-phone-dur', it.kind === 'reel'
                ? '▶ ' + (it.duration ? fmtDur(it.duration) : '') : `❐ ${(it.files || []).length}`));
            t.addEventListener('click', () => onTile(it));
            grid.appendChild(t);
        });
        if (!order.length) grid.appendChild(el('p', 'reels-phone-empty', 'Nothing posted or scheduled yet.'));
        scr.appendChild(grid);
        ph.appendChild(scr);
        wrap.appendChild(ph);
        wrap.appendChild(el('p', 'reels-how', 'Newest first, as on the profile: the posted ones (✓), then the ' +
            'schedule (#1 goes out first). Pinned posts are not shown at the top here.'));
        return wrap;
    }

    // a post marked elsewhere (the bot, the post command) shows when the page comes back into view
    function listen() {
        if (listening) return;
        listening = true;
        const refresh = async () => {
            if (document.hidden || !doc || !redraw) return;
            const before = doc.ok ? state().rev : null;
            const fresh = await load();
            if (!fresh) return;
            const busy = document.activeElement && document.activeElement.classList.contains('reels-caption');
            if (!busy && (!fresh.ok || fresh.state.rev !== before)) redraw();
        };
        window.addEventListener('focus', refresh);
        document.addEventListener('visibilitychange', refresh);
    }

    function render(root, d, rerender) {
        if (d) doc = d;
        redraw = rerender;
        listen();
        if (!doc.ok) {
            root.appendChild(el('p', 'reels-empty', doc.reason === 'unreachable'
                ? `The content folder isn't reachable (${doc.folder}): mount its share, then reload.`
                : `No content.json in ${doc.folder} yet: export the posts there first.`));
            return;
        }
        const st = state();
        const all = items();
        const byId = Object.fromEntries(all.map(it => [it.id, it]));
        const sch = schedule(st);
        const posted = Object.entries(st.posted || {}).filter(([id]) => byId[id])
            .sort((a, b) => String(a[1].date).localeCompare(String(b[1].date)));
        const queued = new Set(st.queue || []);
        const backlog = all.filter(it => !queued.has(it.id) && !(st.posted || {})[it.id]);

        root.appendChild(el('p', 'reels-how', [doc.content.name, doc.account ? '@' + doc.account : 'account not set up yet',
            `${all.length} ready`, `${posted.length} posted`, `${sch.length} scheduled`, `${backlog.length} in the backlog`]
            .filter(Boolean).join(' · ')));
        (doc.content.notes || []).forEach(n => root.appendChild(el('p', 'reels-how', n)));
        if (statusMsg) root.appendChild(el('p', 'biz-status', statusMsg));

        const ctl = el('div', 'biz-controls');
        const startL = el('label', '', 'Start ');
        const start = el('input');
        start.type = 'date';
        start.value = isDay(st.start) ? st.start : '';
        start.addEventListener('change', () => change(s => { s.start = start.value || null; }));
        startL.appendChild(start);
        const everyL = el('label', '', 'One every ');
        const every = el('input');
        every.type = 'number';
        every.min = '1';
        every.max = '30';
        every.value = String(st.every || 1);
        every.addEventListener('change', () => change(s => { s.every = Math.max(1, parseInt(every.value, 10) || 1); }));
        everyL.append(every, document.createTextNode(' day(s)'));
        ctl.append(startL, everyL, el('span', 'biz-next', sch.length
            ? `Next: ${fmtDay(sch[0][1])}, ${label(byId[sch[0][0]])}` : 'Nothing scheduled'));
        root.appendChild(ctl);

        const layout = el('div', 'reels-layout');
        const left = el('div', 'reels-list');
        const right = el('div', 'reels-side');
        left.appendChild(el('h2', 'biz-h', `Scheduled (${sch.length})`));
        if (!sch.length) left.appendChild(el('p', 'reels-empty', 'Nothing scheduled: add posts from the backlog below.'));
        sch.forEach(([id, date], i) => left.appendChild(row(byId[id], 'scheduled', { n: i + 1, date, i, total: sch.length })));
        if (posted.length) {
            left.appendChild(el('h2', 'biz-h', `Posted (${posted.length})`));
            posted.slice().reverse().forEach(([id]) => left.appendChild(row(byId[id], 'posted')));
        }
        left.appendChild(el('h2', 'biz-h', `Backlog (${backlog.length})`));
        backlog.forEach(it => left.appendChild(row(it, 'backlog')));

        const order = posted.map(([id]) => byId[id]).concat(sch.map(([id]) => byId[id]));
        right.appendChild(phone(order, posted.length, it => {
            const target = document.getElementById(`biz-row-${it.id}`);
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
