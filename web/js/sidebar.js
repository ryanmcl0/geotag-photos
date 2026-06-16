/**
 * Sidebar Navigation Component
 * Handles hierarchical navigation between all trips, years, and individual trips
 */

(function() {
    'use strict';

    let tripsData = [];       // trips shown in sidebar (public, or all if unlocked)
    let allSidebarTrips = []; // full index including non-public trips
    let yearGroups = {};

    function hasAllAccess() {
        return document.cookie.split(';').some(c => {
            const t = c.trim();
            return t.startsWith('all_access=') && t.length > 'all_access='.length;
        });
    }

    // Strip leading "YYYY:MM " or "YYYY " prefix from trip names — these are
    // internal folder naming conventions, not display labels.
    function formatTripName(name) {
        return name.replace(/^\d{4}[:\d]*\s+/, '');
    }

    function buildYearGroups(trips) {
        const groups = {};
        trips.forEach(trip => {
            // Prefer year from trip name (immune to corrupted EXIF start dates)
            const nameYear = trip.name && trip.name.match(/^(\d{4})/);
            const year = nameYear ? parseInt(nameYear[1]) : (trip.year || new Date(trip.dates.start).getFullYear());
            if (!groups[year]) groups[year] = [];
            groups[year].push(trip);
        });
        return groups;
    }

    /**
     * Initialize sidebar
     */
    async function initSidebar() {
        ensureSidebarNav();
        await loadTripsData();
        renderNavigation();
        renderSeeAllSection();
        initMobileToggle();
        highlightCurrentPage();
    }

    /**
     * Generated trip/year pages ship with a bare header. Inject a Back (to the
     * full map) + Home nav so they match the main map page. Pages that already
     * provide their own .sidebar-nav (map.html, gallery.html) are left alone.
     */
    function ensureSidebarNav() {
        const header = document.querySelector('.sidebar-header');
        if (!header || header.querySelector('.sidebar-nav')) return;
        // VIEW_CONFIG is a global `const`, so it's a bare binding — NOT a window
        // property. Read it directly (window.VIEW_CONFIG is always undefined).
        const basePath = (typeof VIEW_CONFIG !== 'undefined' && VIEW_CONFIG.basePath) || '';
        const nav = document.createElement('div');
        nav.className = 'sidebar-nav';
        nav.innerHTML = `
            <a class="sidebar-back" href="${basePath}map.html">&larr; All maps</a>
            <a class="sidebar-homeicon" href="${basePath}index.html" title="Home" aria-label="Home">
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                    <path d="M3 11.5 12 4l9 7.5M5.5 9.7V20h13V9.7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </a>
        `;
        header.insertBefore(nav, header.firstChild);
    }

    /**
     * Load trips data from index.json
     */
    async function loadTripsData() {
        try {
            const basePath = VIEW_CONFIG.basePath || '';
            const response = await fetch(`${basePath}trips/index.json?t=${Date.now()}`);
            const data = await response.json();
            allSidebarTrips = data.trips || [];

            const viewMode = (window.VIEW_CONFIG && VIEW_CONFIG.mode) || 'all';
            if (viewMode === 'all' && !hasAllAccess()) {
                tripsData = allSidebarTrips.filter(t => t.public !== false);
            } else {
                tripsData = allSidebarTrips;
            }

            yearGroups = buildYearGroups(tripsData);
        } catch (error) {
            console.error('Failed to load trips data for sidebar:', error);
        }
    }

    /**
     * Render navigation structure
     */
    function renderNavigation() {
        const navList = document.getElementById('nav-list');
        if (!navList) return;

        const basePath = VIEW_CONFIG.basePath || '';

        // Build navigation HTML. (No "All Trips" link — it pointed at the all-trips
        // map you're already on; the year sections below are the nav.)
        let html = '';

        // Get years sorted descending (most recent first)
        const years = Object.keys(yearGroups).sort((a, b) => b - a);

        // Restore which years were left open, so navigating into a trip doesn't
        // collapse the dropdowns the user opened.
        const expanded = readExpandedYears();

        years.forEach(year => {
            const trips = yearGroups[year];
            const tripsHtml = trips.map(trip => {
                const tripSlug = trip.id.replace(/-\d{4}$/, '');
                const galleryHref = `${basePath}gallery.html?trip=${encodeURIComponent(trip.id)}&year=${year}`;
                return `
                    <li class="trip-item">
                        <a href="${basePath}${year}/${tripSlug}/index.html"
                           class="nav-link trip-map-link"
                           data-trip-id="${trip.id}">
                            ${formatTripName(trip.name)}
                        </a>
                        <a href="${galleryHref}"
                           class="trip-gallery-link"
                           data-gallery-id="${trip.id}"
                           title="View photos as a gallery">Gallery</a>
                    </li>
                `;
            }).join('');

            const isOpen = expanded.has(String(year)) ? ' expanded' : '';
            html += `
                <li class="year-section${isOpen}" data-year="${year}">
                    <div class="year-header" role="button" tabindex="0" aria-label="Toggle ${year}">
                        <span class="year-label">${year}</span>
                        <span class="arrow">▶</span>
                    </div>
                    <ul class="trip-list">
                        ${tripsHtml}
                    </ul>
                </li>
            `;
        });

        navList.innerHTML = html;

        // Clicking a year just expands/collapses the trips it contains, and we
        // remember that choice so it survives navigation. Year selection
        // (filtering the map) is handled by the top dropdown.
        document.querySelectorAll('.year-header').forEach(header => {
            const toggle = () => {
                const section = header.closest('.year-section');
                const open = section.classList.toggle('expanded');
                setYearExpanded(section.dataset.year, open);
            };
            header.addEventListener('click', toggle);
            header.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
            });
        });
    }

    // Expanded year sections persist for the session so trip navigation keeps
    // the same dropdowns open.
    const EXPANDED_YEARS_KEY = 'geotagPhotos.expandedYears';

    function readExpandedYears() {
        try {
            return new Set(JSON.parse(sessionStorage.getItem(EXPANDED_YEARS_KEY)) || []);
        } catch (e) {
            return new Set();
        }
    }

    function setYearExpanded(year, open) {
        const set = readExpandedYears();
        if (open) set.add(String(year)); else set.delete(String(year));
        try { sessionStorage.setItem(EXPANDED_YEARS_KEY, JSON.stringify([...set])); } catch (e) {}
    }

    /**
     * Highlight current page in navigation
     */
    function highlightCurrentPage() {
        // VIEW_CONFIG is a global `const` (a lexical binding), not a window
        // property, so window.VIEW_CONFIG is always undefined — read it directly.
        const config = (typeof VIEW_CONFIG !== 'undefined' && VIEW_CONFIG) || {};

        // Remove all active states first
        document.querySelectorAll('.nav-link.active, .year-header.active, .trip-gallery-link.active').forEach(el => {
            el.classList.remove('active');
        });
        document.querySelectorAll('.trip-item.trip-active').forEach(el => el.classList.remove('trip-active'));

        if (config.mode === 'year') {
            // Highlight year and expand it (and remember it stays open)
            const yearSection = document.querySelector(`.year-section[data-year="${config.year}"]`);
            if (yearSection) {
                yearSection.classList.add('expanded');
                setYearExpanded(config.year, true);
                const yearHeader = yearSection.querySelector('.year-header');
                if (yearHeader) yearHeader.classList.add('active');
            }
        } else if (config.mode === 'trip') {
            // Highlight specific trip (map or gallery) and expand its year
            const tripLink = document.querySelector(`.trip-map-link[data-trip-id="${config.tripId}"]`);
            if (tripLink) {
                // Highlight the whole row via .trip-active (don't also add
                // .active to the link — its grey background would split the row).
                const item = tripLink.closest('.trip-item');
                if (item) item.classList.add('trip-active');
                if (config.view === 'gallery') {
                    const galLink = document.querySelector(`.trip-gallery-link[data-gallery-id="${config.tripId}"]`);
                    if (galLink) galLink.classList.add('active');
                }
                const yearSection = tripLink.closest('.year-section');
                if (yearSection) {
                    yearSection.classList.add('expanded');
                    setYearExpanded(yearSection.dataset.year, true);
                }
            }
        }
    }

    /**
     * Render the "See All" button at the bottom of the sidebar.
     * Only shown in 'all' mode when non-public trips exist.
     */
    function renderSeeAllSection() {
        const existing = document.getElementById('see-all-section');
        if (existing) existing.remove();

        const viewMode = (window.VIEW_CONFIG && VIEW_CONFIG.mode) || 'all';
        if (viewMode !== 'all') return;

        const hasNonPublic = allSidebarTrips.some(t => t.public === false);
        if (!hasNonPublic) return;

        const sidebar = document.getElementById('sidebar');
        const section = document.createElement('div');
        section.id = 'see-all-section';
        section.className = 'see-all-section';

        if (hasAllAccess()) {
            section.innerHTML = `
                <div class="see-all-unlocked">
                    &#10003; All trips visible
                    <button class="see-all-lock-btn" id="see-all-lock-btn">Public only</button>
                </div>
            `;
        } else {
            // On mobile the inline password form causes an iOS visual-viewport
            // bug (keyboard inside a position:fixed sidebar shifts the layout
            // and the controls never come back).  Instead, close the sidebar
            // and open the body-level modal which handles this correctly.
            const onMobile = window.matchMedia('(max-width: 768px)').matches;
            if (onMobile) {
                section.innerHTML = `
                    <button class="see-all-btn" id="see-all-btn">&#128274; See All Trips</button>
                `;
            } else {
                section.innerHTML = `
                    <button class="see-all-btn" id="see-all-btn">&#128274; See All Trips</button>
                    <div class="see-all-form" id="see-all-form">
                        <input type="password" id="all-password" placeholder="Password" autocomplete="current-password">
                        <div class="see-all-actions">
                            <button class="see-all-submit" id="all-submit">Unlock</button>
                            <button class="see-all-cancel" id="all-cancel">Cancel</button>
                        </div>
                        <p class="see-all-error" id="see-all-error"></p>
                    </div>
                `;
            }
        }

        const tripInfo = document.getElementById('trip-info');
        if (tripInfo) tripInfo.appendChild(section);
        else sidebar.appendChild(section);

        if (hasAllAccess()) {
            document.getElementById('see-all-lock-btn').addEventListener('click', () => {
                document.cookie = 'all_access=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
                if (typeof window.lockAllAccess === 'function') window.lockAllAccess();
                tripsData = allSidebarTrips.filter(t => t.public !== false);
                yearGroups = buildYearGroups(tripsData);
                renderNavigation();
                renderSeeAllSection();
                highlightCurrentPage();
                if (typeof window.repaintFixedControls === 'function') {
                    requestAnimationFrame(window.repaintFixedControls);
                }
            });
        } else {
            const onMobile = window.matchMedia('(max-width: 768px)').matches;
            if (onMobile) {
                // Close the sidebar, then let the body-level modal handle auth
                document.getElementById('see-all-btn').addEventListener('click', () => {
                    if (typeof window.closeMobileSidebar === 'function') {
                        window.closeMobileSidebar();
                    }
                    setTimeout(() => {
                        if (typeof window.openAllTripsModal === 'function') {
                            window.openAllTripsModal();
                        }
                    }, 360);
                });
            } else {
                document.getElementById('see-all-btn').addEventListener('click', () => {
                    document.getElementById('see-all-btn').style.display = 'none';
                    document.getElementById('see-all-form').style.display = 'block';
                    document.getElementById('all-password').focus();
                });

                document.getElementById('all-cancel').addEventListener('click', () => {
                    document.getElementById('see-all-btn').style.display = '';
                    document.getElementById('see-all-form').style.display = 'none';
                    document.getElementById('all-password').value = '';
                    document.getElementById('see-all-error').textContent = '';
                });

                document.getElementById('all-submit').addEventListener('click', submitAllPassword);
                document.getElementById('all-password').addEventListener('keydown', e => {
                    if (e.key === 'Enter') submitAllPassword();
                });
            }
        }
    }

    async function submitAllPassword() {
        const input = document.getElementById('all-password');
        const errorEl = document.getElementById('see-all-error');
        const btn = document.getElementById('all-submit');
        const password = input.value;
        if (!password) return;

        btn.textContent = 'Unlocking...';
        btn.disabled = true;

        try {
            const formData = new FormData();
            formData.append('password', password);
            const res = await fetch('/auth-all', { method: 'POST', body: formData });
            const data = await res.json();

            if (data.ok) {
                // Dismiss iOS keyboard and let the viewport restore BEFORE any
                // map resize/zoom, otherwise Leaflet measures a short container.
                input.blur();
                await new Promise(r => setTimeout(r, 450));
                // Force iOS visual-viewport back to y=0 now that keyboard is gone
                if (typeof window.remeasureMap === 'function') window.remeasureMap();
                if (typeof window.unlockAllAccess === 'function') {
                    await window.unlockAllAccess();
                }
                tripsData = allSidebarTrips;
                yearGroups = buildYearGroups(tripsData);
                renderNavigation();
                renderSeeAllSection();
                highlightCurrentPage();
                if (typeof window.repaintFixedControls === 'function') {
                    requestAnimationFrame(window.repaintFixedControls);
                }
            } else {
                errorEl.textContent = 'Incorrect password.';
                btn.textContent = 'Unlock';
                btn.disabled = false;
            }
        } catch (e) {
            errorEl.textContent = 'Error — try again.';
            btn.textContent = 'Unlock';
            btn.disabled = false;
        }
    }

    /**
     * Initialize mobile sidebar toggle
     */
    function initMobileToggle() {
        const toggle = document.getElementById('sidebar-toggle');
        const sidebar = document.getElementById('sidebar');

        if (!toggle || !sidebar) return;

        const isMobile = () => window.matchMedia('(max-width: 768px)').matches;

        // Create overlay element
        const overlay = document.createElement('div');
        overlay.className = 'sidebar-overlay';
        document.body.appendChild(overlay);

        let closeTimer = null;

        function openSidebar() {
            if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
            if (isMobile()) {
                sidebar.style.display = 'flex';
                // Force iOS to flush any stale GPU-cached content that accumulated
                // while the sidebar was display:none, so filter-driven DOM updates
                // (trip count, photo count) are always visible on reopen.
                void sidebar.offsetHeight;
                // Two rAFs: first renders display:flex at left:-280px,
                // second adds .open to start the CSS left transition
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    toggle.classList.add('active');
                    sidebar.classList.add('open');
                    overlay.classList.add('active');
                }));
            } else {
                toggle.classList.add('active');
                sidebar.classList.add('open');
                overlay.classList.add('active');
            }
        }

        function closeSidebar() {
            toggle.classList.remove('active');
            sidebar.classList.remove('open');
            overlay.classList.remove('active');
            // After transition, remove from compositing stack so iOS doesn't
            // invalidate other position:fixed elements
            if (isMobile()) {
                closeTimer = setTimeout(() => {
                    sidebar.style.display = 'none';
                    if (typeof window.remeasureMap === 'function') window.remeasureMap();
                    if (typeof window.repaintFixedControls === 'function') {
                        requestAnimationFrame(() => requestAnimationFrame(window.repaintFixedControls));
                    }
                }, 350);
            }
        }

        // transitionend fires when the left transition finishes — clear the fallback timer
        sidebar.addEventListener('transitionend', (e) => {
            if (e.propertyName === 'left' && !sidebar.classList.contains('open') && isMobile()) {
                if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
                sidebar.style.display = 'none';
                if (typeof window.remeasureMap === 'function') window.remeasureMap();
                if (typeof window.repaintFixedControls === 'function') {
                    requestAnimationFrame(() => requestAnimationFrame(window.repaintFixedControls));
                }
            }
        });

        toggle.addEventListener('click', () => {
            if (sidebar.classList.contains('open')) closeSidebar();
            else openSidebar();
        });

        overlay.addEventListener('click', closeSidebar);

        // Expose so renderSeeAllSection can close the sidebar before opening
        // the body-level modal (prevents iOS keyboard-in-fixed-element bug)
        window.closeMobileSidebar = closeSidebar;
    }

    // Initialize on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSidebar);
    } else {
        initSidebar();
    }

    // Expose for external use if needed
    window.SidebarNav = {
        refresh: initSidebar
    };
})();
