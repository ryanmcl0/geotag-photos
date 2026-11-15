/*
 * Posts ▸ Reels ▸ Post queue (localhost only, owner 2026-11-07).
 *
 * He reviews the finished reels one by one; each one he approves he copies its file path into the box here. The
 * slider sets the time frame to post them over and shows what that means per day. The video project's Mac helper
 * (video-editor tools/reelbot/queue_sync.py) checks each line, copies the good ones to the bot on the NAS and writes
 * back what it found and the bot's schedule; at 07:00 the bot sends each reel of the day in Telegram (Approve / Skip /
 * Caption, then Confirm). 2026-11-15: Posting schedule shows the proposed schedule (the bot's planner on this queue,
 * video-editor tools/reelbot/propose.py through the local helper) and has Send to NAS (the helper runs queue_sync.py,
 * which waits for the bot's plan, so the schedule table is the bot's real one after it). A line he gave Post is never
 * refused for a check any more: its warnings (flags) show on the line and in Telegram.
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
 *
 * Where it shows (owner 2026-11-14, the Reels tab in sections, posts-reels.js): panel('queue') is the Queue section
 * (the post queue, the slider, the bot's schedule and the shortlist); panel('rejects') is the Rejected box under To
 * review in the Review section (posts-review.js), whose Post / Shortlist / Reject… are addPaths() and rejectControl()
 * here, so there is still one state. decisionOf(path) tells the other parts what has been decided about a reel. Every
 * redraw (a change saved, the 30 s refresh) fires "reels:changed" on document, for the section counts and To review.
 */
