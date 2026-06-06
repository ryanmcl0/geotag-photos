/**
 * Travel Photography Map - Main Application
 */

const CUSTOM_COUNTRY_NAMES = { SCT: 'Scotland', WAL: 'Wales', ENG: 'England' };
const _intlNames = new Intl.DisplayNames(['en'], { type: 'region' });
function countryName(cc) {
    if (CUSTOM_COUNTRY_NAMES[cc]) return CUSTOM_COUNTRY_NAMES[cc];
    try { return _intlNames.of(cc); } catch { return cc; }
}

// Configuration
const CONFIG = {
    // Map settings
    defaultCenter: [38.0, 82.0], // Default center
    defaultZoom: 6,
    maxZoom: 18,

    // Cluster for readability once the map is regional enough. At world/continent
    // zooms, pixel clustering can merge unrelated places into a synthetic marker.
    clusterRadius: 35,
    disableClusteringAtZoom: 13,
    minClusteringZoom: 5,

    // Route styling (colors for different trips)
    routeColors: ['#e11d48', '#2563eb', '#16a34a', '#ca8a04', '#9333ea', '#dc2626'],
    routeWeight: 3,
    routeOpacity: 0.9
};

// Global state
let map;
let allTrips = [];        // trips currently loaded onto the map
let allManifests = [];
let allTripsMeta = [];    // full index — all trips including non-public
let pswpItems = [];
let photoIndexMap = {}; // photoId → index in pswpItems

// Per-trip layers — so each trip can be toggled on/off independently.
// tripLayers[tripId] = { route: L.GeoJSON, markers: L.MarkerClusterGroup, visible: bool }
const tripLayers = {};
const loadedTripIds = new Set();

function checkAllAccess() {
    return document.cookie.split(';').some(c => c.trim() === 'all_access=1');
}

const HIDDEN_TRIPS_STORAGE_KEY = 'geotagPhotos.hiddenTrips';
let activeRouteFilter = 'all'; // 'all' | 'gpx'
let activeCountryFilter = null; // null = all countries, Set<string> = filter by country codes

function loadHiddenTripIds() {
    try {
        return new Set(JSON.parse(localStorage.getItem(HIDDEN_TRIPS_STORAGE_KEY)) || []);
    } catch (e) {
        return new Set();
    }
}

function saveHiddenTripIds(hiddenSet) {
    localStorage.setItem(HIDDEN_TRIPS_STORAGE_KEY, JSON.stringify([...hiddenSet]));
}

/**
 * Initialize the application
 */
let activeYearFilter = null; // null = all years

async function init() {
    initMap();
    await loadTripData();
    initLightbox();
    initYearFilter();
    initCountryFilter();
    initRouteFilter();
    initMobileControls();
}

function tripMatchesYearFilter(trip) {
    if (!activeYearFilter) return true;
    const m = (trip.name || '').match(/^(\d{4})/);
    const tripYear = m ? parseInt(m[1]) : trip.year;
    return tripYear === activeYearFilter;
}

function tripMatchesRouteFilter(trip) {
    if (activeRouteFilter === 'all') return true;
    const layer = tripLayers[trip.id];
    return Boolean(layer && layer.hasGpx);
}

function visibleMarkersForTrip(trip) {
    const layer = tripLayers[trip.id];
    if (!layer || !layer.markers._allMarkers) return [];
    return layer.markers._allMarkers.filter(markerMatchesCountryFilter);
}

function tripMatchesCountryFilter(trip) {
    if (!activeCountryFilter) return true;
    const layer = tripLayers[trip.id];
    const markers = (layer && layer.markers._allMarkers) || [];
    // Per-cluster match: show the trip if any of its clusters is in a selected country.
    if (markers.some(m => m.country && activeCountryFilter.has(m.country))) return true;
    // trip.countries (from GPX route) may cover countries with no photos — always check it.
    if ((trip.countries || []).some(c => activeCountryFilter.has(c))) return true;
    return false;
}

function shouldDisplayTrip(trip) {
    const layer = tripLayers[trip.id];
    if (!layer || !layer.visible) return false;
    if (!tripMatchesRouteFilter(trip)) return false;
    // Country filter overrides year: if countries are selected, skip year filter entirely
    if (activeCountryFilter !== null) return tripMatchesCountryFilter(trip);
    return tripMatchesYearFilter(trip);
}

function syncVisibleTripLayers({ fit = false } = {}) {
    allTrips.forEach(trip => {
        const layer = tripLayers[trip.id];
        if (!layer) return;
        const show = shouldDisplayTrip(trip);
        if (show) {
            refreshMarkerGroup(layer.markers); // apply current country filter to markers
            if (!map.hasLayer(layer.route)) layer.route.addTo(map);
            if (!map.hasLayer(layer.markers)) layer.markers.addTo(map);
        } else {
            map.removeLayer(layer.route);
            map.removeLayer(layer.markers);
        }
    });

    updateTripInfo();
    reinitLightbox();
    if (fit) fitMapToBounds();
}

/**
 * Shared centered container at the top of the map holding the year + country
 * dropdowns side by side. Created on demand by whichever filter initialises first.
 */
function getTopFilterBar() {
    let bar = document.querySelector('.map-top-filters');
    if (!bar) {
        bar = document.createElement('div');
        bar.className = 'map-top-filters';
        document.getElementById('map').appendChild(bar);
    }
    return bar;
}

function initYearFilter() {
    const years = [...new Set(allTrips.map(t => {
        const m = (t.name || '').match(/^(\d{4})/);
        return m ? parseInt(m[1]) : t.year;
    }))].filter(Boolean).sort((a, b) => b - a);

    if (years.length < 2) return; // not worth showing for 1 year

    const countByYear = {};
    allTrips.forEach(t => {
        const m = (t.name || '').match(/^(\d{4})/);
        const y = m ? parseInt(m[1]) : t.year;
        if (y) countByYear[y] = (countByYear[y] || 0) + 1;
    });

    const wrapper = document.createElement('div');
    wrapper.className = 'year-filter-wrapper';
    wrapper.innerHTML = `
        <button class="year-filter-btn" id="yearFilterBtn">
            <span id="yearFilterLabel">All years</span>
            <svg class="year-filter-chevron" viewBox="0 0 10 6" width="10" height="6">
                <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>
            </svg>
        </button>
        <div class="year-filter-menu" id="yearFilterMenu">
            <div class="year-filter-option year-filter-option--active" data-year="">
                <span class="year-filter-check">✓</span>All years
            </div>
            ${years.map(y => `
                <div class="year-filter-option" data-year="${y}">
                    <span class="year-filter-check"></span>${y}
                    <span class="year-filter-count">${countByYear[y]} trip${countByYear[y] !== 1 ? 's' : ''}</span>
                </div>
            `).join('')}
        </div>
    `;
    getTopFilterBar().appendChild(wrapper);

    const btn = wrapper.querySelector('#yearFilterBtn');
    const menu = wrapper.querySelector('#yearFilterMenu');

    // The menu overlays the Leaflet map; without this, wheel-scrolling the menu
    // zooms the map underneath instead of scrolling the (overflowing) list.
    L.DomEvent.disableScrollPropagation(menu);
    L.DomEvent.disableClickPropagation(menu);

    btn.addEventListener('click', e => {
        e.stopPropagation();
        menu.classList.toggle('open');
    });
    document.addEventListener('click', () => menu.classList.remove('open'));
    menu.addEventListener('click', e => e.stopPropagation());

    wrapper.querySelectorAll('.year-filter-option').forEach(opt => {
        opt.addEventListener('click', () => {
            const year = opt.dataset.year ? parseInt(opt.dataset.year) : null;
            setYearFilter(year, wrapper);
            menu.classList.remove('open');
        });
    });
}

