/**
 * Galleries index (galleries.html): one tile per trip, grouped by year, each
 * opening that trip's photo gallery (gallery.html?trip=<id>&year=YYYY). Trip
 * list comes from trips/index.json; cover images are loaded lazily from each
 * trip's manifest as its tile scrolls into view (mirrors the China hub).
 */
(function () {
    'use strict';

    const BASE = '';

    // Per-trip cover overrides (config/tile_covers.json → gallery_covers.json),
    // keyed by trip id. Trips without an entry auto-pick from their manifest.
    let COVERS = {};

    function unlocked() {
        return window.Unlock ? window.Unlock.unlocked() : false;
    }

    // Mirror sidebar.js / gallery-page.js: strip the internal "YYYY:MM " /
    // "YYYY " folder prefix that isn't meant as a display label.
    function formatTripName(name) {
        return (name || '').replace(/^\d{4}[:\d]*\s+/, '');
    }

    function tripYear(trip) {
        const m = (trip.name || '').match(/^(\d{4})/);
        return m ? parseInt(m[1], 10)
                 : (trip.year || new Date(trip.dates.start).getFullYear());
    }

    function el(tag, cls, html) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (html != null) n.innerHTML = html;
        return n;
    }

    // Pick a cover photo from a trip manifest: prefer a landscape frame near the
    // middle of the trip, falling back to the first photo.
    function pickCover(photos) {
        if (!photos || !photos.length) return null;
        const landscape = photos.filter(p => !p.ar || p.ar >= 1.3);
        const pool = landscape.length ? landscape : photos;
        return pool[Math.floor(pool.length / 2)] || photos[0];
    }

    // Fetch a trip's manifest once its tile is near the viewport, then set the
    // cover image. One observer drives every tile.
    function makeCoverLoader() {
        const io = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (!entry.isIntersecting) return;
                const card = entry.target;
                io.unobserve(card);
                loadCover(card);
            });
        }, { rootMargin: '200px' });
        return io;
    }

    async function loadCover(card) {
        const img = card.querySelector('.tile-img');
        const path = card.dataset.path;
        const tripId = card.dataset.tripId;
        if (!img) return;

        // Pinned cover from config wins — no manifest fetch needed. Tiles render
        // ~300–900px wide, so use the 2160px display webp (like the China hub),
        // not the 400px thumbnail, or covers look soft/upscaled.
        const pinned = COVERS[tripId];
        if (pinned && pinned.src) { img.src = `${BASE}${pinned.src}`; return; }
        if (pinned && pinned.id) {
            img.src = Gallery.photoUrl({ trip: pinned.trip || tripId, id: pinned.id }, 'display');
            return;
        }

        if (!path) return;
        try {
            const res = await fetch(`${BASE}${path}/manifest.json?t=${Date.now()}`);
            if (!res.ok) throw new Error(`manifest ${res.status}`);
            const manifest = await res.json();
            const cover = pickCover(manifest.photos);
            if (!cover) { Gallery.lockedCover(img); return; }
            img.src = Gallery.photoUrl({ trip: tripId, id: cover.id }, 'display');
        } catch (e) {
            Gallery.lockedCover(img);
        }
    }

    function buildTile(trip) {
        const year = tripYear(trip);

        // Placeholder ("Photos pending") trip: visited but not edited yet, no gallery to
        // link to. Render the shared pending tile (matches china.js) — non-clickable.
        if (trip.pending) {
            const card = el('div', 'tile tile--pending');
            card.dataset.tripId = trip.id;
            card.innerHTML = `
                <div class="tile-inner">
                    <div class="tile-title">${formatTripName(trip.name)}</div>
                    <div class="pending-tag">Photos pending</div>
                </div>
            `;
            return card;
        }

        const href = `${BASE}gallery.html?trip=${encodeURIComponent(trip.id)}&year=${year}`;
        const count = trip.photo_count
            ? `${trip.photo_count} photo${trip.photo_count === 1 ? '' : 's'}`
            : '';

        const card = el('a', 'tile');
        card.href = href;
        card.dataset.tripId = trip.id;
        card.dataset.path = trip.path;
        card.innerHTML = `
            <img class="tile-img" loading="lazy" alt="">
            ${trip.public === false ? '<div class="lock-badge">🔒 Private</div>' : ''}
            <div class="tile-overlay">
                <div class="tile-title">${formatTripName(trip.name)}</div>
                <div class="tile-sub">${count}</div>
            </div>
        `;
        return card;
    }

    function render(trips) {
        const app = document.getElementById('galleries-app');
        if (!app) return;
        app.innerHTML = '';

        const visible = trips.filter(t => unlocked() || t.public !== false);
        if (!visible.length) {
            app.appendChild(el('p', 'gallery-empty', 'No galleries yet.'));
            return;
        }

        // Newest first: by year, then by start date within a year.
        visible.sort((a, b) => {
            const dy = tripYear(b) - tripYear(a);
            if (dy) return dy;
            return (b.dates.start || '').localeCompare(a.dates.start || '');
        });

        // Group into year sections.
        const byYear = new Map();
        visible.forEach(t => {
            const y = tripYear(t);
            if (!byYear.has(y)) byYear.set(y, []);
            byYear.get(y).push(t);
        });
        const years = [...byYear.keys()].sort((a, b) => b - a);

        // Year filter bar — "All" plus one button per year. Selecting a year
        // shows only that year's section; "All" restores everything.
        const yearbar = el('div', 'yearbar');
        const buttons = [];
        function select(value) {
            buttons.forEach(b => b.classList.toggle('active', b.dataset.year === value));
            app.querySelectorAll('[data-year-section]').forEach(sec => {
                sec.style.display = (value === 'all' || sec.dataset.yearSection === value) ? '' : 'none';
            });
        }
        [['all', 'All'], ...years.map(y => [String(y), String(y)])].forEach(([val, label]) => {
            const btn = el('button', '', label);
            btn.type = 'button';
            btn.dataset.year = val;
            btn.addEventListener('click', () => select(val));
            buttons.push(btn);
            yearbar.appendChild(btn);
        });
        app.appendChild(yearbar);

        const io = makeCoverLoader();
        years.forEach(year => {
            const group = byYear.get(year);
            const section = el('div', 'gallery-year');
            section.dataset.yearSection = String(year);
            section.appendChild(el('div', 'section-head',
                `<h2>${year}</h2><span class="count">${group.length} ${group.length === 1 ? 'trip' : 'trips'}</span>`));
            const grid = el('div', 'tiles tiles--dense');
            group.forEach(trip => {
                const tile = buildTile(trip);
                grid.appendChild(tile);
                if (!trip.pending) io.observe(tile);  // pending tiles have no cover to load
            });
            section.appendChild(grid);
            app.appendChild(section);
        });

        select('all');
    }

    // Sticky year headings sit just below the sticky topnav — keep their offset
    // in sync with the topnav's actual height (changes between desktop/mobile).
    function syncTopnavHeight() {
        const nav = document.querySelector('.topnav');
        if (nav) {
            document.documentElement.style.setProperty('--topnav-h', `${nav.offsetHeight}px`);
        }
    }

    async function init() {
        const app = document.getElementById('galleries-app');
        if (!app) return;
        syncTopnavHeight();
        window.addEventListener('resize', syncTopnavHeight);
        app.innerHTML = '<p class="gallery-loading">Loading galleries…</p>';

        // Pinned per-trip covers (optional — auto-pick if the file is absent).
        try {
            const cRes = await fetch(`${BASE}collections/gallery_covers.json?t=${Date.now()}`);
            if (cRes.ok) COVERS = await cRes.json();
        } catch (e) { /* no overrides → auto-pick everywhere */ }

        try {
            const res = await fetch(`${BASE}trips/index.json?t=${Date.now()}`);
            const data = await res.json();
            render(data.trips || []);
        } catch (e) {
            app.innerHTML = '<p class="gallery-empty">Could not load galleries.</p>';
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
