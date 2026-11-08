/*
 * Posts ▸ Reels ▸ Music pool (localhost only, owner 2026-11-08): "a section… in reels tab of posts page that tracks my
 * active music pool for reels and lets me add or remove from it".
 *
 * The pool is the list of songs NEW reels are cut to: the video project's montage batches, the bot's word requests on
 * the NAS and the day reels all pick from it. Taking a song out only stops new reels using it; it stays in the music
 * library and can be added back. (Not the reel cards' "Delete from music library", which takes the file out for good.)
 *
 * State: "reel_music_pool" = {items: [{id, tid, title, label, status, why, added, applied, source}], removed: [...,
 * removed], updated, synced} in the Posts document (kept by functions/api/posts.ts and post.py's mirror like the
 * queue). Ids: "library/<track id>" or "review/<folder>/<file>". A line he adds or takes out is "waiting" until the
 * video project's tools/reelbot/queue_sync.py on the Mac applies it, at the next sync (run at the start of any reels
 * request): then "in" / "out", or "failed" with a plain reason.
 *
 * The lists come from web/phone/reels/music.json (local-only, written by the video project's tools/vlog/music_pool.py:
 * each pool song's length, sections and how many reels use it; the library songs that may be added; the songs taken
 * out with the queued reels still cut to them) and from the local helper (tools/reels_local.py): the review folders
 * read live (~/Downloads/reel-song-candidates-*) and the play button's audio, streamed from the Mac, never copied
 * into web/.
 */
