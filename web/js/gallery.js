/**
 * Shared thumbnail-grid + PhotoSwipe lightbox, reused by the China hub (and later Blogs).
 * Photo references are {trip, id} (+ optional year/title); images resolve to the same
 * webp the map uses — locally via the trips/<slug> symlinks, on deploy via the R2 proxy.
 */
window.Gallery = (function () {
  // Local preview serves images through web/trips/<slug>/{thumbnails,display} symlinks
  // (which deploy never ships); in production the same keys live behind the /photos
  // R2 proxy. Pick by hostname; window.CHINA_PHOTO_BASE still overrides.
  const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  const PHOTO_BASE = (window.CHINA_PHOTO_BASE || (LOCAL ? '/trips' : '/photos'));

  function photoUrl(ref, kind /* 'thumbnails' | 'display' */) {
    return `${PHOTO_BASE}/${ref.trip}/${kind}/${encodeURIComponent(ref.id)}.webp`;
  }

  // Cover image failed to load (gated photo) → padlock placeholder instead of
  // a broken-image icon.
  function lockedCover(img) {
    const d = document.createElement('div');
    d.className = 'tile-cover-locked';
    d.innerHTML = '<span class="pad">🔒</span>Locked';
    img.replaceWith(d);
  }

  function renderGrid(container, photos, opts) {
    opts = opts || {};
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

      rows.forEach(({ row, sumAr }, ri) => {
        let h = (width - GAP * (row.length - 1)) / sumAr;
        if (ri === rows.length - 1 && h > TARGET_H * 1.18) h = TARGET_H; // sparse last row
        const rowEl = document.createElement('div');
        rowEl.className = 'photo-row';
        row.forEach(p => {
          const cell = document.createElement('button');
          cell.className = 'photo-cell';
          cell.type = 'button';
          cell.style.width = `${(arOf(p) * h).toFixed(2)}px`;
          cell.style.height = `${h.toFixed(2)}px`;
          const img = document.createElement('img');
          img.loading = 'lazy';
          img.decoding = 'async';
          img.alt = '';
          img.src = photoUrl(p, 'thumbnails');
          cell.appendChild(img);
          cell.addEventListener('click', () => openLightbox(order, order.indexOf(p)));
          rowEl.appendChild(cell);
        });
        grid.appendChild(rowEl);
      });
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

  function openLightbox(photos, index) {
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
        title: p.title || ''
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
    gallery.init();
  }

  return { photoUrl, renderGrid, openLightbox, lockedCover };
})();
