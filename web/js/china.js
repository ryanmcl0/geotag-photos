/**
 * Collection hub (China, Rooftopping, …): loads web/collections/<id>.json and renders
 * the tile hub + facet views with hash routing (#bridges, #provinces/guizhou, …).
 * Set window.COLLECTION_ID on the page (defaults to 'china').
 * Single-facet collections skip the hub and render the facet directly under the masthead.
 */
(function () {
  const COLL = window.COLLECTION_ID || 'china';
  let DATA = null;
  const app = document.getElementById('app');
  const crumbs = document.getElementById('crumbs');

  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = s => (s == null ? '' : String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));
  const tileById = id => DATA.tiles.find(t => t.id === id);

  /* Facet filters (year pickers) live in the hash as a query string —
   * "#roofs?years=2017,2018" — so clicking into a building and hitting Back
   * returns to the filtered view instead of the full list. Written with
   * replaceState, so ticking years doesn't pile up history entries; only real
   * navigation (a tile link) pushes one. */
  function parseHash() {
    const raw = location.hash.replace(/^#/, '');
    const qi = raw.indexOf('?');
    const query = {};
    if (qi >= 0) {
      raw.slice(qi + 1).split('&').filter(Boolean).forEach(kv => {
        const i = kv.indexOf('=');
        query[decodeURIComponent(i < 0 ? kv : kv.slice(0, i))] =
          i < 0 ? '' : decodeURIComponent(kv.slice(i + 1));
      });
    }
    return { path: qi >= 0 ? raw.slice(0, qi) : raw, query };
  }

  function setHashQuery(patch) {
    const { path, query } = parseHash();
    Object.keys(patch).forEach(k => {
      if (patch[k] == null || patch[k] === '') delete query[k];
      else query[k] = String(patch[k]);
    });
    const qs = Object.keys(query).map(k =>
      // commas read fine in a hash and keep the URL legible
      `${encodeURIComponent(k)}=${encodeURIComponent(query[k]).replace(/%2C/g, ',')}`).join('&');
    history.replaceState(null, '',
      (path || qs) ? `#${path}${qs ? '?' + qs : ''}` : location.pathname + location.search);
  }
  const allSubtiles = tile => (tile.subtiles || []).concat(
    (tile.sections || []).flatMap(sec => sec.subtiles || []));
  const subById = (tile, id) => allSubtiles(tile).find(s => s.id === id);

  // Tile covers render ~900px wide — use the 2160px display webp (lazy-loaded),
  // not the 400px thumbnail, or covers look soft.
  function imgTag(cover) {
    return cover ? `<img class="tile-img" loading="lazy" alt="" onerror="Gallery.lockedCover(this)"
      src="${Gallery.photoUrl(cover, 'display')}">` : '';
  }

  function setCrumbs(parts) {
    crumbs.innerHTML = parts.map((p, i) => {
      const sep = i ? '<span class="sep">›</span>' : '';
      return p.href ? `${sep}<a href="${p.href}">${esc(p.label)}</a>` : `${sep}<span>${esc(p.label)}</span>`;
    }).join(' ');
  }

  /* ---------------- hub ---------------- */
  function buildMasthead() {
    const st = DATA.stats || {};
    const stat = (num, label) =>
      `<div class="stat"><div class="stat-num">${num}</div><div class="stat-label">${esc(label)}</div></div>`;
    const parts = [];
    if (st.provinces) parts.push(stat(`${st.provinces.visited}<span class="stat-frac">/${st.provinces.total}</span>`, 'provinces'));
    if (st.km) parts.push(stat(`${st.km.toLocaleString()}<span class="stat-frac"> km</span>`, 'on the road'));
    if (st.bridges) parts.push(stat(String(st.bridges.visited), `bridges · ${st.bridges.ranked_done}/${st.bridges.ranked_total} highest`));
    if (st.buildings) parts.push(stat(String(st.buildings), 'buildings'));
    if (st.cities) parts.push(stat(String(st.cities), 'cities'));
    if (st.countries) parts.push(stat(String(st.countries), 'countries'));
    if (st.places) parts.push(stat(String(st.places), 'cities & regions'));
    // Blurb under the stats (config/classifications.json `description`): the first
    // paragraph reads as the standfirst, the rest as body copy.
    const paras = DATA.description || [];
    const desc = paras.length
      ? `<div class="masthead-desc">${paras.map((p, i) =>
          `<p class="${i === 0 ? 'masthead-lead' : ''}">${esc(p)}</p>`).join('')}</div>`
      : '';
    return el('header', 'china-masthead',
      `<h1>${esc(DATA.title)}</h1><div class="stat-strip">${parts.join('')}</div>${desc}`);
  }

  function renderHub() {
    setCrumbs([]);  // hub matches the portfolio Work page: nav → grid, no chrome
    const grid = el('div', 'tiles');
    DATA.tiles.forEach(tile => {
      const card = el('a', 'tile');
      const reveal = tile.id === 'provinces' ? buildProvinceRevealHTML(tile) : '';
      card.innerHTML = `
        ${imgTag(tile.cover)}
        ${tile.locked ? '<div class="lock-badge">🔒 See All</div>' : ''}
        <div class="tile-overlay">
          <div class="tile-title">${esc(tile.title)}</div>
          ${tile.infographic ? `<div class="tile-sub">${esc(tile.infographic)}</div>` : ''}
        </div>
        ${reveal}`;
      if (tile.locked) {
        card.href = '#';
        card.addEventListener('click', e => {
          e.preventDefault();
          if (window.Unlock) window.Unlock.open({});
        });
      } else {
        card.href = `#${tile.id}`;
        // Touch devices can't hover the province reveal: first tap shows it,
        // a second tap (or a tap elsewhere, which closes it) navigates.
        if (reveal && window.matchMedia('(hover: none)').matches) {
          card.addEventListener('click', e => {
            if (!card.classList.contains('reveal-open')) {
              e.preventDefault();
              e.stopPropagation();
              card.classList.add('reveal-open');
            }
          });
          document.addEventListener('click', () => card.classList.remove('reveal-open'));
        }
      }
      grid.appendChild(card);
    });
    app.innerHTML = '';
    app.appendChild(buildMasthead());
    app.appendChild(grid);
    observeReveal(grid, '.tile');
  }

  function buildProvinceRevealHTML(tile) {
    // Every province that has been VISITED, which is not the same as every province
    // with photos on the site: a visited one can be locked (all its photos private)
    // or still pending (trip back, photos not edited yet). Only "Not yet visited"
    // is excluded, so the chips match the x/33 count above them.
    const chips = (tile.subtiles || [])
      .filter(s => s.pending !== 'Not yet visited')
      .map(s => `<span class="chip">${esc(s.title)}</span>`).join('');
    return `<div class="tile-reveal">${chips}</div>`;
  }

  // shared scroll-stagger reveal
  function observeReveal(container, selector) {
    const els = container.querySelectorAll(selector);
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: 0.12 });
    els.forEach((el_, i) => {
      el_.classList.add('rv');
      el_.style.transitionDelay = `${(i % 6) * 60}ms`;
      io.observe(el_);
    });
  }

  /* ---------------- facet (tilegroup) views ---------------- */
  function renderFacet(tile) {
    if (tile.kind === 'gallery') return renderGalleryView(tile);
    if (tile.kind === 'tiered_tilegroup') return renderTieredTiles(tile);
    if (tile.id === 'bridges') return renderBridgesRanked(tile);
    setCrumbs([{ label: DATA.title, href: '#' }, { label: tile.title }]);
    app.innerHTML = '';
    const head = el('div', 'section-head',
      `<h2>${esc(tile.title)}</h2>${tile.infographic ? `<span class="count">${esc(tile.infographic)}</span>` : ''}`);
    app.appendChild(head);

    // restored from the hash so Back from a province keeps the filter
    const q = parseHash().query.year;
    const initialYear = q && q !== 'all' && (tile.years || []).includes(Number(q)) ? Number(q) : 'all';
    if (tile.id === 'provinces' && (tile.years || []).length) {
      app.appendChild(buildYearBar(tile.years, year => {
        setHashQuery({ year: year === 'all' ? null : year });
        paintProvinceTiles(tile, year);
      }, initialYear));
    }
    const grid = el('div', 'tiles' + (tile.id === 'roads' ? '' : ' tiles--dense'));
    grid.id = 'facet-grid';
    app.appendChild(grid);
    if (tile.id === 'provinces') paintProvinceTiles(tile, initialYear);
    else {
      tile.subtiles.forEach(s => grid.appendChild(buildSubtile(tile, s)));
      observeReveal(grid, '.tile');
    }
  }

  function paintProvinceTiles(tile, year) {
    const grid = document.getElementById('facet-grid');
    grid.classList.add('tiles--mosaic');
    grid.innerHTML = '';
    // the 3 biggest collections get 2x2 feature tiles — breaks the uniform grid
    const bigIds = new Set(tile.subtiles.filter(s => s.done)
      .slice().sort((a, b) => (b.count || 0) - (a.count || 0)).slice(0, 3).map(s => s.id));
    tile.subtiles.forEach(s => {
      if (s.locked) {
        // all-private provinces show as a locked tile; only on the unfiltered view
        if (year === 'all') grid.appendChild(buildSubtile(tile, s));
        return;
      }
      if (!s.done) {
        // Visited but nothing published yet (photos pending) still shows as a muted
        // tile on the unfiltered view; never-visited provinces stay hidden.
        if (s.pending && s.pending !== 'Not yet visited' && year === 'all') {
          grid.appendChild(buildSubtile(tile, s));
        }
        return;
      }
      if (year !== 'all' && !(s.photos || []).some(p => p.year === year)) return;
      const card = buildSubtile(tile, s, year);
      if (bigIds.has(s.id)) card.classList.add('tile--big');
      grid.appendChild(card);
    });
    observeReveal(grid, '.tile');
  }

  function buildSubtile(tile, s, year) {
    // Gated sub-tile (e.g. a province whose photos are all private): a locked tile
    // behind the See All password, like every other non-public tile.
    if (s.locked) {
      const card = el('a', 'tile tile--locked');
      card.href = '#';
      card.innerHTML = `${imgTag(s.cover)}
        <div class="lock-badge">🔒 See All</div>
        <div class="tile-overlay">
          <div class="tile-title">${esc(s.title)}</div>
          ${s.count ? `<div class="tile-sub">${s.count} photos</div>` : ''}
        </div>`;
      card.addEventListener('click', e => {
        e.preventDefault();
        if (window.Unlock) window.Unlock.open({});
      });
      return card;
    }
    if (!s.done) {
      // Road legs carry car/dates + km even while pending, so the drive still reads
      // on the tile. Province tiles have neither and just show the title.
      const bits = [s.subtitle, s.infographic].filter(Boolean).map(esc).join(' · ');
      // A leg whose GPX is already merged onto the map opens its route, photos or
      // not — the drive is the thing worth seeing while the edits are still to come.
      const linked = s.has_route && s.trip;
      const t = el(linked ? 'a' : 'div', 'tile tile--pending' + (linked ? ' tile--pending-link' : ''));
      if (linked) t.href = `map.html?mode=trip&trip=${encodeURIComponent(s.trip)}`;
      t.innerHTML = `<div class="tile-inner"><div class="tile-title">${esc(s.title)}</div>
        ${bits ? `<div class="tile-sub">${bits}</div>` : ''}
        <div class="pending-tag">${esc(s.pending || 'Pending')}</div>
        ${linked ? '<div class="pending-route-link">View route</div>' : ''}</div>`;
      return t;
    }
    // Roads sub-tiles open the per-trip map; province tiles open the map filtered
    // to that province's photos (more digestible than a 600-photo grid); the rest
    // open an in-hub gallery.
    let href;
    if (s.view === 'map') {
      href = `map.html?mode=trip&trip=${encodeURIComponent(s.trip)}`;
    } else if (tile.id === 'provinces') {
      href = `map.html?collection=${encodeURIComponent(DATA.id)}&facet=${tile.id}` +
             `&sub=${encodeURIComponent(s.id)}&title=${encodeURIComponent(s.title)}`;
    } else {
      // A building climbed in two years is ONE tile, so opening it from a filtered
      // view would otherwise show the other year's photos (Battersea under 2025
      // opened on the 2018 climb). Carry the filter into the gallery, which offers
      // its own All/year bar to see the rest.
      href = `#${tile.id}/${s.id}` + (year && year !== 'all' ? `?year=${year}` : '');
    }
    const zh = s.name_zh ? `<div class="tile-zh">${esc(s.name_zh)}</div>` : '';
    const sub = s.subtitle ? `<div class="tile-sub">${esc(s.subtitle)}</div>` : '';
    const stat = s.infographic ? `<div class="tile-sub">${esc(s.infographic)}</div>` : '';
    const count = s.count ? `<div class="tile-sub">${s.count} photos</div>` : '';
    const inner = `
      ${imgTag(s.cover)}
      <div class="tile-overlay">
        <div class="tile-title">${esc(s.title)}</div>
        ${zh}${sub}${stat}${count}
      </div>`;

    const blogs = s.blogs || [];
    const isProvince = tile.id === 'provinces';

    // No on-tile buttons → the whole tile is one link (the common case).
    if (!isProvince && !blogs.length) {
      const card = el('a', 'tile');
      card.href = href;
      card.innerHTML = inner;
      return card;
    }
    // Province tiles (Map/Gallery toggle) and/or tiles with write-up(s): the tile is
    // a <div> (nested <a> is invalid) holding a full-bleed primary link — the default
    // action, which is the GALLERY for provinces — plus buttons layered on top.
    const card = el('div', 'tile tile--haslink');
    const main = el('a', 'tile-mainlink');
    const galHref = `#${tile.id}/${s.id}` + (year && year !== 'all' ? `?year=${year}` : '');
    main.href = isProvince ? galHref : href;
    main.innerHTML = inner;
    card.appendChild(main);
    const links = el('div', 'tile-bloglinks');

    // Province tiles get a Map/Gallery view toggle controlling how the photos open:
    // Gallery (the default, matching the full-tile link) opens the in-hub photo
    // grid; Map sends to the filtered map. Grouped as a segmented pair so it reads
    // as one control. On mobile .tile-bloglinks shifts to the top-right, clear of the
    // bottom-anchored title/meta.
    if (isProvince) {
      const toggle = el('div', 'tile-viewtoggle');
      const mapBtn = el('a', 'tile-viewbtn', 'Map');
      mapBtn.href = href;
      const galBtn = el('a', 'tile-viewbtn is-active', 'Gallery');
      galBtn.href = galHref;
      toggle.appendChild(mapBtn);
      toggle.appendChild(galBtn);
      links.appendChild(toggle);
    }

    const multi = blogs.length > 1;
    blogs.forEach(b => {
      const link = el('a', 'tile-bloglink' + (b.public ? '' : ' is-gated'));
      link.href = `blogs/${b.slug}.html`;
      // with multiple write-ups, name each; with one, the generic label reads cleaner
      const label = multi ? esc(b.title.replace(/^Deepest [^:]+:\s*/, '')) : 'Read the write-up';
      link.innerHTML = `${b.public ? '' : '🔒 '}${label} →`;
      if (!b.public && !(window.Unlock && window.Unlock.unlocked())) {
        link.addEventListener('click', e => {
          e.preventDefault();
          if (window.Unlock) window.Unlock.open({ href: link.getAttribute('href') });
        });
      }
      links.appendChild(link);
    });
    card.appendChild(links);
    return card;
  }

  /* ---------------- bridges: ranked editorial scroll ---------------- */
  function renderBridgesRanked(tile) {
    setCrumbs([{ label: DATA.title, href: '#' }, { label: tile.title }]);
    app.innerHTML = '';
    app.appendChild(el('div', 'section-head',
      `<h2>${esc(tile.title)}</h2>${tile.infographic ? `<span class="count">${esc(tile.infographic)}</span>` : ''}`));

    const withCoords = tile.subtiles.filter(s => s.lat != null && s.lon != null);
    if (withCoords.length && window.L) app.appendChild(buildBridgeMap(tile, withCoords));

    const list = el('div', 'bridge-list');
    const ranked = tile.subtiles.filter(s => s.rank);
    const extras = tile.subtiles.filter(s => !s.rank);
    let flip = false;
    ranked.forEach(s => {
      list.appendChild(buildBridgeRow(tile, s, flip));
      if (s.done && (s.photos || []).length) flip = !flip;   // alternate photo side
    });
    if (extras.length) {
      list.appendChild(el('div', 'bridge-extras-head', 'Also visited'));
      extras.forEach(s => {
        list.appendChild(buildBridgeRow(tile, s, flip));
        if (s.done && (s.photos || []).length) flip = !flip;
      });
    }
    app.appendChild(list);

    // scroll-reveal
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: 0.18 });
    list.querySelectorAll('.bridge-row').forEach(r => io.observe(r));
  }

  // Overview pin map: every roster bridge with coords, green = visited, red = not
  // yet. The container is rebuilt on every hash route (app.innerHTML = ''), so the
  // previous Leaflet instance must be torn down or its listeners leak.
  let bridgeMap = null;
  function buildBridgeMap(tile, subs) {
    if (bridgeMap) { try { bridgeMap.remove(); } catch (e) { /* already gone */ } bridgeMap = null; }
    const wrap = el('div', 'bridge-map-wrap');
    const mapEl = el('div', 'bridge-map');
    wrap.appendChild(mapEl);
    wrap.appendChild(el('div', 'bridge-map-legend',
      `<span><i class="pin-dot pin-dot--done"></i>Visited</span>
       <span><i class="pin-dot pin-dot--todo"></i>Not yet</span>`));

    const map = L.map(mapEl, { scrollWheelZoom: false, attributionControl: false });
    bridgeMap = map;
    // wheel/trackpad zoom only while the cursor is over the map — enabled globally
    // it hijacks the page scroll whenever the pointer crosses the map mid-scroll
    mapEl.addEventListener('mouseenter', () => map.scrollWheelZoom.enable());
    mapEl.addEventListener('mouseleave', () => map.scrollWheelZoom.disable());
    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
      { maxZoom: 18 }).addTo(map);

    const pin = colour => L.divIcon({
      html: `<svg viewBox="0 0 24 34" width="22" height="31" aria-hidden="true">
               <path fill="${colour}" stroke="#fff" stroke-width="1.2"
                     d="M12 1C6.5 1 2 5.4 2 10.9 2 18.3 12 33 12 33s10-14.7 10-22.1C22 5.4 17.5 1 12 1z"/>
               <circle cx="12" cy="10.9" r="3.4" fill="#fff" fill-opacity=".85"/>
             </svg>`,
      className: 'bridge-pin-icon', iconSize: L.point(22, 31),
      iconAnchor: L.point(11, 30), popupAnchor: L.point(0, -28)
    });
    const done = pin('#2e8b46'), todo = pin('#c8392e');

    const bounds = [];
    subs.forEach(s => {
      const meta = [s.height_m ? `${s.height_m} m` : null, s.province || null,
                    s.done ? null : (s.pending || 'Not visited')].filter(Boolean).join(' · ');
      const m = L.marker([s.lat, s.lon], { icon: s.done ? done : todo }).addTo(map);
      m.bindPopup(`<strong>${esc(s.title)}</strong><br>${esc(meta)}`);
      bounds.push([s.lat, s.lon]);
    });
    map.fitBounds(bounds, { padding: [28, 28] });
    // the container isn't sized until it's actually in the document
    requestAnimationFrame(() => { map.invalidateSize(); map.fitBounds(bounds, { padding: [28, 28] }); });
    return wrap;
  }

  function buildBridgeRow(tile, s, flip) {
    const rank = s.rank != null ? s.rank : '·';
    const metaBits = [];
    if (s.height_m) metaBits.push(`${s.height_m} m`);
    if (s.province) metaBits.push(s.province);
    const hasPhotos = s.done && (s.photos || []).length;

    if (!hasPhotos) {
      // A pending bridge can still carry a picked gallery (visited while under
      // construction, e.g. Yalong Liangshan): same compact row, no photo tile,
      // but the whole row links into the gallery and shows the count.
      const n = (s.photos || []).length;
      const row = el(n ? 'a' : 'div', 'bridge-row bridge-row--pending');
      if (n) row.href = `#${tile.id}/${s.id}`;
      metaBits.push(s.pending || 'Pending');
      row.innerHTML = `
        <div class="bridge-rank">${esc(String(rank))}</div>
        <div class="bridge-text">
          <span class="bridge-name">${esc(s.title)}</span>
          ${s.name_zh ? `<span class="bridge-zh">${esc(s.name_zh)}</span>` : ''}
          <div class="bridge-meta">${esc(metaBits.join(' · '))}</div>
          ${s.highlight ? `<div class="bridge-highlight">★ ${esc(s.highlight)}</div>` : ''}
          ${n ? `<div class="bridge-count">${n} photos →</div>` : ''}
        </div>`;
      const text = row.querySelector('.bridge-text');
      if ((s.renders || []).length) {
        // The row itself can be an <a> (linked gallery), so the renders link swallows
        // the click and routes by hand — same trick as the status toggle below.
        const rl = el('a', 'bridge-renders-link', `Renders + mockups (${s.renders.length}) →`);
        rl.href = `#${tile.id}/${s.id}/renders`;
        rl.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); location.hash = `${tile.id}/${s.id}/renders`; });
        text.appendChild(rl);
      }
      if (s.status_info) text.appendChild(buildBridgeStatus(s.status_info));
      return row;
    }

    const row = el('a', 'bridge-row' + (flip ? ' bridge-row--flip' : ''));
    row.href = `#${tile.id}/${s.id}`;
    row.innerHTML = `
      <div class="bridge-rank">${esc(String(rank))}</div>
      <div class="bridge-text">
        <div class="bridge-name">${esc(s.title)}</div>
        ${s.name_zh ? `<div class="bridge-zh">${esc(s.name_zh)}</div>` : ''}
        <div class="bridge-meta">${esc(metaBits.join(' · '))}</div>
        <div class="bridge-count">${s.photos.length} photos</div>
      </div>
      <div class="bridge-media">${imgTag(s.cover)}</div>`;
    // imgTag emits position:absolute .tile-img — bridge rows need normal flow
    const img = row.querySelector('img');
    if (img) img.classList.remove('tile-img');
    return row;
  }

  // Collapsible construction-status panel for pending rows. A pending row can be
  // an <a> (linked gallery), so the summary toggle is done by hand with the click
  // swallowed — otherwise opening the panel would navigate into the gallery.
  function buildBridgeStatus(info) {
    const det = el('details', 'bridge-status');
    const srcs = (info.sources || []).map(x =>
      `<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` +
      (x.date ? ` <span class="bridge-status-date">${esc(x.date)}</span>` : '') + '</li>').join('');
    det.innerHTML = `
      <summary>Status${info.state ? ` · ${esc(info.state)}` : ''}</summary>
      <div class="bridge-status-body">
        <p>${esc(info.summary || '')}</p>
        ${srcs ? `<ul class="bridge-status-sources">${srcs}</ul>` : ''}
        ${info.as_of ? `<div class="bridge-status-asof">Checked ${esc(info.as_of)}</div>` : ''}
      </div>`;
    det.addEventListener('click', e => {
      e.stopPropagation();
      if (e.target.closest('a')) return;                 // source links navigate normally
      e.preventDefault();
      if (e.target.closest('summary')) det.open = !det.open;
    });
    return det;
  }

  // Reference imagery for a not-yet-visited bridge: renders, mockups, maps and
  // construction shots pulled from HighestBridges. These live outside the photo
  // pipeline (no thumbnails/display variants), so this is its own simple grid +
  // overlay rather than Gallery.renderGrid.
  function renderBridgeRenders(tile, s) {
    setCrumbs([{ label: DATA.title, href: '#' }, { label: tile.title, href: `#${tile.id}` },
               { label: s.title }]);
    app.innerHTML = '';
    const zh = s.name_zh ? ` <span class="count">${esc(s.name_zh)}</span>` : '';
    app.appendChild(el('div', 'section-head', `<h2>${esc(s.title)}</h2>${zh}
      <span class="count">Renders + reference · HighestBridges</span>`));
    const grid = el('div', 'render-grid');
    s.renders.forEach((src, i) => {
      const cell = el('button', 'render-cell');
      cell.innerHTML = `<img src="${esc(src)}" loading="lazy" alt="">`;
      cell.addEventListener('click', () => openRenderOverlay(s, i));
      grid.appendChild(cell);
    });
    app.appendChild(grid);
  }

  function openRenderOverlay(s, start) {
    let i = start;
    const ov = el('div', 'render-overlay');
    ov.innerHTML = `<img src="${esc(s.renders[i])}" alt="">
      <div class="render-overlay-cap">${esc(s.title)} · <span>${i + 1}/${s.renders.length}</span></div>
      <button class="render-nav render-nav--prev" aria-label="Previous">‹</button>
      <button class="render-nav render-nav--next" aria-label="Next">›</button>
      <button class="render-close" aria-label="Close">×</button>`;
    const img = ov.querySelector('img'), cap = ov.querySelector('.render-overlay-cap span');
    const show = d => { i = (i + d + s.renders.length) % s.renders.length;
                        img.src = s.renders[i]; cap.textContent = `${i + 1}/${s.renders.length}`; };
    ov.querySelector('.render-nav--prev').addEventListener('click', e => { e.stopPropagation(); show(-1); });
    ov.querySelector('.render-nav--next').addEventListener('click', e => { e.stopPropagation(); show(1); });
    const close = () => { document.removeEventListener('keydown', onKey); ov.remove(); };
    ov.querySelector('.render-close').addEventListener('click', close);
    ov.addEventListener('click', e => { if (e.target === ov) close(); });
    const onKey = e => {
      if (e.key === 'Escape') close();
      else if (e.key === 'ArrowLeft') show(-1);
      else if (e.key === 'ArrowRight') show(1);
    };
    document.addEventListener('keydown', onKey);
    document.body.appendChild(ov);
  }

  /* ---------------- gallery views ---------------- */
  function renderGalleryView(tile) {
    setCrumbs([{ label: DATA.title, href: '#' }, { label: tile.title }]);
    app.innerHTML = '';
    app.appendChild(el('div', 'section-head',
      `<h2>${esc(tile.title)}</h2>${tile.infographic ? `<span class="count">${esc(tile.infographic)}</span>` : ''}`));
    const grid = el('div');
    let kind = 'all';
    const photos = tile.photos || [];
    const applyFilters = () => Gallery.renderGrid(grid, kind === 'all'
      ? photos : photos.filter(p => Gallery.photoKind(p) === kind));
    const kindBar = Gallery.buildKindBar(photos, k => { kind = k; applyFilters(); });
    if (kindBar) app.appendChild(kindBar);
    app.appendChild(grid);
    applyFilters();
  }

  // roofs: height-tier sections, one tile per building → its gallery
  function renderTieredTiles(tile) {
    app.innerHTML = '';
    if (DATA.tiles.length === 1) {
      // single-facet collection (e.g. Rooftopping): masthead instead of facet heading
      setCrumbs([]);
      app.appendChild(buildMasthead());
    } else {
      setCrumbs([{ label: DATA.title, href: '#' }, { label: tile.title }]);
      app.appendChild(el('div', 'section-head',
        `<h2>${esc(tile.title)}</h2>${tile.infographic ? `<span class="count">${esc(tile.infographic)}</span>` : ''}`));
    }
    // Same All/one-year bar as the Galleries index, restored from the hash so
    // Back from a building keeps the filter.
    const years = tile.years || [];
    const q = parseHash().query.year;
    const initial = q && q !== 'all' && years.includes(Number(q)) ? Number(q) : 'all';
    const asSet = y => (y === 'all' ? null : new Set([y]));
    if (years.length > 1) {
      app.appendChild(buildYearBar(years, y => {
        setHashQuery({ year: y === 'all' ? null : y });
        paintTieredSections(tile, asSet(y));
      }, initial));
    }
    const host = el('div');
    host.id = 'tier-host';
    app.appendChild(host);
    paintTieredSections(tile, asSet(initial));
  }

  // selected: null = all years, otherwise Set of years to show
  function paintTieredSections(tile, selected) {
    const host = document.getElementById('tier-host');
    host.innerHTML = '';
    (tile.sections || []).forEach(sec => {
      const subs = sec.subtiles.filter(s =>
        !selected || (s.years || []).some(y => selected.has(y)));
      if (!subs.length) return;
      // `unit` names what a sub-tile is (building / site / road); roofs predate it.
      // An untitled section (highways' single flat list) renders without a header bar.
      const unit = tile.unit || 'building';
      if (sec.title) {
        host.appendChild(el('div', 'tier-head',
          `<h3>${esc(sec.title)}</h3><span class="count">${subs.length} ${esc(unit)}${subs.length !== 1 ? 's' : ''}</span>`));
      }
      const grid = el('div', 'tiles tiles--dense tiles--mosaic');
      // one year selected → tiles deep-link into that year of the building's gallery
      const only = selected && selected.size === 1 ? [...selected][0] : 'all';
      subs.forEach(s => grid.appendChild(buildSubtile(tile, s, only)));
      host.appendChild(grid);
      observeReveal(grid, '.tile');
    });
    if (!host.children.length) {
      host.appendChild(el('p', 'gallery-empty', 'Nothing for the selected years.'));
    }
  }

  function renderSubGallery(tile, s) {
    // Single-facet collections (e.g. Rooftopping) skip the facet crumb — there's no
    // intermediate nav step (home → Rooftopping → building), so don't show "On the Roofs".
    const crumbs = DATA.tiles.length === 1
      ? [{ label: DATA.title, href: '#' }, { label: s.title }]
      : [{ label: DATA.title, href: '#' }, { label: tile.title, href: `#${tile.id}` }, { label: s.title }];
    setCrumbs(crumbs);
    app.innerHTML = '';
    const sub = s.subtitle ? ` <span class="count">${esc(s.subtitle)}</span>` : '';
    const zh = s.name_zh ? ` <span class="count">${esc(s.name_zh)}</span>` : '';
    app.appendChild(el('div', 'section-head', `<h2>${esc(s.title)}</h2>${zh}${sub}
      <span class="count" id="gallery-count">${(s.photos || []).length} photos</span>`));
    // lightbox caption: name · height · province/city (bridges + buildings carry these)
    const caption = [s.title, s.height_m ? `${s.height_m} m` : null, s.province || s.city || null]
      .filter(Boolean).join(' · ');
    const photos = (s.photos || []).map(p => ({ ...p, title: caption }));

    // Any gallery spanning more than one year gets a year filter — a building
    // climbed twice is one tile, so arriving from a filtered facet must open on
    // the year that was filtered (?year=), not on whichever climb sorts first.
    // Every gallery gets a camera/drone filter when both kinds are present. The
    // two compose.
    const grid = el('div');
    const years = [...new Set(photos.map(p => p.year).filter(Boolean))].sort((a, b) => b - a);
    const q = parseHash().query.year;
    let year = q && q !== 'all' && years.includes(Number(q)) ? Number(q) : 'all';
    let kind = 'all';
    const applyFilters = () => {
      const shown = photos.filter(p =>
        (year === 'all' || p.year === year) &&
        (kind === 'all' || Gallery.photoKind(p) === kind));
      const count = document.getElementById('gallery-count');
      if (count) count.textContent = `${shown.length} photos`;   // header tracks the filter
      Gallery.renderGrid(grid, shown);
    };
    if (years.length > 1) {
      app.appendChild(buildYearBar(years, y => {
        year = y;
        setHashQuery({ year: y === 'all' ? null : y });   // survives Back into the gallery
        applyFilters();
      }, year));
    }
    const kindBar = Gallery.buildKindBar(photos, k => { kind = k; applyFilters(); });
    if (kindBar) app.appendChild(kindBar);
    app.appendChild(grid);
    applyFilters();
  }

  // All / one-year filter bar, same markup and .yearbar styling as the Galleries index
  function buildYearBar(years, onPick, initial) {
    const bar = el('div', 'yearbar');
    const opts = ['all', ...years];
    const active = opts.indexOf(initial) >= 0 ? initial : 'all';
    opts.forEach(y => {
      const b = el('button', y === active ? 'active' : '', y === 'all' ? 'All' : String(y));
      b.type = 'button';
      b.addEventListener('click', () => {
        bar.querySelectorAll('button').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        onPick(y);
      });
      bar.appendChild(b);
    });
    return bar;
  }

  /* ---------------- router ---------------- */
  function route() {
    if (!DATA) return;
    // every view starts with app.innerHTML = '' — drop the Leaflet instance with it
    if (bridgeMap) { try { bridgeMap.remove(); } catch (e) { /* already gone */ } bridgeMap = null; }
    const hash = parseHash().path;   // filters ride in the ?query part, not the route
    window.scrollTo(0, 0);
    if (!hash) return DATA.tiles.length === 1 ? renderFacet(DATA.tiles[0]) : renderHub();
    const [facetId, subId] = hash.split('/');
    const tile = tileById(facetId);
    if (!tile) return renderHub();
    if (tile.locked) {
      renderHub();
      if (window.Unlock && !window.Unlock.unlocked()) window.Unlock.open({});
      return;
    }
    if (!subId) return renderFacet(tile);
    const s = subById(tile, subId);
    const extra = hash.split('/')[2];
    // reference imagery (renders/mockups) hangs off pending bridge rows
    if (s && extra === 'renders' && (s.renders || []).length) return renderBridgeRenders(tile, s);
    // not-done subtiles can still carry a linked gallery (bridge visited under construction)
    if (!s || !(s.done || (s.photos || []).length)) return renderFacet(tile);
    renderSubGallery(tile, s);
  }

  // Unlocked visitors get the full dataset; everyone else the public one.
  // (Collections without a separate full file fall through to the base name.)
  async function loadData() {
    if (window.Unlock && window.Unlock.unlocked()) {
      try {
        const r = await fetch(`collections/${COLL}.all.json?t=` + Date.now());
        if (r.ok) return await r.json();
      } catch (e) { /* fall through */ }
    }
    const r = await fetch(`collections/${COLL}.json?t=` + Date.now());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  }

  loadData()
    .then(data => {
      DATA = data;
      window.addEventListener('hashchange', route);
      route();
    })
    .catch(err => { app.innerHTML = `<p class="gallery-empty">Could not load data: ${esc(err.message)}</p>`; });
})();