function setYearFilter(year, wrapper) {
    activeYearFilter = year;
    const label = wrapper.querySelector('#yearFilterLabel');
    label.textContent = year ? String(year) : 'All years';

    wrapper.querySelectorAll('.year-filter-option').forEach(opt => {
        const optYear = opt.dataset.year ? parseInt(opt.dataset.year) : null;
        const active = optYear === year;
        opt.classList.toggle('year-filter-option--active', active);
        opt.querySelector('.year-filter-check').textContent = active ? '✓' : '';
    });

    syncVisibleTripLayers({ fit: true });
}

function initCountryFilter() {
    rebuildCountryFilter();
}

/**
 * (Re)build the country dropdown from the trips currently loaded onto the map.
 * Rebuilt after unlock/lock so newly available (or removed) countries appear.
 * The active selection is preserved across rebuilds.
 */
function rebuildCountryFilter() {
    const existing = document.querySelector('.country-filter-wrapper');
    if (existing) existing.remove();

    const countByCountry = {};
    allTrips.forEach(t => {
        (t.countries || []).forEach(cc => {
            countByCountry[cc] = (countByCountry[cc] || 0) + 1;
        });
    });

    const countries = Object.keys(countByCountry)
        .map(cc => ({
            code: cc,
            name: countryName(cc),
            count: countByCountry[cc]
        }))
        .sort((a, b) => a.name.localeCompare(b.name));

    if (countries.length < 2) return;

    const wrapper = document.createElement('div');
    wrapper.className = 'country-filter-wrapper';
    wrapper.innerHTML = `
        <button class="country-filter-btn" id="countryFilterBtn">
            <span id="countryFilterLabel">All countries</span>
            <svg class="country-filter-chevron" viewBox="0 0 10 6" width="10" height="6">
                <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>
            </svg>
        </button>
        <div class="country-filter-menu" id="countryFilterMenu">
            <div class="country-filter-actions">
                <button class="country-filter-action-btn" id="countrySelectAll">Select all</button>
                <button class="country-filter-action-btn" id="countrySelectNone">Select none</button>
            </div>
            <div class="country-filter-divider"></div>
            <div class="country-filter-options">
                ${countries.map(c => `
                    <div class="country-filter-option country-filter-option--active" data-country="${c.code}" title="${c.count} trip${c.count !== 1 ? 's' : ''}">
                        <span class="country-filter-check">✓</span>
                        <span class="country-filter-name">${c.name}</span>
                        <span class="country-filter-count">${c.count}</span>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
    getTopFilterBar().appendChild(wrapper);

    const btn = wrapper.querySelector('#countryFilterBtn');
    const menu = wrapper.querySelector('#countryFilterMenu');

    // Stop wheel/click over the menu from reaching the Leaflet map (zoom/pan).
    L.DomEvent.disableScrollPropagation(menu);
    L.DomEvent.disableClickPropagation(menu);

    btn.addEventListener('click', e => {
        e.stopPropagation();
        menu.classList.toggle('open');
    });
    document.addEventListener('click', () => menu.classList.remove('open'));
    menu.addEventListener('click', e => e.stopPropagation());

    wrapper.querySelector('#countrySelectAll').addEventListener('click', () => {
        activeCountryFilter = null;
        wrapper.querySelectorAll('.country-filter-option').forEach(opt => {
            opt.classList.add('country-filter-option--active');
            opt.querySelector('.country-filter-check').textContent = '✓';
        });
        updateCountryFilterLabel(wrapper, null);
        syncVisibleTripLayers({ fit: true });
    });

    wrapper.querySelector('#countrySelectNone').addEventListener('click', () => {
        activeCountryFilter = new Set();
        wrapper.querySelectorAll('.country-filter-option').forEach(opt => {
            opt.classList.remove('country-filter-option--active');
            opt.querySelector('.country-filter-check').textContent = '';
        });
        updateCountryFilterLabel(wrapper, activeCountryFilter);
        syncVisibleTripLayers({ fit: true });
    });

    wrapper.querySelectorAll('.country-filter-option').forEach(opt => {
        opt.addEventListener('click', () => {
            const cc = opt.dataset.country;
            if (!activeCountryFilter) {
                activeCountryFilter = new Set(countries.map(c => c.code));
            }
            if (activeCountryFilter.has(cc)) {
                activeCountryFilter.delete(cc);
                opt.classList.remove('country-filter-option--active');
                opt.querySelector('.country-filter-check').textContent = '';
            } else {
                activeCountryFilter.add(cc);
                opt.classList.add('country-filter-option--active');
                opt.querySelector('.country-filter-check').textContent = '✓';
            }
            if (activeCountryFilter.size === countries.length) {
                activeCountryFilter = null;
            }
            updateCountryFilterLabel(wrapper, activeCountryFilter);
            syncVisibleTripLayers({ fit: true });
        });
    });

    // Restore the active selection across rebuilds (e.g. after unlock/lock).
    if (activeCountryFilter) {
        // Drop any codes no longer present, then normalise a full set back to "all".
        activeCountryFilter = new Set(
            [...activeCountryFilter].filter(cc => cc in countByCountry)
        );
        if (activeCountryFilter.size === countries.length) activeCountryFilter = null;
    }
    wrapper.querySelectorAll('.country-filter-option').forEach(opt => {
        const active = !activeCountryFilter || activeCountryFilter.has(opt.dataset.country);
        opt.classList.toggle('country-filter-option--active', active);
        opt.querySelector('.country-filter-check').textContent = active ? '✓' : '';
    });
    updateCountryFilterLabel(wrapper, activeCountryFilter);
}

