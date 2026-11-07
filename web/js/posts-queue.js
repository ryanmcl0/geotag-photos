/*
 * Posts ▸ Reels ▸ Post queue (localhost only, owner 2026-11-07).
 *
 * He reviews the finished reels one by one; each one he approves he copies its file path into the box here. The
 * slider sets the time frame to post them over and shows what that means per day. The video project's Mac helper
 * (video-editor tools/reelbot/queue_sync.py) checks each line, copies the good ones to the bot on the NAS and writes
 * back what it found and the bot's schedule; the bot asks once each morning in Telegram (Approve all / Skip N).
 *
 * State: "reel_queue" in the Posts document (/api/posts, the same document as the drafts; functions/api/posts.ts keeps
 * it when a drafts-only save leaves it out), so post.py's mirror carries it to the live site and back like the drafts.
 * Every change is a function re-applied to a fresh copy after a 409, like posts.js does.
 */
window.PostsQueue = (function () {
    'use strict';

    const SPAN_DEFAULT = 14;
    const SPAN_MAX = 30;
    const MAX_PER_DAY = 7;           // the bot's posting times in a day (12:00, 20:00, 08:00, 17:00, …)
    let doc = null;                  // { version, posts, reel_queue }
    let chain = Promise.resolve();
    let panelEl = null;
    let timer = null;
    let lastAdd = [];                // the lines of the last paste and what happened to each

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }

    function queue() {
        const q = (doc && doc.reel_queue) || {};
        if (!Array.isArray(q.items)) q.items = [];
        return q;
    }

    async function fetchDoc() {
        const r = await fetch('/api/posts', { cache: 'no-store' });
        if (!r.ok) throw new Error('posts unavailable (' + r.status + ')');
        doc = await r.json();
        return doc;
    }

    // fn(q) changes the queue in place; re-applied to a fresh copy if the document moved on meanwhile (409).
    function change(fn) {
        const run = async () => {
            if (!doc) await fetchDoc();
            const apply = () => {
                const q = Object.assign({ items: [], span_days: SPAN_DEFAULT }, doc.reel_queue || {});
                q.items = (q.items || []).map(i => Object.assign({}, i));
                fn(q);
                q.updated = new Date().toISOString();
                doc.reel_queue = q;
            };
            apply();
            const put = () => fetch('/api/posts', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                // no settings: the API keeps the stored ones; posts as read with this version
                body: JSON.stringify({ baseVersion: doc.version, posts: doc.posts || [], reel_queue: doc.reel_queue })
            });
            let r = await put();
            if (r.status === 409) {
                doc = await r.json();
                apply();
                r = await put();
            }
            if (!r.ok) throw new Error('save failed (' + r.status + ')');
            doc.version = (await r.json()).version;
        };
        chain = chain.then(run).then(() => redraw(), err => { alert('Post queue: ' + err.message); redraw(); });
        return chain;
    }

    // ── what he pastes ──
    function cleanLine(s) {
        let t = s.trim().replace(/^['"]|['"]$/g, '').trim();
        if (t.startsWith('file://')) {
            try { t = decodeURIComponent(new URL(t).pathname); } catch (e) { /* leave it */ }
        }
        return t.replace(/\\ /g, ' ');
    }

    function checkLine(path, have) {
        if (!path.startsWith('/') && !path.startsWith('~')) return 'not a full path';
        if (!/\.mp4$/i.test(path)) return 'not an .mp4 file';
        if (have.has(path)) return 'already in the queue';
        return null;
    }

    function addLines(text) {
        const lines = text.split(/\r?\n/).map(cleanLine).filter(Boolean);
        if (!lines.length) return;
        const have = new Set(queue().items.map(i => i.path));
        lastAdd = [];
        const take = [];
        lines.forEach(p => {
            const why = checkLine(p, have);
            lastAdd.push({ path: p, why });
            if (!why) { have.add(p); take.push(p); }
        });
        if (!take.length) { redraw(); return; }
        const now = new Date().toISOString();
        change(q => {
            const there = new Set(q.items.map(i => i.path));
            take.filter(p => !there.has(p)).forEach(p => q.items.push({ id: p, path: p, added: now, status: 'pending' }));
            if (!q.span_days) q.span_days = SPAN_DEFAULT;
        });
    }

    // ── reading it back ──
    const WAITING = new Set(['pending', 'valid', 'scheduled']);

    function fmtWhen(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        const opt = { timeZone: 'Europe/London', weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false };
        try { return d.toLocaleString('en-GB', opt) + ' China'; } catch (e) { return iso; }
    }

    function statusText(i) {
        switch (i.status) {
            case 'valid': return ['Ready: the bot will fit it in', 'ok'];
            case 'scheduled': return [(i.approved ? 'Approved for ' : 'Planned for ') + fmtWhen(i.when), 'ok'];
            case 'posted': return ['Posted' + (i.when ? ' ' + fmtWhen(i.when) : ''), 'done'];
            case 'skipped': return ['Not posted: ' + (i.why || 'skipped'), 'warn'];
            case 'invalid': return ['Problem: ' + (i.why || 'not usable'), 'bad'];
            default: return [i.why || 'Waiting for the Mac to check it', 'wait'];
        }
    }

    function fileName(p) { return (p || '').split('/').pop(); }

    function perDay(n, days) { return n ? Math.min(MAX_PER_DAY, Math.ceil(n / Math.max(1, days))) : 0; }

    function sliderBlock(q) {
        const wrap = el('div', 'pq-slider');
        const n = q.items.filter(i => WAITING.has(i.status || 'pending')).length;
        let span = q.span_days || SPAN_DEFAULT;
        const label = el('label', 'pq-slider-label');
        const input = el('input');
        input.type = 'range';
        input.min = '1';
        input.max = String(SPAN_MAX);
        input.value = String(span);
        const read = el('div', 'pq-readout');
        const say = () => {
            const p = perDay(n, span);
            read.textContent = `${n} reel${n === 1 ? '' : 's'} in the queue · over ${span} day${span === 1 ? '' : 's'}` +
                (n ? ` · about ${p} a day` : '') +
                (n && Math.ceil(n / span) > MAX_PER_DAY ? ` (at most ${MAX_PER_DAY} a day: it takes longer)` : '');
        };
        say();
        let t = null;
        input.addEventListener('input', () => {
            span = parseInt(input.value, 10);
            say();
            clearTimeout(t);
            t = setTimeout(() => change(qq => { qq.span_days = span; qq.span_set = new Date().toISOString(); }), 600);
        });
        label.append(el('span', '', 'Post them over'), input);
        wrap.append(label, read);
        const s = q.schedule || {};
        if (s.per_day && s.span_end) {
            const end = new Date(s.span_end + 'T00:00:00');
            end.setDate(end.getDate() - 1);
            wrap.appendChild(el('div', 'pq-bot', `The bot's plan: ${s.per_day} a day, the last on about ` +
                end.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) +
                (s.missed ? ` (${s.missed} unanswered morning${s.missed === 1 ? '' : 's'} moved it on)` : '') +
                (s.posting === false ? '. Posting is off on the bot: nothing goes to Instagram yet.' : '.')));
        }
        return wrap;
    }

    function listBlock(q) {
        const box = el('div', 'pq-list');
        if (!q.items.length) {
            box.appendChild(el('p', 'reels-empty', 'Nothing in the queue yet.'));
            return box;
        }
        q.items.forEach((i, n) => {
            const row = el('div', 'pq-row');
            const [txt, cls] = statusText(i);
            const main = el('div', 'pq-main');
            main.appendChild(el('div', 'pq-title', `${n + 1}. ${i.title || fileName(i.path)}`));
            const meta = [i.kind, i.place, i.frame, i.dur ? Math.round(i.dur) + ' s' : null].filter(Boolean).join(' · ');
            if (meta) main.appendChild(el('div', 'reel-meta', meta));
            const p = el('div', 'pq-path', i.path);
            p.title = i.path;
            main.appendChild(p);
            main.appendChild(el('div', 'pq-status ' + cls, txt));
            row.appendChild(main);
            if (i.status !== 'posted') {
                const rm = el('button', 'reels-mini', 'Remove');
                rm.type = 'button';
                rm.title = 'Take it out of the queue (it is not posted)';
                rm.addEventListener('click', () => change(qq => { qq.items = qq.items.filter(x => x.path !== i.path); }));
                row.appendChild(rm);
            }
            box.appendChild(row);
        });
        return box;
    }

    function scheduleBlock(q) {
        const rows = ((q.schedule || {}).rows || []).filter(r => r.time);
        const box = el('div', 'pq-sched');
        box.appendChild(el('div', 'pq-h', 'Schedule'));
        if (!rows.length) {
            box.appendChild(el('p', 'reels-how', 'No schedule yet: the bot makes it once the Mac has checked the reels.'));
            return box;
        }
        const tbl = el('table', 'pq-table');
        const head = el('tr');
        ['Day', 'UK time', 'Reel', ''].forEach(h => head.appendChild(el('th', '', h)));
        tbl.appendChild(head);
        const word = { planned: 'planned', approved: 'approved', posted: 'posted', off: 'posting was off',
            skipped: 'skipped that day', late: 'moved on', removed: 'removed', failed: 'failed', refused: 'not public',
            limit: 'Instagram limit', private: 'not public' };
        rows.forEach(r => {
            const tr = el('tr', 'pq-' + (r.status || 'planned'));
            const d = new Date(r.time);
            const day = d.toLocaleDateString('en-GB', { timeZone: 'Europe/London', weekday: 'short', day: 'numeric', month: 'short' });
            const time = d.toLocaleTimeString('en-GB', { timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', hour12: false });
            [day, time, r.title || r.key, word[r.status] || r.status || ''].forEach(v => tr.appendChild(el('td', '', v)));
            tbl.appendChild(tr);
        });
        box.appendChild(tbl);
        return box;
    }

    function draw() {
        const q = queue();
        const sec = el('section', 'pq');
        sec.appendChild(el('div', 'pq-h', 'Post queue'));
        sec.appendChild(el('p', 'reels-how',
            'Paste the file path of each reel you approved, one per line (in Finder: select the file, hold Option, ' +
            'right-click, Copy … as Pathname). Several at once is fine. The bot spreads them over the time you set ' +
            'and each morning sends one Telegram message with that day\'s posts: Approve all, or Skip a number.'));
        const ta = el('textarea', 'pq-paste');
        ta.rows = 3;
        ta.placeholder = '/Users/…/day12/reels/sxj_d12_day_916.mp4';
        const add = el('button', 'reels-export-btn', 'Add to queue');
        add.type = 'button';
        add.addEventListener('click', () => { addLines(ta.value); ta.value = ''; });
        const bar = el('div', 'pq-addbar');
        bar.append(ta, add);
        sec.appendChild(bar);
        if (lastAdd.length) {
            const res = el('ul', 'pq-added');
            lastAdd.forEach(a => res.appendChild(el('li', a.why ? 'bad' : 'ok',
                (a.why ? '✗ ' : '✓ ') + fileName(a.path) + (a.why ? ': ' + a.why : ': added'))));
            sec.appendChild(res);
        }
        sec.appendChild(sliderBlock(q));
        const cols = el('div', 'pq-cols');
        cols.append(listBlock(q), scheduleBlock(q));
        sec.appendChild(cols);
        sec.appendChild(el('p', 'reels-how', q.synced
            ? 'Last checked by the Mac: ' + new Date(q.synced).toLocaleString('en-GB', { hour12: false })
            : 'Not checked by the Mac yet (video-editor: tools/reelbot/queue_sync.py, or its launchd job).'));
        return sec;
    }

    function redraw() {
        if (!panelEl || !panelEl.isConnected) { clearInterval(timer); timer = null; return; }
        const keep = document.activeElement && panelEl.contains(document.activeElement) &&
            document.activeElement.tagName === 'TEXTAREA' ? document.activeElement.value : null;
        const fresh = draw();
        panelEl.replaceChildren(...fresh.childNodes);
        if (keep) { const ta = panelEl.querySelector('.pq-paste'); ta.value = keep; ta.focus(); }
    }

    // The panel for the Reels tab: drawn from the last copy at once, refreshed from the server, then every 30 s
    // (the Mac's checks and the bot's schedule arrive on their own).
    function panel() {
        panelEl = el('div', 'pq-wrap pq');
        panelEl.append(...(doc ? draw().childNodes : [el('p', 'reels-how', 'Loading the post queue…')]));
        const refresh = () => {
            const typing = document.activeElement && panelEl.contains(document.activeElement);
            if (typing) return;
            fetchDoc().then(redraw, () => {});
        };
        refresh();
        clearInterval(timer);
        timer = setInterval(refresh, 30000);
        return panelEl;
    }

    return { panel };
})();
