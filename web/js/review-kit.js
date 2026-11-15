/*
 * The review controls shared by Posts ▸ Reels ▸ Review (posts-review.js) and Posts ▸ Business ▸ Review
 * (posts-business-review.js), owner 2026-11-15: "I would hope that the feedback logic is set up as a shared code since
 * now two different places are using it". One place for:
 *
 *   stars(onPick, before, current)   the ★1–★5 buttons (Post, and how good it is)
 *   noteBox(slot, opts)              the note box under the verdicts: Shortlist… (note required) and Post + note…
 *                                    (stars optional, note required); Enter saves, Shift+Enter a new line, Esc cancels
 *   reasonBox(slot, opts)            Reject…: quick reasons or his own words (optional)
 *   keys(getRow, acts)               the keys while a review list shows: J/↓ next, K/↑ back, Space play, P Post,
 *                                    1–5 Post with stars, F Post + note, S Shortlist…, R Reject…
 *   keyHelp()                        the line that lists those keys
 *   copy(text, said)                 copy to the clipboard (with a fallback), saying what happened in `said`
 *   starText(n)                      ★★★☆☆
 *
 * What the verdicts mean is the same in both places (owner 2026-11-14): Post = good to go as it is; Shortlist = good,
 * but something needs fixing (the note says what, required); Reject = a hard no (the reason is optional).
 */
