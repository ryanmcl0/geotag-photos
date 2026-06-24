/**
 * Trip gallery page (gallery.html): renders one trip's photos as a justified
 * grid + lightbox, reusing the shared Gallery component. Photos come straight
 * from the trip manifest; aspect ratios ('ar') are baked into the manifest, with
 * a client-side measuring fallback if any are missing.
 */
(function () {
    'use strict';

    function checkAllAccess() {
        return document.cookie.split(';').some(c => {
            const t = c.trim();
            return t.startsWith('all_access=') && t.length > 'all_access='.length;
        });
    }

    // Mirror sidebar.js: strip the internal "YYYY:MM " / "YYYY " folder prefix.
    function formatTripName(name) {
        return (name || '').replace(/^\d{4}[:\d]*\s+/, '');
    }

    function formatDateRange(start, end) {
        const opts = { year: 'numeric', month: 'short', day: 'numeric' };
        try {
            const s = new Date(start).toLocaleDateString('en-US', opts);
            if (!end || end === start) return s;
            const e = new Date(end).toLocaleDateString('en-US', opts);
            return `${s} – ${e}`;
        } catch (e) {
            return '';
        }
    }

    function tripYear(trip) {
        if (VIEW_CONFIG.year) return VIEW_CONFIG.year;
        const m = (trip.name || '').match(/^(\d{4})/);
        return m ? parseInt(m[1], 10) : (trip.year || new Date(trip.dates.start).getFullYear());
    }

    function setInfo(name, dates, count) {
        const nameEl = document.getElementById('trip-name');
        const dateEl = document.getElementById('trip-dates');
        const countEl = document.getElementById('photo-count');
        if (nameEl) nameEl.textContent = name;
        if (dateEl) dateEl.textContent = dates || '';
        if (countEl) countEl.textContent = count || '';
    }

    function renderToolbar(trip) {
        const bar = document.getElementById('gallery-toolbar');
        if (!bar) return;
        const slug = trip.id.replace(/-\d{4}$/, '');
        const mapHref = `${VIEW_CONFIG.basePath}${tripYear(trip)}/${slug}/index.html`;
        bar.innerHTML = `
            <span class="gallery-title">${formatTripName(trip.name)}</span>
            <span class="gallery-switch">
                <a href="${mapHref}">Map</a>
                <a href="${location.href}" class="active" aria-current="page">Gallery</a>
            </span>
        `;
    }

    // Fill in any missing aspect ratios by measuring the thumbnails, so the
    // justified layout stays correct even if the manifest predates 'ar'.
    function ensureAspectRatios(photos) {
        const missing = photos.filter(p => !p.ar);
        if (!missing.length) return Promise.resolve();
        return Promise.all(missing.map(p => new Promise(resolve => {
            const img = new Image();
            img.onload = () => {
                if (img.naturalHeight) p.ar = +(img.naturalWidth / img.naturalHeight).toFixed(3);
                resolve();
            };
            img.onerror = () => resolve();
            img.src = Gallery.photoUrl(p, 'thumbnails');
        })));
    }

    async function loadManifest(trip) {
        const base = VIEW_CONFIG.basePath || '';
        const res = await fetch(`${base}${trip.path}/manifest.json?t=${Date.now()}`);
        if (!res.ok) throw new Error(`manifest ${res.status}`);
        let manifest = await res.json();
        // Unlocked sessions get the unfiltered set for trips with private photos.
        if (manifest.filtered && checkAllAccess()) {
            try {
                const full = await fetch(`${base}${trip.path}/manifest.all.json?t=${Date.now()}`);
                if (full.ok) manifest = await full.json();
            } catch (e) { /* keep the filtered manifest */ }
        }
        return manifest;
    }

    async function init() {
        const grid = document.getElementById('gallery-grid');
        if (!grid) return;

        if (!VIEW_CONFIG.tripId) {
            setInfo('No trip selected');
            grid.innerHTML = '<p class="gallery-empty">No trip selected.</p>';
            return;
        }

        let trip;
        try {
            const base = VIEW_CONFIG.basePath || '';
            const idxRes = await fetch(`${base}trips/index.json?t=${Date.now()}`);
            const index = await idxRes.json();
            trip = (index.trips || []).find(t => t.id === VIEW_CONFIG.tripId);
        } catch (e) {
            setInfo('Error loading trip');
            grid.innerHTML = '<p class="gallery-empty">Could not load trips.</p>';
            return;
        }

        if (!trip) {
            setInfo('Trip not found');
            grid.innerHTML = '<p class="gallery-empty">That trip could not be found.</p>';
            return;
        }

        document.title = `${formatTripName(trip.name)} — Gallery`;
        renderToolbar(trip);

        // Private trips are only viewable with all-access.
        if (trip.public === false && !checkAllAccess()) {
            setInfo(formatTripName(trip.name), formatDateRange(trip.dates.start, trip.dates.end));
            grid.innerHTML = '<p class="gallery-empty">This trip is private. Unlock all trips on the map to view it.</p>';
            return;
        }

        grid.innerHTML = '<p class="gallery-loading">Loading photos…</p>';

        let manifest;
        try {
            manifest = await loadManifest(trip);
        } catch (e) {
            setInfo(formatTripName(trip.name), formatDateRange(trip.dates.start, trip.dates.end));
            grid.innerHTML = '<p class="gallery-empty">Could not load this trip’s photos.</p>';
            return;
        }

        const photos = (manifest.photos || [])
            .slice()
            .sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''))
            .map(p => ({
                trip: trip.id,
                id: p.id,
                ar: p.ar,
                title: p.building || ''
            }));

        setInfo(
            formatTripName(trip.name),
            formatDateRange(trip.dates.start, trip.dates.end),
            `${photos.length} photo${photos.length === 1 ? '' : 's'}`
        );

        await ensureAspectRatios(photos);
        Gallery.renderGrid(grid, photos, { emptyText: 'No photos in this trip yet.' });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
