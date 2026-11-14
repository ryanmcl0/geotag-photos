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

    // what has been decided about a reel, or null
    function decision(r) {
        const d = window.PostsQueue ? PostsQueue.decisionOf(r.path) : null;
        if (d) return d;
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
        btn('Post', 'P', 'rvw-post', 'Into the post queue: the bot fits it into the schedule (as if you pasted its path there)',
            () => verdict(row, 'queue'));
        btn('Shortlist', 'S', 'rvw-short', 'Good, not yet: onto the shortlist (the bot never sees it until Post now)',
            () => verdict(row, 'shortlist'));
        row.rejectSlot = el('div', 'rvw-reject');
        acts.appendChild(row.rejectSlot);
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

    function verdict(row, where) {
        if (row.state || row.busy) return;
        row.busy = where === 'queue' ? 'Adding to the post queue…' : 'Adding to the shortlist…';
        paint(row);
        PostsQueue.addPaths([row.r.path], where, false).then(() => {
            row.busy = null;
            refresh();
        });
    }

    function undo(row) {
        const d = row.state;
        if (!d) return;
        row.busy = 'Undoing…';
        paint(row);
        const done = d.kind === 'queue' ? PostsQueue.removeFromQueue(d.at)
            : d.kind === 'shortlist' ? PostsQueue.removeFromShortlist(d.at)
                : d.kind === 'rejected' ? PostsQueue.undoReject(d.at) : Promise.resolve();
        done.then(() => { row.busy = null; refresh(); });
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
            : d.kind === 'posted' ? (d.profile ? `Posted ${d.when || ''} (ticked on the Profile list)` : 'Posted')
                : SAID[d.kind];
        txt.appendChild(el('div', 'pq-status ' + (d.kind === 'rejected' ? 'bad' : d.kind === 'shortlist' ? 'warn' : 'ok'),
            '✓ ' + what + (row.busy ? ' · ' + row.busy : '')));
        row.slim.appendChild(txt);
        if (d.kind !== 'posted') {
            const u = el('button', 'reels-mini', 'Undo');
            u.type = 'button';
            u.disabled = !!row.busy;
            u.title = d.kind === 'rejected' ? 'Not rejected after all: off the reject list, back to judge'
                : d.kind === 'queue' ? 'Take it out of the post queue again (it is not posted), back to judge'
                    : 'Take it off the shortlist again, back to judge';
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
            const wasPlaying = !!row.video && !row.video.paused;
            row.state = d;
            paint(row);                                 // a judged reel stops playing here
            if (row.rejectSlot && !d && changed) drawReject(row);
            // the reel being looked at was just judged: on to the next one (playing on if this one was playing)
            if (!before && d && i === focusAt) step(1, wasPlaying);
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
        } else if (k === 's' || k === 'S') {
            e.preventDefault();
            verdict(row, 'shortlist');
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
            'The reels you haven\'t judged yet, newest first. Post puts one in the post queue, Shortlist keeps it for ' +
            'later, Reject… asks why and the reel builders learn from it. A judged reel folds away (with Undo) and the ' +
            'next one comes up.'));
        const keys = el('p', 'reels-how rvw-keys');
        [['J', 'next'], ['K', 'back'], ['Space', 'play'], ['P', 'Post'], ['S', 'Shortlist'], ['R', 'Reject…']].forEach(([k, w]) => {
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
        if (window.PostsQueue && !PostsQueue.counts().loaded) {
            listEl.appendChild(el('p', 'reels-how', 'Loading…'));
            PostsQueue.ready().then(go);
        } else {
            go();
        }
        return rootEl;
    }

    function setDoc(doc) { reelsDoc = doc; }

    return { panel, count, setDoc, pending, tripName, newestFirst };
})();
