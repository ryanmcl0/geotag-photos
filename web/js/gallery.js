/**
 * Shared thumbnail-grid + PhotoSwipe lightbox, reused by the China hub (and later Blogs).
 * Photo references are {trip, id} (+ optional year/title); images resolve to the same
 * webp the map uses — locally via the trips/<slug> symlinks, on deploy via the R2 proxy.
 */
window.Gallery = (function () {
  // Local preview serves images through web/trips/<slug>/{thumbnails,display} symlinks
  // (which deploy never ships); in production the same keys live behind the /photos
  // R2 proxy. Pick by hostname; window.CHINA_PHOTO_BASE still overrides.
  // Match LAN IPs / *.local too, so the wrangler dev server reached from a phone
  // (e.g. http://192.168.x.x:8788) still uses the /trips symlinks — otherwise every
  // cover 404s through the (locally unbound) R2 proxy and shows a 🔒 placeholder.
  // Production runs on *.pages.dev / a custom domain, none of which match these.
  const HOST = location.hostname;
  const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(HOST) ||
    HOST.endsWith('.local') ||
    /^10\./.test(HOST) || /^192\.168\./.test(HOST) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(HOST);
  const PHOTO_BASE = (window.CHINA_PHOTO_BASE || (LOCAL ? '/trips' : '/photos'));

  // Drone vs camera, from the filename convention (DJI_* = drone).
  function photoKind(ref) {
    return /^dji/i.test(ref.id || '') ? 'drone' : 'camera';
  }

  // Posts mode (posts.js: the posts_ui cookie rides along with the owner-only
  // posts_auth one) adds a Portrait / Landscape pair to every filter bar, for
  // getting a feel for a post's shape. The pick holds across pages this session.
  const postsMode = () => document.cookie.split(';').some(c => /^\s*posts_ui=./.test(c));
  const ORIENT_KEY = 'postsOrientation';
  let orientation = null;   // null | 'portrait' | 'landscape'
  try { orientation = sessionStorage.getItem(ORIENT_KEY) || null; } catch (e) { /* storage off */ }
  const isPortrait = p => (p.ar || 1.5) < 1;
  const isLandscape = p => (p.ar || 1.5) > 1;
  const byOrientation = list => !orientation || !postsMode() ? list
    : list.filter(orientation === 'portrait' ? isPortrait : isLandscape);

  // "Me" (localhost: the face index's photos of me, camera and phone alike) narrows
  // whichever kind is picked. Held across the pages of one visit like the shape pick.
  let meOnly = false;

  /**
   * Camera/Drone filter chips for any photo list where both kinds appear, plus,
   * when `phone` is given (localhost: phoneFor()), a Phone chip; then a Me toggle
   * when any photo here is flagged `me` (markMe() for camera ones). "All" stays
   * the camera + drone set; phone photos only show when picked. In posts mode a
   * Portrait / Landscape pair sits at the right end of the row. Returns null when
   * there is nothing to pick. onPick gets 'all' | 'camera' | 'drone' | 'phone'
   * (and fires again, same kind, when Me or the shape changes); filterKind()
   * applies every pick.
   */
  function buildKindBar(photos, onPick, phone, initial) {
    const counts = { camera: 0, drone: 0 };
    photos.forEach(p => counts[photoKind(p)]++);
    phone = phone || [];
    const anyMe = photos.some(p => p.me) || phone.some(p => p.me);
    if (initial === 'phone-me') { initial = 'phone'; meOnly = true; }   // the old link form
    if (!anyMe) meOnly = false;              // nothing to narrow to on this page
    const mixed = counts.camera && counts.drone;
    const posts = postsMode();
    if (!mixed && !phone.length && !posts && !anyMe) return null;
    const bar = document.createElement('div');
    bar.className = 'kindbar';
    bar.style.cssText = 'display:flex;gap:8px;margin:10px 0;flex-wrap:wrap';
    const defs = [['all', `All (${photos.length})`]];
    if (mixed) defs.push(['camera', `📷 Camera (${counts.camera})`], ['drone', `🚁 Drone (${counts.drone})`]);
    if (phone.length) defs.push(['phone', `📱 Phone (${phone.length})`]);
    const style = (on, sub) => 'border-radius:999px;padding:5px 14px;cursor:pointer;font-size:13px;' +
      'font-family:inherit;' + (sub && !on ? 'border-style:dashed;' : '') + (on
        ? 'background:#fff;color:#111;border:1px solid #fff'
        : 'background:none;color:#ddd;border:1px solid #555');
    let current = defs.some(d => d[0] === initial) ? initial : 'all';
    const orients = [];
    let meBtn = null;
    const refire = () => { paint(); onPick(current); };
    const paint = () => {
      bar.querySelectorAll('button[data-kind]').forEach(x => { x.style.cssText = style(x.dataset.kind === current); });
      // Me counts within the picked kind; the shape pair within the kind and Me
      const kindList = filterKind(photos, phone, current, true, true);
      if (meBtn) {
        const n = kindList.filter(p => p.me).length;
        meBtn.textContent = `👤 Me (${n})`;
        meBtn.style.cssText = style(meOnly, true);
        meBtn.hidden = !n && !meOnly;
      }
      const base = filterKind(photos, phone, current, true);
      orients.forEach(([b, val, label, test]) => {
        b.textContent = `${label} (${base.filter(test).length})`;
        b.style.cssText = style(orientation === val) + (val === 'portrait' ? ';margin-left:auto' : '');
      });
    };
    defs.forEach(([val, label]) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.dataset.kind = val;
      b.addEventListener('click', () => { current = val; refire(); });
      bar.appendChild(b);
    });
    if (anyMe) {
      meBtn = document.createElement('button');
      meBtn.type = 'button';
      meBtn.dataset.me = '1';
      meBtn.title = 'Only the photos I am in (click again for all)';
      meBtn.addEventListener('click', () => { meOnly = !meOnly; refire(); });
      bar.appendChild(meBtn);
    }
    if (posts) {
      [['portrait', '▯ Portrait', isPortrait], ['landscape', '▭ Landscape', isLandscape]].forEach(([val, label, test]) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.dataset.orient = val;
        b.title = 'Posts mode: show only this shape (click again for both)';
        b.addEventListener('click', () => {
          orientation = orientation === val ? null : val;
          try {
            if (orientation) sessionStorage.setItem(ORIENT_KEY, orientation);
            else sessionStorage.removeItem(ORIENT_KEY);
          } catch (e) { /* storage off: the pick lasts this page */ }
          refire();
        });
        orients.push([b, val, label, test]);
        bar.appendChild(b);
      });
    }
    paint();
    return bar;
  }

  // {reel id: [page keys]} for the reels placed by their GPS (same file, same
  // localhost-only rule); {} elsewhere.
  function reelPages() {
    return loadPlaces().then(d => (d && d.reels) || {});
  }

  // anyShape / anyone: ignore the posts-mode shape pick / the Me toggle (the chips' own counts)
  function filterKind(photos, phone, kind, anyShape, anyone) {
    let list;
    if (kind === 'phone' || kind === 'phone-me') list = phone || [];
    else list = kind === 'all' ? photos : photos.filter(p => photoKind(p) === kind);
    if (meOnly && !anyone) list = list.filter(p => p.me);
    return anyShape ? list : byOrientation(list);
  }

  // Flags the camera / drone photos of me in `photos` (sets p.me) from the face
  // index's list in places.json; → how many. Localhost only, like phoneFor().
  function markMe(photos) {
    return loadPlaces().then(d => {
      const mine = (d && d.me_camera) || {};
      let n = 0;
      photos.forEach(p => {
        if ((mine[p.trip] || []).includes(p.id)) { p.me = true; n++; }
      });
      return n;
    });
  }

  // 'portrait' | 'landscape' while posts mode narrows grids to one shape, else null
  const orientationActive = () => (orientation && postsMode() ? orientation : null);

  /**
   * Phone photos for a page, from web/phone/places.json (tools/phone_places.py).
   * Localhost only: the file is never deployed, and elsewhere it isn't fetched.
   * key: "trip/<slug>" or "<collection>/<facet>/<subtile>". → [] when none.
   */
  let placesP = null;
  function loadPlaces() {
    if (!placesP) {
      placesP = LOCAL
        ? fetch('/phone/places.json', { cache: 'no-store' })
            .then(r => (r.ok ? r.json() : null))
            .then(d => (d && d.schema === 'phone-places.v1' ? d : null))
            .catch(() => null)
        : Promise.resolve(null);
    }
    return placesP;
  }
  function phoneFor(key) {
    return loadPlaces().then(d => ((d && d.pages[key]) || []).map(i => {
      const p = d.photos[i];
      return { trip: p.t, id: p.i, ar: p.ar, year: Number(p.ts.slice(0, 4)),
               timestamp: p.ts, date: p.ts.slice(0, 10), folder: p.f || '', me: !!p.me, phone: true };
    }));
  }

  function photoUrl(ref, kind /* 'thumbnails' | 'display' */) {
    // "phone-" trips live in the local-only phone library (web/phone/, never
    // deployed); route them there on any host so mixed posts render locally.
    const base = ref.trip.startsWith('phone-') ? '/phone/trips' : PHOTO_BASE;
    return `${base}/${ref.trip}/${kind}/${encodeURIComponent(ref.id)}.webp`;
  }

  // Cover image failed to load (gated photo) → padlock placeholder instead of
  // a broken-image icon.
  function lockedCover(img) {
    const d = document.createElement('div');
    d.className = 'tile-cover-locked';
    d.innerHTML = '<span class="pad">🔒</span>Locked';
    img.replaceWith(d);
  }

  /**
   * Shared lightbox navigation across several renderGrid calls (the trip
   * gallery's per-day sections): each grid registers its displayed order, and
   * a click opens the lightbox over the concatenation of all sections instead
   * of just the clicked one — so arrow keys cross the date banners.
   */
  function createNav() {
    const sections = [];
    return {
      register() {
        const s = { order: [] };
        sections.push(s);
        return s;
      },
      locate(photo) {
        const list = [];
        sections.forEach(s => list.push(...s.order));
        return { list, index: list.indexOf(photo) };
      }
    };
  }

  function renderGrid(container, photos, opts) {
    opts = opts || {};
    const section = opts.nav ? opts.nav.register() : null;
    container.innerHTML = '';
    if (!photos || !photos.length) {
      const empty = document.createElement('p');
      empty.className = 'gallery-empty';
      empty.textContent = opts.emptyText || 'No photos yet.';
      container.appendChild(empty);
      return;
    }

    // Explicit justified-rows engine (Flickr/Google-Photos style).
    // Rows are computed in JS, so row height is EXACT for every cell in a row —
    // images render at their native aspect with zero cropping — and we control
    // the rhythm: consecutive rows open with opposite orientations (offset rows),
    // and orientations are woven through each row at their global ratio with no
    // 3-in-a-row runs. Lightbox follows the displayed order.
    const GAP = 8;
    const grid = document.createElement('div');
    grid.className = 'photo-grid';
    container.appendChild(grid);

    const arOf = p => p.ar || 1.5;
    const isL = p => arOf(p) >= 1;

    function layout() {
      const width = grid.clientWidth || container.clientWidth || 1200;
      const TARGET_H = width < 700 ? 190 : 300;
      grid.innerHTML = '';

      const rows = [];
      const order = [];

      if (opts.sequential) {
        // Keep the given order (blogs: narrative sequence matters) — plain greedy
        // justified rows, no orientation weaving.
        let row = [], sumAr = 0;
        for (const p of photos) {
          row.push(p);
          sumAr += arOf(p);
          if (sumAr * TARGET_H + GAP * (row.length - 1) >= width) {
            rows.push({ row, sumAr });
            row = []; sumAr = 0;
          }
        }
        if (row.length) rows.push({ row, sumAr });
        order.push(...photos);
      } else {

      const land = photos.filter(p => isL(p));
      const port = photos.filter(p => !isL(p));
      const totalL = land.length || 1, totalP = port.length || 1;
      let prevStart = 'P';                       // → first row opens landscape

      while (land.length || port.length) {
        const row = [];
        let sumAr = 0;
        let next = prevStart === 'L' ? 'P' : 'L';   // offset from the row above
        while (land.length || port.length) {
          if (next === 'L' && !land.length) next = 'P';
          if (next === 'P' && !port.length) next = 'L';
          const p = (next === 'L' ? land : port).shift();
          row.push(p);
          sumAr += arOf(p);
          if (sumAr * TARGET_H + GAP * (row.length - 1) >= width) break;
          // keep both orientations flowing at their global ratio…
          next = (port.length / totalP) > (land.length / totalL) ? 'P' : 'L';
          // …but never three of the same type consecutively within a row
          const n = row.length;
          if (n >= 2 && isL(row[n - 1]) === isL(row[n - 2]) &&
              isL(row[n - 1]) === (next === 'L')) {
            if (next === 'L' && port.length) next = 'P';
            else if (next === 'P' && land.length) next = 'L';
          }
        }
        prevStart = isL(row[0]) ? 'L' : 'P';
        rows.push({ row, sumAr });
        order.push(...row);
      }

      }

      if (section) section.order = order;
      rows.forEach(({ row, sumAr }, ri) => {
        let h = (width - GAP * (row.length - 1)) / sumAr;
        if (ri === rows.length - 1 && h > TARGET_H * 1.18) h = TARGET_H; // sparse last row
        const rowEl = document.createElement('div');
        rowEl.className = 'photo-row';
        row.forEach(p => {
          const cell = document.createElement('button');
          cell.className = 'photo-cell';
          cell.type = 'button';
          if (p.trip && p.id) { cell.dataset.trip = p.trip; cell.dataset.id = p.id; }
          cell.style.width = `${(arOf(p) * h).toFixed(2)}px`;
          cell.style.height = `${h.toFixed(2)}px`;
          const img = document.createElement('img');
          img.loading = 'lazy';
          img.decoding = 'async';
          img.alt = '';
          img.src = photoUrl(p, 'thumbnails');
          cell.appendChild(img);
          cell.addEventListener('click', () => {
            if (section) {
              const { list, index } = opts.nav.locate(p);
              openLightbox(list, index);
            } else {
              openLightbox(order, order.indexOf(p));
            }
          });
          rowEl.appendChild(cell);
        });
        grid.appendChild(rowEl);
      });
      // Owner-only Posts feature (no-op stub when locked); called after every
      // (re)layout because a relayout rebuilds all cells.
      if (window.Posts) Posts.onGridRender(grid, order);
    }

    layout();
    if (window.ResizeObserver) {
      let raf = 0, lastW = grid.clientWidth;
      new ResizeObserver(() => {
        const w = grid.clientWidth;
        if (Math.abs(w - lastW) < 2) return;
        lastW = w;
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(layout);
      }).observe(grid);
    }
  }

  // Trackpad / mouse-wheel zoom for desktop (same fix as the map's lightbox):
  // a macOS trackpad pinch arrives as a wheel event with ctrlKey=true; PhotoSwipe
  // v4 has no desktop wheel-zoom, so without this the browser zooms the whole
  // page and PhotoSwipe treats the scroll as a close gesture. Intercept at
  // document level (capture, before PhotoSwipe) and zoom the slide instead.
  function addWheelZoom(gallery, pswpEl) {
    const MAX_ZOOM = 3;
    let targetZoom = null;   // accumulated target (live zoom isn't readable mid-gesture)

    function minZoom() {
      return (gallery.currItem && gallery.currItem.initialZoomLevel) || 0.1;
    }

    function onWheel(e) {
      if (!pswpEl.classList.contains('pswp--open')) return;
      // Overlays (e.g. the posts picker) stack above the lightbox in <body>;
      // wheel events on them must scroll the overlay, not zoom the photo.
      if (e.target instanceof Element && !pswpEl.contains(e.target)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (targetZoom === null) targetZoom = gallery.getZoomLevel();
      const factor = e.ctrlKey ? 0.01 : 0.0025;   // pinch deltas are small, wheel large
      targetZoom = Math.min(MAX_ZOOM,
        Math.max(minZoom(), targetZoom * Math.exp(-e.deltaY * factor)));
      gallery.zoomTo(targetZoom, { x: e.clientX, y: e.clientY }, 0);
    }

    document.addEventListener('wheel', onWheel, { passive: false, capture: true });
    gallery.listen('afterChange', () => { targetZoom = null; });
    gallery.listen('destroy', () => {
      document.removeEventListener('wheel', onWheel, { capture: true });
    });
  }

  // Localhost only: a "Finder" button in the viewer's top bar reveals the photo's
  // edited file (the Lightroom export the site was built from) in Finder, via the
  // local helper on 127.0.0.1:8791 (tools/reels_local.py, started by serve.sh).
  // The viewer element is shared by every open, so the button is added once and
  // reads the current item at click time.
  const FINDER_URL = 'http://127.0.0.1:8791/reveal';
  function addFinderButton(gallery, pswpEl) {
    const bar = pswpEl.querySelector('.pswp__top-bar');
    if (!bar) return;
    let btn = bar.querySelector('.finder-btn');
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pswp__button finder-btn';
      btn.title = 'Show the edited file in Finder';
      btn.textContent = 'Finder';
      bar.insertBefore(btn, bar.querySelector('.pswp__button--close') || null);
    }
    btn.onclick = async e => {
      e.stopPropagation();
      const ref = gallery.currItem && gallery.currItem.ref;
      if (!ref || !ref.trip || !ref.id) return;
      const say = (t, ms) => { btn.textContent = t; setTimeout(() => { btn.textContent = 'Finder'; }, ms); };
      try {
        const r = await fetch(`${FINDER_URL}?trip=${encodeURIComponent(ref.trip)}&id=${encodeURIComponent(ref.id)}`,
          { cache: 'no-store' });
        say(r.ok ? 'Shown' : 'Not found', 1500);
      } catch (err) {
        say('Helper off', 2500);     // reels_local.py is not running (serve.sh starts it)
      }
    };
  }

  function openLightbox(photos, index, opts) {
    const pswpEl = document.querySelector('.pswp');
    if (!pswpEl || typeof PhotoSwipe === 'undefined') return;
    const items = photos.map(p => {
      // aspect ratio from build-time data sizes the slide exactly; display webps
      // are 2160px on the longest edge
      const ar = p.ar || 1.5;
      const w = ar >= 1 ? 2160 : Math.round(2160 * ar);
      const h = ar >= 1 ? Math.round(2160 / ar) : 2160;
      return {
        src: photoUrl(p, 'display'),
        msrc: photoUrl(p, 'thumbnails'),
        w, h, _needsSize: !p.ar,
        title: p.title || '',
        ref: p
      };
    });
    const gallery = new PhotoSwipe(pswpEl, PhotoSwipeUI_Default, items, {
      index, history: false, loop: true, shareEl: false,
      fullscreenEl: false, tapToClose: false, bgOpacity: 0.96, showHideOpacity: true
    });
    gallery.listen('gettingData', (idx, item) => {
      if (!item._needsSize) return;
      const img = new Image();
      img.onload = () => {
        item.w = img.naturalWidth; item.h = img.naturalHeight;
        item._needsSize = false;
        gallery.invalidateCurrItems(); gallery.updateSize(true);
      };
      img.src = item.src;
    });
    addWheelZoom(gallery, pswpEl);
    if (LOCAL) addFinderButton(gallery, pswpEl);
    gallery.init();
    if (window.SiteAnalytics) SiteAnalytics.attachLightbox(gallery, 'gallery');
    if (window.Posts) Posts.attachLightbox(gallery, pswpEl, opts);
  }

  // In-page player for phone videos (uncompressed NAS originals; the <video>
  // element range-streams them just like a tab would). Esc / ✕ / backdrop
  // click closes; Esc is captured so an overlay underneath doesn't also close.
  function openVideo(url, label) {
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:3000;background:rgba(0,0,0,.92);' +
      'display:flex;align-items:center;justify-content:center';
    const vid = document.createElement('video');
    vid.src = url;
    vid.controls = true;
    vid.autoplay = true;
    vid.playsInline = true;
    vid.style.cssText = 'max-width:96vw;max-height:92vh;outline:none';
    const onKey = e => {
      if (e.key !== 'Escape') return;
      e.stopImmediatePropagation();
      close();
    };
    const close = () => {
      vid.pause();
      vid.removeAttribute('src');
      vid.load();   // actually release the stream
      document.removeEventListener('keydown', onKey, true);
      ov.remove();
    };
    document.addEventListener('keydown', onKey, true);
    ov.addEventListener('click', e => { if (e.target === ov) close(); });
    const x = document.createElement('button');
    x.type = 'button';
    x.textContent = '✕';
    x.style.cssText = 'position:absolute;top:14px;right:16px;background:rgba(0,0,0,.6);' +
      'color:#fff;border:1px solid #555;border-radius:6px;padding:4px 10px;' +
      'cursor:pointer;font-size:15px';
    x.addEventListener('click', close);
    ov.append(vid, x);
    if (label) {
      const cap = document.createElement('div');
      cap.textContent = label;
      cap.style.cssText = 'position:absolute;bottom:12px;left:16px;color:#ccc;' +
        'font:12px -apple-system,sans-serif;opacity:.8';
      ov.appendChild(cap);
    }
    document.body.appendChild(ov);
  }

  return { photoUrl, renderGrid, openLightbox, openVideo, lockedCover, photoKind, buildKindBar, filterKind,
           orientationActive, phoneFor, markMe, reelPages, createNav, isLocal: LOCAL };
})();