function updateCountryFilterLabel(wrapper, filter) {
    const label = wrapper.querySelector('#countryFilterLabel');
    if (!filter) {
        label.textContent = 'All countries';
    } else if (filter.size === 0) {
        label.textContent = 'No countries';
    } else {
        label.textContent = `${filter.size} countr${filter.size === 1 ? 'y' : 'ies'}`;
    }
}

function initRouteFilter() {
    const wrapper = document.createElement('div');
    wrapper.className = 'route-filter-wrapper';
    wrapper.innerHTML = `
        <button class="route-filter-btn" data-filter="all">All</button>
        <button class="route-filter-btn" data-filter="gpx">GPX</button>
    `;
    document.getElementById('map').appendChild(wrapper);

    wrapper.querySelectorAll('.route-filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === activeRouteFilter);
        btn.addEventListener('click', () => setRouteFilter(btn.dataset.filter, wrapper));
    });
}

function setRouteFilter(filter, wrapper) {
    activeRouteFilter = filter === 'gpx' ? 'gpx' : 'all';
    wrapper.querySelectorAll('.route-filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === activeRouteFilter);
    });
    syncVisibleTripLayers({ fit: true });
}

// Base layer definitions
const BASE_LAYERS = {
    satellite: {
        label: 'Satellite',
        icon: '🛰',
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        options: { attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics' },
        labels: true
    },
    streets: {
        label: 'Streets',
        icon: '🗺',
        url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
        options: { attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>' },
        labels: false
    },
    topo: {
        label: 'Topo',
        icon: '⛰',
        url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
        options: { maxZoom: 17, attribution: 'Map data &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>, <a href="http://viewfinderpanoramas.org">SRTM</a> &bull; Style &copy; <a href="https://opentopomap.org">OpenTopoMap</a>' },
        labels: false
    }
};

const BASE_LAYER_KEY = 'geotagPhotos.baseLayer';
let currentBaseLayer = null;
let labelsLayer = null;

/**
 * Initialize Leaflet map
 */
function initMap() {
    map = L.map('map', {
        center: CONFIG.defaultCenter,
        zoom: CONFIG.defaultZoom,
        zoomControl: true
    });

    const saved = localStorage.getItem(BASE_LAYER_KEY) || 'satellite';
    setBaseLayer(saved in BASE_LAYERS ? saved : 'satellite');

    // Relocate the floating controls INTO #map so they share the Leaflet
    // container's compositing layer. Body/map-container-level elements get
    // dropped by iOS Safari when the map re-tiles after a zoom; elements
    // inside #map (like the year/route filters) do not.
    const mapEl = document.getElementById('map');
    ['sidebar-toggle', 'mobile-see-all-trigger'].forEach(id => {
        const el = document.getElementById(id);
        if (el) mapEl.appendChild(el);
    });

    initMapStyleControl();
    initDoubleTapZoom();

    // Re-measure the map whenever iOS changes the viewport (rotation, address
    // bar show/hide, keyboard). Without this Leaflet keeps a stale size and the
    // tiles render at the wrong offset.
    let resizeRAF;
    const remeasure = () => {
        cancelAnimationFrame(resizeRAF);
        resizeRAF = requestAnimationFrame(() => map.invalidateSize({ animate: false }));
    };
    window.addEventListener('resize', remeasure);
    window.addEventListener('orientationchange', remeasure);
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', remeasure);
    }
}

function initDoubleTapZoom() {
    let lastTap = 0;
    let lastPoint = null;

    map.getContainer().addEventListener('touchend', function(e) {
        if (e.touches.length > 0) return;
        if (e.changedTouches.length !== 1) return;

        const touch = e.changedTouches[0];
        const now = Date.now();
        const timeDiff = now - lastTap;
        const point = L.point(touch.clientX, touch.clientY);
        const isClose = lastPoint && point.distanceTo(lastPoint) < 40;
        const isDoubleTap = timeDiff > 30 && timeDiff < 350 && isClose;

        // Don't zoom when tapping markers, popups, or buttons
        const isInteractive = !!e.target.closest('.photo-marker-icon, .leaflet-popup, button, a, input');

        if (isDoubleTap && !isInteractive) {
            const rect = map.getContainer().getBoundingClientRect();
            const containerPoint = L.point(touch.clientX - rect.left, touch.clientY - rect.top);
            map.setZoomAround(containerPoint, map.getZoom() + 1);
            e.preventDefault();
            lastTap = 0;
            lastPoint = null;
        } else {
            lastTap = now;
            lastPoint = point;
        }
    }, { passive: false });
}

function setBaseLayer(key) {
    const def = BASE_LAYERS[key];
    if (!def) return;
    if (currentBaseLayer) map.removeLayer(currentBaseLayer);
    if (labelsLayer) { map.removeLayer(labelsLayer); labelsLayer = null; }
    currentBaseLayer = L.tileLayer(def.url, { ...def.options, maxZoom: CONFIG.maxZoom }).addTo(map);
    if (def.labels) {
        labelsLayer = L.tileLayer(
            'https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png',
            { maxZoom: CONFIG.maxZoom, pane: 'overlayPane' }
        ).addTo(map);
    }
    localStorage.setItem(BASE_LAYER_KEY, key);
    document.querySelectorAll('.map-style-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.layer === key);
    });
}

function initMapStyleControl() {
    const ctrl = document.createElement('div');
    ctrl.className = 'map-style-control';
    const current = localStorage.getItem(BASE_LAYER_KEY) || 'satellite';
    ctrl.innerHTML = Object.entries(BASE_LAYERS).map(([key, def]) => `
        <button class="map-style-btn${current === key ? ' active' : ''}" data-layer="${key}" title="${def.label}">
            <span class="map-style-icon">${def.icon}</span>
            <span class="map-style-label">${def.label}</span>
        </button>
    `).join('');
    ctrl.querySelectorAll('.map-style-btn').forEach(btn =>
        btn.addEventListener('click', () => setBaseLayer(btn.dataset.layer))
    );
    // Append inside #map so it shares the Leaflet container's compositing
    // layer and survives iOS re-tiling repaints.
    document.getElementById('map').appendChild(ctrl);
}

function makeClusterGroup() {
    return L.markerClusterGroup({
        maxClusterRadius: zoom => zoom < CONFIG.minClusteringZoom ? 1 : CONFIG.clusterRadius,
        disableClusteringAtZoom: CONFIG.disableClusteringAtZoom,
        spiderfyOnMaxZoom: false,
        showCoverageOnHover: false,
        zoomToBoundsOnClick: true,
        animate: true,
        animateAddingMarkers: false,
        iconCreateFunction: createClusterIcon
    });
}

