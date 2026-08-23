/**
 * "Phone photos" companion view for post drafts.
 *
 * A post is a curated set of CAMERA photos; this shows the PHONE photos taken
 * around the same wall-clock time as each selected camera photo (behind-the-
 * scenes shots for the post). Time is the matching key, not GPS: many phone
 * photos have no GPS, and clock proximity is what "same moment" really means.
 *
 * Each phone photo is assigned to its NEAREST camera photo in the post (so a
 * shot never appears twice), capped at +/-24h, and the visible window is
 * user-adjustable with chips (15m ... 24h) because the right bound varies by
 * situation. Timestamps on both sides are local wall-clock (the pipeline's
 * pseudo-Z ISO strings), compared directly.
 *
 * Only activates when the local-only phone library (web/phone/, never
 * deployed) is present — same probe as phone-mode.js, so this code is inert
 * in production. posts.js calls PhoneCompanion.decorateCard(bar, post) on
 * each post card.
 */
window.PhoneCompanion = (function () {
    let availableP = null;
    function available() {
        if (!availableP) {
            availableP = fetch('/phone/trips/index.json', { method: 'HEAD' })
                .then(r => r.ok && (r.headers.get('content-type') || '').includes('json'))
                .catch(() => false);
        }
        return availableP;
    }

    const H = 3600e3;
    const WINDOWS = [
        { label: '±15m', ms: 0.25 * H },
        { label: '±1h', ms: 1 * H },
        { label: '±3h', ms: 3 * H },
        { label: '±12h', ms: 12 * H },
        { label: '±24h', ms: 24 * H },
    ];
    const HARD_CAP = 24 * H;
    const PER_SECTION = 80;
    let windowMs = 3 * H;

    // Both libraries store local wall-clock time with a decorative Z; strip it
    // so the comparison stays wall-clock and browser-timezone-independent.
    const parseTs = s => new Date(s.replace('Z', '')).getTime();

    async function j(url) {
        const r = await fetch(url);
        if (!r.ok) throw new Error(`${r.status} ${url}`);
        return r.json();
    }

    async function collectCameraPhotos(refs) {
        const byTrip = new Map();
        refs.forEach((ref, i) => {
            if (ref.trip.startsWith('phone-')) return;  // already a phone shot
            if (!byTrip.has(ref.trip)) byTrip.set(ref.trip, []);
            byTrip.get(ref.trip).push({ ref, order: i });
        });
        const out = [];
        const tripTimes = new Map();  // trip -> ALL manifest timestamps, for clock-offset estimation
        for (const [trip, list] of byTrip) {
            let idx, all;
            try {
                const m = await j(`trips/${trip}/manifest.json?t=${Date.now()}`);
                idx = new Map(m.photos.map(p => [p.id, p]));
                all = m.photos;
                if (m.filtered && list.some(x => !idx.has(x.ref.id))) {
                    const full = await j(`trips/${trip}/manifest.all.json?t=${Date.now()}`);
                    idx = new Map(full.photos.map(p => [p.id, p]));
                    all = full.photos;
                }
            } catch (e) { continue; }
            tripTimes.set(trip, all.filter(p => p.timestamp).map(p => parseTs(p.timestamp)));
            for (const { ref, order } of list) {
                const p = idx.get(ref.id);
                if (p && p.timestamp) out.push({ ref, order, ts: parseTs(p.timestamp), raw: p.timestamp });
            }
        }
        return { photos: out.sort((a, b) => a.order - b.order), tripTimes };
    }

    /**
     * The camera clock is not reliably on local time (timezone sometimes
     * changed mid-trip, sometimes not at all), while the phone always is.
     * Per camera trip, estimate the constant clock offset that best aligns
     * the trip's photo times with the phone photo times: try half-hour
     * offsets in [-14h, +14h] and score each by how many camera photos then
     * have a phone photo within 20 minutes. Apply only when clearly better
     * than no offset, and surface it in the overlay header.
     */
    function estimateOffset(cameraTs, phoneTs) {
        if (cameraTs.length < 5 || phoneTs.length < 5) return 0;
        const P = [...phoneTs].sort((a, b) => a - b);
        const near = t => {
            let lo = 0, hi = P.length - 1;
            while (lo < hi) {
                const mid = (lo + hi) >> 1;
                if (P[mid] < t) lo = mid + 1; else hi = mid;
            }
            const d1 = Math.abs(P[lo] - t);
            const d0 = lo > 0 ? Math.abs(P[lo - 1] - t) : Infinity;
            return Math.min(d0, d1);
        };
        const TOL = 20 * 60e3;
        const score = o => cameraTs.reduce((n, t) => n + (near(t + o) <= TOL ? 1 : 0), 0) / cameraTs.length;
        const zero = score(0);
        let best = 0, bestScore = zero;
        for (let o = -14 * H; o <= 14 * H; o += 0.5 * H) {
            if (o === 0) continue;
            const sc = score(o);
            if (sc > bestScore) { bestScore = sc; best = o; }
        }
        return (Math.abs(best) >= H && bestScore >= zero + 0.15) ? best : 0;
    }

    async function collectPhonePhotos(cameraPhotos) {
        const idx = await j(`/phone/trips/index.json?t=${Date.now()}`);
        const times = cameraPhotos.map(c => c.ts);
        const out = [];
        for (const t of idx.trips) {
            const start = parseTs(`${t.dates.start}T00:00:00`) - HARD_CAP;
            const end = parseTs(`${t.dates.end}T23:59:59`) + HARD_CAP;
            if (!times.some(ts => ts >= start && ts <= end)) continue;
            let m;
            try { m = await j(`/phone/${t.path}/manifest.json?t=${Date.now()}`); }
            catch (e) { continue; }
            for (const p of m.photos) {
                out.push({ trip: t.id, id: p.id, ar: p.ar, ts: parseTs(p.timestamp) });
            }
            // Videos are indexed but never compressed: the manifest's videos/
            // symlink points straight at the NAS originals.
            for (const v of (m.videos || [])) {
                out.push({ trip: t.id, kind: 'video', file: v.file,
                           bytes: v.bytes, ts: parseTs(v.timestamp),
                           path: t.path });
            }
        }
        return out;
    }

    // Every phone photo goes to its nearest camera photo (within the hard cap),
    // so tightening the window chips never re-shuffles assignments.
    function assign(cameraPhotos, phonePhotos) {
        const matches = new Map(cameraPhotos.map(c => [c, []]));
        for (const p of phonePhotos) {
            let best = null, bestD = Infinity;
            for (const c of cameraPhotos) {
                const d = Math.abs(p.ts - c.effTs);
                if (d < bestD) { bestD = d; best = c; }
            }
            if (best && bestD <= HARD_CAP) {
                matches.get(best).push({ ...p, dt: p.ts - best.effTs, adt: bestD });
            }
        }
        matches.forEach(list => list.sort((a, b) => a.adt - b.adt));
        return matches;
    }

    function fmtDt(dt) {
        const sign = dt < 0 ? '−' : '+';
        const a = Math.abs(dt);
        if (a < H) return `${sign}${Math.round(a / 60e3)}m`;
        return `${sign}${(a / H).toFixed(1)}h`;
    }

    function fmtWhen(ts) {
        const d = new Date(ts);
        return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) +
            ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    }

    function buildOverlay(post, cameraPhotos, matches, offsets) {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;inset:0;z-index:1400;background:rgba(10,10,12,.97);' +
            'overflow-y:auto;padding:20px;color:#eee;font:14px -apple-system,sans-serif';

        const head = document.createElement('div');
        head.style.cssText = 'display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px;' +
            'position:sticky;top:-20px;background:rgba(10,10,12,.97);padding:12px 0;z-index:1';
        const title = document.createElement('h2');
        title.style.cssText = 'margin:0;font-size:17px';
        const summary = document.createElement('span');
        summary.style.cssText = 'opacity:.65';
        const chips = document.createElement('div');
        chips.style.cssText = 'display:flex;gap:6px;margin-left:auto';
        const close = document.createElement('button');
        close.textContent = '✕';
        close.style.cssText = 'background:none;border:1px solid #555;color:#eee;border-radius:6px;' +
            'padding:4px 10px;cursor:pointer;font-size:14px';
        close.addEventListener('click', () => {
            overlay.remove();
            // Adds made from the overlay changed the doc in place; repaint the
            // posts page underneath so the Phone section reflects them.
            if (document.getElementById('posts-app') && window.Posts) Posts.initPostsPage();
        });
        overlay.addEventListener('keydown', e => { if (e.key === 'Escape') overlay.remove(); });

        const body = document.createElement('div');

        function render() {
            let total = 0;
            body.innerHTML = '';
            // Flat list of the currently visible phone PHOTOS across all
            // sections, so the lightbox arrows walk the whole result set.
            const visiblePhotos = [];
            for (const c of cameraPhotos) {
                const list = (matches.get(c) || []).filter(p => p.adt <= windowMs);
                const sec = document.createElement('div');
                sec.style.cssText = 'display:flex;gap:14px;padding:14px 0;border-top:1px solid #2a2a2e';
                const anchor = document.createElement('div');
                anchor.style.cssText = 'flex:0 0 150px';
                const aimg = document.createElement('img');
                aimg.src = Gallery.photoUrl(c.ref, 'thumbnails');
                aimg.loading = 'lazy';
                aimg.style.cssText = 'width:150px;border-radius:6px;display:block';
                const meta = document.createElement('div');
                meta.style.cssText = 'opacity:.65;font-size:12px;margin-top:6px;line-height:1.4';
                meta.textContent = `${c.ref.trip}\n${fmtWhen(c.effTs)}`;
                meta.style.whiteSpace = 'pre-line';
                anchor.append(aimg, meta);

                const row = document.createElement('div');
                row.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;align-content:flex-start';
                if (!list.length) {
                    const none = document.createElement('span');
                    none.style.cssText = 'opacity:.4;align-self:center';
                    none.textContent = 'no phone photos in this window';
                    row.appendChild(none);
                }
                list.slice(0, PER_SECTION).forEach(p => {
                    const cell = document.createElement('a');
                    cell.target = '_blank';
                    cell.style.cssText = 'position:relative;display:block';
                    const addBtn = document.createElement('button');
                    addBtn.type = 'button';
                    addBtn.textContent = '+';
                    addBtn.title = 'Add to this post (Phone section; counts toward the 18 cap on XHS posts)';
                    addBtn.style.cssText = 'position:absolute;top:4px;left:4px;z-index:2;' +
                        'background:rgba(0,0,0,.65);color:#fff;border:none;border-radius:4px;' +
                        'cursor:pointer;font-size:13px;line-height:1;padding:2px 7px';
                    addBtn.addEventListener('click', ev => {
                        ev.preventDefault(); ev.stopPropagation();
                        const ref = p.kind === 'video'
                            ? { trip: p.trip, file: p.file }
                            : { trip: p.trip, id: p.id, ar: p.ar };
                        if (window.Posts && Posts.addToPost) Posts.addToPost(post.id, [ref]);
                        addBtn.textContent = '✓';
                    });
                    if (p.kind === 'video') {
                        cell.href = `/phone/${p.path}/videos/` +
                            p.file.split('/').map(encodeURIComponent).join('/');
                        cell.style.cssText += ';height:110px;width:150px;border-radius:5px;' +
                            'background:#26262c;color:#ddd;display:flex;flex-direction:column;' +
                            'align-items:center;justify-content:center;gap:4px;text-decoration:none';
                        const play = document.createElement('span');
                        play.textContent = '▶';
                        play.style.cssText = 'font-size:26px';
                        const label = document.createElement('span');
                        const mb = p.bytes / 1048576;
                        label.textContent = `${p.file.split('/').pop().slice(0, 18)} · ` +
                            (mb >= 1024 ? `${(mb / 1024).toFixed(1)}GB` : `${Math.round(mb)}MB`);
                        label.style.cssText = 'font-size:10px;opacity:.7;max-width:140px;' +
                            'overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
                        cell.append(play, label);
                    } else {
                        cell.href = Gallery.photoUrl({ trip: p.trip, id: p.id }, 'display');
                        const ref = { trip: p.trip, id: p.id, ar: p.ar };
                        const myIndex = visiblePhotos.length;
                        visiblePhotos.push(ref);
                        // in-page viewer with arrow-key navigation; plain href
                        // still works for cmd/middle-click new-tab
                        cell.addEventListener('click', ev => {
                            if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
                            ev.preventDefault();
                            Gallery.openLightbox(visiblePhotos, myIndex);
                        });
                        const img = document.createElement('img');
                        img.src = Gallery.photoUrl({ trip: p.trip, id: p.id }, 'thumbnails');
                        img.loading = 'lazy';
                        img.style.cssText = 'height:110px;border-radius:5px;display:block';
                        cell.appendChild(img);
                    }
                    cell.appendChild(addBtn);
                    const badge = document.createElement('span');
                    badge.textContent = fmtDt(p.dt);
                    badge.style.cssText = 'position:absolute;bottom:4px;right:4px;background:rgba(0,0,0,.72);' +
                        'border-radius:4px;padding:1px 5px;font-size:11px;color:#fff';
                    cell.appendChild(badge);
                    row.appendChild(cell);
                });
                if (list.length > PER_SECTION) {
                    const more = document.createElement('span');
                    more.style.cssText = 'opacity:.5;align-self:center';
                    more.textContent = `+${list.length - PER_SECTION} more (tighten the window)`;
                    row.appendChild(more);
                }
                total += list.length;
                sec.append(anchor, row);
                body.appendChild(sec);
            }
            title.textContent = `📱 ${post.name}`;
            summary.textContent = `${total} phone photos/videos near ${cameraPhotos.length} camera photos`;
        }

        WINDOWS.forEach(w => {
            const b = document.createElement('button');
            b.textContent = w.label;
            const style = on => 'border-radius:99px;padding:4px 10px;cursor:pointer;font-size:12px;' +
                (on ? 'background:#1d4ed8;border:1px solid #1d4ed8;color:#fff'
                    : 'background:none;border:1px solid #555;color:#ddd');
            b.style.cssText = style(w.ms === windowMs);
            b.addEventListener('click', () => {
                windowMs = w.ms;
                chips.querySelectorAll('button').forEach((btn, i) => btn.style.cssText = style(WINDOWS[i].ms === windowMs));
                render();
            });
            chips.appendChild(b);
        });

        head.append(title, summary, chips, close);
        overlay.append(head);
        if (offsets && offsets.size) {
            const note = document.createElement('p');
            note.style.cssText = 'margin:0 0 10px;font-size:12px;color:#f0b429';
            note.textContent = 'Camera clock offset auto-corrected: ' +
                [...offsets].map(([t, o]) => `${t} ${o > 0 ? '+' : '−'}${Math.abs(o / H).toFixed(1)}h`).join(', ') +
                ' (camera vs phone time cross-correlation)';
            overlay.appendChild(note);
        }
        overlay.append(body);
        render();
        return overlay;
    }

    async function open(post) {
        const { photos: cameraPhotos, tripTimes } = await collectCameraPhotos(post.photos);
        if (!cameraPhotos.length) {
            alert('No camera photos with timestamps in this post.');
            return;
        }
        cameraPhotos.forEach(c => { c.effTs = c.ts; });
        const phonePhotos = await collectPhonePhotos(cameraPhotos);
        const phoneTs = phonePhotos.map(p => p.ts);
        const offsets = new Map();
        for (const [trip, ts] of tripTimes) {
            const o = estimateOffset(ts, phoneTs);
            if (o) offsets.set(trip, o);
        }
        cameraPhotos.forEach(c => { c.effTs = c.ts + (offsets.get(c.ref.trip) || 0); });
        const matches = assign(cameraPhotos, phonePhotos);
        document.body.appendChild(buildOverlay(post, cameraPhotos, matches, offsets));
    }

    function decorateCard(bar, post) {
        available().then(ok => {
            if (!ok || bar.querySelector('.posts-phone-btn')) return;
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'posts-phone-btn';
            btn.textContent = '📱 Phone photos';
            btn.style.cssText = 'background:none;border:1px solid #1d4ed8;color:#7ab8ff;' +
                'border-radius:6px;padding:4px 10px;cursor:pointer;font-size:13px';
            btn.addEventListener('click', () => {
                btn.disabled = true;
                open(post).finally(() => { btn.disabled = false; });
            });
            bar.insertBefore(btn, bar.querySelector('.posts-delete'));
        });
    }

    return { decorateCard };
})();