window.PostsMusic = (function () {
    'use strict';

    const HOST = location.hostname;
    const API = `http://${HOST === 'localhost' ? '127.0.0.1' : HOST}:8791`;
    const OPEN_KEY = 'reels_music_open';
    const SHOWN = 30;
    let doc = null;
    let man = null;
    let review = null;              // the helper's live list, or null (then the manifest's)
    let panelEl = null;
    let timer = null;
    let chain = Promise.resolve();
    let query = '';
    let showAllRemoved = false;
    let player = null;              // {audio, id}
    let note = '';

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }
    const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
    const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } };
    const norm = s => String(s || '').toLowerCase().replace(/_/g, ' ').replace(/\s+/g, ' ').trim();

    function fmtDur(s) {
        if (!s && s !== 0) return '';
        s = Math.round(s);
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }
    function fmtDay(iso) {
        if (!iso) return '';
        try { return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); } catch (e) { return ''; }
    }
    const name = r => (r.artist ? `${r.artist} — ${r.title}` : (r.title || r.name || r.id));

    // ── loading ──
    async function fetchDoc() {
        const r = await fetch('/api/posts', { cache: 'no-store' });
        if (!r.ok) throw new Error('posts unavailable (' + r.status + ')');
        doc = await r.json();
        return doc;
    }
    async function fetchManifest() {
        try {
            const r = await fetch('/phone/reels/music.json', { cache: 'no-store' });
            const d = r.ok ? await r.json() : null;
            man = d && d.schema === 'music-pool.v1' ? d : null;
        } catch (e) { man = null; }
        return man;
    }
    async function fetchReview() {
        try {
            const r = await fetch(`${API}/reels/music/review`, { cache: 'no-store' });
            review = r.ok ? (await r.json()).files : null;
        } catch (e) { review = null; }
        return review;
    }

    // the page's lines: the stored list, or (before the Mac's first sync) the manifest's pool, all in
    function pool() {
        const p = doc && doc.reel_music_pool;
        if (p && Array.isArray(p.items)) return { items: p.items, removed: Array.isArray(p.removed) ? p.removed : [] };
        const items = ((man && man.pool) || []).map(s => ({ id: s.id, tid: s.tid, title: name(s), label: s.label || undefined,
            status: 'in', source: 'seed' }));
        return { items, removed: [] };
    }
    const KEEP = ['id', 'tid', 'title', 'label', 'status', 'why', 'added', 'removed', 'applied', 'source'];
    const clean = l => Object.fromEntries(KEEP.filter(k => l[k] !== undefined && l[k] !== null).map(k => [k, l[k]]));

    // the manifest's row for a page line (by id, then by library id)
    function info(line) {
        if (!man) return null;
        const rows = [...(man.pool || []), ...(man.removed || []), ...(man.library || [])];
        return rows.find(r => r.id === line.id) || (line.tid ? rows.find(r => r.tid === line.tid) : null) ||
            (man.review || []).find(r => r.id === line.id) || null;
    }

    // fn(mp) changes the pool lines in place; re-applied to a fresh copy if the document moved on meanwhile (409)
    function change(fn) {
        const run = async () => {
            if (!doc) await fetchDoc();
            const apply = () => {
                const p = pool();
                const mp = Object.assign({}, doc.reel_music_pool || {});
                mp.items = p.items.map(i => clean(i));
                mp.removed = p.removed.map(i => clean(i));
                fn(mp);
                mp.removed = mp.removed.slice(0, 500);
                mp.updated = new Date().toISOString();
                doc.reel_music_pool = mp;
            };
            apply();
            const put = () => fetch('/api/posts', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                // no settings and no queue: the API keeps the stored ones; posts as read with this version
                body: JSON.stringify({ baseVersion: doc.version, posts: doc.posts || [], reel_music_pool: doc.reel_music_pool })
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
        chain = chain.then(run).then(() => redraw(), err => { alert('Music pool: ' + err.message); redraw(); });
        return chain;
    }

    const now = () => new Date().toISOString();

    function add(row, source) {
        const id = row.in_library ? `library/${row.in_library}` : row.id;
        const tid = row.in_library || row.tid;
        note = `Added ${name(row)}: waiting for the Mac`;
        return change(mp => {
            if (mp.items.some(i => i.id === id || (tid && i.tid === tid))) return;
            mp.removed = mp.removed.filter(r => r.id !== id && !(tid && r.tid === tid));
            mp.items.push(clean({ id, tid, title: name(row).slice(0, 300), status: 'waiting', added: now(), source }));
        });
    }

    function takeOut(line) {
        note = '';
        return change(mp => {
            mp.items = mp.items.filter(i => i.id !== line.id);
            if (line.status === 'waiting' || (line.status === 'failed' && !line.label)) return;   // never was in
            mp.removed = mp.removed.filter(r => r.id !== line.id);
            mp.removed.unshift(clean(Object.assign({}, line, { status: 'waiting', removed: now(), why: undefined,
                applied: undefined, added: undefined })));
        });
    }

    function addBack(line) {
        note = '';
        return change(mp => {
            mp.removed = mp.removed.filter(r => r.id !== line.id);
            if (!mp.items.some(i => i.id === line.id)) {
                mp.items.push(clean(Object.assign({}, line, { status: 'waiting', added: now(), removed: undefined,
                    why: undefined, applied: undefined })));
            }
        });
    }

    function retry(line) {
        return change(mp => {
            const it = mp.items.find(i => i.id === line.id);
            if (it) { it.status = 'waiting'; it.added = now(); delete it.why; delete it.applied; }
        });
    }

    // ── listening ──
    function playButton(id, label) {
        const b = el('button', 'reels-mini pm-play', player && player.id === id && !player.audio.paused ? '❚❚' : '▶');
        b.type = 'button';
        b.title = 'Listen to ' + label;
        b.addEventListener('click', () => {
            if (player && player.id === id) {
                if (player.audio.paused) player.audio.play().catch(() => {}); else player.audio.pause();
                redraw();
                return;
            }
            if (player) player.audio.pause();
            const audio = new Audio(`${API}/reels/music/play?ref=${encodeURIComponent(id)}`);
            player = { audio, id };
            audio.addEventListener('ended', redraw);
            audio.addEventListener('pause', redraw);
            audio.addEventListener('error', () => {
                note = 'Can\'t play that one: the file isn\'t there, or the local helper is older than this page ' +
                    '(restart ./serve.sh) or not running';
                player = null;
                redraw();
            });
            audio.play().then(redraw, () => {});
        });
        return b;
    }

    // ── drawing ──
    function statusLine(line) {
        if (line.status === 'waiting') return el('div', 'pq-status wait', 'Waiting for the Mac to add it (at the next sync)');
        if (line.status === 'failed') return el('div', 'pq-status bad', 'Couldn\'t add it: ' + (line.why || 'no reason given'));
        return null;
    }

    function poolRow(line) {
        const r = info(line) || {};
        const row = el('div', 'pq-row pm-row' + (line.status === 'in' ? '' : ' pm-' + line.status));
        const main = el('div', 'pq-main');
        const head = el('div', 'pm-head');
        head.appendChild(playButton(line.id, line.title || name(r)));
        head.appendChild(el('span', 'pm-label', line.label || r.label || ''));
        head.appendChild(el('span', 'pq-title', line.title || name(r)));
        main.appendChild(head);
        const meta = [r.duration ? fmtDur(r.duration) : null,
            r.uses !== undefined && r.uses !== null ? (r.uses === 1 ? 'used in 1 reel' : `used in ${r.uses} reels`) : null,
            r.a && r.a !== '—' ? `A ${r.a}` : null, r.b && r.b !== '—' ? `B ${r.b}` : null,
            r.sections_from === 'measured' ? 'sections measured by the Mac: check by ear' : null].filter(Boolean);
        if (meta.length) main.appendChild(el('div', 'pm-meta', meta.join(' · ')));
        const st = statusLine(line);
        if (st) main.appendChild(st);
        row.appendChild(main);
        const btns = el('div', 'pq-btns');
        if (line.status === 'failed') {
            const again = el('button', 'reels-mini', 'Try again');
            again.type = 'button';
            again.addEventListener('click', () => retry(line));
            btns.appendChild(again);
        }
        const rm = el('button', 'reels-mini', line.status === 'in' ? 'Remove from pool' : 'Cancel');
        rm.type = 'button';
        rm.title = line.status === 'in'
            ? 'New reels stop using it. It stays in your music library; you can add it back.'
            : 'Don\'t add it';
        rm.addEventListener('click', () => takeOut(line));
        btns.appendChild(rm);
        row.appendChild(btns);
        return row;
    }

    function removedRow(line) {
        const r = info(line) || {};
        const row = el('div', 'pq-row pm-row pm-out');
        const main = el('div', 'pq-main');
        const head = el('div', 'pm-head');
        head.appendChild(playButton(line.id, line.title || name(r)));
        head.appendChild(el('span', 'pm-label', line.label || r.label || ''));
        head.appendChild(el('span', 'pq-title', line.title || name(r)));
        main.appendChild(head);
        main.appendChild(el('div', 'pq-status ' + (line.status === 'waiting' ? 'wait' : 'done'),
            line.status === 'waiting' ? 'Waiting for the Mac to take it out (at the next sync)'
                : `Out of the pool${line.removed ? ' since ' + fmtDay(line.removed) : ''}` +
                  (line.why ? ` (${line.why})` : '') + '. Still in your music library.'));
        const q = (r.queued || []);
        if (q.length) {
            const w = el('details', 'pm-queued');
            w.appendChild(el('summary', '', q.length === 1 ? '1 queued reel still uses it (not made yet)'
                : `${q.length} queued reels still use it (not made yet)`));
            const ul = el('ul');
            q.slice(0, 40).forEach(x => ul.appendChild(el('li', '', `${x.name} · ${x.where}${x.state === 'being made' ? ' · being made now' : ''}`)));
            if (q.length > 40) ul.appendChild(el('li', '', `… and ${q.length - 40} more`));
            w.appendChild(ul);
            w.appendChild(el('p', 'reels-how', 'They keep this song unless you say otherwise: nothing is re-cut or taken out of a queue by itself.'));
            main.appendChild(w);
        }
        row.appendChild(main);
        const btns = el('div', 'pq-btns');
        const back = el('button', 'reels-mini', line.status === 'waiting' ? 'Undo' : 'Add back');
        back.type = 'button';
        back.addEventListener('click', () => addBack(line));
        btns.appendChild(back);
        row.appendChild(btns);
        return row;
    }

    function addRow(r, source, extra) {
        const row = el('div', 'pq-row pm-row');
        const main = el('div', 'pq-main');
        const head = el('div', 'pm-head');
        head.appendChild(playButton(r.in_library ? `library/${r.in_library}` : r.id, name(r)));
        head.appendChild(el('span', 'pq-title', name(r)));
        main.appendChild(head);
        const meta = [r.duration ? fmtDur(r.duration) : null, r.sung ? 'sung' : null, extra].filter(Boolean);
        if (meta.length) main.appendChild(el('div', 'pm-meta', meta.join(' · ')));
        row.appendChild(main);
        const btns = el('div', 'pq-btns');
        const b = el('button', 'reels-mini pm-add', 'Add');
        b.type = 'button';
        b.title = source === 'review' && !r.in_library
            ? 'Into the pool: the Mac moves it into your music library (out of Downloads), measures it and works out its sections'
            : 'Into the pool: new reels can use it';
        b.addEventListener('click', () => add(r, source));
        btns.appendChild(b);
        row.appendChild(btns);
        return row;
    }

    // a song held back in the library (reserved) or deleted from it: never offered
    function held(fileName) {
        const n = norm(String(fileName || '').replace(/\.[a-z0-9]+$/i, ''));
        return ((man && man.held_patterns) || []).some(p => n.includes(norm(p))) ||
            ((man && man.rejected) || []).some(x => x && (n === x || n.startsWith(x.replace(/[. ]+$/, ''))));
    }

    function draw() {
        const out = document.createDocumentFragment();
        const sec = el('section', 'pq pm');
        const p = pool();
        const open = lsGet(OPEN_KEY, true);
        const nIn = p.items.filter(i => i.status === 'in').length;
        const nWait = p.items.filter(i => i.status === 'waiting').length + p.removed.filter(i => i.status === 'waiting').length;
        const h = el('button', 'pq-h pm-toggle', `${open ? '▾' : '▸'} Music pool for reels (${nIn} songs` +
            `${nWait ? `, ${nWait} change${nWait > 1 ? 's' : ''} waiting for the Mac` : ''})`);
        h.type = 'button';
        h.addEventListener('click', () => { lsSet(OPEN_KEY, !open); redraw(); });
        sec.appendChild(h);
        if (!open) { out.appendChild(sec); return out; }
        sec.appendChild(el('p', 'reels-how',
            'The songs new reels are cut to: the montage batches, the bot\'s reels from your words and the day reels ' +
            'all pick from these. Removing a song only stops new reels using it: it stays in your music library and ' +
            'you can add it back. Changes are made on the Mac at the next sync (run at the start of any reels request).'));
        if (!man) {
            sec.appendChild(el('p', 'pq-status warn', 'The song details aren\'t here yet: the Mac writes them ' +
                '(web/phone/reels/music.json) at the next sync.'));
        }
        if (note) sec.appendChild(el('p', 'pq-status ok', note));

        const list = el('div', 'pq-list');
        if (!p.items.length) list.appendChild(el('p', 'reels-empty', 'No songs in the pool.'));
        p.items.forEach(line => list.appendChild(poolRow(line)));
        sec.appendChild(list);

        // taken out: newest first, with Add back and the queued reels still cut to each
        if (p.removed.length) {
            const rs = el('div', 'pm-removed');
            rs.appendChild(el('div', 'pm-sub', `Taken out (${p.removed.length})`));
            (showAllRemoved ? p.removed : p.removed.slice(0, 8)).forEach(line => rs.appendChild(removedRow(line)));
            if (p.removed.length > 8) {
                const more = el('button', 'reels-mini pq-all', showAllRemoved ? 'Show the newest only' : `Show all ${p.removed.length}`);
                more.type = 'button';
                more.addEventListener('click', () => { showAllRemoved = !showAllRemoved; redraw(); });
                rs.appendChild(more);
            }
            sec.appendChild(rs);
        }

        // add a song: the review folders first, then a search over the library
        const taken = new Set(p.items.flatMap(i => [i.id, i.tid && `library/${i.tid}`]).filter(Boolean));
        const addBox = el('div', 'pm-addbox');
        addBox.appendChild(el('div', 'pm-sub', 'Add a song'));
        const libByName = new Map(((man && man.library) || []).map(r => [String(r.file_name || '').toLowerCase(), r.tid]));
        const poolNames = new Set(((man && man.pool) || []).map(r => String(r.file_name || '').toLowerCase()));
        const revSrc = review || (man && man.review) || [];
        const rev = revSrc.map(r => {
            const m = (man && man.review || []).find(x => x.id === r.id) || {};
            const inLib = r.in_library || m.in_library || libByName.get(String(r.name || '').toLowerCase()) || null;
            return Object.assign({ artist: m.artist, title: m.title }, r, { in_library: inLib });
        }).filter(r => !held(r.name) && !poolNames.has(String(r.name || '').toLowerCase()) &&
            !taken.has(r.id) && !(r.in_library && taken.has(`library/${r.in_library}`)));
        const rb = el('div', 'pm-review');
        rb.appendChild(el('div', 'pm-sub2', `Waiting for your review (${rev.length})`));
        rb.appendChild(el('p', 'reels-how', 'Songs in your Downloads folders named reel-song-candidates-… (folders inside ' +
            'them too). Listen, then Add: the Mac moves the song into your music library (no copy is left in ' +
            'Downloads), measures it and works out its sections, so the reel builders can use it.' +
            (review ? '' : ' (This list is from the last sync: for a live one, the local helper must be running and ' +
                'up to date: restart ./serve.sh.)')));
        if (!rev.length) rb.appendChild(el('p', 'reels-empty', 'Nothing waiting.'));
        rev.forEach(r => {
            if (!r.title) { r.title = String(r.name || '').replace(/\.[a-z0-9]+$/i, ''); }
            rb.appendChild(addRow(r, 'review', r.in_library ? 'already in your library' : r.folder));
        });
        addBox.appendChild(rb);

        const sb = el('div', 'pm-search');
        sb.appendChild(el('div', 'pm-sub2', 'From your music library'));
        const input = el('input', 'pq-reason-text pm-q');
        input.type = 'search';
        input.placeholder = 'Search by song or artist…';
        input.value = query;
        input.dataset.keep = 'pm-q';
        input.addEventListener('input', () => { query = input.value; redrawResults(); });
        sb.appendChild(input);
        const results = el('div', 'pm-results');
        sb.appendChild(results);
        const redrawResults = () => {
            results.replaceChildren();
            const lib = ((man && man.library) || []).filter(r => !taken.has(r.id) && !held(r.file_name));
            const q = norm(query);
            if (!q) {
                results.appendChild(el('p', 'reels-how', `${lib.length} songs you can add. Type to find one.`));
                return;
            }
            const hits = lib.filter(r => norm(`${r.artist} ${r.title} ${r.file_name}`).includes(q));
            if (!hits.length) results.appendChild(el('p', 'reels-empty', 'No song in the library matches.'));
            hits.slice(0, SHOWN).forEach(r => results.appendChild(addRow(r, 'library')));
            if (hits.length > SHOWN) results.appendChild(el('p', 'reels-how', `${hits.length - SHOWN} more: type more of the name.`));
        };
        redrawResults();
        addBox.appendChild(sb);
        sec.appendChild(addBox);
        out.appendChild(sec);
        return out;
    }

    function redraw() {
        if (!panelEl || !panelEl.isConnected) { clearInterval(timer); timer = null; return; }
        const a = document.activeElement;
        const focus = a && panelEl.contains(a) && a.dataset && a.dataset.keep ? a.dataset.keep : null;
        const pos = focus && a.selectionStart;
        panelEl.replaceChildren(draw());
        if (focus) {
            const t = panelEl.querySelector(`[data-keep="${focus}"]`);
            if (t) { t.focus(); try { t.setSelectionRange(pos, pos); } catch (e) { /* not a text field */ } }
        }
    }

    // The section for the Reels tab: drawn at once, refreshed from the server and the Mac's files, then every 30 s
    // (the Mac's marks arrive on their own after a sync).
    function panel() {
        panelEl = el('div', 'pm-wrap');
        if (doc || man) panelEl.appendChild(draw());          // the last copy at once, then fresh
        else panelEl.appendChild(el('p', 'reels-how pq', 'Loading the music pool…'));
        const refresh = (first) => {
            const typing = document.activeElement && panelEl.contains(document.activeElement) && !first;
            if (typing) return;
            Promise.all([fetchDoc().catch(() => null), fetchManifest(), fetchReview()]).then(redraw, redraw);
        };
        refresh(true);
        clearInterval(timer);
        timer = setInterval(() => refresh(false), 30000);
        return panelEl;
    }

    return { panel };
})();
