/*
 * Posts ▸ Reels ▸ Review ▸ To review (localhost only, owner 2026-11-14: "one to control the approvals and the rejects
 * and the feedback"). The reels he has not judged yet, one under the other, to go through here instead of typing
 * verdicts into a chat session.
 *
 * Which reels: every reel in web/phone/reels/reels.json (the video project's reel_site.py) with no decision yet: not
 * in the post queue, not on the shortlist, not on the reject list (posts-queue.js decisionOf: by its file path, else
 * its file name) and not ticked Posted on the Profile list. Newest first (the day it was delivered), with a trip
 * filter (remembered in this browser).
 *
 * The three verdicts are the post queue's own, so there is one state ("reel_queue" / "reel_rejects" in the Posts
 * document, posts-queue.js): Post = into the post queue (as if its path were pasted there), Shortlist = onto the
 * shortlist, Reject… = the reject list's reason picker (quick reasons and his own words). A judged reel folds into one
 * line with what was decided and Undo (Remove from the queue / the shortlist, or the reject list's Undo), and the next
 * reel comes up. Keys, while this section shows and no text box has the cursor: J or ↓ the next reel, K or ↑ the one
 * before, Space play / pause, P Post, S Shortlist, R Reject… (Enter in the reason box rejects, Esc cancels).
 *
 * What the three mean (owner 2026-11-14: "reject button is hard reject with optional feedback. shortlist button is
 * like its good but needs smth fixed and then requires feedback. post is good to go as is"):
 *   Post       good to go as it is.
 *   Shortlist  good, but something needs fixing: S opens a text box and the note is REQUIRED (Enter saves, Shift+Enter
 *              a new line, Esc cancels). The reel goes onto the shortlist and the note is kept on this Mac by the
 *              local helper (POST /reels/feedback → web/phone/reels/reel_feedback.json) for the reel builders to act
 *              on; the folded line shows it. Undo takes the reel off the shortlist and withdraws the note.
 *   Reject…    a hard reject; the reason is optional ("No reason given" when nothing is written).
 *   Post + note  F (owner 2026-11-14: "if I type F it also counts as post and also lets me provide a star rating
 *              but also lets me provide new feedback even if it's counted as post"): a box with stars (optional) and
 *              a note (required). Saved = the note (kind "post": feedback for the next reels, the reel itself stays
 *              as it is), the stars, then into the post queue. Undo takes all three back.
 *   ★1–★5      (owner 2026-11-14: "as well as p for post i want to be able to give a 1-5 star rating. those count as
 *              'post'"): Post with how good it is. Keys 1–5. The stars are kept by the local helper (POST /reels/rating
 *              → web/phone/reels/reel_ratings.json) for the reel builders to learn from; Undo withdraws them too.
 *
 * Reviewed also replaces the old Queue page (owner 2026-11-15): Post puts a reel in the post queue, the bot's schedule is
 * made from those, and this page shows it (the spread slider, the schedule table, each posted reel's planned time).
 * "Feedback to agent" copies the shortlisted and rejected reels (paths, notes, reasons) and the notes on posted reels,
 * with a prompt and instructions, for him to paste to an AI agent.
 *
 * Posts ▸ Reels ▸ Reviewed (reviewedPanel, owner 2026-11-14: "another tab for reviewed which tracks everything that
 * i've already reviewed and given feedback"): every reel judged, last judged first, with its verdict, stars, note or
 * reason; filter by verdict and trip; play it; change the stars of a posted one; Undo sends it back to Review.
 *
 * Hundreds of reels: a page of 12 at a time ("Show 12 more", or moving past the last one), each video
 * preload="none" with its cover as the poster, so nothing loads until it is played. Videos stream from the local
 * helper like the reel cards (reels-site.js mediaUrl), falling back to the dev server's copy.
 */