/**
 * Create custom cluster icon
 */
function createClusterIcon(cluster) {
    const count = cluster.getChildCount();
    let size = 'small';

    if (count >= 10) size = 'large';
    else if (count >= 5) size = 'medium';

    return L.divIcon({
        html: `<div>${count}</div>`,
        className: `marker-cluster marker-cluster-${size}`,
        iconSize: L.point(40, 40)
    });
}

/**
 * Load trip manifest and route data
 */
async function loadTripData() {
    try {
        const basePath = (typeof VIEW_CONFIG !== 'undefined' && VIEW_CONFIG.basePath) || '';

        const indexResponse = await fetch(`${basePath}trips/index.json?t=${Date.now()}`);
        const index = await indexResponse.json();
        allTripsMeta = index.trips;

        let trips = [...allTripsMeta];

        // Filter by VIEW_CONFIG (year/trip pages)
        if (typeof VIEW_CONFIG !== 'undefined') {
            if (VIEW_CONFIG.mode === 'year' && VIEW_CONFIG.year) {
                trips = trips.filter(t => (t.year || new Date(t.dates.start).getFullYear()) === VIEW_CONFIG.year);
            } else if (VIEW_CONFIG.mode === 'trip' && VIEW_CONFIG.tripId) {
                trips = trips.filter(t => t.id === VIEW_CONFIG.tripId);
            }
        }

        // Always hide private trips unless user has all_access cookie
        if (!checkAllAccess()) {
            trips = trips.filter(t => t.public !== false);
        }

        if (trips.length === 0) {
            document.getElementById('trip-name').textContent = 'No trips found';
            return;
        }

        for (const trip of trips) {
            try {
                await loadSingleTrip(trip, basePath);
            } catch (e) {
                console.warn(`Skipped trip ${trip.id}:`, e.message);
            }
        }

        updateTripInfo();
        fitMapToBounds();

    } catch (error) {
        console.error('Failed to load trip data:', error);
        document.getElementById('trip-name').textContent = 'Error loading trip data';
    }
}

/**
 * Fetch and render a single trip's manifest + route onto the map.
 */
async function loadSingleTrip(trip, basePath) {
    basePath = basePath !== undefined ? basePath : ((typeof VIEW_CONFIG !== 'undefined' && VIEW_CONFIG.basePath) || '');
    const colorIndex = allTrips.length;
    const tripPath = `${basePath}${trip.path}`;
    const color = CONFIG.routeColors[colorIndex % CONFIG.routeColors.length];

    const [manifestRes, routeRes] = await Promise.all([
        fetch(`${tripPath}/manifest.json?t=${Date.now()}`),
        fetch(`${tripPath}/route.geojson?t=${Date.now()}`)
    ]);
    const manifest = await manifestRes.json();
    const routeData = await routeRes.json();

    manifest.tripId = trip.id;
    manifest.tripIndex = colorIndex;
    manifest.tripPath = tripPath;

    const hidden = loadHiddenTripIds();
    const hasGpx = Boolean(manifest.source && manifest.source.gpx_path);
    tripLayers[trip.id] = {
        route: buildRouteLayer(routeData, color, trip.name),
        markers: buildMarkerLayer(manifest, hasGpx),
        color,
        hasGpx,
        visible: !hidden.has(trip.id),
    };

    allTrips.push(trip);
    allManifests.push(manifest);
    loadedTripIds.add(trip.id);

    if (shouldDisplayTrip(trip)) {
        tripLayers[trip.id].route.addTo(map);
        tripLayers[trip.id].markers.addTo(map);
    }
}

/**
 * Load non-public trips after the user has unlocked all-access.
 * Called by sidebar after successful /auth-all.
 */
async function unlockAllAccess() {
    const basePath = (typeof VIEW_CONFIG !== 'undefined' && VIEW_CONFIG.basePath) || '';
    const unloaded = allTripsMeta.filter(t => !loadedTripIds.has(t.id));
    if (unloaded.length === 0) return;

    for (const trip of unloaded) {
        await loadSingleTrip(trip, basePath);
    }

    rebuildCountryFilter();
    updateTripInfo();
    reinitLightbox();
    fitMapToBounds();
    updateMobileSeeAll();
}
window.unlockAllAccess = unlockAllAccess;

/**
 * Remove non-public trips from the map when user returns to public-only view.
 */
function lockAllAccess() {
    const nonPublicIds = new Set(
        allTripsMeta.filter(t => t.public === false).map(t => t.id)
    );
    if (nonPublicIds.size === 0) return;

    for (const tripId of nonPublicIds) {
        if (tripLayers[tripId]) {
            map.removeLayer(tripLayers[tripId].route);
            map.removeLayer(tripLayers[tripId].markers);
            delete tripLayers[tripId];
        }
        loadedTripIds.delete(tripId);
    }

    allTrips = allTrips.filter(t => !nonPublicIds.has(t.id));
    allManifests = allManifests.filter(m => !nonPublicIds.has(m.tripId));

    rebuildCountryFilter();
    updateTripInfo();
    reinitLightbox();
    fitMapToBounds();
    updateMobileSeeAll();
}
window.lockAllAccess = lockAllAccess;

/**
 * Update trip info overlay (reflects only currently-visible trips)
 */
