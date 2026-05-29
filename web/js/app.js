/**
 * Travel Photography Map - Main Application
 */

// Configuration
const CONFIG = {
    // Map settings
    defaultCenter: [38.0, 82.0], // Default center
    defaultZoom: 6,
    maxZoom: 18,

    // Clustering settings — keep markers separated until the map is dense
    clusterRadius: 35,
    disableClusteringAtZoom: 13,

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
let showExif = false;
let lightbox;

// Per-trip layers — so each trip can be toggled on/off independently.
// tripLayers[tripId] = { route: L.GeoJSON, markers: L.MarkerClusterGroup, visible: bool }
const tripLayers = {};
const loadedTripIds = new Set();

function checkAllAccess() {
    return document.cookie.split(';').some(c => c.trim() === 'all_access=1');
}

const HIDDEN_TRIPS_STORAGE_KEY = 'geotagPhotos.hiddenTrips';

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
async function init() {
    initMap();
    await loadTripData();
    initLightbox();
    initExifToggle();
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
    initMapStyleControl();
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
    document.getElementById('map').appendChild(ctrl);
}

function makeClusterGroup() {
    return L.markerClusterGroup({
        maxClusterRadius: CONFIG.clusterRadius,
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

        // On 'all' view: show only public trips unless user has all_access cookie
        const viewMode = (typeof VIEW_CONFIG !== 'undefined' && VIEW_CONFIG.mode) || 'all';
        if (viewMode === 'all' && !checkAllAccess()) {
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
        visible: !hidden.has(trip.id),
    };
    if (tripLayers[trip.id].visible) {
        tripLayers[trip.id].route.addTo(map);
        tripLayers[trip.id].markers.addTo(map);
    }

    allTrips.push(trip);
    allManifests.push(manifest);
    loadedTripIds.add(trip.id);
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
}
window.lockAllAccess = lockAllAccess;

/**
 * Update trip info overlay (reflects only currently-visible trips)
 */
function updateTripInfo() {
    const visibleTrips = allTrips.filter(t => !tripLayers[t.id] || tripLayers[t.id].visible);
    const visibleManifests = allManifests.filter(m =>
        !tripLayers[m.tripId] || tripLayers[m.tripId].visible);
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
        const thumbnailUrl = `${manifest.tripPath}/${photos[0].thumbnail}`;
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
    if (visible) {
        entry.route.addTo(map);
        entry.markers.addTo(map);
    } else {
        map.removeLayer(entry.route);
        map.removeLayer(entry.markers);
    }
    const hidden = loadHiddenTripIds();
    if (visible) hidden.delete(tripId);
    else hidden.add(tripId);
    saveHiddenTripIds(hidden);
    updateTripInfo();
    reinitLightbox();
}
window.setTripVisibility = setTripVisibility;

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
    const exifHtml = showExif ? createExifHtml(photo) : '';

    return `
        <div class="photo-popup">
            <img src="${photo.tripPath}/${photo.thumbnail}"
                 alt=""
                 class="popup-thumbnail"
                 data-photo-id="${photo.id}"
                 onclick="openGallery('${photo.id}')">
            ${exifHtml ? `<div class="popup-info">${exifHtml}</div>` : ''}
        </div>
    `;
}

/**
 * Create popup for multiple photos
 */
function createMultiPhotoPopup(photos, location) {
    const thumbnails = photos.map(photo => `
        <img src="${photo.tripPath}/${photo.thumbnail}"
             alt=""
             data-photo-id="${photo.id}"
             onclick="openGallery('${photo.id}')">
    `).join('');

    return `
        <div class="cluster-popup">
            <div class="photo-grid">
                ${thumbnails}
            </div>
        </div>
    `;
}

/**
 * Create EXIF info HTML
 */
function createExifHtml(photo) {
    if (!photo.camera_settings) return '';

    const { iso, aperture, shutter } = photo.camera_settings;
    return `
        <div class="exif-info">
            <span>ISO ${iso}</span>
            <span>${aperture}</span>
            <span>${shutter}</span>
        </div>
    `;
}

/**
 * Fit map to show currently-visible trips' content
 */
function fitMapToBounds() {
    const bounds = L.latLngBounds([]);
    Object.values(tripLayers).forEach(entry => {
        if (!entry.visible) return;
        try { bounds.extend(entry.route.getBounds()); } catch (e) {}
        if (entry.markers.getLayers().length > 0) {
            bounds.extend(entry.markers.getBounds());
        }
    });
    if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [50, 50] });
    }
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
    allManifests.forEach(manifest => {
        if (tripLayers[manifest.tripId] && !tripLayers[manifest.tripId].visible) return;
        manifest.photos.forEach(photo => {
            const a = document.createElement('a');
            a.href = `${manifest.tripPath}/${photo.display}`;
            a.className = 'glightbox';
            a.dataset.photoId = photo.id;
            a.dataset.gallery = 'trip-photos';
            if (showExif && photo.camera_settings) {
                const { iso, aperture, shutter } = photo.camera_settings;
                a.dataset.description = `ISO ${iso} | ${aperture} | ${shutter}`;
            }
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

/**
 * Initialize EXIF toggle button
 */
function initExifToggle() {
    const toggle = document.getElementById('exif-toggle');

    toggle.addEventListener('click', () => {
        showExif = !showExif;
        toggle.classList.toggle('active', showExif);

        // Reinitialize lightbox with/without EXIF descriptions
        reinitLightbox();
    });
}

function reinitLightbox() {
    rebuildLightbox();
}

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', init);
