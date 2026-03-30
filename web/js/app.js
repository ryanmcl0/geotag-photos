/**
 * Travel Photography Map - Main Application
 */

// Configuration
const CONFIG = {
    // Map settings
    defaultCenter: [38.0, 82.0], // Default center
    defaultZoom: 6,
    maxZoom: 18,

    // Clustering settings - more aggressive clustering
    clusterRadius: 80,
    disableClusteringAtZoom: 18,

    // Route styling (colors for different trips)
    routeColors: ['#e11d48', '#2563eb', '#16a34a', '#ca8a04', '#9333ea', '#dc2626'],
    routeWeight: 3,
    routeOpacity: 0.9
};

// Global state
let map;
let markerClusterGroup;
let routeLayers = [];
let allTrips = [];
let allManifests = [];
let showExif = false;
let lightbox;

/**
 * Initialize the application
 */
async function init() {
    initMap();
    await loadTripData();
    initLightbox();
    initExifToggle();
}

/**
 * Initialize Leaflet map
 */
function initMap() {
    map = L.map('map', {
        center: CONFIG.defaultCenter,
        zoom: CONFIG.defaultZoom,
        zoomControl: true
    });

    // Add tile layer (CartoDB Positron - English labels)
    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
        maxZoom: CONFIG.maxZoom,
        attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
    }).addTo(map);

    // Initialize marker cluster group with better settings
    markerClusterGroup = L.markerClusterGroup({
        maxClusterRadius: CONFIG.clusterRadius,
        disableClusteringAtZoom: CONFIG.disableClusteringAtZoom,
        spiderfyOnMaxZoom: false,
        showCoverageOnHover: false,
        zoomToBoundsOnClick: true,
        animate: true,
        animateAddingMarkers: false,
        iconCreateFunction: createClusterIcon
    });

    map.addLayer(markerClusterGroup);
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
        // Get base path from VIEW_CONFIG
        const basePath = (typeof VIEW_CONFIG !== 'undefined' && VIEW_CONFIG.basePath) || '';

        // Load trips index
        const indexResponse = await fetch(`${basePath}trips/index.json`);
        const index = await indexResponse.json();
        let trips = index.trips;

        if (trips.length === 0) {
            document.getElementById('trip-name').textContent = 'No trips found';
            return;
        }

        // Filter trips based on VIEW_CONFIG
        if (typeof VIEW_CONFIG !== 'undefined') {
            if (VIEW_CONFIG.mode === 'year' && VIEW_CONFIG.year) {
                trips = trips.filter(t => {
                    const tripYear = t.year || new Date(t.dates.start).getFullYear();
                    return tripYear === VIEW_CONFIG.year;
                });
            } else if (VIEW_CONFIG.mode === 'trip' && VIEW_CONFIG.tripId) {
                trips = trips.filter(t => t.id === VIEW_CONFIG.tripId);
            }
        }

        allTrips = trips;

        if (allTrips.length === 0) {
            document.getElementById('trip-name').textContent = 'No trips found';
            return;
        }

        // Load all trip manifests and routes
        for (let i = 0; i < allTrips.length; i++) {
            const trip = allTrips[i];
            const tripPath = `${basePath}${trip.path}`;
            const color = CONFIG.routeColors[i % CONFIG.routeColors.length];

            // Load manifest
            const manifestResponse = await fetch(`${tripPath}/manifest.json`);
            const manifest = await manifestResponse.json();
            manifest.tripId = trip.id;
            manifest.tripIndex = i;
            manifest.tripPath = tripPath; // Store full path for later use
            allManifests.push(manifest);

            // Load and add route to map
            const routeResponse = await fetch(`${tripPath}/route.geojson`);
            const routeData = await routeResponse.json();
            addRouteToMap(routeData, color, trip.name);
        }

        // Update UI
        updateTripInfo();

        // Add all photo markers
        addAllPhotoMarkers();

        // Fit map to content
        fitMapToBounds();

    } catch (error) {
        console.error('Failed to load trip data:', error);
        document.getElementById('trip-name').textContent = 'Error loading trip data';
    }
}

/**
 * Update trip info overlay
 */
