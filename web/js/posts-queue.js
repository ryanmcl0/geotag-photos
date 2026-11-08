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
 *
 * Two boxes: the post queue (reel_queue.items) is what can go out now; the shortlist (reel_queue.shortlist) holds the
 * reels he approved but isn't posting yet. Only this page reads the shortlist: queue_sync.py hands the bot the queue's
 * lines alone, so a shortlisted reel goes nowhere until "Post now" moves it into the queue.
 *
 * The reject list (owner 2026-11-08): "Reject" on a queued or shortlisted reel, on a picked reel below, or pasted paths
 * in the Rejected box, each with a reason (quick reasons to tap, or his own words). State: "reel_rejects" =
 * {items: [{id, path, reason, rejected, noted}], updated} in the same document, kept by functions/api/posts.ts and
 * post.py's mirror like reel_queue. A rejected reel leaves the queue and the shortlist and can't be added back until
 * "Undo". queue_sync.py on the Mac keeps the bot from ever offering it and logs the reason for the reel builders
 * (Short Form Content/REJECTS.jsonl; "noted" is when it did): the feedback loop.
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
    let lastAdd = { where: null, lines: [] };   // the last paste: which box, its lines and what happened to each
    let openReject = null;           // the path whose reason form is open (kept across redraws)
    let showAllRejects = false;
    let lastRejectSig = null;        // what the reject list looked like when listeners were last told
    const rejectListeners = new Set();
    const REJECTS_SHOWN = 20;
    // quick reasons to tap (owner 2026-11-08); several can be combined, and his own words added
    const QUICK_REASONS = ["song doesn't fit", 'too slow', 'repeated shot', 'bad grade / colour', 'wrong place name',
        'boring'];

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }

    function queue() {
        const q = (doc && doc.reel_queue) || {};
        if (!Array.isArray(q.items)) q.items = [];
        if (!Array.isArray(q.shortlist)) q.shortlist = [];
        return q;
    }

    function rejects() {
        const r = (doc && doc.reel_rejects) || {};
        return Array.isArray(r.items) ? r.items : [];
    }

    function rejectOf(path) { return rejects().find(i => i.path === path) || null; }

    // the reels tab's other parts (posts-reels.js rows) redraw when the reject list changes
    function tellRejects() {
        const sig = JSON.stringify(rejects().map(i => [i.path, i.reason]));
        if (sig === lastRejectSig) return;
        const first = lastRejectSig === null;
        lastRejectSig = sig;
        if (!first || rejects().length) rejectListeners.forEach(fn => { try { fn(); } catch (e) { /* its own problem */ } });
    }

    async function fetchDoc() {
        const r = await fetch('/api/posts', { cache: 'no-store' });
        if (!r.ok) throw new Error('posts unavailable (' + r.status + ')');
        doc = await r.json();
        return doc;
    }

    // fn(q, rj) changes the queue and the reject list in place; re-applied to a fresh copy if the document moved on
    // meanwhile (409).
    function change(fn) {
        const run = async () => {
            if (!doc) await fetchDoc();
            const apply = () => {
                const now = new Date().toISOString();
                const q = Object.assign({ items: [], span_days: SPAN_DEFAULT }, doc.reel_queue || {});
                q.items = (q.items || []).map(i => Object.assign({}, i));
                q.shortlist = (q.shortlist || []).map(i => Object.assign({}, i));
                const rj = Object.assign({}, doc.reel_rejects || {});
                rj.items = (rj.items || []).map(i => Object.assign({}, i));
                const before = JSON.stringify(rj.items);
                fn(q, rj);
                // a rejected reel can't be queued or shortlisted, whichever save put it there
                const out = new Set(rj.items.map(i => i.path));
                q.items = q.items.filter(i => !out.has(i.path) || i.status === 'posted');
                q.shortlist = q.shortlist.filter(i => !out.has(i.path));
                if (!q.shortlist.length) delete q.shortlist;
                q.updated = now;
                doc.reel_queue = q;
                if (JSON.stringify(rj.items) !== before) rj.updated = now;
                if (doc.reel_rejects || rj.items.length) doc.reel_rejects = rj;
            };
            apply();
            const put = () => fetch('/api/posts', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                // no settings: the API keeps the stored ones; posts as read with this version
                body: JSON.stringify(Object.assign(
                    { baseVersion: doc.version, posts: doc.posts || [], reel_queue: doc.reel_queue },
                    doc.reel_rejects ? { reel_rejects: doc.reel_rejects } : {}))
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

    function checkLine(path, inQueue, inShort) {
        if (!path.startsWith('/') && !path.startsWith('~')) return 'not a full path';
        if (!/\.mp4$/i.test(path)) return 'not an .mp4 file';
        const rj = rejectOf(path);
        if (rj) return `rejected (${rj.reason}): Undo it under Rejected first`;
        if (inQueue.has(path)) return 'already in the queue';
        if (inShort.has(path)) return 'already on the shortlist';
        return null;
    }

    function queueLine(p, now) { return { id: p, path: p, added: now, status: 'pending' }; }

    // where: 'queue' (post now) or 'shortlist' (approved, not yet)
    function addLines(text, where) {
        const lines = text.split(/\r?\n/).map(cleanLine).filter(Boolean);
        if (!lines.length) return;
        const q0 = queue();
        const inQueue = new Set(q0.items.map(i => i.path));
        const inShort = new Set(q0.shortlist.map(i => i.path));
        lastAdd = { where, lines: [] };
        const take = [];
        lines.forEach(p => {
            const why = checkLine(p, inQueue, inShort);
            lastAdd.lines.push({ path: p, why });
            if (!why) { (where === 'queue' ? inQueue : inShort).add(p); take.push(p); }
        });
        if (!take.length) { redraw(); return; }
        const now = new Date().toISOString();
        change((q, rj) => {
            const there = new Set([...q.items, ...q.shortlist, ...rj.items].map(i => i.path));
            take.filter(p => !there.has(p)).forEach(p =>
                where === 'queue' ? q.items.push(queueLine(p, now)) : q.shortlist.push({ id: p, path: p, added: now }));
            if (!q.span_days) q.span_days = SPAN_DEFAULT;
        });
    }

    // shortlist → queue: these can go out now
    function postNow(paths) {
        const take = new Set(paths);
        const now = new Date().toISOString();
        change((q, rj) => {
            const there = new Set([...q.items, ...rj.items].map(i => i.path));
            q.shortlist.filter(i => take.has(i.path) && !there.has(i.path)).forEach(i => q.items.push(queueLine(i.path, now)));
            q.shortlist = q.shortlist.filter(i => !take.has(i.path));
            if (!q.span_days) q.span_days = SPAN_DEFAULT;
        });
    }

    // ── the reject list ──
    // paths → rejected with this reason: out of the queue and the shortlist; one rejected before gets the new reason
    function reject(paths, reason) {
        reason = (reason || '').trim().slice(0, 500);
        paths = paths.filter(Boolean);
        if (!reason || !paths.length) return Promise.resolve();
        const take = new Set(paths);
        const now = new Date().toISOString();
        openReject = null;
        return change((q, rj) => {
            q.items = q.items.filter(i => !take.has(i.path) || i.status === 'posted');
            q.shortlist = q.shortlist.filter(i => !take.has(i.path));
            paths.forEach(p => {
                const old = rj.items.find(i => i.path === p);
                if (old) {
                    if (old.reason !== reason) { old.reason = reason; old.rejected = now; delete old.noted; }
                } else {
                    rj.items.unshift({ id: p, path: p, reason, rejected: now });
                }
            });
        });
    }

    // not rejected after all: off the list (it does not go back into the queue by itself)
    function undoReject(path) {
        return change((q, rj) => { rj.items = rj.items.filter(i => i.path !== path); });
    }

    // the reason box: quick reasons to tap (they toggle, several combine with ";"), his own words, Reject / Cancel
    function reasonForm(key, onReject, onCancel, label) {
        const f = el('div', 'pq-reason');
        const input = el('input', 'pq-reason-text');
        input.type = 'text';
        input.maxLength = 500;
        input.dataset.keep = 'reason:' + key;
        input.placeholder = 'Why? Tap a reason, or write your own';
        const go = el('button', 'reels-mini pq-reject-go', label || 'Reject');
        go.type = 'button';
        const chips = el('div', 'pq-chips');
        const parts = () => input.value.split(';').map(x => x.trim()).filter(Boolean);
        const paint = () => {
            go.disabled = !input.value.trim();
            chips.querySelectorAll('button').forEach(c => c.classList.toggle('active', parts().includes(c.textContent)));
        };
        QUICK_REASONS.forEach(r => {
            const c = el('button', 'reel-chip', r);
            c.type = 'button';
            c.addEventListener('click', () => {
                const ps = parts();
                const at = ps.indexOf(r);
                if (at >= 0) ps.splice(at, 1); else ps.push(r);
                input.value = ps.join('; ');
                paint();
                input.focus();
            });
            chips.appendChild(c);
        });
        input.addEventListener('input', paint);
        input.addEventListener('keydown', e => {
            if (e.key === 'Enter' && input.value.trim()) go.click();
            if (e.key === 'Escape' && onCancel) onCancel();
        });
        go.addEventListener('click', () => onReject(input.value.trim()));
        const row = el('div', 'pq-reason-row');
        row.append(input, go);
        if (onCancel) {
            const no = el('button', 'reels-mini', 'Cancel');
            no.type = 'button';
            no.addEventListener('click', onCancel);
            row.appendChild(no);
        }
        f.append(chips, row);
        paint();
        return f;
    }

    // "Reject…" on a row: opens the reason box under it (one at a time)
    function rejectButton(path, after) {
        const b = el('button', 'reels-mini pq-reject-btn', 'Reject…');
        b.type = 'button';
        b.title = 'Not good enough: give a reason. It leaves the queue and the shortlist, the bot never offers it, ' +
            'and the reason goes to the reel builders';
        b.addEventListener('click', () => { openReject = openReject === path ? null : path; (after || redraw)(); });
        return b;
    }

    function rejectFormFor(path, after) {
        return reasonForm(path, reason => reject([path], reason).then(() => after && after()),
            () => { openReject = null; (after || redraw)(); });
    }

    // queue → shortlist: still good, just not yet (the bot drops it at the next check, like Remove)
    function notYet(path) {
        const now = new Date().toISOString();
        change(q => {
            q.items = q.items.filter(i => i.path !== path);
            if (!q.shortlist.some(i => i.path === path)) q.shortlist.push({ id: path, path, added: now });
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
                const btns = el('div', 'pq-btns');
                const back = el('button', 'reels-mini', 'Back to shortlist');
                back.type = 'button';
                back.title = 'Still good to go, just not yet: keep it on the shortlist instead (it is not posted)';
                back.addEventListener('click', () => notYet(i.path));
                const rm = el('button', 'reels-mini', 'Remove');
                rm.type = 'button';
                rm.title = 'Take it out of the queue (it is not posted)';
                rm.addEventListener('click', () => change(qq => { qq.items = qq.items.filter(x => x.path !== i.path); }));
                btns.append(back, rm, rejectButton(i.path));
                row.appendChild(btns);
                if (openReject === i.path) row.appendChild(rejectFormFor(i.path));
            }
            box.appendChild(row);
        });
        return box;
    }

    function shortlistBlock(q) {
        const box = el('div', 'pq-list');
        if (!q.shortlist.length) {
            box.appendChild(el('p', 'reels-empty', 'Nothing on the shortlist.'));
            return box;
        }
        q.shortlist.forEach((i, n) => {
            const row = el('div', 'pq-row');
            const main = el('div', 'pq-main');
            main.appendChild(el('div', 'pq-title', `${n + 1}. ${fileName(i.path)}`));
            const p = el('div', 'pq-path', i.path);
            p.title = i.path;
            main.appendChild(p);
            if (i.added) {
                main.appendChild(el('div', 'pq-status wait', 'Shortlisted ' +
                    new Date(i.added).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })));
            }
            row.appendChild(main);
            const btns = el('div', 'pq-btns');
            const go = el('button', 'reels-mini', 'Post now');
            go.type = 'button';
            go.title = 'Move it into the post queue: the bot fits it into the schedule';
            go.addEventListener('click', () => postNow([i.path]));
            const rm = el('button', 'reels-mini', 'Remove');
            rm.type = 'button';
            rm.title = 'Take it off the shortlist';
            rm.addEventListener('click', () => change(qq => { qq.shortlist = qq.shortlist.filter(x => x.path !== i.path); }));
            btns.append(go, rm, rejectButton(i.path));
            row.appendChild(btns);
            if (openReject === i.path) row.appendChild(rejectFormFor(i.path));
            box.appendChild(row);
        });
        return box;
    }

    function fmtDay(iso) {
        if (!iso) return '';
        try { return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); } catch (e) { return ''; }
    }

    function rejectsBlock() {
        const all = rejects();
        const box = el('div', 'pq-list');
        if (!all.length) {
            box.appendChild(el('p', 'reels-empty', 'Nothing rejected.'));
            return box;
        }
        const shown = showAllRejects ? all : all.slice(0, REJECTS_SHOWN);
        shown.forEach(i => {
            const row = el('div', 'pq-row pq-rejected');
            const main = el('div', 'pq-main');
            main.appendChild(el('div', 'pq-title', fileName(i.path)));
            const p = el('div', 'pq-path', i.path);
            p.title = i.path;
            main.appendChild(p);
            main.appendChild(el('div', 'pq-status bad', `Rejected ${fmtDay(i.rejected)}: ${i.reason}`));
            main.appendChild(el('div', 'pq-status ' + (i.noted ? 'done' : 'wait'), i.noted
                ? 'Noted for the reel builders ' + fmtDay(i.noted)
                : 'Waiting for the Mac to note it'));
            row.appendChild(main);
            const btns = el('div', 'pq-btns');
            const un = el('button', 'reels-mini', 'Undo');
            un.type = 'button';
            un.title = 'Not rejected after all: take it off this list (it does not go back into the queue by itself)';
            un.addEventListener('click', () => undoReject(i.path));
            const re = el('button', 'reels-mini', 'Change reason');
            re.type = 'button';
            re.addEventListener('click', () => { openReject = openReject === i.path ? null : i.path; redraw(); });
            btns.append(un, re);
            row.appendChild(btns);
            if (openReject === i.path) {
                row.appendChild(reasonForm(i.path, reason => reject([i.path], reason),
                    () => { openReject = null; redraw(); }, 'Save reason'));
            }
            box.appendChild(row);
        });
        if (all.length > REJECTS_SHOWN) {
            const more = el('button', 'reels-mini pq-all', showAllRejects ? 'Show the newest only' : `Show all ${all.length}`);
            more.type = 'button';
            more.addEventListener('click', () => { showAllRejects = !showAllRejects; redraw(); });
            box.appendChild(more);
        }
        return box;
    }

    // the Rejected box's paste: paths, one reason for all of them
    function rejectPasteBar(sec) {
        const ta = el('textarea', 'pq-paste');
        ta.rows = 2;
        ta.dataset.box = 'reject';
        ta.dataset.keep = 'box:reject';
        ta.placeholder = '/Volumes/Crucial X9/Short Form Content/…/2 Shoots/sxj_d12_s2_toksun_916_globe.mp4';
        sec.appendChild(ta);
        sec.appendChild(reasonForm('paste', reason => {
            const lines = ta.value.split(/\r?\n/).map(cleanLine).filter(Boolean);
            lastAdd = { where: 'reject', lines: [] };
            const take = [];
            lines.forEach(p => {
                const why = !p.startsWith('/') && !p.startsWith('~') ? 'not a full path'
                    : !/\.mp4$/i.test(p) ? 'not an .mp4 file' : null;
                lastAdd.lines.push({ path: p, why, done: rejectOf(p) ? 'reason changed' : 'rejected' });
                if (!why && !take.includes(p)) take.push(p);
            });
            if (!take.length) { redraw(); return; }
            ta.value = '';
            const keep = panelEl && panelEl.querySelector('[data-keep="reason:paste"]');
            if (keep) keep.value = '';
            reject(take, reason);
        }, null, 'Reject these'));
        if (lastAdd.where === 'reject' && lastAdd.lines.length) {
            const res = el('ul', 'pq-added');
            lastAdd.lines.forEach(a => res.appendChild(el('li', a.why ? 'bad' : 'ok',
                (a.why ? '✗ ' : '✓ ') + fileName(a.path) + ': ' + (a.why || a.done))));
            sec.appendChild(res);
        }
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

    function pasteBar(sec, where, label) {
        const ta = el('textarea', 'pq-paste');
        ta.rows = 3;
        ta.dataset.box = where;
        ta.dataset.keep = 'box:' + where;
        ta.placeholder = '/Users/…/day12/reels/sxj_d12_day_916.mp4';
        const add = el('button', 'reels-export-btn', label);
        add.type = 'button';
        add.addEventListener('click', () => { addLines(ta.value, where); ta.value = ''; });
        const bar = el('div', 'pq-addbar');
        bar.append(ta, add);
        sec.appendChild(bar);
        if (lastAdd.where === where && lastAdd.lines.length) {
            const res = el('ul', 'pq-added');
            lastAdd.lines.forEach(a => res.appendChild(el('li', a.why ? 'bad' : 'ok',
                (a.why ? '✗ ' : '✓ ') + fileName(a.path) + (a.why ? ': ' + a.why : ': added'))));
            sec.appendChild(res);
        }
    }

    function draw() {
        const q = queue();
        const out = el('div');
        const sec = el('section', 'pq');
        sec.appendChild(el('div', 'pq-h', 'Post queue: post these now'));
        sec.appendChild(el('p', 'reels-how',
            'Paste the file path of each reel that can go out now, one per line (in Finder: select the file, hold Option, ' +
            'right-click, Copy … as Pathname). Several at once is fine. The bot spreads them over the time you set ' +
            'and each morning sends one Telegram message with that day\'s posts: Approve all, or Skip a number.'));
        pasteBar(sec, 'queue', 'Add to queue');
        sec.appendChild(sliderBlock(q));
        const cols = el('div', 'pq-cols');
        cols.append(listBlock(q), scheduleBlock(q));
        sec.appendChild(cols);
        sec.appendChild(el('p', 'reels-how', q.synced
            ? 'Last checked by the Mac: ' + new Date(q.synced).toLocaleString('en-GB', { hour12: false })
            : 'Not checked by the Mac yet (video-editor: tools/reelbot/queue_sync.py, or its launchd job).'));

        const sl = el('section', 'pq pq-short');
        sl.appendChild(el('div', 'pq-h', 'Shortlist: good to go, not yet'));
        sl.appendChild(el('p', 'reels-how',
            'Reels you approved but aren\'t posting yet. The bot never sees these: "Post now" moves one into the ' +
            'queue above, and "Back to shortlist" on a queued reel brings it back here.'));
        pasteBar(sl, 'shortlist', 'Add to shortlist');
        if (q.shortlist.length > 1) {
            const all = el('button', 'reels-mini pq-all', `Post all ${q.shortlist.length} now`);
            all.type = 'button';
            all.title = 'Move the whole shortlist into the post queue';
            all.addEventListener('click', () => postNow(q.shortlist.map(i => i.path)));
            sl.appendChild(all);
        }
        sl.appendChild(shortlistBlock(q));

        const rs = el('section', 'pq pq-rejects');
        rs.appendChild(el('div', 'pq-h', `Rejected: not for posting (${rejects().length})`));
        rs.appendChild(el('p', 'reels-how',
            'Reels from Short Form Content that aren\'t good enough, each with why. A rejected reel leaves the queue and ' +
            'the shortlist, can\'t be added back until Undo, and the bot never offers it. The Mac notes every reason for ' +
            'the reel builders (Short Form Content/REJECTS.md), so the next batch learns from it. Use "Reject…" on a ' +
            'reel above or below, or paste paths here.'));
        rejectPasteBar(rs);
        rs.appendChild(rejectsBlock());
        out.append(sec, sl, rs);
        return out;
    }

    function redraw() {
        if (!panelEl || !panelEl.isConnected) { clearInterval(timer); timer = null; return; }
        // keep what's typed in the boxes and the reason fields (and the focus) across the redraw
        const typed = {};
        panelEl.querySelectorAll('[data-keep]').forEach(t => { if (t.value) typed[t.dataset.keep] = t.value; });
        const a = document.activeElement;
        const focus = a && panelEl.contains(a) && a.dataset && a.dataset.keep ? a.dataset.keep : null;
        const fresh = draw();
        panelEl.replaceChildren(...fresh.childNodes);
        panelEl.querySelectorAll('[data-keep]').forEach(t => {
            if (typed[t.dataset.keep]) {
                t.value = typed[t.dataset.keep];
                t.dispatchEvent(new Event('input'));          // the reason form's chips and button follow the text
            }
            if (t.dataset.keep === focus) t.focus();
        });
        tellRejects();
    }

    // The panel for the Reels tab: drawn from the last copy at once, refreshed from the server, then every 30 s
    // (the Mac's checks and the bot's schedule arrive on their own).
    function panel() {
        panelEl = el('div', 'pq-wrap');
        panelEl.append(...(doc ? draw().childNodes : [el('p', 'reels-how pq', 'Loading the post queue…')]));
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

    // For posts-reels.js: a picked reel's reject control. Rejected → its reason and Undo; else "Reject…" and, when
    // open, the reason box. after() runs once the list changed (the caller redraws its rows).
    function rejectControl(path, after) {
        const wrap = el('div', 'pq-reject-ctl');
        if (!path) return wrap;
        const rj = rejectOf(path);
        if (rj) {
            wrap.appendChild(el('span', 'pq-status bad', `Rejected ${fmtDay(rj.rejected)}: ${rj.reason}`));
            const un = el('button', 'reels-mini', 'Undo');
            un.type = 'button';
            un.addEventListener('click', () => undoReject(path).then(() => after && after()));
            wrap.appendChild(un);
            return wrap;
        }
        wrap.appendChild(rejectButton(path, after));
        if (openReject === path) wrap.appendChild(rejectFormFor(path, after));
        return wrap;
    }

    function onRejects(fn) { rejectListeners.add(fn); return () => rejectListeners.delete(fn); }

    return { panel, rejectOf, reject, undoReject, rejectControl, onRejects };
})();