function updateTripInfo() {
    const visibleTrips = allTrips.filter(shouldDisplayTrip);
    // Count photos and countries from the markers actually shown, so the totals
    // stay accurate when a country filter hides part of a multi-country trip.
    let totalPhotos = 0;
    const uniqueCountries = new Set();
    visibleTrips.forEach(trip => {
        const markers = visibleMarkersForTrip(trip);
        markers.forEach(m => {
            totalPhotos += m.photoData.length;
            if (m.country) uniqueCountries.add(m.country);
        });
        // trip.countries (from GPX route) covers countries with no photo clusters.
        (trip.countries || []).forEach(c => uniqueCountries.add(c));
    });
    const viewConfig = typeof VIEW_CONFIG !== 'undefined' ? VIEW_CONFIG : { mode: 'all' };

    let titleText = '';
    let subtitleText = '';

    if (visibleTrips.length === 1) {
        titleText = visibleTrips[0].name;
        subtitleText = `${formatDate(visibleTrips[0].dates.start)} – ${formatDate(visibleTrips[0].dates.end)}`;
    } else if (viewConfig.mode === 'year' && viewConfig.year) {
        titleText = `${viewConfig.year}`;
        subtitleText = `${visibleTrips.length} trips`;
    } else {
        titleText = `${visibleTrips.length} Trips`;
        subtitleText = '';
    }

    document.getElementById('trip-name').textContent = titleText;
    document.getElementById('trip-dates').textContent = subtitleText;

    document.getElementById('photo-count').textContent =
        `${totalPhotos.toLocaleString()} photos`;

    const PENDING_COUNTRIES = [
        'Albania','Belgium','Bosnia','Croatia',
        'Ireland','Luxembourg','Montenegro','Netherlands','Slovakia','Tunisia',
    ];
    const TOTAL_COUNTRIES = 55;

    const sorted = [...uniqueCountries].map(countryName).sort();
    const onMap = uniqueCountries.size;
    const pending = PENDING_COUNTRIES.length;

    const summaryEl = document.getElementById('country-summary');
    const countryListEl = document.getElementById('country-list');
    const availLabelEl = document.getElementById('country-available-label');
    const pendingLabelEl = document.getElementById('country-pending-label');
    const pendingListEl = document.getElementById('country-pending-list');

    if (summaryEl) summaryEl.textContent = `${TOTAL_COUNTRIES} countries visited`;
    if (availLabelEl) availLabelEl.textContent = `${onMap} on map`;
    if (countryListEl) countryListEl.textContent = sorted.join(', ');
    if (pendingLabelEl) pendingLabelEl.textContent = `${pending} pending`;
    if (pendingListEl) pendingListEl.textContent = PENDING_COUNTRIES.join(', ');
}

/**
 * Format date for display
 */
function formatDate(dateStr) {
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric'
    });
}

/**
 * Build a polyline layer for a trip's GPX route.
 */
function buildRouteLayer(routeData, color, tripName) {
    const layer = L.geoJSON(routeData, {
        style: {
            color: color,
            weight: CONFIG.routeWeight,
            opacity: CONFIG.routeOpacity
        }
    });
    layer.bindTooltip(tripName, { permanent: false, sticky: true });
    return layer;
}

/**
 * Build a MarkerClusterGroup for a single trip's photos.
 */
function buildMarkerLayer(manifest, hasGpx) {
    const group = makeClusterGroup();
    const photoLookup = {};
    manifest.photos.forEach(photo => {
        photo.tripName = manifest.trip_name;
        photo.tripIndex = manifest.tripIndex;
        photo.tripId = manifest.tripId;
        photo.tripPath = manifest.tripPath;
        photoLookup[photo.id] = photo;
    });

    // Build a marker per cluster. They start unattached to the group — membership
    // is applied by refreshMarkerGroup(), which honours the active country filter
    // so multi-country trips only show markers for the selected countries.
    const allMarkers = manifest.clusters.map(cluster => {
        const photos = cluster.photo_ids.map(id => photoLookup[id]);
        const thumbnailUrl = resolveUrl(manifest.tripPath, photos[0].thumbnail);
        const marker = L.marker([cluster.lat, cluster.lon]);
        marker.bindPopup(() => buildMarkerPopup(marker));
        marker.photoData = photos;
        marker.locationName = cluster.location;
        marker.country = cluster.country || null;
        marker._clusterGroup = group;
        marker._pendingPage = 0;
        marker._photoCount = photos.length;
        marker._thumbnailUrl = thumbnailUrl;
        return marker;
    });
    group._allMarkers = allMarkers;
    group._hasGpx = hasGpx;
    refreshMarkerGroup(group);
    return group;
}

/**
 * Does this marker's cluster belong to a currently-selected country?
 * Clusters without a country code (older data) are always kept so nothing
 * silently disappears.
 */
function markerMatchesCountryFilter(marker) {
    if (!activeCountryFilter) return true;
    if (!marker.country) return true;
    return activeCountryFilter.has(marker.country);
}

/**
 * Rebuild a cluster group's membership from its full marker set, keeping only
 * markers that pass the country filter. Endpoint rings (start/end) and the
 * sibling chain used for cross-cluster paging are recomputed over the visible
 * subset so they stay correct after filtering.
 */
function refreshMarkerGroup(group) {
    const all = group._allMarkers || [];
    const visible = all.filter(markerMatchesCountryFilter);
    group.clearLayers();
    const lastIdx = visible.length - 1;
    visible.forEach((marker, i) => {
        // Only flag start/end for GPX trips where route order is meaningful,
        // and only when there's more than one stop.
        let endpoint = null;
        if (group._hasGpx && lastIdx > 0) {
            if (i === 0) endpoint = 'start';
            else if (i === lastIdx) endpoint = 'end';
        }
        marker.setIcon(createPhotoIcon(marker._photoCount, marker._thumbnailUrl, endpoint));
        marker._sibs = visible;
        marker._sibIdx = i;
    });
    group.addLayers(visible);
}

/**
 * Build the popup content for a marker, honouring a pending start page set by
 * cross-cluster navigation. Single-photo clusters and multi-photo clusters both
 * support paging onward to the adjacent cluster.
 */
function buildMarkerPopup(marker) {
    let startPage = marker._pendingPage;
    marker._pendingPage = 0; // consume; default for a fresh marker click
    if (marker.photoData.length === 1) {
        return createSinglePhotoPopup(marker);
    }
    return createMultiPhotoPopup(marker, startPage);
}

/**
 * Open an adjacent cluster's popup. dir +1 = next (opens at first page),
 * dir -1 = previous (opens at its last page). Uses zoomToShowLayer so the
 * target marker is revealed even if currently collapsed inside a cluster bubble.
 * Returns true if a sibling existed in that direction.
 */
function openSiblingCluster(marker, dir) {
    const sibs = marker._sibs;
    if (!sibs) return false;
    const target = sibs[marker._sibIdx + dir];
    if (!target) return false;
    target._pendingPage = dir > 0 ? 0 : 'last';
    map.closePopup();
    const group = target._clusterGroup;
    if (group && typeof group.zoomToShowLayer === 'function') {
        group.zoomToShowLayer(target, () => target.openPopup());
    } else {
        target.openPopup();
    }
    return true;
}

/**
 * Show or hide all of a trip's content (route + markers). Called by the sidebar
 * checkbox handler. Persists the hidden set in localStorage.
 */
function setTripVisibility(tripId, visible) {
    const entry = tripLayers[tripId];
    if (!entry || entry.visible === visible) return;
    entry.visible = visible;
    const hidden = loadHiddenTripIds();
    if (visible) hidden.delete(tripId);
    else hidden.add(tripId);
    saveHiddenTripIds(hidden);
    syncVisibleTripLayers();
}
window.setTripVisibility = setTripVisibility;

function resolveUrl(tripPath, photoPath) {
    return photoPath.startsWith('http') ? photoPath : `${tripPath}/${photoPath}`;
}

