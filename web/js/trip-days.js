/**
 * Trip days: the "Day N [Regions] Place - Place" folder names of a trip's raw
 * footage, as the gallery's day headings and the map's day breakdown.
 *
 * Every photo already carries its raw folder's name (manifest `building`, set
 * from --raws-root when the trip is processed), so nothing is read from the
 * drive at page load. On localhost the reel feeder (video project,
 * tools/vlog/reel_site.py) also writes /phone/trip_days.json with each day's
 * date taken from the footage itself; where it exists it supplies the date,
 * otherwise the most common photo date of the day is used.
 */
window.TripDays = (function () {
    'use strict';

    const DAY_RE = /^Day\s+(\d+)\b\s*(?:\[([^\]]*)\])?\s*(.*)$/i;

    /** "Day 3 [Gansu] Jinchang - Gaotai - Jiayuguan" → {n, regions, places, title, label}. */
    function parse(name) {
        const m = DAY_RE.exec((name || '').trim());
        if (!m) return null;
        const n = parseInt(m[1], 10);
        const regions = (m[2] || '').split(',').map(s => s.trim()).filter(Boolean);
        // ":" stands in for "/" in folder names ("Wudaoliang : Budong").
        const rest = (m[3] || '').replace(/\s*:\s*/g, ' / ').trim();
        const places = rest.split(/\s+-\s+/).map(s => s.trim()).filter(Boolean);
        const title = places.join(' → ');
        return { n, regions, places, title, label: title ? `Day ${n} · ${title}` : `Day ${n}` };
    }

    /** A cluster / folder name as shown to people: day folders read "Day 3 · A → B". */
    function pretty(name) {
        const d = parse(name);
        return d ? d.label : name;
    }

    let localP = null;
    /** {tripId: {days: [{n, date, name, ...}]}} from the local feeder, or {} anywhere else. */
    function loadLocal() {
        if (!localP) {
            localP = (window.Gallery && Gallery.isLocal) || /^(localhost|127\.0\.0\.1)$/.test(location.hostname)
                ? fetch('/phone/trip_days.json', { cache: 'no-store' })
                    .then(r => (r.ok ? r.json() : {})).catch(() => ({}))
                : Promise.resolve({});
        }
        return localP;
    }

    function mode(values) {
        const c = new Map();
        values.filter(Boolean).forEach(v => c.set(v, (c.get(v) || 0) + 1));
        let best = null, bestN = 0;
        c.forEach((count, v) => { if (count > bestN) { best = v; bestN = count; } });
        return best;
    }

    /**
     * Photos ({building|day folder, date, timestamp, ...}) → ordered day groups
     * [{n, info, photos, date}] plus the leftovers that sit in no day folder.
     */
    function group(photos, folderOf) {
        const days = new Map();
        const rest = [];
        photos.forEach(p => {
            const info = parse(folderOf(p));
            if (!info) { rest.push(p); return; }
            if (!days.has(info.n)) days.set(info.n, { n: info.n, info, photos: [] });
            days.get(info.n).photos.push(p);
        });
        const out = [...days.values()].sort((a, b) => a.n - b.n);
        out.forEach(g => { g.date = mode(g.photos.map(p => p.date)); });
        return { days: out, rest };
    }

    function fmtDate(iso, long) {
        if (!iso) return '';
        try {
            return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-GB', long
                ? { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' }
                : { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
        } catch (e) { return iso; }
    }

    /** The feeder's date for day n of a trip, when it has one. */
    async function dateOf(tripId, n) {
        const doc = await loadLocal();
        const t = doc[tripId];
        const d = t && (t.days || []).find(x => x.n === n);
        return d ? d.date : null;
    }

    // ── Map: day badges along the route + a "Days" list that frames each day ──
    function addToMap(map, manifest, trip, color) {
        if (!map || !manifest || !window.L) return;
        const photos = (manifest.photos || []).filter(p => p.lat != null && p.lon != null)
            .map(p => Object.assign({}, p, { date: (p.timestamp || '').slice(0, 10) }))
            .sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
        const { days } = group(photos, p => p.building);
        if (days.length < 2) return;

        const badges = L.layerGroup();
        days.forEach(g => {
            g.bounds = L.latLngBounds(g.photos.map(p => [p.lat, p.lon]));
            const first = g.photos[0];
            const icon = L.divIcon({
                className: 'trip-day-badge',
                html: `<span style="border-color:${color || '#fff'}">Day ${g.n}</span>`,
                iconSize: [50, 20], iconAnchor: [25, 10]
            });
            const m = L.marker([first.lat, first.lon], { icon, zIndexOffset: 500, keyboard: false });
            m.bindTooltip(g.info.label, { direction: 'top', offset: [0, -10], className: 'route-tooltip' });
            m.on('click', () => frame(g));
            badges.addLayer(m);
        });
        badges.addTo(map);

        function frame(g) {
            map.fitBounds(g.bounds.pad(0.15), { maxZoom: 13, animate: true });
            panel.querySelectorAll('.trip-days-row').forEach(r =>
                r.classList.toggle('active', +r.dataset.n === g.n));
        }

        const Ctl = L.Control.extend({
            options: { position: 'topright' },
            onAdd() {
                const box = L.DomUtil.create('div', 'leaflet-control trip-days-panel');
                L.DomEvent.disableClickPropagation(box);
                L.DomEvent.disableScrollPropagation(box);
                return box;
            }
        });
        const ctl = new Ctl();
        map.addControl(ctl);
        const panel = ctl.getContainer();
        const head = document.createElement('button');
        head.type = 'button';
        head.className = 'trip-days-head';
        head.textContent = `Days (${days.length})`;
        const list = document.createElement('div');
        list.className = 'trip-days-list';
        const collapsed = window.matchMedia('(max-width: 768px)').matches;
        panel.classList.toggle('collapsed', collapsed);
        head.addEventListener('click', () => panel.classList.toggle('collapsed'));
        panel.append(head, list);
        days.forEach(g => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'trip-days-row';
            row.dataset.n = g.n;
            const num = document.createElement('span');
            num.className = 'trip-days-n';
            num.textContent = g.n;
            const txt = document.createElement('span');
            txt.className = 'trip-days-text';
            const t1 = document.createElement('span');
            t1.className = 'trip-days-title';
            t1.textContent = g.info.title || `Day ${g.n}`;
            const t2 = document.createElement('span');
            t2.className = 'trip-days-sub';
            t2.textContent = [fmtDate(g.date), g.info.regions.join(', '),
                `${g.photos.length} photo${g.photos.length === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
            txt.append(t1, t2);
            row.append(num, txt);
            row.addEventListener('click', () => frame(g));
            list.appendChild(row);
            dateOf(trip.id, g.n).then(d => {
                if (d && d !== g.date) {
                    g.date = d;
                    t2.textContent = [fmtDate(d), g.info.regions.join(', '),
                        `${g.photos.length} photo${g.photos.length === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
                }
            });
        });
    }

    return { parse, pretty, group, fmtDate, dateOf, loadLocal, addToMap };
})();
