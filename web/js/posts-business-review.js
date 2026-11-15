/*
 * Posts ▸ Business ▸ Review and Reviewed (localhost only, owner 2026-11-15: "same as in reels i have these two tabs …
 * load the existing content with the same feedback tools so i can review it and provide feedback to set up my queue
 * and schedule of marketing content"). The controls are the Reels review's own (review-kit.js), so the two pages
 * judge the same way:
 *
 *   Post (P), ★1–★5 (keys 1–5)  good to go as it is: into the schedule (a post already in it keeps its place, a new one
 *                               goes to the end), with how good it is
 *   Post + note… (F)            the same, with stars (optional) and a note (required): taste for the next posts
 *   Shortlist… (S)              good, but something needs fixing: the note (required) says what; off the schedule
 *                               until it is fixed. Its maker sets the review's status to "handled" once it is, and the
 *                               post comes back here.
 *   Reject… (R)                 a hard no, the reason optional; off the schedule
 *
 * The verdicts live in the business state's "reviews" ({id: {verdict, stars?, note?, time, queued?, status?}}); like
 * every change on the Business tab they wait in the Mac's draft until Send to NAS (posts-business.js). Reviewed lists
 * every judged post with Undo (back to Review) and "Feedback to agent", the text to paste to the agent that makes the
 * posts.
 */