function preloadDisplay(url) {
    const img = new Image();
    img.src = url;
}

/**
 * Create icon for photo marker with thumbnail preview
 */
function createPhotoIcon(count, thumbnailUrl, endpoint) {
    const countBadge = count > 1 ? `<span class="photo-marker-count">${count}</span>` : '';
    // endpoint: 'start' | 'end' | null — adds a coloured ring + label to the
    // first/last cluster of a GPX trip so the route's beginning and end are visible.
    const endpointClass = endpoint ? ` photo-marker-${endpoint}` : '';
    const endpointLabel = endpoint
        ? `<span class="photo-marker-endpoint">${endpoint === 'start' ? 'START' : 'END'}</span>`
        : '';

    return L.divIcon({
        html: `
            <div class="photo-marker-wrapper${endpointClass}">
                <img src="${thumbnailUrl}" class="photo-marker-thumb" alt=""
                     width="44" height="44" decoding="async">
                ${countBadge}
                ${endpointLabel}
            </div>
        `,
        className: `photo-marker-icon${endpointClass}`,
        iconSize: L.point(44, 44),
        iconAnchor: L.point(22, 22),
        popupAnchor: L.point(0, -22)
    });
}

/**
 * Create popup for single photo
 */
function createSinglePhotoPopup(marker) {
    const photo = marker.photoData[0];
    const location = marker.locationName;
    preloadDisplay(resolveUrl(photo.tripPath, photo.display));
    const title = location || photo.tripName;

    const container = document.createElement('div');
    container.className = 'photo-popup';

    if (title) {
        const header = document.createElement('div');
        header.className = 'cluster-popup-header';
        header.textContent = title;
        container.appendChild(header);
        if (location && photo.tripName && photo.tripName !== location) {
            const sub = document.createElement('div');
            sub.className = 'cluster-popup-subheader';
            sub.textContent = photo.tripName;
            container.appendChild(sub);
        }
    }

    const img = document.createElement('img');
    img.src = resolveUrl(photo.tripPath, photo.thumbnail);
    img.alt = '';
    img.className = 'popup-thumbnail';
    img.dataset.photoId = photo.id;
    img.addEventListener('click', () => openGallery(photo));
    container.appendChild(img);

    // If this single-photo cluster has neighbours, allow paging onward to them
    // so cross-cluster navigation stays continuous.
    const hasPrevCluster = marker._sibs && marker._sibIdx > 0;
    const hasNextCluster = marker._sibs && marker._sibIdx < marker._sibs.length - 1;
    if (hasPrevCluster || hasNextCluster) {
        const nav = document.createElement('div');
        nav.className = 'cluster-popup-nav';

        const prevBtn = document.createElement('button');
        prevBtn.type = 'button';
        prevBtn.className = 'cluster-popup-navbtn';
        prevBtn.innerHTML = '‹';
        prevBtn.disabled = !hasPrevCluster;

        const counter = document.createElement('span');
        counter.className = 'cluster-popup-counter';
        counter.textContent = '1 of 1';

        const nextBtn = document.createElement('button');
        nextBtn.type = 'button';
        nextBtn.className = 'cluster-popup-navbtn';
        nextBtn.innerHTML = '›';
        nextBtn.disabled = !hasNextCluster;

        prevBtn.addEventListener('click', () => openSiblingCluster(marker, -1));
        nextBtn.addEventListener('click', () => openSiblingCluster(marker, 1));

        nav.append(prevBtn, counter, nextBtn);
        container.appendChild(nav);
    }

    return container;
}

/**
 * Create popup for multiple photos.
 *
 * Paginated: shows a fixed page of thumbnails with prev/next navigation rather
 * than dumping every photo into one giant grid. A huge grid makes the popup very
 * tall, which forces Leaflet to auto-pan the whole map (and on mobile it can't
 * fit at all — the tap just shifts the map and nothing useful appears).
 * Returns a DOM node so we can wire pagination without leaking global state.
 */
const CLUSTER_POPUP_PAGE_SIZE = 9; // 3×3 grid keeps the popup compact + stable

function createMultiPhotoPopup(marker, startPage) {
    const photos = marker.photoData;
    const location = marker.locationName;
    const container = document.createElement('div');
    container.className = 'cluster-popup';

    const tripName = photos[0] && photos[0].tripName;
    const title = location || tripName;
    if (title) {
        const header = document.createElement('div');
        header.className = 'cluster-popup-header';
        header.textContent = title;
        container.appendChild(header);
        // Show which trip the photos are from as a subtitle when we have a
        // specific place name for the title.
        if (location && tripName && tripName !== location) {
            const sub = document.createElement('div');
            sub.className = 'cluster-popup-subheader';
            sub.textContent = tripName;
            container.appendChild(sub);
        }
    }

    const grid = document.createElement('div');
    grid.className = 'photo-grid';
    container.appendChild(grid);

    const totalPages = Math.ceil(photos.length / CLUSTER_POPUP_PAGE_SIZE);
    let page = startPage === 'last' ? totalPages - 1 : (startPage || 0);
    if (page < 0 || page >= totalPages) page = 0;

    const hasPrevCluster = marker._sibs && marker._sibIdx > 0;
    const hasNextCluster = marker._sibs && marker._sibIdx < marker._sibs.length - 1;

    const renderPage = () => {
        grid.innerHTML = '';
        const start = page * CLUSTER_POPUP_PAGE_SIZE;
        photos.slice(start, start + CLUSTER_POPUP_PAGE_SIZE).forEach(photo => {
            const img = document.createElement('img');
            img.src = resolveUrl(photo.tripPath, photo.thumbnail);
            img.alt = '';
            img.dataset.photoId = photo.id;
            const displayUrl = resolveUrl(photo.tripPath, photo.display);
            img.addEventListener('touchstart', () => preloadDisplay(displayUrl), { passive: true });
            img.addEventListener('mousedown', () => preloadDisplay(displayUrl));
            img.addEventListener('click', () => openGallery(photo));
            grid.appendChild(img);
        });
    };

    if (totalPages > 1 || hasPrevCluster || hasNextCluster) {
        const nav = document.createElement('div');
        nav.className = 'cluster-popup-nav';

        const prevBtn = document.createElement('button');
        prevBtn.type = 'button';
        prevBtn.className = 'cluster-popup-navbtn';
        prevBtn.innerHTML = '‹';

        const counter = document.createElement('span');
        counter.className = 'cluster-popup-counter';

        const nextBtn = document.createElement('button');
        nextBtn.type = 'button';
        nextBtn.className = 'cluster-popup-navbtn';
        nextBtn.innerHTML = '›';

        const updateNav = () => {
            const start = page * CLUSTER_POPUP_PAGE_SIZE;
            const end = Math.min(start + CLUSTER_POPUP_PAGE_SIZE, photos.length);
            counter.textContent = `${start + 1}–${end} of ${photos.length}`;
            // Enabled at a boundary when an adjacent cluster exists to page into.
            prevBtn.disabled = page === 0 && !hasPrevCluster;
            nextBtn.disabled = page >= totalPages - 1 && !hasNextCluster;
        };

        prevBtn.addEventListener('click', () => {
            if (page > 0) { page--; renderPage(); updateNav(); }
            else openSiblingCluster(marker, -1);
        });
        nextBtn.addEventListener('click', () => {
            if (page < totalPages - 1) { page++; renderPage(); updateNav(); }
            else openSiblingCluster(marker, 1);
        });

        nav.append(prevBtn, counter, nextBtn);
        container.appendChild(nav);
        renderPage();
        updateNav();
    } else {
        renderPage();
    }

    return container;
}