window.PostsQueue = (function () {
    'use strict';

    const SPAN_DEFAULT = 14;
    const SPAN_MAX = 120;           // owner 2026-11-15: 30 days was too short for a big queue
    const MAX_PER_DAY = 7;           // the bot's posting times in a day (12:00, 20:00, 08:00, 17:00, …)
    let doc = null;                  // { version, posts, reel_queue }
    let chain = Promise.resolve();
    let panelEl = null;
    let part = 'queue';              // what the shown panel draws: 'queue' (queue + shortlist) or 'rejects'
    let timer = null;
    let readyP = null;
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
    // several paths in one paste: one per line, or Finder's "Copy as Pathname" of many files, which puts them on ONE
    // line as '…mp4' '…mp4' (owner 2026-11-09: seven reels became one queue line)
    function splitPaste(text) {
        const out = [];
        String(text || '').split(/\r?\n/).forEach(line => {
            // a separator is: .mp4, maybe a closing quote, spaces, maybe an opening quote, then the next path
            line.split(/(?<=\.mp4)['"]?\s+['"]?(?=\/|~|file:\/\/)/i).forEach(x => out.push(x));
        });
        return out;
    }

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

    // hold: approved but held until he releases it (owner 2026-11-15, the vlog reels: "it shouldn't go into the usual
    // posting schedule … marked as approved until I give the go-ahead"); queue_sync keeps held lines from the bot
    function queueLine(p, now, hold) {
        const l = { id: p, path: p, added: now, status: 'pending' };
        if (hold) l.hold = true;
        return l;
    }

    // where: 'queue' (post now) or 'shortlist' (approved, not yet)
    function addLines(text, where) {
        addPaths(splitPaste(text).map(cleanLine).filter(Boolean), where, true);
    }

    // the same for paths already clean (To review's Post / Shortlist): report = show each line's result under the
    // paste box, as a paste does. → a promise, settled once saved.
    function addPaths(lines, where, report, opts) {
        const hold = !!(opts && opts.hold);
        if (!lines.length) return Promise.resolve();
        const q0 = queue();
        const inQueue = new Set(q0.items.map(i => i.path));
        const inShort = new Set(q0.shortlist.map(i => i.path));
        const res = [];
        const take = [];
        lines.forEach(p => {
            const why = checkLine(p, inQueue, inShort);
            res.push({ path: p, why });
            if (!why) { (where === 'queue' ? inQueue : inShort).add(p); take.push(p); }
        });
        if (report) lastAdd = { where, lines: res };
        if (!take.length) { redraw(); return Promise.resolve(); }
        const now = new Date().toISOString();
        return change((q, rj) => {
            const there = new Set([...q.items, ...q.shortlist, ...rj.items].map(i => i.path));
            take.filter(p => !there.has(p)).forEach(p =>
                where === 'queue' ? q.items.push(queueLine(p, now, hold)) : q.shortlist.push({ id: p, path: p, added: now }));
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
    // optional (the reject form, owner 2026-11-14: "reject button is hard reject with optional feedback"): the button
    // works with nothing written, and the reject is recorded as NO_REASON
    const NO_REASON = 'No reason given';
    function reasonForm(key, onReject, onCancel, label, optional) {
        const f = el('div', 'pq-reason');
        const input = el('input', 'pq-reason-text');
        input.type = 'text';
        input.maxLength = 500;
        input.dataset.keep = 'reason:' + key;
        input.placeholder = optional ? 'Why? (optional) Tap a reason, or write your own' : 'Why? Tap a reason, or write your own';
        const go = el('button', 'reels-mini pq-reject-go', label || 'Reject');
        go.type = 'button';
        const chips = el('div', 'pq-chips');
        const parts = () => input.value.split(';').map(x => x.trim()).filter(Boolean);
        const paint = () => {
            go.disabled = !optional && !input.value.trim();
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
            if (e.key === 'Enter' && (optional || input.value.trim())) go.click();
            if (e.key === 'Escape' && onCancel) onCancel();
        });
        go.addEventListener('click', () => onReject(input.value.trim() || (optional ? NO_REASON : '')));
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
            () => { openReject = null; (after || redraw)(); }, null, true);
    }

    // Remove on a queued reel (it is not posted) and on a shortlisted one
    function removeFromQueue(path) {
        return change(qq => { qq.items = qq.items.filter(x => x.path !== path); });
    }
    function removeFromShortlist(path) {
        return change(qq => { qq.shortlist = qq.shortlist.filter(x => x.path !== path); });
    }

    // What has been decided about a reel: {kind: 'rejected' | 'posted' | 'queue' | 'shortlist', at: the stored path,
    // reason?} or null. Matched by its path, else by its file name (a line pasted from another copy of the same file).
    function decisionOf(path) {
        if (!path) return null;
        const name = fileName(path);
        const same = i => i.path === path || (!!name && fileName(i.path) === name);
        const rj = rejects().find(same);
        if (rj) return { kind: 'rejected', at: rj.path, reason: rj.reason, when: rj.rejected };
        const q = queue();
        const it = q.items.find(same);
        if (it) return { kind: it.status === 'posted' ? 'posted' : 'queue', at: it.path, when: it.when, added: it.added, status: it.status,
            held: !!it.hold && it.status !== 'posted' };
        const sl = q.shortlist.find(same);
        if (sl) return { kind: 'shortlist', at: sl.path, added: sl.added };
        return null;
    }

    // held → released: these go to the bot at the next check (the vlog reels of a day, once the long video is out)
    function release(paths) {
        const take = new Set(paths);
        return change(q => { q.items.forEach(i => { if (take.has(i.path) && i.hold) i.hold = false; }); });
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
        try { return d.toLocaleString('en-GB', opt) + ' UK'; } catch (e) { return iso; }
    }

    // refresh on demand (video-editor docs/POST_QUEUE.md "Refresh on demand"): the Mac checks each queued reel against
    // the reel audit first. i.refresh = refreshed (re-rendered with the current code, then queued) | waiting (to be
    // re-rendered at a later sync) | rebuild | retire | by hand | failed (left out; i.why says why, in plain words)
    function upFirst(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

    function statusText(i) {
        if (i.hold && i.status !== 'posted' && i.status !== 'invalid') return ['Approved, held: it posts only once you release it', 'wait'];
        const fresh = i.refresh === 'refreshed' ? ' · re-rendered with the current code' +
            (i.refreshed ? ' on ' + fmtDay(i.refreshed) : '') : '';
        if (i.status === 'invalid' && ['rebuild', 'retire', 'by hand', 'failed'].includes(i.refresh)) {
            return [upFirst(i.why || 'left out by the reel audit'), i.refresh === 'retire' ? 'warn' : 'bad'];
        }
        if (i.status === 'pending' && i.refresh === 'waiting') {
            return [upFirst(i.why || 'waiting to be refreshed with the current code'), 'wait'];
        }
        const warn = i.flags && i.flags.length ? ' · ⚠️ ' + i.flags.join('; ') + ' (shown in Telegram: skip it there)' : '';
        switch (i.status) {
            case 'valid': return ['Ready: the bot will fit it in' + fresh + warn, 'ok'];
            case 'scheduled': return [(i.approved ? 'Approved for ' : 'Planned for ') + fmtWhen(i.when) + fresh + warn, 'ok'];
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
        const n = q.items.filter(i => WAITING.has(i.status || 'pending') && !i.hold).length;
        // two ways to set the pace (owner 2026-11-15): over how many days, or how many a day (the bot then posts
        // exactly that many and the days are however long that takes)
        let mode = q.per_day ? 'per_day' : 'days';
        let span = q.span_days || SPAN_DEFAULT;
        let pd = q.per_day || Math.max(1, perDay(n, span));
        const modes = el('div', 'reel-chips pq-modes');
        const label = el('label', 'pq-slider-label');
        const what = el('span', '');
        const input = el('input');
        input.type = 'range';
        input.min = '1';
        const read = el('div', 'pq-readout');
        const say = () => {
            const reels = `${n} reel${n === 1 ? '' : 's'} to post`;
            if (mode === 'per_day') {
                const days = n ? Math.ceil(n / pd) : 0;
                read.textContent = reels + ` · ${pd} a day` +
                    // the days only: the bot's real end date (pairs wait, a place never twice in a row) is in the
                    // proposed schedule below (owner 2026-11-15: the numbers have to tell one story)
                    (n ? ` · at least ${days} day${days === 1 ? '' : 's'} (the dates: Proposed schedule)` : '');
                return;
            }
            const p = perDay(n, span);
            read.textContent = reels + ` · over ${span} day${span === 1 ? '' : 's'}` +
                (n ? ` · about ${p} a day` : '') +
                (n && Math.ceil(n / span) > MAX_PER_DAY ? ` (at most ${MAX_PER_DAY} a day: it takes longer)` : '');
        };
        const setUp = () => {
            what.textContent = mode === 'per_day' ? 'Posts a day' : 'Post them over';
            input.max = String(mode === 'per_day' ? MAX_PER_DAY : SPAN_MAX);
            input.value = String(mode === 'per_day' ? pd : span);
            modes.replaceChildren();
            [['days', 'Over days'], ['per_day', 'Per day']].forEach(([k, txt]) => {
                const c = el('button', 'reel-chip' + (k === mode ? ' active' : ''), txt);
                c.type = 'button';
                c.addEventListener('click', () => {
                    if (k === mode) return;
                    mode = k;
                    setUp();
                    save();
                });
                modes.appendChild(c);
            });
            say();
        };
        let t = null;
        const save = () => {
            clearTimeout(t);
            t = setTimeout(() => change(qq => {
                if (mode === 'per_day') qq.per_day = pd;
                else { qq.per_day = 0; qq.span_days = span; }      // 0 = off (a removed key may not merge)
                qq.span_set = new Date().toISOString();
            }), 600);
        };
        input.addEventListener('input', () => {
            const v = parseInt(input.value, 10);
            if (mode === 'per_day') pd = v; else span = v;
            say();
            save();
        });
        setUp();
        label.append(what, input);
        wrap.appendChild(modes);
        wrap.append(label, read);
        const s = q.schedule || {};
        const planned = (s.rows || []).filter(r => r.time && ['planned', 'approved'].includes(r.status));
        if (s.per_day && planned.length) {
            // one story with the counts above (owner 2026-11-15: the numbers didn't add up): what the bot holds now
            const last = planned.map(r => r.day || r.time.slice(0, 10)).sort().pop();
            wrap.appendChild(el('div', 'pq-bot', `The bot's plan (as of the last Send to NAS): ${planned.length} planned, ` +
                `${s.per_day} a day${s.per_day_fixed ? ' (as you set)' : ''}, the last on ` + fmtDay(last + 'T12:00:00') +
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
                rm.addEventListener('click', () => removeFromQueue(i.path));
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
            rm.addEventListener('click', () => removeFromShortlist(i.path));
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
            const lines = splitPaste(ta.value).map(cleanLine).filter(Boolean);
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

    function scheduleBlock(q, label) {
        const rows = ((q.schedule || {}).rows || []).filter(r => r.time);
        // folded by default (owner 2026-11-15: "make the schedule collapsible otherwise it takes up huge amount of space")
        const box = el('details', 'pq-sched');
        let open = false;
        try { open = localStorage.getItem('reels_sched_table') === '1'; } catch (e) { /* private window */ }
        box.open = open;
        box.addEventListener('toggle', () => { try { localStorage.setItem('reels_sched_table', box.open ? '1' : '0'); } catch (e) { /* none */ } });
        const live = rows.filter(r => ['planned', 'approved'].includes(r.status)).length;
        box.appendChild(el('summary', 'pq-h', `${label || 'Schedule'} (${live} planned` +
            (rows.length > live ? `, ${rows.length - live} done or moved` : '') + ')'));
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
            limit: 'Instagram limit', private: 'not public', nomaster: 'not posted: no full-quality file' };
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
        const out = el('div');
        if (part === 'rejects') out.appendChild(rejectsSection());
        else out.append(...queueSections());
        return out;
    }

    // the Queue section: the post queue (paste, slider, lines, the bot's schedule) and the shortlist
    function queueSections() {
        const q = queue();
        const sec = el('section', 'pq');
        sec.appendChild(el('div', 'pq-h', 'Post queue: post these now'));
        sec.appendChild(el('p', 'reels-how',
            'Paste the file path of each reel that can go out now, one per line (in Finder: select the file, hold Option, ' +
            'right-click, Copy … as Pathname). Several at once is fine. The bot spreads them over the time you set ' +
            'and each morning at 07:00 sends that day\'s reels to Telegram, one message each: Approve, Skip or change the ' +
            'caption, then Confirm once. Send to NAS (Reviewed ▸ Posting schedule) hands the queue to the bot.'));
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
        return [sec, sl];
    }

    // the Rejected box (the Review section, under To review)
    function rejectsSection() {
        const rs = el('section', 'pq pq-rejects');
        rs.appendChild(el('div', 'pq-h', `Rejected: not for posting (${rejects().length})`));
        rs.appendChild(el('p', 'reels-how',
            'Reels from Short Form Content that aren\'t good enough, each with why. A rejected reel leaves the queue and ' +
            'the shortlist, can\'t be added back until Undo, and the bot never offers it. The Mac notes every reason for ' +
            'the reel builders (Short Form Content/REJECTS.md), so the next batch learns from it. Use "Reject…" on a ' +
            'reel under To review, in the Queue or on the Profile list, or paste paths here.'));
        rejectPasteBar(rs);
        rs.appendChild(rejectsBlock());
        return rs;
    }

    // the section counts and To review follow every redraw, whether or not a panel is showing
    function tellChanged() {
        try { document.dispatchEvent(new CustomEvent('reels:changed', { detail: 'queue' })); } catch (e) { /* no DOM */ }
    }

    function redraw() {
        if (!panelEl || !panelEl.isConnected) {
            clearInterval(timer);
            timer = null;
            tellRejects();
            tellChanged();
            return;
        }
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
        tellChanged();
    }

    // The panel for a Reels tab section ('queue': the queue and the shortlist; 'rejects': the Rejected box): drawn
    // from the last copy at once, refreshed from the server, then every 30 s (the Mac's checks and the bot's schedule
    // arrive on their own). One shows at a time.
    function panel(which) {
        part = which === 'rejects' ? 'rejects' : 'queue';
        panelEl = el('div', 'pq-wrap');
        panelEl.append(...(doc ? draw().childNodes : [el('p', 'reels-how pq',
            part === 'rejects' ? 'Loading the reject list…' : 'Loading the post queue…')]));
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

    // The document once (for the parts that need it with no panel showing: the counts, To review, the Profile rows'
    // rejects); a failed read is retried at the next call.
    function ready() {
        if (doc) return Promise.resolve(doc);
        if (!readyP) {
            readyP = fetchDoc().then(d => { tellRejects(); tellChanged(); return d; },
                () => { readyP = null; return null; });
        }
        return readyP;
    }

    // For Reels ▸ Reviewed (owner 2026-11-15: the Queue page is superseded by Reviewed; "anything clicked post enters
    // the queue and the schedule is derived based off of those"): a queued reel's line ([text, class]), the spread
    // slider + the bot's schedule as one box, and a fresh read of the document.
    function statusOf(path) {
        const name = fileName(path);
        const it = queue().items.find(i => i.path === path || (!!name && fileName(i.path) === name));
        return it ? statusText(it) : null;
    }
    // ── the proposed schedule and Send to NAS (owner 2026-11-15) ──
    // "a button that has the proposed schedule and based on my schedule settings and based on what's in the queue. and
    // then have a button that i click that's like send to nas. and then it sends the whole plan to the nas where then
    // the telegram bot takes over." The proposal is the bot's own planner (video-editor tools/reelbot/propose.py, through
    // the local helper) on the queue as it is now; Send to NAS runs the Mac's queue_sync.py, which waits for the bot's
    // plan from the new list, so the schedule table below is the bot's real one afterwards.
    const api = () => (window.ReelsSite && ReelsSite.api) || 'http://127.0.0.1:8791';
    let proposal = { sig: null, data: null, busy: false, err: null, timer: null };
    let send = { run: null, poll: null, err: null };

    // the page's lines as one story (owner 2026-11-15: "205 marked Post · 152 going to the bot · 53 not sent")
    function story(q) {
        const its = q.items;
        const c = { posted: 0, held: 0, refused: 0, unchecked: 0, bot: 0, flagged: 0 };
        its.forEach(i => {
            if (i.status === 'posted') c.posted++;
            else if (i.status === 'invalid' || i.status === 'skipped') c.refused++;
            else if (i.hold) c.held++;
            else if (i.status === 'valid' || i.status === 'scheduled') { c.bot++; if (i.flags && i.flags.length) c.flagged++; }
            else c.unchecked++;
        });
        const not = [c.held && `${c.held} held (vlog reels until you release them)`,
            c.refused && `${c.refused} refused (see their lines)`,
            c.unchecked && `${c.unchecked} not checked yet (Send to NAS checks them)`,
            c.posted && `${c.posted} already posted`].filter(Boolean);
        return `${its.length} marked Post · ${c.bot} going to the bot` +
            (c.flagged ? ` (${c.flagged} with warnings you'll see in Telegram)` : '') +
            (not.length ? ` · ${its.length - c.bot} not sent: ` + not.join(', ') : '');
    }

    function propSig(q) {
        return JSON.stringify([q.items.map(i => [i.path, i.status, !!i.hold, i.key || '', i.place || '']), q.span_days,
            q.per_day || 0, q.span_set || '']);
    }

    function loadProposal(q) {
        const sig = propSig(q);
        if (proposal.sig === sig || proposal.busy) return;
        clearTimeout(proposal.timer);
        proposal.timer = setTimeout(() => {
            proposal.busy = true;
            proposal.sig = sig;
            fetch(api() + '/reels/propose', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reel_queue: { items: q.items, span_days: q.span_days, per_day: q.per_day || 0,
                    span_set: q.span_set } }) })
                .then(r => r.json().then(j => (r.ok ? j : Promise.reject(new Error(j.error || r.status)))))
                .then(d => { proposal.data = d; proposal.err = null; },
                    e => { proposal.err = 'Couldn\'t make the proposed schedule: ' + e.message +
                        ' (is the local helper running? ./serve.sh starts it)'; proposal.sig = null; })
                .then(() => { proposal.busy = false; redraw(); tellChanged(); });
        }, 400);
    }

    function hhmm(iso) {
        return new Date(iso).toLocaleTimeString('en-GB', { timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', hour12: false });
    }
    function dayName(iso) {
        return new Date(iso).toLocaleDateString('en-GB', { timeZone: 'Europe/London', weekday: 'short', day: 'numeric', month: 'short' });
    }

    function proposedBlock(q) {
        const box = el('details', 'pq-sched pq-proposed');
        let open = true;
        try { open = localStorage.getItem('reels_proposed') === '1'; /* folded unless he opened it */ } catch (e) { /* none */ }
        box.open = open;
        box.addEventListener('toggle', () => { try { localStorage.setItem('reels_proposed', box.open ? '1' : '0'); } catch (e) { /* none */ } });
        const d = proposal.data;
        box.appendChild(el('summary', 'pq-h', 'Proposed schedule' + (d ? ` (${d.count} reel${d.count === 1 ? '' : 's'})` : '')));
        loadProposal(q);
        if (proposal.err) { box.appendChild(el('p', 'pq-status bad', proposal.err)); return box; }
        if (!d) { box.appendChild(el('p', 'reels-how', 'Working it out…')); return box; }
        if (!d.count) { box.appendChild(el('p', 'reels-how', 'Nothing to schedule: no reel in the queue goes to the bot.')); return box; }
        const times = (d.slots || []).slice(0, Math.max(1, d.per_day || 1)).slice().sort().join(', ');
        box.appendChild(el('p', 'pq-bot',
            `What the bot will do with these settings: ${d.count} reel${d.count === 1 ? '' : 's'}, ${d.per_day} a day` +
            `${d.per_day_fixed ? ' (as you set)' : ''}, from ${dayName(d.first_day + 'T12:00:00Z')} to ` +
            `${dayName(d.last_day + 'T12:00:00Z')}. Each morning at ${d.queue_time} (UK) it sends that day's reels to ` +
            `Telegram; they post at ${times} UK.` + (d.unchecked ? ` ${d.unchecked} not checked by the Mac yet: Send to NAS ` +
            'checks them and their places, so the order can still move a little.' : '') +
            (proposal.busy ? ' Updating…' : '')));
        const tbl = el('table', 'pq-table');
        const head = el('tr');
        ['Day', 'UK time', 'Reel', ''].forEach(h => head.appendChild(el('th', '', h)));
        tbl.appendChild(head);
        const word = { proposed: '', asked: 'sent to Telegram', approved: 'approved', missed: 'unanswered: moves to a later day' };
        d.rows.forEach(r => {
            const tr = el('tr', 'pq-' + (r.status === 'approved' ? 'approved' : 'planned'));
            [dayName(r.time), hhmm(r.time), r.title + (r.flags ? ' ⚠️' : ''), word[r.status] || ''].forEach(v => tr.appendChild(el('td', '', v)));
            tbl.appendChild(tr);
        });
        box.appendChild(tbl);
        return box;
    }

    function pollSend() {
        clearTimeout(send.poll);
        fetch(api() + '/reels/send-to-nas', { cache: 'no-store' }).then(r => r.json()).then(r => {
            send.run = r.state === 'none' ? null : r;
            send.err = null;
            if (r.state === 'running') send.poll = setTimeout(pollSend, 3000);
            else if (r.state === 'done') refresh().then(redraw);
            redraw();
        }, () => { send.err = 'The local helper didn\'t answer (./serve.sh starts it).'; redraw(); });
    }

    function sendResult(res) {
        const out = [];
        const n = res.lines, nb = res.to_bot;
        const not = [res.held && `${res.held} held (vlog reels until you release them)`,
            res.posted && `${res.posted} already posted`,
            res.refused.length && `${res.refused.length} refused: ` + Object.entries(res.reasons || {}).map(([k, v]) => `${k} (${v})`).join('; '),
            res.waiting.length && `${res.waiting.length} couldn't be copied yet (tried again next time)`].filter(Boolean);
        out.push(`Sent. ${n} marked Post · ${nb} going to the bot` + (res.flagged ? ` (${res.flagged} with warnings you'll see in Telegram)` : '') +
            (n - nb ? ` · ${n - nb} not sent: ` + not.join(', ') : '') + '.');
        const s = res.schedule || {};
        if (res.bot_fresh === false) {
            out.push('The bot hasn\'t made its plan from the new list yet (it may be restarting): the schedule below is its last one. Press Send to NAS again in a few minutes.');
        } else if (s.planned) {
            out.push(`The bot's plan: ${s.planned} planned, ${s.per_day} a day, the last on ${dayName(s.last_day + 'T12:00:00Z')}.` +
                (s.posting === false ? ' Posting is off on the bot: you switch it on with /posting on in Telegram.' : ''));
        }
        return out;
    }

    function sendBlock() {
        const box = el('div', 'pq-send');
        const b = el('button', 'reels-export-btn', 'Send to NAS');
        b.type = 'button';
        b.title = 'Check every reel in the queue on this Mac, copy them to the bot on the NAS and get the bot\'s real schedule back. ' +
            'From tomorrow 07:00 the Telegram bot sends each day\'s reels for your Approve / Skip.';
        const running = send.run && send.run.state === 'running';
        b.disabled = !!running;
        b.addEventListener('click', () => {
            b.disabled = true;
            fetch(api() + '/reels/send-to-nas', { method: 'POST' }).then(r => r.json().then(j => {
                if (!r.ok && r.status !== 409) throw new Error(j.error || r.status);
                send.run = j;
            })).then(() => { send.err = null; pollSend(); },
                e => { send.err = 'Couldn\'t start: ' + e.message; redraw(); });
        });
        box.appendChild(b);
        const r = send.run;
        if (send.err) box.appendChild(el('div', 'pq-status bad', send.err));
        else if (running) {
            const last = (r.log || []).filter(x => x.trim()).slice(-1)[0] || '';
            box.appendChild(el('div', 'pq-status wait', 'Sending… the Mac checks each reel, copies it to the NAS, then waits for the bot\'s plan (up to 4 minutes).'));
            if (last) box.appendChild(el('div', 'pq-path', last.replace(/^\S+ \S+ /, '')));
        } else if (r && r.state === 'done' && r.result) {
            sendResult(r.result).forEach(t => box.appendChild(el('div', 'pq-status ok', t)));
            if (r.result.refused.length) {
                const det = el('details', 'pq-sched');
                det.appendChild(el('summary', 'pq-h', `Refused (${r.result.refused.length})`));
                const ul = el('ul', 'pq-added');
                r.result.refused.forEach(x => ul.appendChild(el('li', 'bad', '✗ ' + fileName(x.path) + ': ' + x.why)));
                det.appendChild(ul);
                box.appendChild(det);
            }
            box.appendChild(el('div', 'reels-how', 'Finished ' + new Date(r.finished).toLocaleString('en-GB', { hour12: false })));
        } else if (r && r.state === 'failed') {
            box.appendChild(el('div', 'pq-status bad', 'Send to NAS failed: ' + (r.error || 'see the log')));
        }
        return box;
    }

    function scheduleBox() {
        const q = queue();
        const box = el('div', 'pq-schedbox');
        if (send.run === null && !send.poll && !send.err) { send.poll = -1; pollSend(); }   // a run left going: show it
        box.appendChild(el('div', 'pq-readout', story(q)));
        box.appendChild(sliderBlock(q));
        box.appendChild(proposedBlock(q));
        box.appendChild(sendBlock());
        box.appendChild(scheduleBlock(q, 'The bot\'s schedule'));
        box.appendChild(el('p', 'reels-how', q.synced
            ? 'Last checked by the Mac: ' + new Date(q.synced).toLocaleString('en-GB', { hour12: false })
            : 'Not checked by the Mac yet (video-editor: tools/reelbot/queue_sync.py).'));
        return box;
    }
    function refresh() {
        return fetchDoc().then(() => { tellRejects(); tellChanged(); }, () => {});
    }

    // the Queue section's count: reels in the post queue still to go out, and the shortlist
    function counts() {
        const q = queue();
        return { loaded: !!doc, queue: q.items.filter(i => i.status !== 'posted').length, shortlist: q.shortlist.length,
            rejected: rejects().length };
    }

    return { panel, ready, counts, rejectOf, reject, undoReject, rejectControl, onRejects, addPaths, release, removeFromQueue,
        removeFromShortlist, decisionOf, fileName, statusOf, scheduleBox, refresh,
        // the bot's schedule rows ({key, title, day, time, status}) for Profile's schedule preview, or null before the load
        scheduleRows: () => (doc ? ((queue().schedule || {}).rows || []) : null) };
})();
