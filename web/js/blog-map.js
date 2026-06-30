/*
 * Blog trip-overview map.
 *
 * Each blog declares its trip slug(s) in config/blogs.json (window.BLOG.trips). This
 * draws a compact interactive Leaflet map at the top of the post showing only each
 * trip's recorded route (route.geojson), fit to the combined bounds. route.geojson is
 * static JSON served from /trips/<slug>/ on both local and deployed.
 *
 * Scroll-zoom is enabled only while the cursor is over the map, so trackpad zoom works
 * on hover but never hijacks page scrolling once the pointer leaves.
 */
(function () {
    const BLOG = window.BLOG || {};
    const trips = Array.isArray(BLOG.trips) ? BLOG.trips : [];
    const wrap = document.getElementById('blog-trip-map-wrap');
    const el = document.getElementById('blog-trip-map');
    if (!el || !wrap || !trips.length || typeof L === 'undefined') {
        if (wrap) wrap.remove();
        return;
    }

    const ROUTE_COLORS = ['#e11d48', '#2563eb', '#16a34a', '#ca8a04', '#9333ea', '#dc2626'];

    const map = L.map(el, { scrollWheelZoom: false, zoomControl: true });
    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
        attribution: '© OpenStreetMap, © CARTO', maxZoom: 19,
    }).addTo(map);
    el.addEventListener('mouseenter', () => map.scrollWheelZoom.enable());
    el.addEventListener('mouseleave', () => map.scrollWheelZoom.disable());

    const bounds = L.latLngBounds([]);

    const jobs = trips.map((slug, i) => {
        const color = ROUTE_COLORS[i % ROUTE_COLORS.length];
        return fetch(`/trips/${slug}/route.geojson?t=${Date.now()}`)
            .then(r => (r.ok ? r.json() : null))
            .then(geo => {
                if (!geo) return;
                const layer = L.geoJSON(geo, { style: { color, weight: 3, opacity: 0.85 } }).addTo(map);
                try { bounds.extend(layer.getBounds()); } catch (e) { /* empty geom */ }
            })
            .catch(() => {});
    });

    Promise.all(jobs).then(() => {
        map.invalidateSize();
        if (bounds.isValid()) {
            map.fitBounds(bounds, { padding: [30, 30], maxZoom: 12 });
        } else {
            wrap.remove();
        }
    });
})();