/**
 * Fit map to show currently-visible trips' content
 */
function fitMapToBounds() {
    if (window.matchMedia('(max-width: 768px)').matches) map.invalidateSize();
    const bounds = L.latLngBounds([]);
    allTrips.forEach(trip => {
        if (!shouldDisplayTrip(trip)) return;
        const entry = tripLayers[trip.id];
        try { bounds.extend(entry.route.getBounds()); } catch (e) {}
        if (entry.markers.getLayers().length > 0) {
            bounds.extend(entry.markers.getBounds());
        }
    });
    if (bounds.isValid()) {
        const isMobile = window.matchMedia('(max-width: 768px)').matches;
        map.fitBounds(bounds, { padding: [50, 50], animate: !isMobile });
    }
}

/**
 * Mobile: floating See All button + inline password modal.
 * Only rendered if non-public trips exist; hidden on desktop via CSS.
 */
function openMobilePwModal() {
    if (checkAllAccess()) {
        document.getElementById('mobile-pw-overlay').dataset.mode = 'lock';
        document.querySelector('.mobile-pw-title').textContent = 'Return to public trips only?';
        document.getElementById('mobile-pw-input').style.display = 'none';
        document.getElementById('mobile-pw-submit').textContent = 'Yes, lock';
        document.getElementById('mobile-pw-error').textContent = '';
    } else {
        document.getElementById('mobile-pw-overlay').dataset.mode = 'unlock';
        document.querySelector('.mobile-pw-title').textContent = '🔒 Unlock All Trips';
        document.getElementById('mobile-pw-input').style.display = '';
        document.getElementById('mobile-pw-submit').textContent = 'Unlock';
        document.getElementById('mobile-pw-error').textContent = '';
    }
    document.getElementById('mobile-pw-overlay').classList.add('visible');
    if (!checkAllAccess()) {
        document.getElementById('mobile-pw-input').focus();
    }
}
window.openAllTripsModal = openMobilePwModal;

function initMobileControls() {
    const hasNonPublic = allTripsMeta.some(t => t.public === false);
    const trigger = document.getElementById('mobile-see-all-trigger');
    if (!trigger || !hasNonPublic) return;

    updateMobileSeeAll();

    trigger.addEventListener('click', openMobilePwModal);

    document.getElementById('mobile-pw-cancel').addEventListener('click', closeMobilePwModal);

    document.getElementById('mobile-pw-submit').addEventListener('click', async () => {
        const mode = document.getElementById('mobile-pw-overlay').dataset.mode;
        if (mode === 'lock') {
            document.cookie = 'all_access=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
            lockAllAccess();
            window.SidebarNav && window.SidebarNav.refresh();
            closeMobilePwModal();
        } else {
            await mobileSubmitPassword();
        }
    });

    document.getElementById('mobile-pw-input').addEventListener('keydown', e => {
        if (e.key === 'Enter') mobileSubmitPassword();
        if (e.key === 'Escape') closeMobilePwModal();
    });

    document.getElementById('mobile-pw-overlay').addEventListener('click', e => {
        if (e.target === document.getElementById('mobile-pw-overlay')) closeMobilePwModal();
    });
}

function closeMobilePwModal() {
    const overlay = document.getElementById('mobile-pw-overlay');
    overlay.classList.remove('visible');
    const input = document.getElementById('mobile-pw-input');
    input.value = '';
    input.style.display = '';
    document.getElementById('mobile-pw-error').textContent = '';
    document.getElementById('mobile-pw-submit').textContent = 'Unlock';
}

async function mobileSubmitPassword() {
    const input = document.getElementById('mobile-pw-input');
    const errorEl = document.getElementById('mobile-pw-error');
    const btn = document.getElementById('mobile-pw-submit');
    const password = input.value.trim();
    if (!password) return;
    btn.disabled = true;
    btn.textContent = 'Unlocking…';
    errorEl.textContent = '';
    try {
        const fd = new FormData();
        fd.append('password', password);
        const res = await fetch('/auth-all', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.ok) {
            closeMobilePwModal();
            // Blur first so iOS keyboard dismisses before we touch the viewport
            document.getElementById('mobile-pw-input').blur();
            // Wait for keyboard to fully retract and iOS to restore compositing
            await new Promise(r => setTimeout(r, 450));
            map.invalidateSize();
            await unlockAllAccess();
            window.SidebarNav && window.SidebarNav.refresh();
            // Force-repaint fixed controls after all viewport changes settle
            requestAnimationFrame(() => repaintFixedControls());
        } else {
            errorEl.textContent = 'Incorrect password';
            input.value = '';
            input.focus();
            btn.disabled = false;
            btn.textContent = 'Unlock';
        }
    } catch {
        errorEl.textContent = 'Connection error';
        btn.disabled = false;
        btn.textContent = 'Unlock';
    }
}

function repaintFixedControls() {
    const ids = ['sidebar-toggle', 'mobile-see-all-trigger'];
    const selectors = ['.map-style-control', '.map-top-filters', '.route-filter-wrapper'];
    const els = [
        ...ids.map(id => document.getElementById(id)),
        ...selectors.map(s => document.querySelector(s)),
    ].filter(Boolean);
    els.forEach(el => { el.style.display = 'none'; });
    void document.body.offsetHeight;
    els.forEach(el => { el.style.display = ''; });
}
window.repaintFixedControls = repaintFixedControls;

