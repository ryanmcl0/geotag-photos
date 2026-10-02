/**
 * Home page Galleries reel: a slowly drifting, endlessly looping 3D coverflow of
 * gallery covers (same card placement as js/highlights.js). The whole reel is one
 * link to the Galleries page.
 *
 * Hovering hands control to the visitor: a sideways trackpad swipe (or a finger
 * swipe on touch screens) moves the reel at their pace and it settles on the
 * nearest gallery; the drift resumes once the pointer leaves. Vertical scrolling
 * is left alone so the page never gets stuck under the pointer.
 *
 * Galleries are the trips from trips/index.json (public ones, or all of them once
 * unlocked), newest first, with their covers from collections/gallery_covers.json.
 * A trip without a pinned cover picks a landscape frame from its manifest, like
 * the Galleries index does.
 */
(function () {
    const reel = document.getElementById('gallery-reel');
    if (!reel) return;
    const stage = reel.querySelector('.gallery-reel-stage');
    const caption = reel.querySelector('.gallery-reel-caption');
    const countEl = reel.querySelector('.gallery-reel-count');

    const H = location.hostname;
    const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(H) || H.endsWith('.local') ||
        /^10\./.test(H) || /^192\.168\./.test(H) || /^172\.(1[6-9]|2\d|3[01])\./.test(H);
    const PB = LOCAL ? 'trips' : 'photos';

    const SPEED = 0.22;        // cards per second of drift
    const VISIBLE = 4.2;       // cards either side of the focus that are drawn
    const SWIPE_PX = 240;      // horizontal wheel/drag distance that moves one card
    const EASE = 0.18;         // how quickly the reel catches up with a swipe
    const SETTLE_MS = 160;     // pause after the last swipe before snapping to a card
    const TOUCH_HOLD_MS = 2500;  // after a finger swipe, wait this long before drifting again
    const REDUCED = window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let cards = [], trips = [], cardW = 400;
    // cur is the rendered position and target where it is heading; neither wraps
    // (rendering takes them modulo the card count), so easing never jumps.
    let cur = 0, target = 0, last = 0, onScreen = true;
    let hovering = false, touching = false, touchHoldUntil = 0, settleTimer = 0;

    const unlocked = () => (window.Unlock ? window.Unlock.unlocked() : false);
    const displayName = name => (name || '').replace(/^\d{4}[:\d]*\s+/, '');
    const tripYear = t => {
        const m = (t.name || '').match(/^(\d{4})/);
        return m ? m[1] : (t.year || '');
    };
    const coverUrl = ref => `${PB}/${ref.trip}/display/${encodeURIComponent(ref.id)}.webp`;

    Promise.all([
        fetch(`trips/index.json?t=${Date.now()}`).then(r => r.json()),
        fetch(`collections/gallery_covers.json?t=${Date.now()}`)
            .then(r => (r.ok ? r.json() : {})).catch(() => ({})),
    ]).then(([index, covers]) => build(index.trips || [], covers)).catch(() => {});

    function build(all, covers) {
        const open = unlocked();
        trips = all
            // skip galleries with nothing viewable (e.g. every photo blocked by people privacy)
            .filter(t => !t.pending && (open || t.public !== false) &&
                         (t.photo_count_all ?? t.photo_count ?? 1) > 0)
            .sort((a, b) => ((b.dates && b.dates.start) || '').localeCompare((a.dates && a.dates.start) || ''));
        if (!trips.length) return;
        if (countEl) countEl.textContent = `${trips.length} galleries`;

        cards = trips.map(t => {
            const fig = document.createElement('figure');
            fig.className = 'gallery-reel-card';
            const img = document.createElement('img');
            img.alt = '';
            img.decoding = 'async';
            img.onerror = () => { fig.classList.add('is-empty'); };
            fig.appendChild(img);
            stage.appendChild(fig);
            return { fig, img, trip: t, cover: covers[t.id] || null, loaded: false };
        });
        reel.classList.add('is-ready');

        layout();
        window.addEventListener('resize', () => {
            cancelAnimationFrame(layout._raf);
            layout._raf = requestAnimationFrame(layout);
        });
        initControls();
        if ('IntersectionObserver' in window) {
            new IntersectionObserver(es => { onScreen = es[0].isIntersecting; })
                .observe(reel);
        }
        requestAnimationFrame(tick);
    }

    const manual = () => hovering || touching || performance.now() < touchHoldUntil;

    function settleSoon() {
        clearTimeout(settleTimer);
        settleTimer = setTimeout(() => { target = Math.round(target); }, SETTLE_MS);
    }

    function initControls() {
        reel.addEventListener('mouseenter', () => { hovering = true; target = cur; });
        reel.addEventListener('mouseleave', () => { hovering = false; });

        // Trackpad: sideways swipes drive the reel (and don't trigger the browser's
        // back/forward gesture); mostly-vertical ones scroll the page as usual.
        reel.addEventListener('wheel', e => {
            if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
            e.preventDefault();
            hovering = true;
            target += e.deltaX / SWIPE_PX;
            settleSoon();
        }, { passive: false });

        // Touch: drag sideways to move it; a drag is never treated as a tap.
        let startX = 0, startTarget = 0, dragged = false;
        reel.addEventListener('pointerdown', e => {
            if (e.pointerType !== 'touch') return;
            touching = true; dragged = false;
            startX = e.clientX; startTarget = target = cur;
        });
        reel.addEventListener('pointermove', e => {
            if (!touching) return;
            const dx = e.clientX - startX;
            if (Math.abs(dx) > 8) dragged = true;
            target = startTarget - dx / (cardW * 0.62);
        });
        const endTouch = () => {
            if (!touching) return;
            touching = false;
            touchHoldUntil = performance.now() + TOUCH_HOLD_MS;
            target = Math.round(target);
        };
        reel.addEventListener('pointerup', endTouch);
        reel.addEventListener('pointercancel', endTouch);
        reel.addEventListener('click', e => {
            if (dragged) { e.preventDefault(); dragged = false; }
        }, true);
    }

    // Covers load only as they come within reach of the focus.
    function ensureImage(c) {
        if (c.loaded) return;
        c.loaded = true;
        if (c.cover && c.cover.src) { c.img.src = c.cover.src; return; }
        if (c.cover && c.cover.id) { c.img.src = coverUrl({ trip: c.cover.trip || c.trip.id, id: c.cover.id }); return; }
        fetch(`${c.trip.path}/manifest.json?t=${Date.now()}`)
            .then(r => (r.ok ? r.json() : Promise.reject()))
            .then(m => {
                const photos = m.photos || [];
                const land = photos.filter(p => !p.ar || p.ar >= 1.3);
                const pool = land.length ? land : photos;
                const pick = pool[Math.floor(pool.length / 2)];
                if (pick) c.img.src = coverUrl({ trip: c.trip.id, id: pick.id });
                else c.fig.classList.add('is-empty');
            })
            .catch(() => c.fig.classList.add('is-empty'));
    }

    function layout() {
        const h = stage.clientHeight || 360;
        cardW = Math.min(h * 0.86 * 1.5, stage.clientWidth * 0.62);
        const cardH = cardW / 1.5;
        cards.forEach(c => {
            c.fig.style.width = cardW.toFixed(1) + 'px';
            c.fig.style.height = cardH.toFixed(1) + 'px';
        });
    }

    // Coverflow placement for a card d slots from the focus (from highlights.js):
    // first neighbours peek beside the centre card, farther ones stack away.
    function place(fig, d, w) {
        const ad = Math.abs(d), sg = Math.sign(d);
        if (ad > VISIBLE) { fig.style.visibility = 'hidden'; return; }
        fig.style.visibility = 'visible';
        const x = sg * (Math.min(ad, 1) * 0.62 + Math.max(0, ad - 1) * 0.24) * w;
        const z = -(Math.min(ad, 1) * 190 + Math.max(0, ad - 1) * 95);
        const ry = -sg * Math.min(ad, 1.5) * 38;
        const sc = Math.max(0.7, 1 - Math.min(ad, 1) * 0.06 - Math.max(0, ad - 1) * 0.06);
        fig.style.transform = 'translate(-50%, -50%)' +
            ` translate3d(${x.toFixed(1)}px, 0, ${z.toFixed(1)}px)` +
            ` rotateY(${ry.toFixed(2)}deg) scale(${sc.toFixed(3)})`;
        fig.style.opacity = Math.max(0, Math.min(1, 1 - Math.max(0, ad - 1.3) * 0.4)).toFixed(3);
        fig.style.zIndex = String(200 - Math.round(ad * 10));
    }

    let shown = -1;
    function tick(now) {
        const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
        last = now;
        const n = cards.length;
        if (manual()) {
            cur += (target - cur) * EASE;
            if (Math.abs(target - cur) < 0.0005) cur = target;
        } else if (!REDUCED && onScreen && !document.hidden) {
            cur += SPEED * dt;
            target = cur;
        }

        cards.forEach((c, i) => {
            // shortest way round the loop, so the reel wraps seamlessly
            let d = i - cur;
            d -= Math.round(d / n) * n;
            if (Math.abs(d) <= VISIBLE + 1) ensureImage(c);
            place(c.fig, d, cardW);
        });

        const focus = ((Math.round(cur) % n) + n) % n;
        if (focus !== shown && caption) {
            shown = focus;
            const t = trips[focus];
            caption.textContent = `${displayName(t.name)} · ${tripYear(t)}`;
        }
        requestAnimationFrame(tick);
    }
})();