window.PostsBizReview = (function () {
    'use strict';

    const K = window.ReviewKit;
    const el = (t, c, x) => K.el(t, c, x);
    const REASONS = ['Not good enough', 'Text hard to read', 'Too long / too slow', 'Off-brand', 'Claim not on the site',
        'Wrong photos'];
    let core = null;
    let rows = [];
    let focusAt = -1;
    let rootEl = null;
    let listening = false;
    let headEl = null;

    const reviews = c => (c.state().reviews || {});
    const posted = c => (c.state().posted || {});
    const nowIso = () => new Date().toISOString();
    // a shortlisted post its maker fixed comes back to Review
    const isPending = (c, it) => !posted(c)[it.id] &&
        (!reviews(c)[it.id] || (reviews(c)[it.id].verdict === 'shortlist' && reviews(c)[it.id].status === 'handled'));

    // the plan's order first (content.json "plan": what its maker would post first), then the rest as content.json
    // lists them. Nothing is scheduled until he posts it here (owner 2026-11-15: the 13 planned "should move back to
    // review tab").
    function ordered(c) {
        const q = c.plan();
        const pos = id => { const i = q.indexOf(id); return i < 0 ? 1e6 : i; };
        return c.items().map((it, i) => [it, i]).sort((a, b) => pos(a[0].id) - pos(b[0].id) || a[1] - b[1]).map(x => x[0]);
    }
    function pending(c) { return ordered(c).filter(it => isPending(c, it)); }

    // what was decided, or null: {kind: post|shortlist|reject|posted, stars, note, time}
    function decision(c, it) {
        const p = posted(c)[it.id];
        const r = reviews(c)[it.id];
        if (p) return Object.assign({ kind: 'posted', when: p.date, via: p.via }, r ? { stars: r.stars, note: r.note, time: r.time } : {});
        if (!r || isPending(c, it)) return null;
        return { kind: r.verdict, stars: r.stars, note: r.note, time: r.time };
    }
    function reviewed(c) {
        return c.items().map(it => ({ it, d: decision(c, it) })).filter(x => x.d)
            .sort((a, b) => String(b.d.time || b.d.when || '').localeCompare(String(a.d.time || a.d.when || '')));
    }

    // ── the verdicts (each a change to the draft) ──
    function judge(it, verdict, extra) {
        return core.mutate(s => {
            const r = Object.assign({ verdict, time: nowIso() }, extra || {});
            if (verdict === 'post') {
                if (!s.queue.includes(it.id)) { s.queue.push(it.id); r.queued = true; }
            } else {
                s.queue = s.queue.filter(x => x !== it.id);
                if (verdict === 'shortlist') r.status = 'open';
            }
            s.reviews[it.id] = r;
        });
    }
    function undoJudge(it) {
        return core.mutate(s => {
            const r = s.reviews[it.id];
            if (r && r.verdict === 'post' && r.queued) s.queue = s.queue.filter(x => x !== it.id);
            delete s.reviews[it.id];
        });
    }
    function setStars(it, n) {
        return core.mutate(s => { if (s.reviews[it.id]) s.reviews[it.id].stars = n; });
    }

    // ── one post to judge ──
    function player(it) {
        const wrap = el('div', 'rvw-player biz-rvw-player' + (it.kind === 'reel' ? '' : ' feed'));
        const box = core.preview(it);
        const v = box.querySelector('video');
        if (v) {
            v.addEventListener('play', () => {
                document.querySelectorAll('.biz-rvw video').forEach(x => { if (x !== v && !x.paused) x.pause(); });
            });
        }
        wrap.appendChild(box);
        return { wrap, box, video: v };
    }

    function makeRow(it) {
        const row = { it, el: el('div', 'rvw-row'), state: null, busy: null };
        row.el.tabIndex = -1;
        row.el.addEventListener('click', () => { const i = rows.indexOf(row); if (i !== focusAt) setFocus(i, false); });
        const full = el('div', 'rvw-full');
        const pl = player(it);
        row.video = pl.video;
        full.appendChild(pl.wrap);
        const body = el('div', 'rvw-body');
        body.appendChild(el('div', 'rvw-title', core.label(it)));
        body.appendChild(el('div', 'reel-meta', [core.kindText(it), it.section, it.pin ? `pin ${it.pin} on the profile` : null]
            .filter(Boolean).join(' · ')));
        if (it.kind === 'carousel') body.appendChild(core.slides(it, pl.box));
        if (it.kind === 'reel' && it.sound) body.appendChild(el('div', 'biz-sound', '♪ ' + it.sound));
        const r = reviews(core)[it.id];
        if (r && r.status === 'handled') body.appendChild(el('div', 'pq-status ok', `Fixed after your note: ${r.note}`));
        body.appendChild(el('div', 'biz-caption-done', core.captionOf(it)));
        const acts = el('div', 'rvw-acts');
        const btn = (text, key, cls, title, fn) => {
            const b = el('button', 'reels-mini rvw-act ' + cls);
            b.type = 'button';
            b.title = title;
            b.append(document.createTextNode(text + ' '), el('kbd', '', key));
            b.addEventListener('click', e => { e.stopPropagation(); setFocus(rows.indexOf(row), false); fn(); });
            acts.appendChild(b);
        };
        btn('Post', 'P', 'rvw-post', 'Good to go as it is: into the schedule', () => verdict(row, 'post'));
        acts.appendChild(K.stars(n => verdict(row, 'post', { stars: n }), () => setFocus(rows.indexOf(row), false)));
        btn('Post + note…', 'F', 'rvw-post', 'Good to go as it is, with stars if you like and a note for the next posts',
            () => openNote(row, true));
        btn('Shortlist…', 'S', 'rvw-short', 'Good, but something needs fixing: say what (required); off the schedule until fixed',
            () => openNote(row, false));
        row.rejectSlot = el('div', 'rvw-reject');
        acts.appendChild(row.rejectSlot);
        row.reject = K.reasonBox(row.rejectSlot, {
            reasons: REASONS,
            before: () => setFocus(rows.indexOf(row), false),
            onReject: reason => verdict(row, 'reject', reason ? { note: reason } : {})
        });
        row.fbSlot = el('div', 'rvw-fbbox');
        acts.appendChild(row.fbSlot);
        body.appendChild(acts);
        row.note = el('div', 'pq-status wait rvw-busy');
        body.appendChild(row.note);
        full.appendChild(body);
        row.full = full;
        row.slim = el('div', 'rvw-slim');
        row.el.append(full, row.slim);
        return row;
    }

    function openNote(row, post) {
        if (row.state || row.busy) return;
        K.noteBox(row.fbSlot, {
            post,
            onCancel: () => row.el.focus(),
            onSave: (note, stars) => verdict(row, post ? 'post' : 'shortlist', Object.assign({ note }, stars ? { stars } : {}))
        });
    }

    function verdict(row, v, extra) {
        if (row.state || row.busy) return;
        row.busy = 'Saving…';
        paint(row);
        judge(row.it, v, extra).then(ok => {
            row.busy = null;
            row.missing = ok ? null : 'Not saved: the local helper did not answer (relaunch the desktop app).';
            const was = row.state;
            row.state = decision(core, row.it);
            paint(row);
            core.paintTop();
            paintHead();
            if (!was && row.state && rows.indexOf(row) === focusAt) step(1, true);
        });
    }

    function undo(row) {
        row.busy = 'Undoing…';
        paint(row);
        undoJudge(row.it).then(() => {
            row.busy = null;
            row.state = decision(core, row.it);
            paint(row);
            core.paintTop();
            paintHead();
        });
    }

    function paintHead() {
        if (headEl) headEl.textContent = `To review (${rows.filter(r => !r.state).length})`;
    }

    const SAID = { post: 'Post: in the schedule', shortlist: 'Shortlist', reject: 'Rejected', posted: 'Posted' };
    function verdictText(d) {
        const base = d.kind === 'reject' ? `Rejected${d.note ? ': ' + d.note : ''}`
            : d.kind === 'shortlist' ? `Shortlist, to fix: ${d.note || ''}`
                : d.kind === 'posted' ? `Posted ${d.when || ''}` : SAID[d.kind];
        const withNote = (d.kind === 'post' || d.kind === 'posted') && d.note ? `${base} · note: ${d.note}` : base;
        return d.stars ? `${withNote} ${K.starText(d.stars)}` : withNote;
    }
    const cls = d => (d.kind === 'reject' ? 'bad' : d.kind === 'shortlist' ? 'warn' : 'ok');

    function thumb(it) {
        if (!it.cover) return null;
        const img = el('img', 'rvw-thumb');
        img.src = core.fileURL(it.cover);
        img.alt = '';
        img.loading = 'lazy';
        img.addEventListener('error', () => img.remove());
        return img;
    }

    function paint(row) {
        const d = row.state;
        row.el.classList.toggle('decided', !!d);
        row.full.hidden = !!d;
        row.slim.hidden = !d;
        row.note.textContent = row.busy || row.missing || '';
        row.el.querySelectorAll('.rvw-act').forEach(b => { b.disabled = !!row.busy; });
        if (!d) return;
        if (row.video && !row.video.paused) row.video.pause();
        row.slim.replaceChildren();
        const t = thumb(row.it);
        if (t) row.slim.appendChild(t);
        const txt = el('div', 'rvw-slim-text');
        txt.appendChild(el('div', 'pq-title', core.label(row.it)));
        txt.appendChild(el('div', 'pq-status ' + cls(d), '✓ ' + verdictText(d) + (row.busy ? ' · ' + row.busy : '')));
        row.slim.appendChild(txt);
        if (d.kind !== 'posted') {
            const u = el('button', 'reels-mini', 'Undo');
            u.type = 'button';
            u.disabled = !!row.busy;
            u.title = 'Take the verdict back: back to judge';
            u.addEventListener('click', e => { e.stopPropagation(); undo(row); });
            row.slim.appendChild(u);
        }
    }

    function setFocus(i, scroll) {
        if (i < 0 || i >= rows.length) return;
        if (focusAt >= 0 && rows[focusAt]) rows[focusAt].el.classList.remove('focus');
        focusAt = i;
        rows[i].el.classList.add('focus');
        if (scroll) rows[i].el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    function step(dir, play) {
        let i = focusAt;
        for (;;) {
            i += dir;
            if (i < 0 || i >= rows.length) return false;
            if (!rows[i].state) break;
        }
        setFocus(i, true);
        if (play && rows[i].video) rows[i].video.play().catch(() => {});
        return true;
    }

    const onKey = K.keys(() => rootEl && rootEl.isConnected, () => rows[focusAt], row => !!row.state, {
        next: () => { if (focusAt < 0) setFocus(0, true); else step(1, false); },
        back: () => step(-1, false),
        play: row => { if (!row.video) return; if (row.video.paused) row.video.play().catch(() => {}); else row.video.pause(); },
        post: row => verdict(row, 'post'),
        stars: (row, n) => verdict(row, 'post', { stars: n }),
        postNote: row => openNote(row, true),
        shortlist: row => openNote(row, false),
        reject: row => row.reject.open()
    });

    function panel(c) {
        core = c;
        rootEl = el('section', 'pq rvw biz-rvw');
        const list = pending(c);
        headEl = el('div', 'pq-h', `To review (${list.length})`);
        rootEl.appendChild(headEl);
        rootEl.appendChild(el('p', 'reels-how',
            'The posts you haven\'t judged yet, in the planned order; nothing is scheduled until you post it. Post: good to go as it is, into the schedule; ' +
            'the stars (1–5) post it too and say how good it is. Shortlist…: good, but something needs fixing; it asks ' +
            'what, and it comes back here once fixed. Reject…: a hard no; saying why is optional. Then Send to NAS.'));
        rootEl.appendChild(K.keyHelp());
        const listEl = el('div', 'rvw-list');
        rootEl.appendChild(listEl);
        rows = [];
        focusAt = -1;
        list.forEach(it => {
            const row = makeRow(it);
            rows.push(row);
            listEl.appendChild(row.el);
            paint(row);
        });
        if (!list.length) listEl.appendChild(el('p', 'reels-empty', 'Everything is judged: see Reviewed and Schedule.'));
        if (rows.length) setFocus(0, false);
        if (!listening) {
            listening = true;
            document.addEventListener('keydown', onKey);
        }
        return rootEl;
    }

    // ── Reviewed ──
    const KIND_KEY = 'business_reviewed_kind';
    const KINDS = [['all', 'All'], ['post', 'Post'], ['shortlist', 'Shortlist'], ['reject', 'Rejected'], ['posted', 'Posted']];

    // the text to paste to the agent that makes the posts
    function agentText(c) {
        const all = reviewed(c);
        const fmt = t => (t ? String(t).slice(0, 10) : '');
        const line = (x, extra) => `- ${x.it.id} (${core.label(x.it)}): ${x.it.file || (x.it.files || [])[0] || ''}\n  ${extra}` +
            (fmt(x.d.time || x.d.when) ? ` (${fmt(x.d.time || x.d.when)})` : '');
        const short = all.filter(x => x.d.kind === 'shortlist');
        const rej = all.filter(x => x.d.kind === 'reject');
        const good = all.filter(x => x.d.kind === 'post' || x.d.kind === 'posted')
            .sort((a, b) => (b.d.stars || 0) - (a.d.stars || 0) || (b.d.note ? 1 : 0) - (a.d.note ? 1 : 0));
        const out = [
            '# Expedit marketing post feedback (Posts ▸ Business ▸ Reviewed, copied ' + new Date().toISOString().slice(0, 16).replace('T', ' ') + ')',
            '',
            `You make the marketing posts for the Expedit Instagram (@${c.account() || 'the brand account'}): the reels and ` +
            'carousels in ~/Documents/Projects/expedit, marketing/social (read its README.md and POSTING.md, and the ' +
            'social-content skill, first; work on a branch, merged by PR). Below is my feedback on posts I reviewed. Act on it:',
            '',
            '1. SHORTLISTED = good, but fix exactly what my note says and keep everything else. Re-render it, run ' +
            '`./post.py export` so the fixed files reach the content folder, then in that folder\'s state.json set ' +
            'reviews.<id>.status to "handled" (read it fresh, write it back whole with rev + 1) so it comes back to my Review list.',
            '2. REJECTED = a hard no. Never re-make or re-post them. Read the reasons as my taste: when one repeats, make it a ' +
            'rule in POSTING.md for new posts.',
            '3. POSTED = good as it is (stars say how good, 5 = best): never change those. Look at the 4–5 star ones and ' +
            'write down what they share, then make more like them. A note on a posted one is taste for the next posts.',
            '4. Every claim must match expedit.uk. Only public photos.',
            '5. Tell me in plain words, post by post, what you did, and anything you could not do.',
            ''
        ];
        out.push(`## Shortlisted: fix these (${short.length})`);
        short.forEach(x => out.push(line(x, 'Fix: ' + (x.d.note || '(no note given: ask me)'))));
        if (!short.length) out.push('(none)');
        out.push('', `## Rejected (${rej.length})`);
        rej.forEach(x => out.push(line(x, 'Why: ' + (x.d.note || 'No reason given'))));
        if (!rej.length) out.push('(none)');
        out.push('', `## Posted: the good ones, make more like these (${good.length}, best first)`);
        good.forEach(x => out.push(line(x, (x.d.stars ? `${x.d.stars}/5 stars` : 'No stars') + (x.d.note ? '. Note: ' + x.d.note : ''))));
        if (!good.length) out.push('(none)');
        return out.join('\n') + '\n';
    }

    function reviewedPanel(c) {
        core = c;
        rootEl = null;
        const root = el('section', 'pq rvw rvd biz-rvw');
        let kind = 'all';
        try { kind = JSON.parse(localStorage.getItem(KIND_KEY)) || 'all'; } catch (e) { /* private mode */ }
        const head = el('div', 'pq-h');
        const fb = el('button', 'reels-export-btn rvd-agent', 'Feedback to agent');
        fb.type = 'button';
        fb.title = 'Copy the shortlisted, rejected and posted posts (files, stars, your notes and reasons), with ' +
            'instructions, to paste to the agent that makes them';
        const said = el('span', 'pq-status ok rvd-agent-said');
        fb.addEventListener('click', () => {
            const text = agentText(c);
            const n = (text.match(/^- /gm) || []).length;
            K.copy(text, said, `Copied ${n} post${n === 1 ? '' : 's'} with instructions: paste it to your agent.`);
        });
        const bar = el('div', 'rvd-agentbar');
        bar.append(fb, said);
        const chips = el('div', 'reel-chips rvw-trips');
        const list = el('div', 'rvw-list');
        root.append(head, el('p', 'reels-how', 'Every post you have judged, last judged first. Change the stars of a ' +
            'Post here; Undo takes the verdict back and the post goes back to Review.'), bar, chips, list);

        const draw = () => {
            const all = reviewed(c);
            const stars = all.filter(x => x.d.stars);
            head.textContent = `Reviewed (${all.length})` + (stars.length
                ? ` · ${stars.length} rated, average ${(stars.reduce((a, x) => a + x.d.stars, 0) / stars.length).toFixed(1)} ★` : '');
            chips.replaceChildren();
            KINDS.forEach(([k, t]) => {
                const n = k === 'all' ? all.length : all.filter(x => x.d.kind === k).length;
                if (k !== 'all' && !n && k !== kind) return;
                const b = el('button', 'reel-chip' + (k === kind ? ' active' : ''), `${t} (${n})`);
                b.type = 'button';
                b.addEventListener('click', () => {
                    kind = k;
                    try { localStorage.setItem(KIND_KEY, JSON.stringify(k)); } catch (e) { /* private mode */ }
                    draw();
                });
                chips.appendChild(b);
            });
            const here = kind === 'all' ? all : all.filter(x => x.d.kind === kind);
            list.replaceChildren();
            if (!here.length) list.appendChild(el('p', 'reels-empty', all.length ? 'Nothing judged with this filter.' : 'Nothing reviewed yet.'));
            here.forEach(x => list.appendChild(rvRow(x, draw)));
        };
        draw();
        return root;
    }

    function rvRow(x, draw) {
        const { it, d } = x;
        const row = el('div', 'rvw-row rv-row');
        const top = el('div', 'rv-top');
        const t = thumb(it);
        if (t) { t.className = 'rv-thumb'; top.appendChild(t); }
        const txt = el('div', 'rvw-slim-text');
        txt.appendChild(el('div', 'pq-title', core.label(it)));
        txt.appendChild(el('div', 'reel-meta', [core.kindText(it), it.section,
            d.time ? 'Judged ' + new Date(d.time).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : null]
            .filter(Boolean).join(' · ')));
        txt.appendChild(el('div', 'pq-status ' + cls(d), verdictText(d)));
        const busy = el('div', 'pq-status wait rvw-busy');
        txt.appendChild(busy);
        top.appendChild(txt);
        const acts = el('div', 'rv-acts');
        if (d.kind === 'post') {
            acts.appendChild(K.stars(n => {
                busy.textContent = 'Saving the stars…';
                setStars(it, n).then(ok => {
                    busy.textContent = ok ? '' : 'Not saved: the local helper did not answer.';
                    core.paintTop();
                    if (ok) draw();
                });
            }, null, d.stars));
        }
        const play = el('button', 'reels-mini', 'Play');
        play.type = 'button';
        let open = null;
        play.addEventListener('click', () => {
            if (open) { open.remove(); open = null; play.textContent = 'Play'; return; }
            const pl = player(it);
            open = el('div', 'rv-player');
            open.appendChild(pl.wrap);
            if (it.kind === 'carousel') open.appendChild(core.slides(it, pl.box));
            row.appendChild(open);
            play.textContent = 'Close';
            if (pl.video) pl.video.play().catch(() => {});
        });
        acts.appendChild(play);
        if (d.kind !== 'posted') {
            const u = el('button', 'reels-mini', 'Undo');
            u.type = 'button';
            u.title = 'Take the verdict back: the post goes back to Review';
            u.addEventListener('click', () => {
                u.disabled = true;
                busy.textContent = 'Undoing…';
                undoJudge(it).then(() => { core.paintTop(); draw(); });
            });
            acts.appendChild(u);
        }
        top.appendChild(acts);
        row.appendChild(top);
        return row;
    }

    return { panel, reviewedPanel, pending, reviewed, agentText };
})();