// Called by sidebar.js to force iOS visual-viewport sync after keyboard
// dismissal or sidebar close.  invalidateSize recalculates the Leaflet
// container, and the scroll-reset nudges iOS out of any lingering
// keyboard-height offset on the layout viewport.
window.remeasureMap = function() {
    if (map && typeof map.invalidateSize === 'function') {
        map.invalidateSize({ animate: false });
    }
    // iOS can retain a non-zero document scroll offset after the soft
    // keyboard retracts even with overflow:hidden / position:fixed on the
    // body.  Resetting it prevents controls from rendering above the fold.
    try {
        document.body.scrollTop = 0;
        document.documentElement.scrollTop = 0;
        window.scrollTo(0, 0);
    } catch (_) {}
};

function updateMobileSeeAll() {
    const trigger = document.getElementById('mobile-see-all-trigger');
    if (!trigger) return;
    const unlocked = checkAllAccess();
    trigger.textContent = unlocked ? '✓ All trips' : '🔒 See All';
    trigger.classList.toggle('mobile-see-all-trigger--unlocked', unlocked);
}

/**
 * Initialize PhotoSwipe item list
 */
function initLightbox() {
    rebuildLightbox();
}

function rebuildLightbox() {
    pswpItems = [];
    photoIndexMap = {};
    // Per visible trip, the set of photo ids whose cluster passes the country
    // filter — so the gallery never pages into hidden-country photos.
    const visiblePhotoIds = {};
    allTrips.filter(shouldDisplayTrip).forEach(trip => {
        const ids = new Set();
        visibleMarkersForTrip(trip).forEach(m => m.photoData.forEach(p => ids.add(p.id)));
        visiblePhotoIds[trip.id] = ids;
    });
    allManifests.forEach(manifest => {
        const ids = visiblePhotoIds[manifest.tripId];
        if (!ids) return;
        manifest.photos.forEach(photo => {
            if (!ids.has(photo.id)) return;
            // Key by trip + id: photo ids (file stems like DJI_0099) collide across trips.
            photoIndexMap[`${manifest.tripId}::${photo.id}`] = pswpItems.length;
            const thumbUrl = resolveUrl(manifest.tripPath, photo.thumbnail);
            // If the thumbnail is already in the browser cache (shown in map markers/popups),
            // use its aspect ratio to size the slide so msrc shows immediately.
            const probe = new Image();
            probe.src = thumbUrl;
            let w = 2160, h = 1440, needsSize = true; // provisional; corrected on open
            if (probe.complete && probe.naturalWidth > 0) {
                const ratio = probe.naturalWidth / probe.naturalHeight;
                w = ratio >= 1 ? 2160 : Math.round(2160 * ratio);
                h = ratio >= 1 ? Math.round(2160 / ratio) : 2160;
                needsSize = false; // ratio-correct, no distortion
            }
            pswpItems.push({
                src: resolveUrl(manifest.tripPath, photo.display),
                msrc: thumbUrl,
                w,
                h,
                _needsSize: needsSize
            });
        });
    });
}

/**
 * Double-tap + hold + drag up/down to continuously zoom.
 *
 * Listens at document level with capture:true — this fires before ANY handler
 * on any child element, including PhotoSwipe's own listeners, regardless of
 * whether PhotoSwipe uses capture or bubble phase.
 */
function addDoubleTapDragZoom(gallery, pswpEl) {
    let lastTapTime = 0;
    let dragActive = false;
    let dragStartY = 0;
    let dragStartZoom = 1;
    const TAP_GAP = 300;
    const MAX_ZOOM = 3;

    function currentZoom() {
        if (!gallery.currItem) return 0.5;
        return gallery.currItem.currZoomLevel || gallery.currItem.initialZoomLevel || 0.5;
    }
    function minZoom() {
        return (gallery.currItem && gallery.currItem.initialZoomLevel) || 0.1;
    }

    function onStart(e) {
        if (!pswpEl.classList.contains('pswp--open')) return;
        if (e.touches.length !== 1) { dragActive = false; lastTapTime = 0; return; }
        const now = Date.now();
        if (lastTapTime > 0 && now - lastTapTime < TAP_GAP) {
            dragActive = true;
            dragStartY = e.touches[0].clientY;
            dragStartZoom = currentZoom();
            lastTapTime = 0;
            e.stopImmediatePropagation();
            e.preventDefault();
        } else {
            lastTapTime = now;
            dragActive = false;
        }
    }

    function onMove(e) {
        if (!dragActive || e.touches.length !== 1) return;
        e.stopImmediatePropagation();
        e.preventDefault();
        const dy = dragStartY - e.touches[0].clientY;
        const newZoom = Math.min(MAX_ZOOM, Math.max(minZoom(), dragStartZoom * Math.pow(1.004, dy)));
        gallery.zoomTo(newZoom, { x: e.touches[0].clientX, y: e.touches[0].clientY }, 0);
    }

    function onEnd() { dragActive = false; }

    document.addEventListener('touchstart',  onStart, { passive: false, capture: true });
    document.addEventListener('touchmove',   onMove,  { passive: false, capture: true });
    document.addEventListener('touchend',    onEnd,   { capture: true });
    document.addEventListener('touchcancel', onEnd,   { capture: true });

    gallery.listen('destroy', () => {
        document.removeEventListener('touchstart',  onStart, { capture: true });
        document.removeEventListener('touchmove',   onMove,  { capture: true });
        document.removeEventListener('touchend',    onEnd,   { capture: true });
        document.removeEventListener('touchcancel', onEnd,   { capture: true });
    });
}

/**
 * Open PhotoSwipe gallery at a specific photo
 */
function openGallery(photo) {
    // Accept a photo object (preferred) or a bare id for backward-compat.
    const uid = (photo && typeof photo === 'object')
        ? `${photo.tripId}::${photo.id}` : photo;
    const index = photoIndexMap[uid];
    if (index === undefined) return;

    map.closePopup();
    const pswpEl = document.querySelector('.pswp');
    const gallery = new PhotoSwipe(pswpEl, PhotoSwipeUI_Default, pswpItems, {
        index,
        history: false,
        loop: true,
        shareEl: false,
        fullscreenEl: false,
        tapToClose: false,
        bgOpacity: 0.95,
        showHideOpacity: true
    });

    // Resolve real dimensions after load so PhotoSwipe sizes slides correctly.
    // Items that only have provisional (fallback) dimensions are flagged
    // _needsSize — load the real image and set true dimensions to avoid stretch.
    gallery.listen('gettingData', (idx, item) => {
        if (!item._needsSize) return;
        const img = new Image();
        img.onload = () => {
            item.w = img.naturalWidth;
            item.h = img.naturalHeight;
            item._needsSize = false;
            gallery.invalidateCurrItems();
            gallery.updateSize(true);
        };
        img.src = item.src;
    });

    gallery.init();
    addDoubleTapDragZoom(gallery, pswpEl);
}

function reinitLightbox() {
    rebuildLightbox();
}

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', init);
