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
    return el('header', 'china-masthead',
      `<h1>${esc(DATA.title)}</h1><div class="stat-strip">${parts.join('')}</div>`);
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
      }
      grid.appendChild(card);
    });
    app.innerHTML = '';
    app.appendChild(buildMasthead());
    app.appendChild(grid);
    observeReveal(grid, '.tile');
  }

  function buildProvinceRevealHTML(tile) {
    const chips = (tile.subtiles || []).filter(s => s.done).map(s =>
      `<span class="chip">${esc(s.title)}</span>`).join('');
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

    if (tile.id === 'provinces' && (tile.years || []).length) {
      app.appendChild(buildYearBar(tile.years, year => paintProvinceTiles(tile, year)));
    }
    const grid = el('div', 'tiles' + (tile.id === 'roads' ? '' : ' tiles--dense'));
    grid.id = 'facet-grid';
    app.appendChild(grid);
    if (tile.id === 'provinces') paintProvinceTiles(tile, 'all');
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
        // unvisited provinces stay hidden
        if (s.pending === 'Not public yet' && year === 'all') grid.appendChild(buildSubtile(tile, s));
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
      const t = el('div', 'tile tile--pending');
      t.innerHTML = `<div class="tile-inner"><div class="tile-title">${esc(s.title)}</div>
        <div class="pending-tag">${esc(s.pending || 'Pending')}</div></div>`;
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
      href = `#${tile.id}/${s.id}`;
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
    // action, which is the map for provinces — plus buttons layered on top.
    const card = el('div', 'tile tile--haslink');
    const main = el('a', 'tile-mainlink');
    main.href = href;
    main.innerHTML = inner;
    card.appendChild(main);
    const links = el('div', 'tile-bloglinks');

    // Province tiles get a Map/Gallery view toggle controlling how the photos open:
    // Map (the default, matching the full-tile link) sends to the filtered map;
    // Gallery opens the in-hub photo grid. Grouped as a segmented pair so it reads
    // as one control. On mobile .tile-bloglinks shifts to the top-right, clear of the
    // bottom-anchored title/meta.
    if (isProvince) {
      const toggle = el('div', 'tile-viewtoggle');
      const mapBtn = el('a', 'tile-viewbtn is-active', 'Map');
      mapBtn.href = href;
      const galBtn = el('a', 'tile-viewbtn', 'Gallery');
      galBtn.href = `#${tile.id}/${s.id}`;
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

  function buildBridgeRow(tile, s, flip) {
    const rank = s.rank != null ? s.rank : '·';
    const metaBits = [];
    if (s.height_m) metaBits.push(`${s.height_m} m`);
    if (s.province) metaBits.push(s.province);
    const hasPhotos = s.done && (s.photos || []).length;

    if (!hasPhotos) {
      const row = el('div', 'bridge-row bridge-row--pending');
      metaBits.push(s.pending || 'Pending');
      row.innerHTML = `
        <div class="bridge-rank">${esc(String(rank))}</div>
        <div class="bridge-text">
          <span class="bridge-name">${esc(s.title)}</span>
          ${s.name_zh ? `<span class="bridge-zh">${esc(s.name_zh)}</span>` : ''}
          <div class="bridge-meta">${esc(metaBits.join(' · '))}</div>
        </div>`;
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

  /* ---------------- gallery views ---------------- */
  function renderGalleryView(tile) {
    setCrumbs([{ label: DATA.title, href: '#' }, { label: tile.title }]);
    app.innerHTML = '';
    app.appendChild(el('div', 'section-head',
      `<h2>${esc(tile.title)}</h2>${tile.infographic ? `<span class="count">${esc(tile.infographic)}</span>` : ''}`));
    const grid = el('div'); app.appendChild(grid);
    Gallery.renderGrid(grid, tile.photos || []);
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
    if ((tile.years || []).length > 1) {
      app.appendChild(buildYearMenu(tile, years => paintTieredSections(tile, years)));
    }
    const host = el('div');
    host.id = 'tier-host';
    app.appendChild(host);
    paintTieredSections(tile, null);
  }

  // selected: null = all years, otherwise Set of years to show
  function paintTieredSections(tile, selected) {
    const host = document.getElementById('tier-host');
    host.innerHTML = '';
    (tile.sections || []).forEach(sec => {
      const subs = sec.subtiles.filter(s =>
        !selected || (s.years || []).some(y => selected.has(y)));
      if (!subs.length) return;
      host.appendChild(el('div', 'tier-head',
        `<h3>${esc(sec.title)}</h3><span class="count">${subs.length} building${subs.length !== 1 ? 's' : ''}</span>`));
      const grid = el('div', 'tiles tiles--dense tiles--mosaic');
      subs.forEach(s => grid.appendChild(buildSubtile(tile, s)));
      host.appendChild(grid);
      observeReveal(grid, '.tile');
    });
    if (!host.children.length) {
      host.appendChild(el('p', 'gallery-empty', 'Nothing for the selected years.'));
    }
  }

  // Multi-select year dropdown (same interaction pattern as the map's country
  // filter): tick years to show, with Select all / none shortcuts.
  function buildYearMenu(tile, onChange) {
    const years = tile.years;
    const countByYear = {};
    (tile.sections || []).forEach(sec => sec.subtiles.forEach(s =>
      (s.years || []).forEach(y => { countByYear[y] = (countByYear[y] || 0) + 1; })));

    let selected = null;  // null = all
    const wrap = el('div', 'year-menu-wrapper');
    wrap.innerHTML = `
      <button class="year-menu-btn" type="button">
        <span class="year-menu-label">All years</span>
        <svg viewBox="0 0 10 6" width="10" height="6">
          <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>
        </svg>
      </button>
      <div class="year-menu">
        <div class="year-menu-actions">
          <button type="button" data-act="all">Select all</button>
          <button type="button" data-act="none">Select none</button>
        </div>
        ${years.map(y => `
          <div class="year-menu-option on" data-year="${y}">
            <span class="tick">✓</span><span>${y}</span>
            <span class="year-menu-count">${countByYear[y] || 0}</span>
          </div>`).join('')}
      </div>`;

    const btn = wrap.querySelector('.year-menu-btn');
    const menu = wrap.querySelector('.year-menu');
    const label = wrap.querySelector('.year-menu-label');

    function paintOptions() {
      wrap.querySelectorAll('.year-menu-option').forEach(opt => {
        const on = !selected || selected.has(Number(opt.dataset.year));
        opt.classList.toggle('on', on);
      });
      if (!selected) label.textContent = 'All years';
      else if (!selected.size) label.textContent = 'No years';
      else if (selected.size === 1) label.textContent = String([...selected][0]);
      else label.textContent = `${selected.size} years`;
    }
    function apply() { paintOptions(); onChange(selected); }

    btn.addEventListener('click', e => {
      e.stopPropagation();
      menu.classList.toggle('open');
    });
    document.addEventListener('click', () => menu.classList.remove('open'));
    menu.addEventListener('click', e => e.stopPropagation());

    menu.querySelector('[data-act="all"]').addEventListener('click', () => { selected = null; apply(); });
    menu.querySelector('[data-act="none"]').addEventListener('click', () => { selected = new Set(); apply(); });
    menu.querySelectorAll('.year-menu-option').forEach(opt => {
      opt.addEventListener('click', () => {
        const y = Number(opt.dataset.year);
        if (!selected) selected = new Set(years);
        if (selected.has(y)) selected.delete(y); else selected.add(y);
        if (selected.size === years.length) selected = null;
        apply();
      });
    });
    return wrap;
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
      <span class="count">${(s.photos || []).length} photos</span>`));
    // lightbox caption: name · height · province/city (bridges + buildings carry these)
    const caption = [s.title, s.height_m ? `${s.height_m} m` : null, s.province || s.city || null]
      .filter(Boolean).join(' · ');
    const photos = (s.photos || []).map(p => ({ ...p, title: caption }));

    // Province galleries get a year filter over their own photos.
    if (tile.id === 'provinces') {
      const years = [...new Set(photos.map(p => p.year).filter(Boolean))].sort((a, b) => b - a);
      const grid = el('div');
      if (years.length > 1) {
        app.appendChild(buildYearBar(years, y =>
          Gallery.renderGrid(grid, y === 'all' ? photos : photos.filter(p => p.year === y))));
      }
      app.appendChild(grid);
      Gallery.renderGrid(grid, photos);
    } else {
      const grid = el('div'); app.appendChild(grid);
      Gallery.renderGrid(grid, photos);
    }
  }

  function buildYearBar(years, onPick) {
    const bar = el('div', 'yearbar');
    const opts = ['all', ...years];
    opts.forEach((y, i) => {
      const b = el('button', i === 0 ? 'active' : '', y === 'all' ? 'All years' : String(y));
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
    const hash = location.hash.replace(/^#/, '');
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
    if (!s || !s.done) return renderFacet(tile);
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