function updateTripInfo() {
    const totalPhotos = allManifests.reduce((sum, m) => sum + m.photos.length, 0);
    const viewConfig = typeof VIEW_CONFIG !== 'undefined' ? VIEW_CONFIG : { mode: 'all' };

    let titleText = '';
    let subtitleText = '';

    if (viewConfig.mode === 'trip' && allTrips.length === 1) {
        // Single trip view
        titleText = allTrips[0].name;
        subtitleText = `${formatDate(allTrips[0].dates.start)} - ${formatDate(allTrips[0].dates.end)}`;
    } else if (viewConfig.mode === 'year' && viewConfig.year) {
        // Year view
        titleText = `${viewConfig.year}`;
        subtitleText = allTrips.map(t => t.name).join(', ');
    } else {
        // All trips view
        titleText = allTrips.length === 1 ? allTrips[0].name : `${allTrips.length} Trips`;
        if (allTrips.length === 1) {
            subtitleText = `${formatDate(allTrips[0].dates.start)} - ${formatDate(allTrips[0].dates.end)}`;
        } else {
            subtitleText = allTrips.map(t => t.name).join(', ');
        }
    }

    document.getElementById('trip-name').textContent = titleText;
    document.getElementById('trip-dates').textContent = subtitleText;
    document.getElementById('photo-count').textContent = `${totalPhotos} photos`;
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
 * Add route polyline to map
 */
function addRouteToMap(routeData, color, tripName) {
    const layer = L.geoJSON(routeData, {
        style: {
            color: color,
            weight: CONFIG.routeWeight,
            opacity: CONFIG.routeOpacity
        }
    }).addTo(map);

    // Add tooltip showing trip name
    layer.bindTooltip(tripName, {
        permanent: false,
        sticky: true
    });

    routeLayers.push(layer);
}

/**
 * Add photo markers from all trips to cluster group
 */
function addAllPhotoMarkers() {
    // Combine all photos from all manifests
    allManifests.forEach(manifest => {
        // Create lookup for quick photo access
        const photoLookup = {};
        manifest.photos.forEach(photo => {
            photo.tripName = manifest.trip_name; // Add trip name to each photo
            photo.tripIndex = manifest.tripIndex; // Add trip index for coloring
            photo.tripId = manifest.tripId; // Add trip ID for path lookup
            photo.tripPath = manifest.tripPath; // Add trip path for asset URLs
            photoLookup[photo.id] = photo;
        });

        // Add markers for each cluster
        manifest.clusters.forEach(cluster => {
            const photos = cluster.photo_ids.map(id => photoLookup[id]);

            // Use first photo's thumbnail for the marker
            const thumbnailUrl = `${manifest.tripPath}/${photos[0].thumbnail}`;

            // Create marker
            const marker = L.marker([cluster.lat, cluster.lon], {
                icon: createPhotoIcon(photos.length, thumbnailUrl)
            });

            // Bind popup
            if (photos.length === 1) {
                marker.bindPopup(() => createSinglePhotoPopup(photos[0], cluster.location));
            } else {
                marker.bindPopup(() => createMultiPhotoPopup(photos, cluster.location));
            }

            // Store photo data on marker for gallery
            marker.photoData = photos;
            marker.locationName = cluster.location;

            markerClusterGroup.addLayer(marker);
        });
    });
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
    const exifHtml = showExif ? createExifHtml(photo) : '';

    return `
        <div class="photo-popup">
            <img src="${photo.tripPath}/${photo.thumbnail}"
                 alt="${location}"
                 class="popup-thumbnail"
                 data-photo-id="${photo.id}"
                 onclick="openGallery('${photo.id}')">
            <div class="popup-info">
                <strong>${location}</strong>
                <div style="font-size: 0.85em; color: #666;">${photo.tripName}</div>
                ${exifHtml}
            </div>
        </div>
    `;
}

/**
 * Create popup for multiple photos
 */
function createMultiPhotoPopup(photos, location) {
    const thumbnails = photos.map(photo => `
        <img src="${photo.tripPath}/${photo.thumbnail}"
             alt="${photo.id}"
             data-photo-id="${photo.id}"
             onclick="openGallery('${photo.id}')">
    `).join('');

    return `
        <div class="cluster-popup">
            <h3>${location} (${photos.length} photos)</h3>
            <div style="font-size: 0.85em; color: #666; margin-bottom: 8px;">${photos[0].tripName}</div>
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
 * Fit map to show all content
 */
function fitMapToBounds() {
    const bounds = L.latLngBounds([]);

    // Include all route bounds
    routeLayers.forEach(layer => {
        bounds.extend(layer.getBounds());
    });

    // Include marker bounds
    if (markerClusterGroup.getLayers().length > 0) {
        bounds.extend(markerClusterGroup.getBounds());
    }

    if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [50, 50] });
    }
}

/**
 * Initialize GLightbox
 */
function initLightbox() {
    // Build gallery elements from all manifests
    const galleryContainer = document.getElementById('gallery');

    allManifests.forEach(manifest => {
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

    // Initialize GLightbox
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

/**
 * Reinitialize lightbox (e.g., after EXIF toggle)
 */
function reinitLightbox() {
    if (lightbox) {
        lightbox.destroy();
    }

    // Clear and rebuild gallery
    const galleryContainer = document.getElementById('gallery');
    galleryContainer.innerHTML = '';

    allManifests.forEach(manifest => {
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

    lightbox = GLightbox({
        selector: '.glightbox',
        touchNavigation: true,
        loop: true
    });
}

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', init);
