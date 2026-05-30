/**
 * Travel Photography Map - Main Application
 */

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
let lightbox;

// Per-trip layers — so each trip can be toggled on/off independently.
// tripLayers[tripId] = { route: L.GeoJSON, markers: L.MarkerClusterGroup, visible: bool }
const tripLayers = {};
const loadedTripIds = new Set();

function checkAllAccess() {
    return document.cookie.split(';').some(c => c.trim() === 'all_access=1');
}

const HIDDEN_TRIPS_STORAGE_KEY = 'geotagPhotos.hiddenTrips';
let activeRouteFilter = 'all'; // 'all' | 'gpx'

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

function shouldDisplayTrip(trip) {
    const layer = tripLayers[trip.id];
    return Boolean(layer && layer.visible && tripMatchesYearFilter(trip) && tripMatchesRouteFilter(trip));
}

function syncVisibleTripLayers({ fit = false } = {}) {
    allTrips.forEach(trip => {
        const layer = tripLayers[trip.id];
        if (!layer) return;
        const show = shouldDisplayTrip(trip);
        if (show) {
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
    document.getElementById('map').appendChild(wrapper);

    const btn = wrapper.querySelector('#yearFilterBtn');
    const menu = wrapper.querySelector('#yearFilterMenu');

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
            await loadSingleTrip(trip, basePath);
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
    tripLayers[trip.id] = {
        route: buildRouteLayer(routeData, color, trip.name),
        markers: buildMarkerLayer(manifest),
        color,
        hasGpx: Boolean(manifest.source && manifest.source.gpx_path),
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
    const visibleTripIds = new Set(visibleTrips.map(t => t.id));
    const visibleManifests = allManifests.filter(m => visibleTripIds.has(m.tripId));
    const totalPhotos = visibleManifests.reduce((sum, m) => sum + m.photos.length, 0);
    const uniqueCountries = new Set(visibleTrips.flatMap(t => t.countries || []));
    const countryNames = new Intl.DisplayNames(['en'], { type: 'region' });
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

    const countryText = uniqueCountries.size > 0 ? ` · ${uniqueCountries.size} countries` : '';
    document.getElementById('photo-count').textContent =
        `${totalPhotos.toLocaleString()} photos${countryText}`;

    const countryListEl = document.getElementById('country-list');
    if (countryListEl) {
        countryListEl.textContent = uniqueCountries.size > 0
            ? [...uniqueCountries].map(cc => { try { return countryNames.of(cc); } catch { return cc; } }).sort().join(', ')
            : '';
    }
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
function buildMarkerLayer(manifest) {
    const group = makeClusterGroup();
    const photoLookup = {};
    manifest.photos.forEach(photo => {
        photo.tripName = manifest.trip_name;
        photo.tripIndex = manifest.tripIndex;
        photo.tripId = manifest.tripId;
        photo.tripPath = manifest.tripPath;
        photoLookup[photo.id] = photo;
    });

    manifest.clusters.forEach(cluster => {
        const photos = cluster.photo_ids.map(id => photoLookup[id]);
        const thumbnailUrl = resolveUrl(manifest.tripPath, photos[0].thumbnail);
        const marker = L.marker([cluster.lat, cluster.lon], {
            icon: createPhotoIcon(photos.length, thumbnailUrl)
        });
        if (photos.length === 1) {
            marker.bindPopup(() => createSinglePhotoPopup(photos[0], cluster.location));
        } else {
            marker.bindPopup(() => createMultiPhotoPopup(photos, cluster.location));
        }
        marker.photoData = photos;
        marker.locationName = cluster.location;
        group.addLayer(marker);
    });
    return group;
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

/**
 * Create icon for photo marker with thumbnail preview
 */
function createPhotoIcon(count, thumbnailUrl) {
    const countBadge = count > 1 ? `<span class="photo-marker-count">${count}</span>` : '';

    return L.divIcon({
        html: `
            <div class="photo-marker-wrapper">
                <img src="${thumbnailUrl}" class="photo-marker-thumb" alt="">
                ${countBadge}
            </div>
        `,
        className: 'photo-marker-icon',
        iconSize: L.point(44, 44),
        iconAnchor: L.point(22, 22),
        popupAnchor: L.point(0, -22)
    });
}

/**
 * Create popup for single photo
 */
function createSinglePhotoPopup(photo, location) {
    return `
        <div class="photo-popup">
            <img src="${resolveUrl(photo.tripPath, photo.thumbnail)}"
                 alt=""
                 class="popup-thumbnail"
                 data-photo-id="${photo.id}"
                 onclick="openGallery('${photo.id}')">
        </div>
    `;
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

function createMultiPhotoPopup(photos, location) {
    const container = document.createElement('div');
    container.className = 'cluster-popup';

    if (location) {
        const header = document.createElement('div');
        header.className = 'cluster-popup-header';
        header.textContent = location;
        container.appendChild(header);
    }

    const grid = document.createElement('div');
    grid.className = 'photo-grid';
    container.appendChild(grid);

    const totalPages = Math.ceil(photos.length / CLUSTER_POPUP_PAGE_SIZE);
    let page = 0;

    const renderPage = () => {
        grid.innerHTML = '';
        const start = page * CLUSTER_POPUP_PAGE_SIZE;
        photos.slice(start, start + CLUSTER_POPUP_PAGE_SIZE).forEach(photo => {
            const img = document.createElement('img');
            img.src = resolveUrl(photo.tripPath, photo.thumbnail);
            img.alt = '';
            img.dataset.photoId = photo.id;
            img.addEventListener('click', () => openGallery(photo.id));
            grid.appendChild(img);
        });
    };

    if (totalPages > 1) {
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
            prevBtn.disabled = page === 0;
            nextBtn.disabled = page >= totalPages - 1;
        };

        prevBtn.addEventListener('click', () => {
            if (page > 0) { page--; renderPage(); updateNav(); }
        });
        nextBtn.addEventListener('click', () => {
            if (page < totalPages - 1) { page++; renderPage(); updateNav(); }
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
function initMobileControls() {
    const hasNonPublic = allTripsMeta.some(t => t.public === false);
    const trigger = document.getElementById('mobile-see-all-trigger');
    if (!trigger || !hasNonPublic) return;

    updateMobileSeeAll();

    trigger.addEventListener('click', () => {
        if (checkAllAccess()) {
            document.getElementById('mobile-pw-overlay').dataset.mode = 'lock';
            document.querySelector('.mobile-pw-title').textContent = 'Return to public trips only?';
            document.getElementById('mobile-pw-input').style.display = 'none';
            document.getElementById('mobile-pw-submit').textContent = 'Yes, lock';
            document.getElementById('mobile-pw-error').textContent = '';
            document.getElementById('mobile-pw-overlay').classList.add('visible');
        } else {
            document.getElementById('mobile-pw-overlay').dataset.mode = 'unlock';
            document.querySelector('.mobile-pw-title').textContent = '🔒 Unlock All Trips';
            document.getElementById('mobile-pw-input').style.display = '';
            document.getElementById('mobile-pw-submit').textContent = 'Unlock';
            document.getElementById('mobile-pw-error').textContent = '';
            document.getElementById('mobile-pw-overlay').classList.add('visible');
            document.getElementById('mobile-pw-input').focus();
        }
    });

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
    ['sidebar-toggle', 'mobile-see-all-trigger'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.style.display = 'none';
        void el.offsetHeight;
        el.style.display = '';
    });
    const mc = document.querySelector('.map-style-control');
    if (mc) { mc.style.display = 'none'; void mc.offsetHeight; mc.style.display = ''; }
}
window.repaintFixedControls = repaintFixedControls;

function updateMobileSeeAll() {
    const trigger = document.getElementById('mobile-see-all-trigger');
    if (!trigger) return;
    const unlocked = checkAllAccess();
    trigger.textContent = unlocked ? '✓ All trips' : '🔒 See All';
    trigger.classList.toggle('mobile-see-all-trigger--unlocked', unlocked);
}

/**
 * Initialize GLightbox
 */
function initLightbox() {
    rebuildLightbox();
}

function rebuildLightbox() {
    const galleryContainer = document.getElementById('gallery');
    galleryContainer.innerHTML = '';
    const visibleTripIds = new Set(allTrips.filter(shouldDisplayTrip).map(t => t.id));
    allManifests.forEach(manifest => {
        if (!visibleTripIds.has(manifest.tripId)) return;
        manifest.photos.forEach(photo => {
            const a = document.createElement('a');
            a.href = resolveUrl(manifest.tripPath, photo.display);
            a.className = 'glightbox';
            a.dataset.photoId = photo.id;
            a.dataset.gallery = 'trip-photos';
            galleryContainer.appendChild(a);
        });
    });
    if (lightbox) lightbox.destroy();
    lightbox = GLightbox({
        selector: '.glightbox',
        touchNavigation: true,
        loop: true,
        autoplayVideos: true
    });
}

/**
 * Open gallery at specific photo
 */
function openGallery(photoId) {
    // Find photo index across all manifests
    let photoIndex = 0;
    let found = false;

    for (const manifest of allManifests) {
        const index = manifest.photos.findIndex(p => p.id === photoId);
        if (index !== -1) {
            photoIndex += index;
            found = true;
            break;
        }
        photoIndex += manifest.photos.length;
    }

    if (found) {
        lightbox.openAt(photoIndex);
    }
}

function reinitLightbox() {
    rebuildLightbox();
}

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', init);