window.ReviewKit = (function () {
    'use strict';

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }
    const starText = n => '★'.repeat(n) + '☆'.repeat(5 - n);

    function stars(onPick, before, current) {
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

    // opts: {post: bool (stars + "Post with this note"), placeholder?, onSave(note, stars), onCancel?}
    function noteBox(slot, opts) {
        const post = !!opts.post;
        let picked = 0;
        const ta = el('textarea', 'pq-reason-text rvw-fbtext');
        ta.rows = 3;
        ta.placeholder = opts.placeholder || (post
            ? 'Your feedback (required): what works, what to do differently next time. Enter saves, Shift+Enter a new line, Esc cancels'
            : 'What needs fixing? (required: Enter saves, Shift+Enter a new line, Esc cancels)');
        const save = el('button', 'reels-mini rvw-fbsave', post ? 'Post with this note' : 'Shortlist with this note');
        save.type = 'button';
        save.disabled = true;
        ta.addEventListener('input', () => { save.disabled = !ta.value.trim(); });
        const cancel = el('button', 'reels-mini', 'Cancel');
        cancel.type = 'button';
        const close = () => slot.replaceChildren();
        const go = () => {
            const note = ta.value.trim();
            if (!note) return;
            close();
            opts.onSave(note, picked);
        };
        save.addEventListener('click', e => { e.stopPropagation(); go(); });
        cancel.addEventListener('click', e => { e.stopPropagation(); close(); if (opts.onCancel) opts.onCancel(); });
        ta.addEventListener('keydown', e => {
            e.stopPropagation();
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); }
            else if (e.key === 'Escape') { e.preventDefault(); close(); if (opts.onCancel) opts.onCancel(); }
        });
        const bar = el('div', 'rvw-fbbar');
        bar.append(save, cancel);
        if (post) {
            const pick = el('div', 'rvw-fbstars');
            pick.appendChild(el('span', 'reel-meta', 'Stars (optional): '));
            const g = stars(n => {
                picked = picked === n ? 0 : n;            // the same star again clears it
                g.querySelectorAll('.rvw-star').forEach(x => x.classList.toggle('on', +x.dataset.n <= picked));
                ta.focus();
            });
            pick.appendChild(g);
            slot.replaceChildren(pick, ta, bar);
        } else {
            slot.replaceChildren(ta, bar);
        }
        ta.focus();
        return ta;
    }

    // Reject…: a button that opens quick reasons and a text box (optional). opts: {reasons: [..], onReject(reason)}
    function reasonBox(slot, opts) {
        const btn = el('button', 'reels-mini rvw-act pq-reject-btn', 'Reject… ');
        btn.type = 'button';
        btn.append(el('kbd', '', 'R'));
        const open = () => {
            const box = el('div', 'pq-reason');
            const chips = el('div', 'reel-chips');
            (opts.reasons || []).forEach(r => {
                const c = el('button', 'reel-chip', r);
                c.type = 'button';
                c.addEventListener('click', e => { e.stopPropagation(); ta.value = ta.value.trim() ? `${ta.value.trim()}; ${r}` : r; ta.focus(); });
                chips.appendChild(c);
            });
            const ta = el('textarea', 'pq-reason-text');
            ta.rows = 2;
            ta.placeholder = 'Why? (optional: Enter rejects, Shift+Enter a new line, Esc cancels)';
            const go = () => { const r = ta.value.trim(); reset(); opts.onReject(r); };
            const yes = el('button', 'reels-mini pq-reject-btn', 'Reject');
            yes.type = 'button';
            yes.addEventListener('click', e => { e.stopPropagation(); go(); });
            const no = el('button', 'reels-mini', 'Cancel');
            no.type = 'button';
            no.addEventListener('click', e => { e.stopPropagation(); reset(); });
            ta.addEventListener('keydown', e => {
                e.stopPropagation();
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); }
                else if (e.key === 'Escape') { e.preventDefault(); reset(); }
            });
            const bar = el('div', 'rvw-fbbar');
            bar.append(yes, no);
            box.append(chips, ta, bar);
            slot.replaceChildren(box);
            ta.focus();
        };
        const reset = () => slot.replaceChildren(btn);
        btn.addEventListener('click', e => { e.stopPropagation(); if (opts.before) opts.before(); open(); });
        reset();
        return { open: () => { const t = slot.querySelector('textarea'); if (t) t.focus(); else open(); } };
    }

    // keys while a review list shows. isLive(): the list is on the page; row(): the focused row or null;
    // acts: {next, back, play, post, stars(n), postNote, shortlist, reject}; judged(row) → its verdict is in
    function keys(isLive, row, judged, acts) {
        return function onKey(e) {
            if (!isLive() || e.metaKey || e.ctrlKey || e.altKey) return;
            const t = e.target;
            if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
            const k = e.key;
            if (k === 'j' || k === 'J' || k === 'ArrowDown') { e.preventDefault(); acts.next(); return; }
            if (k === 'k' || k === 'K' || k === 'ArrowUp') { e.preventDefault(); acts.back(); return; }
            const r = row();
            if (!r || judged(r)) return;
            if (k === ' ' && t && t.tagName !== 'BUTTON' && t.tagName !== 'VIDEO') { e.preventDefault(); acts.play(r); }
            else if (k === 'p' || k === 'P') { e.preventDefault(); acts.post(r); }
            else if (k >= '1' && k <= '5') { e.preventDefault(); acts.stars(r, +k); }
            else if (k === 'f' || k === 'F') { e.preventDefault(); acts.postNote(r); }
            else if (k === 's' || k === 'S') { e.preventDefault(); acts.shortlist(r); }
            else if (k === 'r' || k === 'R') { e.preventDefault(); acts.reject(r); }
        };
    }

    function keyHelp() {
        const keys = el('p', 'reels-how rvw-keys');
        [['J', 'next'], ['K', 'back'], ['Space', 'play'], ['P', 'Post'], ['1–5', 'Post with stars'],
         ['F', 'Post + stars + note'], ['S', 'Shortlist…'], ['R', 'Reject…']].forEach(([k, w]) => {
            keys.append(el('kbd', '', k), document.createTextNode(' ' + w + '  '));
        });
        return keys;
    }

    function copy(text, said, what) {
        const done = n => { said.textContent = n; setTimeout(() => { said.textContent = ''; }, 6000); };
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
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(() => done(what || 'Copied.'), fallback);
        } else fallback();
    }

    return { el, stars, noteBox, reasonBox, keys, keyHelp, copy, starText };
})();
