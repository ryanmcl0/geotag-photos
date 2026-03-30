/**
 * Sidebar Navigation Component
 * Handles hierarchical navigation between all trips, years, and individual trips
 */

(function() {
    'use strict';

    let tripsData = [];
    let yearGroups = {};

    /**
     * Initialize sidebar
     */
    async function initSidebar() {
        await loadTripsData();
        renderNavigation();
        initMobileToggle();
        highlightCurrentPage();
    }

    /**
     * Load trips data from index.json
     */
    async function loadTripsData() {
        try {
            const basePath = VIEW_CONFIG.basePath || '';
            const response = await fetch(`${basePath}trips/index.json`);
            const data = await response.json();
            tripsData = data.trips || [];

            // Group trips by year
            yearGroups = {};
            tripsData.forEach(trip => {
                const year = trip.year || new Date(trip.dates.start).getFullYear();
                if (!yearGroups[year]) {
                    yearGroups[year] = [];
                }
                yearGroups[year].push(trip);
            });
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

        years.forEach(year => {
            const trips = yearGroups[year];
            const tripsHtml = trips.map(trip => {
                const tripSlug = trip.id.replace(/-\d{4}$/, ''); // Remove year suffix for URL
                return `
                    <li>
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

        // Add click handlers for year headers
        document.querySelectorAll('.year-header').forEach(header => {
            header.addEventListener('click', (e) => {
                // Don't toggle if clicking the year link itself
                if (e.target.classList.contains('year-link')) return;

                const section = header.closest('.year-section');
                section.classList.toggle('expanded');
            });
        });
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