window.PostsReview = (function () {
    'use strict';

    const PAGE = 12;
    const TRIP_KEY = 'reels_review_trip';
    let reelsDoc = null;
    let trip = lsGet(TRIP_KEY, 'all');
    let list = [];                   // the reels still to judge when the list was drawn (for this filter), in order
    let rows = [];                   // the rows drawn: {r, el, full, slim, video, state, busy}
    let drawn = 0;                   // how many of list have been offered as rows
    let focusAt = -1;
    let rootEl = null;
    let listEl = null;
    let headEl = null;
    let chipsEl = null;
    let moreEl = null;
    let listening = false;
    let notes = {};                  // reel id → its open feedback note (reel_feedback.json's item)
    let notesP = null;
    let ratings = {};                // reel id → {stars, time} (reel_ratings.json's item)
    let ratingsP = null;

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }
    function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
    function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } }

    // ── which reels ──
    // the trip as he named its delivery folder (…/Short Form Content/<trip>/…), else the manifest's trip key
    function tripName(r) {
        const m = /\/Short Form Content\/([^/]+)\//.exec(r.path || '');
        return m ? m[1] : (r.trip || 'Other');
    }

    // ── feedback notes (the local helper keeps them) ──
    const api = () => (window.ReelsSite && ReelsSite.api) || '';
    function takeNotes(doc) {
        notes = {};
        ((doc && doc.items) || []).forEach(it => { if (it && it.status === 'open' && it.id) notes[it.id] = it; });
    }
    function loadNotes() {
        if (!notesP) {
            notesP = fetch(api() + '/reels/feedback', { cache: 'no-store' })
                .then(r => (r.ok ? r.json() : null)).then(takeNotes).catch(() => {});
        }
        return notesP;
    }
    // {id, note} saves the reel's note, {id, undo: true} withdraws it → true when the helper took it
    function sendNote(body) {
        return fetch(api() + '/reels/feedback', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        }).then(r => (r.ok ? r.json() : null)).then(doc => { if (doc) takeNotes(doc); return !!doc; }).catch(() => false);
    }

    // ── stars (the local helper keeps them too) ──
    function takeRatings(doc) {
        ratings = {};
        const items = (doc && doc.items) || {};
        Object.keys(items).forEach(id => {
            const it = items[id];
            if (it && it.stars >= 1 && it.stars <= 5) ratings[id] = it;
        });
    }
    function loadRatings() {
        if (!ratingsP) {
            ratingsP = fetch(api() + '/reels/ratings', { cache: 'no-store' })
                .then(r => (r.ok ? r.json() : null)).then(takeRatings).catch(() => {});
        }
        return ratingsP;
    }
    // {id, stars} rates the reel, {id, undo: true} withdraws it → true when the helper took it
    function sendRating(body) {
        return fetch(api() + '/reels/rating', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        }).then(r => (r.ok ? r.json() : null)).then(doc => { if (doc) takeRatings(doc); return !!doc; }).catch(() => false);
    }
    const starText = n => '★'.repeat(n) + '☆'.repeat(5 - n);

    // what has been decided about a reel, or null
    function decision(r) {
        const d0 = window.PostsQueue ? PostsQueue.decisionOf(r.path) : null;
        const d = d0 && (d0.kind === 'queue' || d0.kind === 'posted') && ratings[r.id]
            ? Object.assign({}, d0, { stars: ratings[r.id].stars, rated: ratings[r.id].time }) : d0;
        if (d) return ((d.kind === 'shortlist' || d.kind === 'queue' || d.kind === 'posted') && notes[r.id])
            ? Object.assign({}, d, { note: notes[r.id].note, noted: notes[r.id].time }) : d;
        if (notes[r.id] && notes[r.id].kind !== 'post') return { kind: 'shortlist', at: r.path, note: notes[r.id].note, noteOnly: true };
        const st = window.ReelsSite && ReelsSite.state();
        if (st && st.posted && st.posted[r.id]) return { kind: 'posted', profile: true, when: st.posted[r.id] };
        return null;
    }

    // newest first: the day delivered, then trip, day and name so a batch stays together
    function newestFirst(a, b) {
        return String(b.delivered || '').localeCompare(String(a.delivered || '')) ||
            tripName(a).localeCompare(tripName(b)) || (a.day || 0) - (b.day || 0) || String(a.id).localeCompare(String(b.id));
    }

    function pending(doc, decide) {
        decide = decide || decision;
        return ((doc && doc.reels) || []).filter(r => r.path && !decide(r)).sort(newestFirst);
    }

    // the section button's number: reels still to judge (all trips), or null before the reject list is loaded
    function count() {
        if (!reelsDoc || !window.PostsQueue || !PostsQueue.counts().loaded) return null;
        return pending(reelsDoc).length;
    }

    // ── one reel ──
    const SAID = {
        queue: 'In the post queue', shortlist: 'On the shortlist', rejected: 'Rejected', posted: 'Posted'
    };

    // onMissing(): neither the helper nor the dev server has the file (moved or deleted since reels.json was made)
    function video(r, onMissing) {
        const v = el('video', 'rvw-video');
        v.preload = 'none';
        v.controls = true;
        v.playsInline = true;
        if (r.cover) v.poster = r.cover;
        const own = r.src;
        const helper = window.ReelsSite ? ReelsSite.mediaUrl(own) : own;
        v.src = helper;
        let tries = helper !== own ? 1 : 0;
        v.addEventListener('error', () => {
            if (tries-- > 0) {
                // the helper answered the state call but not this file: play the dev server's copy instead
                v.src = own;
                v.play().catch(() => {});
            } else if (onMissing) {
                onMissing();
            }
        });
        // one at a time
        v.addEventListener('play', () => {
            if (window.ReelsSite) ReelsSite.stopPlaying();
            rows.forEach(x => { if (x.video && x.video !== v && !x.video.paused) x.video.pause(); });
        });
        return v;
    }

    function makeRow(r) {
        const row = { r, el: el('div', 'rvw-row'), state: null, busy: null };
        row.el.tabIndex = -1;
        row.el.addEventListener('click', () => {
            const i = rows.indexOf(row);
            if (i !== focusAt) setFocus(i, false);
        });
        // the full view: the player and the verdicts beside it
        const full = el('div', 'rvw-full');
        const wide = r.aspect && r.aspect !== '9:16';
        const box = el('div', 'rvw-player' + (wide ? ' wide' : ''));
        row.video = video(r, () => {
            row.missing = 'The video file isn\'t there any more (moved or deleted since the reel list was made).';
            paint(row);
        });
        box.appendChild(row.video);
        full.appendChild(box);
        const body = el('div', 'rvw-body');
        body.appendChild(el('div', 'rvw-title', r.title || r.id));
        const meta = [tripName(r), r.day_name || (r.day ? `Day ${r.day}` : 'Whole trip'), r.kind_label, r.aspect,
            r.dur ? ReelsSite.fmtDur(r.dur) : null].filter(Boolean);
        body.appendChild(el('div', 'reel-meta', meta.join(' · ')));
        if (r.song) body.appendChild(el('div', 'reel-meta', '♪ ' + r.song));
        if (r.places && r.places.length && r.places_from !== 'day') {
            body.appendChild(el('div', 'reel-meta', r.places.slice(0, 4).join(', ') + (r.places.length > 4 ? '…' : '')));
        }
        if (r.delivered) {
            body.appendChild(el('div', 'reel-meta', 'Made ' + new Date(r.delivered + 'T00:00:00')
                .toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })));
        }
        const st = window.ReelsSite && ReelsSite.state();
        if (st && st.queue && st.queue.includes(r.id)) body.appendChild(el('div', 'pq-status done', 'On your Profile list'));
        const p = el('div', 'pq-path', r.path);
        p.title = r.path;
        body.appendChild(p);
        const acts = el('div', 'rvw-acts');
        const btn = (label, key, cls, title, fn) => {
            const b = el('button', 'reels-mini rvw-act ' + cls);
            b.type = 'button';
            b.title = title;
            b.append(document.createTextNode(label + ' '), el('kbd', '', key));
            b.addEventListener('click', e => { e.stopPropagation(); setFocus(rows.indexOf(row), false); fn(); });
            acts.appendChild(b);
            return b;
        };
        btn('Post', 'P', 'rvw-post', 'Good to go as it is: into the post queue, the bot fits it into the schedule',
            () => verdict(row, 'queue'));
        acts.appendChild(starButtons(n => verdict(row, 'queue', n), () => setFocus(rows.indexOf(row), false)));
        btn('Post + note…', 'F', 'rvw-post', 'Good to go as it is (into the post queue), with stars if you like and ' +
            'a note on what to keep or change in the next reels', () => openFeedback(row, 'post'));
        btn('Shortlist…', 'S', 'rvw-short', 'Good, but something needs fixing: says what (required), then onto the ' +
            'shortlist; the reel builders act on the note', () => openFeedback(row));
        row.rejectSlot = el('div', 'rvw-reject');
        acts.appendChild(row.rejectSlot);
        row.fbSlot = el('div', 'rvw-fbbox');
        acts.appendChild(row.fbSlot);
        body.appendChild(acts);
        row.note = el('div', 'pq-status wait rvw-busy');
        body.appendChild(row.note);
        full.appendChild(body);
        row.full = full;
        // the folded view once judged: what was decided, and Undo
        const slim = el('div', 'rvw-slim');
        row.slim = slim;
        row.el.append(full, slim);
        drawReject(row);
        return row;
    }

    // "Reject…" from the reject list: the reason form opens under it
    function drawReject(row) {
        if (!window.PostsQueue) return;
        const ctl = PostsQueue.rejectControl(row.r.path, () => {
            drawReject(row);
            const input = row.rejectSlot.querySelector('.pq-reason-text');
            if (input) input.focus();
        });
        row.rejectSlot.replaceChildren(ctl);
        const b = ctl.querySelector('.pq-reject-btn');
        if (b && !b.querySelector('kbd')) b.append(document.createTextNode(' '), el('kbd', '', 'R'));
    }

    // Shortlist…: the text box under the verdicts (what needs fixing: required). Enter saves, Shift+Enter a new
    // line, Esc cancels. Saved = the note to the helper, then the reel onto the shortlist.
    // Post + note… (mode 'post', key F): the same box with stars to pick (optional) above it; saved = the note (kind
    // "post"), the stars, then into the post queue.
    function openFeedback(row, mode) {
        if (row.state || row.busy) return;
        const post = mode === 'post';
        const open = row.fbSlot.querySelector('textarea');
        if (open && row.fbMode === (post ? 'post' : 'shortlist')) { open.focus(); return; }
        row.fbMode = post ? 'post' : 'shortlist';
        let stars = 0;
        const ta = el('textarea', 'pq-reason-text rvw-fbtext');
        ta.rows = 3;
        ta.placeholder = post
            ? 'Your feedback (required): what works, what to do differently next time. Enter saves, Shift+Enter a new line, Esc cancels'
            : 'What needs fixing? (required: Enter saves, Shift+Enter a new line, Esc cancels)';
        const save = el('button', 'reels-mini rvw-fbsave', post ? 'Post with this note' : 'Shortlist with this note');
        save.disabled = true;
        ta.addEventListener('input', () => { save.disabled = !ta.value.trim(); });
        save.type = 'button';
        const cancel = el('button', 'reels-mini', 'Cancel');
        cancel.type = 'button';
        const close = () => row.fbSlot.replaceChildren();
        const go = () => {
            const note = ta.value.trim();
            if (!note || row.busy) return;
            row.busy = post ? 'Saving the note, adding to the post queue…' : 'Saving the note, adding to the shortlist…';
            paint(row);
            sendNote(post ? { id: row.r.id, note, kind: 'post' } : { id: row.r.id, note })
                .then(ok => (ok && post && stars ? sendRating({ id: row.r.id, stars }) : ok))
                .then(ok => {
                    if (!ok) {
                        row.busy = null;
                        row.missing = (post ? 'Not posted' : 'Not shortlisted') +
                            ': the note could not be saved (the local helper did not answer; relaunch the desktop app).';
                        refresh();
                        return null;
                    }
                    close();
                    return PostsQueue.addPaths([row.r.path], post ? 'queue' : 'shortlist', false).then(() => { row.busy = null; refresh(); });
                });
        };
        save.addEventListener('click', e => { e.stopPropagation(); go(); });
        cancel.addEventListener('click', e => { e.stopPropagation(); close(); });
        ta.addEventListener('keydown', e => {
            e.stopPropagation();
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); }
            else if (e.key === 'Escape') { e.preventDefault(); close(); row.el.focus(); }
        });
        const bar = el('div', 'rvw-fbbar');
        bar.append(save, cancel);
        if (post) {
            const pick = el('div', 'rvw-fbstars');
            pick.appendChild(el('span', 'reel-meta', 'Stars (optional): '));
            const g = starButtons(n => {
                stars = stars === n ? 0 : n;            // the same star again clears it
                g.querySelectorAll('.rvw-star').forEach(x => x.classList.toggle('on', +x.dataset.n <= stars));
                ta.focus();
            });
            pick.appendChild(g);
            row.fbSlot.replaceChildren(pick, ta, bar);
        } else {
            row.fbSlot.replaceChildren(ta, bar);
        }
        ta.focus();
    }

    // ★1–★5: Post with stars (the same as Post, and how good it is)
    function starButtons(onPick, before, current) {
        const g = el('div', 'rvw-stars');
        g.title = 'Post, and how good it is (keys 1–5)';
        for (let n = 1; n <= 5; n++) {
            const b = el('button', 'reels-mini rvw-act rvw-star' + (current && n <= current ? ' on' : ''), '★');
            b.type = 'button';
            b.title = `Post: ${n} of 5 (key ${n})`;
            b.dataset.n = String(n);
            b.addEventListener('click', e => { e.stopPropagation(); if (before) before(); onPick(n); });
            b.addEventListener('mouseenter', () => g.querySelectorAll('.rvw-star').forEach(x => x.classList.toggle('hover', +x.dataset.n <= n)));
            g.appendChild(b);
        }
        g.addEventListener('mouseleave', () => g.querySelectorAll('.rvw-star').forEach(x => x.classList.remove('hover')));
        return g;
    }

    function verdict(row, where, stars) {
        if (row.state || row.busy) return;
        row.busy = where === 'queue' ? 'Adding to the post queue…' : 'Adding to the shortlist…';
        paint(row);
        const rated = stars ? sendRating({ id: row.r.id, stars }) : Promise.resolve(true);
        rated.then(ok => {
            if (!ok) {
                row.busy = null;
                row.missing = 'Not posted: the stars could not be saved (the local helper did not answer; relaunch the desktop app).';
                refresh();
                return null;
            }
            return PostsQueue.addPaths([row.r.path], where, false).then(() => { row.busy = null; refresh(); });
        });
    }

    // a decision taken back (the reel goes back to To review): off the queue / shortlist / reject list, its stars or
    // note withdrawn → a promise
    function undoDecision(r, d) {
        const offList = () => (d.noteOnly ? Promise.resolve() : PostsQueue.removeFromShortlist(d.at));
        return d.kind === 'queue' ? PostsQueue.removeFromQueue(d.at)
            .then(() => (d.stars ? sendRating({ id: r.id, undo: true }) : null))
            .then(() => (d.note ? sendNote({ id: r.id, undo: true }) : null))
            : d.kind === 'shortlist' ? offList().then(() => (d.note ? sendNote({ id: r.id, undo: true }) : null))
                : d.kind === 'rejected' ? PostsQueue.undoReject(d.at) : Promise.resolve();
    }

    function undo(row) {
        const d = row.state;
        if (!d) return;
        row.busy = 'Undoing…';
        paint(row);
        undoDecision(row.r, d).then(() => { row.busy = null; refresh(); });
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
        if (row.r.cover) {
            const img = el('img', 'rvw-thumb');
            img.src = row.r.cover;
            img.alt = '';
            img.loading = 'lazy';
            img.addEventListener('error', () => img.remove());   // no cover made for it
            row.slim.appendChild(img);
        }
        const txt = el('div', 'rvw-slim-text');
        txt.appendChild(el('div', 'pq-title', row.r.title || row.r.id));
        const what = d.kind === 'rejected' ? `Rejected: ${d.reason || ''}`
            : d.kind === 'shortlist' && d.note ? `On the shortlist, to fix: ${d.note}`
            : d.kind === 'queue' && d.note ? `In the post queue · note: ${d.note}`
            : d.kind === 'posted' ? (d.profile ? `Posted ${d.when || ''} (ticked on the Profile list)` : 'Posted')
                : SAID[d.kind];
        const shown = d.stars ? `${what} ${starText(d.stars)}` : what;
        txt.appendChild(el('div', 'pq-status ' + (d.kind === 'rejected' ? 'bad' : d.kind === 'shortlist' ? 'warn' : 'ok'),
            '✓ ' + shown + (row.busy ? ' · ' + row.busy : '')));
        row.slim.appendChild(txt);
        if (d.kind !== 'posted') {
            const u = el('button', 'reels-mini', 'Undo');
            u.type = 'button';
            u.disabled = !!row.busy;
            u.title = d.kind === 'rejected' ? 'Not rejected after all: off the reject list, back to judge'
                : d.kind === 'queue' ? 'Take it out of the post queue again (it is not posted), back to judge'
                    : 'Take it off the shortlist again (its note is withdrawn), back to judge';
            u.addEventListener('click', e => { e.stopPropagation(); undo(row); });
            row.slim.appendChild(u);
        }
    }

    // ── moving through ──
    function setFocus(i, scroll) {
        if (i < 0 || i >= rows.length) return;
        if (focusAt >= 0 && rows[focusAt]) rows[focusAt].el.classList.remove('focus');
        focusAt = i;
        const row = rows[i];
        row.el.classList.add('focus');
        if (scroll) row.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    // the next (dir 1) or previous (-1) reel still to judge; past the last drawn, the next page comes in
    function step(dir, play) {
        let i = focusAt;
        for (;;) {
            i += dir;
            if (i >= rows.length && dir > 0 && drawn < list.length) addPage();
            if (i < 0 || i >= rows.length) return false;
            if (!rows[i].state) break;
        }
        setFocus(i, true);
        if (play && rows[i].video) rows[i].video.play().catch(() => {});
        return true;
    }

    function addPage() {
        let added = 0;
        while (drawn < list.length && added < PAGE) {
            const r = list[drawn++];
            if (decision(r)) continue;          // judged since the list was drawn
            const row = makeRow(r);
            rows.push(row);
            listEl.appendChild(row.el);
            paint(row);
            added++;
        }
        paintMore();
    }

    function paintMore() {
        if (!moreEl) return;
        const left = list.slice(drawn).filter(r => !decision(r)).length;
        moreEl.replaceChildren();
        if (!left) return;
        const b = el('button', 'reels-mini pq-all', `Show ${Math.min(PAGE, left)} more (${left} left)`);
        b.type = 'button';
        b.addEventListener('click', () => addPage());
        moreEl.appendChild(b);
    }

    // after any change to the queue or the reject list (here or anywhere): each row's state, the counts
    function refresh() {
        if (!rootEl || !rootEl.isConnected) return;
        rows.forEach((row, i) => {
            const before = row.state;
            const d = decision(row.r);
            const changed = JSON.stringify(before) !== JSON.stringify(d);
            row.state = d;
            paint(row);                                 // a judged reel stops playing here
            if (row.rejectSlot && !d && changed) drawReject(row);
            // the reel being looked at was just judged: on to the next one, which starts playing (owner 2026-11-14:
            // "when I provide feedback … and it jumps to the next reel, it doesn't start playing automatically, but I want it to")
            if (!before && d && i === focusAt) step(1, true);
        });
        paintHead();
        paintMore();
    }

    function paintHead() {
        if (!headEl) return;
        const all = pending(reelsDoc);
        const here = trip === 'all' ? all : all.filter(r => tripName(r) === trip);
        headEl.textContent = `To review (${here.length}${trip === 'all' ? '' : ` of ${all.length}`})`;
        // the trip chips: every trip with reels to judge, and the one picked
        chipsEl.replaceChildren();
        const per = new Map();
        all.forEach(r => per.set(tripName(r), (per.get(tripName(r)) || 0) + 1));
        const trips = [...per.keys()].sort();
        if (trip !== 'all' && !per.has(trip)) trips.push(trip);
        [['all', `All trips (${all.length})`], ...trips.map(t => [t, `${t} (${per.get(t) || 0})`])].forEach(([k, label]) => {
            const c = el('button', 'reel-chip' + (k === trip ? ' active' : ''), label);
            c.type = 'button';
            c.addEventListener('click', () => {
                if (k === trip) return;
                trip = k;
                lsSet(TRIP_KEY, k);
                drawList();
            });
            chipsEl.appendChild(c);
        });
    }

    function drawList() {
        rows.forEach(row => { if (row.video) { row.video.pause(); } });
        const all = pending(reelsDoc);
        list = trip === 'all' ? all : all.filter(r => tripName(r) === trip);
        rows = [];
        drawn = 0;
        focusAt = -1;
        listEl.replaceChildren();
        if (!list.length) {
            listEl.appendChild(el('p', 'reels-empty', trip === 'all'
                ? 'Nothing to review: every reel has been posted, queued, shortlisted or rejected.'
                : 'Nothing to review from this trip.'));
        }
        addPage();
        if (rows.length) setFocus(0, false);
        paintHead();
    }

    // keys: only while this list shows and the cursor is not in a text box
    function onKey(e) {
        if (!rootEl || !rootEl.isConnected || e.metaKey || e.ctrlKey || e.altKey) return;
        const t = e.target;
        if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
        const k = e.key;
        const row = rows[focusAt];
        if (k === 'j' || k === 'J' || k === 'ArrowDown') { e.preventDefault(); if (focusAt < 0) setFocus(0, true); else step(1, false); return; }
        if (k === 'k' || k === 'K' || k === 'ArrowUp') { e.preventDefault(); step(-1, false); return; }
        if (!row || row.state) return;
        if (k === ' ' && t && t.tagName !== 'BUTTON' && t.tagName !== 'VIDEO') {
            e.preventDefault();
            if (row.video.paused) row.video.play().catch(() => {}); else row.video.pause();
        } else if (k === 'p' || k === 'P') {
            e.preventDefault();
            verdict(row, 'queue');
        } else if (k >= '1' && k <= '5') {
            e.preventDefault();
            verdict(row, 'queue', +k);
        } else if (k === 'f' || k === 'F') {
            e.preventDefault();
            openFeedback(row, 'post');
        } else if (k === 's' || k === 'S') {
            e.preventDefault();
            openFeedback(row);
        } else if (k === 'r' || k === 'R') {
            e.preventDefault();
            const b = row.rejectSlot.querySelector('.pq-reject-btn');
            const open = row.rejectSlot.querySelector('.pq-reason-text');
            if (open) open.focus();
            else if (b) b.click();
        }
    }

    // ── the panel ──
    function panel(doc) {
        reelsDoc = doc;
        rootEl = el('section', 'pq rvw');
        headEl = el('div', 'pq-h', 'To review');
        rootEl.appendChild(headEl);
        rootEl.appendChild(el('p', 'reels-how',
            'The reels you haven\'t judged yet, newest first. Post: good to go as it is, into the post queue; ' +
            'the stars (1–5) post it too and say how good it is. ' +
            'Shortlist…: good, but something needs fixing; it asks what, and the reel builders act on your note. ' +
            'Reject…: a hard no; saying why is optional. A judged reel folds away (with Undo) and the next one comes up.'));
        const keys = el('p', 'reels-how rvw-keys');
        [['J', 'next'], ['K', 'back'], ['Space', 'play'], ['P', 'Post'], ['1–5', 'Post with stars'], ['F', 'Post + stars + note'], ['S', 'Shortlist…'], ['R', 'Reject…']].forEach(([k, w]) => {
            keys.append(el('kbd', '', k), document.createTextNode(' ' + w + '  '));
        });
        rootEl.appendChild(keys);
        chipsEl = el('div', 'reel-chips rvw-trips');
        rootEl.appendChild(chipsEl);
        listEl = el('div', 'rvw-list');
        rootEl.appendChild(listEl);
        moreEl = el('div', 'rvw-more');
        rootEl.appendChild(moreEl);
        if (!listening) {
            listening = true;
            document.addEventListener('keydown', onKey);
            document.addEventListener('reels:changed', () => refresh());
            if (window.ReelsSite) ReelsSite.onChange(() => refresh());
        }
        const go = () => drawList();
        const ready = (window.PostsQueue && !PostsQueue.counts().loaded) ? PostsQueue.ready() : Promise.resolve();
        listEl.appendChild(el('p', 'reels-how', 'Loading…'));
        Promise.all([ready, loadNotes(), loadRatings()]).then(go);
        return rootEl;
    }

    // ── Posts ▸ Reels ▸ Reviewed: everything judged ──
    const RV_PAGE = 30;
    const RV_KIND_KEY = 'reels_reviewed_kind';
    const RV_TRIP_KEY = 'reels_reviewed_trip';
    const RV_KINDS = [['all', 'All'], ['queue', 'Post'], ['shortlist', 'Shortlist'], ['rejected', 'Rejected'], ['posted', 'Posted']];
    let rv = null;

    // when it was judged: the stars or note's time, else when it went onto the list, else the reject's time
    function judgedAt(d) {
        return String(d.rated || d.noted || d.added || (d.kind !== 'queue' ? d.when : '') || '');
    }

    function reviewed(doc) {
        return ((doc && doc.reels) || []).filter(r => r.path).map(r => ({ r, d: decision(r) })).filter(x => x.d)
            .sort((a, b) => judgedAt(b.d).localeCompare(judgedAt(a.d)) || newestFirst(a.r, b.r));
    }

    // the section button's number: reels judged, or null before the lists are loaded
    function reviewedCount() {
        if (!reelsDoc || !window.PostsQueue || !PostsQueue.counts().loaded) return null;
        return reviewed(reelsDoc).length;
    }

    function verdictLine(d) {
        const what = d.kind === 'rejected' ? `Rejected: ${d.reason || ''}`
            : d.kind === 'shortlist' ? (d.note ? `Shortlist, to fix: ${d.note}` : 'Shortlist (no note)')
                : d.kind === 'posted' ? (d.profile ? `Posted ${d.when || ''}` : 'Posted')
                    : 'Post: in the post queue';
        // a queued reel: where it stands with the bot (ready, planned for a time, a problem the Mac found)
        const st = d.kind === 'queue' && window.PostsQueue && PostsQueue.statusOf ? PostsQueue.statusOf(d.at) : null;
        const base = st ? `Post: ${st[0].charAt(0).toLowerCase()}${st[0].slice(1)}` : what;
        const full = (d.kind === 'queue' || d.kind === 'posted') && d.note ? `${base} · note: ${d.note}` : base;
        const cls = d.kind === 'rejected' ? 'bad' : d.kind === 'shortlist' ? 'warn' : st ? st[1] : 'ok';
        return el('div', 'pq-status ' + cls, full);
    }

    function fmtJudged(d) {
        const t = judgedAt(d);
        if (!t) return null;
        const x = new Date(t);
        return isNaN(x) ? null : 'Judged ' + x.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
    }

    function rvRow(x) {
        const { r, d } = x;
        const row = el('div', 'rvw-row rv-row');
        const top = el('div', 'rv-top');
        if (r.cover) {
            const img = el('img', 'rv-thumb');
            img.src = r.cover;
            img.alt = '';
            img.loading = 'lazy';
            img.addEventListener('error', () => img.remove());
            top.appendChild(img);
        }
        const txt = el('div', 'rvw-slim-text');
        txt.appendChild(el('div', 'pq-title', r.title || r.id));
        const meta = [tripName(r), r.day_name || (r.day ? `Day ${r.day}` : 'Whole trip'), r.kind_label, r.aspect,
            r.dur ? ReelsSite.fmtDur(r.dur) : null, fmtJudged(d)].filter(Boolean);
        txt.appendChild(el('div', 'reel-meta', meta.join(' · ')));
        const p = el('div', 'pq-path', r.path);
        p.title = r.path;
        txt.appendChild(p);
        txt.appendChild(verdictLine(d));
        const busy = el('div', 'pq-status wait rvw-busy');
        txt.appendChild(busy);
        top.appendChild(txt);
        const acts = el('div', 'rv-acts');
        if (d.kind === 'queue' || d.kind === 'posted') {
            acts.appendChild(starButtons(n => {
                busy.textContent = 'Saving the stars…';
                sendRating({ id: r.id, stars: n }).then(ok => {
                    busy.textContent = ok ? '' : 'The stars could not be saved (the local helper did not answer).';
                    if (ok) rvDraw(true);
                });
            }, null, d.stars));
        }
        const play = el('button', 'reels-mini', 'Play');
        play.type = 'button';
        let player = null;
        play.addEventListener('click', () => {
            if (player) { player.remove(); player = null; play.textContent = 'Play'; return; }
            const wide = r.aspect && r.aspect !== '9:16';
            player = el('div', 'rvw-player rv-player' + (wide ? ' wide' : ''));
            const v = video(r, () => { busy.textContent = 'The video file isn\'t there any more.'; });
            player.appendChild(v);
            row.appendChild(player);
            play.textContent = 'Close';
            v.play().catch(() => {});
        });
        acts.appendChild(play);
        if (d.kind !== 'posted') {
            const u = el('button', 'reels-mini', 'Undo');
            u.type = 'button';
            u.title = 'Take the verdict back: the reel goes back to Review';
            u.addEventListener('click', () => {
                u.disabled = true;
                busy.textContent = 'Undoing…';
                undoDecision(r, d).then(() => rvDraw(true));
            });
            acts.appendChild(u);
        }
        top.appendChild(acts);
        row.appendChild(top);
        return row;
    }

    function rvChips(box, items, current, label, pick) {
        box.replaceChildren();
        items.forEach(([k, text, n]) => {
            const c = el('button', 'reel-chip' + (k === current ? ' active' : ''), `${text} (${n})`);
            c.type = 'button';
            c.title = label;
            c.addEventListener('click', () => { if (k !== current) pick(k); });
            box.appendChild(c);
        });
    }

    // force: redraw even when nothing judged changed (a filter picked, stars changed here)
    function rvDraw(force) {
        if (!rv || !rv.root.isConnected) return;
        const all = reviewed(reelsDoc);
        const sig = JSON.stringify([all.map(x => [x.r.id, x.d.kind, x.d.stars, x.d.note, x.d.reason, x.d.status, x.d.when]),
            window.PostsQueue && PostsQueue.counts()]);
        // the schedule box follows the document, except while its slider is in the hand
        if (window.PostsQueue && rv.sched && !rv.sched.contains(document.activeElement)) rv.sched.replaceChildren(PostsQueue.scheduleBox());
        if (!force && sig === rv.sig) return;
        rv.sig = sig;
        const byKind = rv.kind === 'all' ? all : all.filter(x => x.d.kind === rv.kind);
        const here = rv.trip === 'all' ? byKind : byKind.filter(x => tripName(x.r) === rv.trip);
        const stars = all.filter(x => x.d.stars);
        rv.head.textContent = `Reviewed (${all.length})` + (stars.length
            ? ` · ${stars.length} rated, average ${(stars.reduce((a, x) => a + x.d.stars, 0) / stars.length).toFixed(1)} ★` : '');
        rvChips(rv.kinds, RV_KINDS.map(([k, t]) => [k, t, k === 'all' ? all.length : all.filter(x => x.d.kind === k).length])
            .filter(([k, , n]) => k === 'all' || n || k === rv.kind), rv.kind, 'Verdict', k => {
            rv.kind = k; lsSet(RV_KIND_KEY, k); rvDraw(true);
        });
        const per = new Map();
        byKind.forEach(x => per.set(tripName(x.r), (per.get(tripName(x.r)) || 0) + 1));
        const trips = [...per.keys()].sort();
        if (rv.trip !== 'all' && !per.has(rv.trip)) trips.push(rv.trip);
        rvChips(rv.trips, [['all', 'All trips', byKind.length], ...trips.map(t => [t, t, per.get(t) || 0])], rv.trip, 'Trip', k => {
            rv.trip = k; lsSet(RV_TRIP_KEY, k); rvDraw(true);
        });
        rv.items = here;
        rv.drawn = 0;
        rv.list.replaceChildren();
        if (!here.length) {
            rv.list.appendChild(el('p', 'reels-empty', all.length ? 'Nothing judged with this filter.' : 'Nothing reviewed yet.'));
        }
        rvMore();
    }

    function rvMore() {
        const next = rv.items.slice(rv.drawn, rv.drawn + RV_PAGE);
        next.forEach(x => rv.list.appendChild(rvRow(x)));
        rv.drawn += next.length;
        rv.more.replaceChildren();
        const left = rv.items.length - rv.drawn;
        if (left > 0) {
            const b = el('button', 'reels-mini pq-all', `Show ${Math.min(RV_PAGE, left)} more (${left} left)`);
            b.type = 'button';
            b.addEventListener('click', rvMore);
            rv.more.appendChild(b);
        }
    }

    // "Feedback to agent": the text he pastes to his AI agent. Shortlisted reels (fix this), rejected reels (a hard no:
    // taste for the next reels) and posted reels with a note (posted as is: taste for the next reels), each with its
    // full path, plus what to do with them.
    function agentText(items) {
        const fmt = t => { const x = new Date(t); return isNaN(x) ? '' : x.toISOString().slice(0, 10); };
        const line = (x, extra) => `- ${x.r.path}\n  ${extra}` + (fmt(judgedAt(x.d)) ? ` (${fmt(judgedAt(x.d))})` : '');
        const short = items.filter(x => x.d.kind === 'shortlist');
        const rej = items.filter(x => x.d.kind === 'rejected');
        const posted = items.filter(x => (x.d.kind === 'queue' || x.d.kind === 'posted') && x.d.note);
        const out = [
            '# Reel review feedback (Posts ▸ Reels ▸ Reviewed, copied ' + new Date().toISOString().slice(0, 16).replace('T', ' ') + ')',
            '',
            'You are the reel editor for my Instagram (@ryanmcl0), working in ~/Documents/Projects/video-editor (read its ' +
            'AGENTS.md and your memory first). Below is my feedback on reels I reviewed. Act on it:',
            '',
            '1. First run the queue sync: cd ~/Documents/Projects/video-editor-r4 && .venv/bin/python tools/reelbot/queue_sync.py',
            '2. SHORTLISTED = good, but fix exactly what my note says and keep everything else as it is. Before changing a ' +
            'reel, look up my earlier notes on that same reel and apply them too. Re-render it to the same path (move the ' +
            'old file to the archive, never delete it), check the new one on its frames yourself, then set its note to ' +
            '"status": "handled" in ~/Documents/Projects/geotag-photos/web/phone/reels/reel_feedback.json so it comes ' +
            'back to my Review list. A shortlisted reel with no note: ask me what to fix.',
            '3. REJECTED = a hard no. Never re-make, re-post or delete them. Read the reasons as my taste: when a reason ' +
            'repeats, turn it into a rule or a check for future reels.',
            '4. POSTED WITH A NOTE = posted exactly as it is: never change those reels. The note is taste for the next reels.',
            '5. Never use private clips. When you want me to look at something, give me its full file path on its own line.',
            '6. Tell me in plain words, reel by reel, what you did, and anything you could not do.',
            ''
        ];
        out.push(`## Shortlisted: fix these (${short.length})`);
        short.forEach(x => out.push(line(x, 'Fix: ' + (x.d.note || '(no note given: ask me)'))));
        if (!short.length) out.push('(none)');
        out.push('', `## Rejected (${rej.length})`);
        rej.forEach(x => out.push(line(x, 'Why: ' + (x.d.reason || 'No reason given'))));
        if (!rej.length) out.push('(none)');
        out.push('', `## Posted, with a note for the next reels (${posted.length})`);
        posted.forEach(x => out.push(line(x, (x.d.stars ? `${x.d.stars}/5 stars. ` : '') + 'Note: ' + x.d.note)));
        if (!posted.length) out.push('(none)');
        return out.join('\n') + '\n';
    }

    function reviewedPanel(doc) {
        reelsDoc = doc;
        const root = el('section', 'pq rvw rvd');
        rv = { root, sig: null, kind: lsGet(RV_KIND_KEY, 'all'), trip: lsGet(RV_TRIP_KEY, 'all'), items: [], drawn: 0 };
        if (!RV_KINDS.some(k => k[0] === rv.kind)) rv.kind = 'all';
        rv.head = el('div', 'pq-h', 'Reviewed');
        const fb = el('button', 'reels-export-btn rvd-agent', 'Feedback to agent');
        fb.type = 'button';
        fb.title = 'Copy the shortlisted and rejected reels (file paths, your notes and reasons) and your notes on posted ' +
            'reels, with instructions, to paste to your AI agent';
        const fbSaid = el('span', 'pq-status ok rvd-agent-said');
        fb.addEventListener('click', () => {
            const text = agentText(reviewed(reelsDoc));
            const done = n => { fbSaid.textContent = n; setTimeout(() => { fbSaid.textContent = ''; }, 6000); };
            const fallback = () => {
                const ta = el('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                let ok = false;
                try { ok = document.execCommand('copy'); } catch (e) { /* none */ }
                ta.remove();
                done(ok ? 'Copied.' : 'Could not copy: the browser refused.');
            };
            const n = (text.match(/^- /gm) || []).length;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(() => done(`Copied ${n} reel${n === 1 ? '' : 's'} with instructions: paste it to your agent.`), fallback);
            } else fallback();
        });
        const bar = el('div', 'rvd-agentbar');
        bar.append(fb, fbSaid);
        root.appendChild(rv.head);
        root.appendChild(el('p', 'reels-how',
            'Every reel you have judged, last judged first: its verdict, stars, note or reason. Change the stars of a ' +
            'posted one here; Undo takes the verdict back and the reel goes back to Review.'));
        rv.kinds = el('div', 'reel-chips rvw-trips');
        rv.trips = el('div', 'reel-chips rvw-trips');
        rv.list = el('div', 'rvw-list');
        rv.more = el('div', 'rvw-more');
        const sum = el('summary', 'pq-h', 'Posting schedule');
        rv.schedWrap = el('details', 'rvd-sched');
        rv.schedWrap.open = !!lsGet('reels_reviewed_sched', true);
        rv.schedWrap.addEventListener('toggle', () => lsSet('reels_reviewed_sched', rv.schedWrap.open));
        rv.schedWrap.append(sum, rv.sched = el('div'));
        root.append(bar, rv.schedWrap, rv.kinds, rv.trips, rv.list, rv.more);
        rv.list.appendChild(el('p', 'reels-how', 'Loading…'));
        if (!rv.listening) {
            document.addEventListener('reels:changed', () => rvDraw(false));
            if (window.ReelsSite) ReelsSite.onChange(() => rvDraw(false));
            if (window.PostsQueue) PostsQueue.onRejects(() => rvDraw(false));
        }
        rv.listening = true;
        const ready = (window.PostsQueue && !PostsQueue.counts().loaded) ? PostsQueue.ready() : Promise.resolve();
        Promise.all([ready, loadNotes(), loadRatings()]).then(() => rvDraw(true));
        // the bot's schedule and the Mac's checks arrive on their own: a fresh read every 30 s while this shows
        clearInterval(rv.timer);
        rv.timer = setInterval(() => {
            if (!root.isConnected) { clearInterval(rv.timer); return; }
            if (window.PostsQueue && PostsQueue.refresh) PostsQueue.refresh();
        }, 30000);
        return root;
    }

    function setDoc(doc) { reelsDoc = doc; loadNotes(); loadRatings(); }

    return { panel, reviewedPanel, agentText, count, reviewedCount, setDoc, pending, reviewed, judgedAt, tripName, newestFirst,
        takeNotes, takeRatings, decision };
})();
