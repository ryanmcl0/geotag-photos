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

    function buildYearGroups(trips) {
        const groups = {};
        trips.forEach(trip => {
            const year = trip.year || new Date(trip.dates.start).getFullYear();
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
                              aria-label="Toggle ${trip.name}">`
                    : '';
                return `
                    <li class="trip-item">
                        ${checkbox}
                        <a href="${basePath}${year}/${tripSlug}/index.html"
                           class="nav-link"
                           data-trip-id="${trip.id}">
                            ${trip.name}
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

        sidebar.appendChild(section);

        if (hasAllAccess()) {
            document.getElementById('see-all-lock-btn').addEventListener('click', () => {
                document.cookie = 'all_access=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
                if (typeof window.lockAllAccess === 'function') window.lockAllAccess();
                tripsData = allSidebarTrips.filter(t => t.public !== false);
                yearGroups = buildYearGroups(tripsData);
                renderNavigation();
                renderSeeAllSection();
                highlightCurrentPage();
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
                if (typeof window.unlockAllAccess === 'function') {
                    await window.unlockAllAccess();
                }
                tripsData = allSidebarTrips;
                yearGroups = buildYearGroups(tripsData);
                renderNavigation();
                renderSeeAllSection();
                highlightCurrentPage();
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

        // Create overlay element
        const overlay = document.createElement('div');
        overlay.className = 'sidebar-overlay';
        document.body.appendChild(overlay);

        toggle.addEventListener('click', () => {
            toggle.classList.toggle('active');
            sidebar.classList.toggle('open');
            overlay.classList.toggle('active');
        });

        overlay.addEventListener('click', () => {
            toggle.classList.remove('active');
            sidebar.classList.remove('open');
            overlay.classList.remove('active');
        });
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
