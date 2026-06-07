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
        return document.cookie.split(';').some(c => c.trim() === 'all_access=1');
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
        await loadTripsData();
        renderNavigation();
        renderSeeAllSection();
        initMobileToggle();
        highlightCurrentPage();
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

        // Build navigation HTML
        let html = `
            <li class="nav-item">
                <a href="${basePath}index.html" class="nav-link nav-all">All Trips</a>
            </li>
        `;

        // Get years sorted descending (most recent first)
        const years = Object.keys(yearGroups).sort((a, b) => b - a);

        // Only "all" and "year" views support the toggle — single-trip pages
        // don't load other trips so toggling them is meaningless.
        const showToggles = VIEW_CONFIG.mode === 'all' || VIEW_CONFIG.mode === 'year';
        const hidden = readHiddenTrips();

        years.forEach(year => {
            const trips = yearGroups[year];
            const tripsHtml = trips.map(trip => {
                const tripSlug = trip.id.replace(/-\d{4}$/, '');
                const isHidden = hidden.has(trip.id);
                const checkbox = showToggles
                    ? `<input type="checkbox" class="trip-toggle"
                              data-trip-id="${trip.id}"
                              ${isHidden ? '' : 'checked'}
                              aria-label="Toggle ${formatTripName(trip.name)}">`
                    : '';
                return `
                    <li class="trip-item">
                        ${checkbox}
                        <a href="${basePath}${year}/${tripSlug}/index.html"
                           class="nav-link"
                           data-trip-id="${trip.id}">
                            ${formatTripName(trip.name)}
                        </a>
                    </li>
                `;
            }).join('');

            html += `
                <li class="year-section" data-year="${year}">
                    <div class="year-header">
                        <a href="${basePath}${year}/index.html" class="nav-link year-link">${year}</a>
                        <span class="arrow">▶</span>
                    </div>
                    <ul class="trip-list">
                        ${tripsHtml}
                    </ul>
                </li>
            `;
        });

        navList.innerHTML = html;

        document.querySelectorAll('.year-header').forEach(header => {
            header.addEventListener('click', (e) => {
                if (e.target.classList.contains('year-link')) return;
                if (e.target.classList.contains('trip-toggle')) return;
                const section = header.closest('.year-section');
                section.classList.toggle('expanded');
            });
        });

        // Wire checkbox -> map visibility toggle.
        document.querySelectorAll('.trip-toggle').forEach(cb => {
            cb.addEventListener('click', (e) => e.stopPropagation());
            cb.addEventListener('change', (e) => {
                const tripId = e.target.dataset.tripId;
                if (typeof window.setTripVisibility === 'function') {
                    window.setTripVisibility(tripId, e.target.checked);
                }
            });
        });
    }

    function readHiddenTrips() {
        try {
            return new Set(JSON.parse(localStorage.getItem('geotagPhotos.hiddenTrips')) || []);
        } catch (e) {
            return new Set();
        }
    }

    /**
     * Highlight current page in navigation
     */
    function highlightCurrentPage() {
        const config = window.VIEW_CONFIG || {};

        // Remove all active states first
        document.querySelectorAll('.nav-link.active, .year-header.active').forEach(el => {
            el.classList.remove('active');
        });

        if (config.mode === 'all') {
            // Highlight "All Trips"
            const allLink = document.querySelector('.nav-all');
            if (allLink) allLink.classList.add('active');
        } else if (config.mode === 'year') {
            // Highlight year and expand it
            const yearSection = document.querySelector(`.year-section[data-year="${config.year}"]`);
            if (yearSection) {
                yearSection.classList.add('expanded');
                const yearHeader = yearSection.querySelector('.year-header');
                if (yearHeader) yearHeader.classList.add('active');
            }
        } else if (config.mode === 'trip') {
            // Highlight specific trip and expand its year
            const tripLink = document.querySelector(`[data-trip-id="${config.tripId}"]`);
            if (tripLink) {
                tripLink.classList.add('active');
                const yearSection = tripLink.closest('.year-section');
                if (yearSection) yearSection.classList.add('expanded');
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
