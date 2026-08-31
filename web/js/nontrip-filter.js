/**
 * Non-trip filter for the local-only phone library.
 *
 * The phone library holds two kinds of bucket (see tools/build_phone_site.py):
 * real trips, and "non-trip" month buckets built from everything else on the
 * phone — the camera roll between trips, WhatsApp, Snapchat, old phone backups.
 * There are tens of thousands of the latter and they would otherwise bury the
 * trips, so they are hidden by default and revealed with a "Non-trip" filter
 * that sits alongside the year filters on each page. The preference lives in
 * localStorage and applies across map, galleries, sidebar and the Posts
 * companion.
 *
 * Which sources exist at all is a separate, upstream decision made in
 * local_browse/nontrip_sources.json: disabling a source there means its photos
 * are never built into the library, and no toggle brings them back. This file
 * only filters what was built.
 *
 * Inert outside phone mode: the camera library has no non-trip buckets, so
 * filterTrips() is a pass-through there and no filter button is drawn.
 */
(function () {
    'use strict';

    const KEY = 'phoneShowNonTrip';
    const phoneLib = new URLSearchParams(location.search).get('library') === 'phone';

    function shown() {
        try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
    }

    function setShown(v) {
        try { localStorage.setItem(KEY, v ? '1' : '0'); } catch (e) { /* private mode */ }
    }

    /**
     * Drop non-trip buckets unless the toggle is on. Takes and returns the raw
     * index.trips array, so every consumer filters identically from one place.
     * Also records whether any non-trip bucket exists, which is what decides
     * whether the toggle is worth drawing at all.
     */
    function filterTrips(trips) {
        if (!Array.isArray(trips)) return trips;
        if (trips.some(t => t && t.nontrip)) window._hasNonTrip = true;
        if (!phoneLib || shown()) return trips;
        return trips.filter(t => !(t && t.nontrip));
    }

    window.NonTrip = { shown, setShown, filterTrips, active: phoneLib };
})();
