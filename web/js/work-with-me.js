/* Work With Me page (web/work-with-me.html, built by build_work_with_me.py).
   Photos: elements carry data-key (<slug>/display/<id>.webp) and no src. Locally the
   keys resolve under /trips (symlinks), on the live site under the /photos proxy, so
   the base is picked by host, as on the home page.
   Reel: the home page's drifting 3D coverflow (js/home-galleries.js), fed by the
   picks the builder embedded in #wwm-reel. Hover takes over (trackpad swipe or drag),
   click a side card to bring it forward, click the centre card to open it large.
   Lightbox: photos open in the page, never a new tab. */
(function () {
  'use strict';
  const H = location.hostname;
  const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(H) || H.endsWith('.local') ||
    /^10\./.test(H) || /^192\.168\./.test(H) || /^172\.(1[6-9]|2\d|3[01])\./.test(H);
  const PB = LOCAL ? '/trips/' : '/photos/';
  const url = key => PB + key;

  document.querySelectorAll('img[data-key]').forEach(im => { im.src = url(im.dataset.key); });

  // ---------------------------------------------------------------- lightbox
  const LB = (() => {
    let el, img, cap, num, list = [], i = 0;
    function make() {
      el = document.createElement('div');
      el.className = 'lb';
      el.innerHTML = '<span class="lb-n"></span><button class="lb-x" aria-label="Close">&times;</button>' +
        '<button class="lb-prev" aria-label="Previous">&#8249;</button><button class="lb-next" aria-label="Next">&#8250;</button>' +
        '<img alt=""><div class="lb-cap"></div>';
      document.body.appendChild(el);
      img = el.querySelector('img'); cap = el.querySelector('.lb-cap'); num = el.querySelector('.lb-n');
      el.querySelector('.lb-x').onclick = close;
      el.querySelector('.lb-prev').onclick = e => { e.stopPropagation(); go(-1); };
      el.querySelector('.lb-next').onclick = e => { e.stopPropagation(); go(1); };
      el.addEventListener('click', e => { if (e.target === el) close(); });
      document.addEventListener('keydown', e => {
        if (!el.classList.contains('open')) return;
        if (e.key === 'Escape') close();
        if (e.key === 'ArrowLeft') go(-1);
        if (e.key === 'ArrowRight') go(1);
      });
      let sx = null;
      el.addEventListener('touchstart', e => { sx = e.touches[0].clientX; }, { passive: true });
      el.addEventListener('touchend', e => {
        if (sx == null) return;
        const dx = e.changedTouches[0].clientX - sx; sx = null;
        if (Math.abs(dx) > 40) go(dx < 0 ? 1 : -1);
      });
    }
    function show() {
      const p = list[i];
      img.src = p.src; cap.textContent = p.caption || '';
      num.textContent = list.length > 1 ? `${i + 1} / ${list.length}` : '';
      el.querySelectorAll('.lb-prev,.lb-next').forEach(b => { b.style.visibility = list.length > 1 ? 'visible' : 'hidden'; });
    }
    function go(d) { i = (i + d + list.length) % list.length; show(); }
    function open(l, idx) { if (!el) make(); list = l; i = idx || 0; show(); el.classList.add('open'); }
    function close() { el.classList.remove('open'); img.removeAttribute('src'); }
    return { open };
  })();

  // ---------------------------------------------------------------- reel
  function Reel(root, photos, opts) {
    const o = Object.assign({ speed: 0.2, visible: 4.2, fill: 0.86, maxW: 0.6, spread: 0.62, cap: null }, opts || {});
    const stage = document.createElement('div');
    stage.className = 'reel-stage';
    root.appendChild(stage);
    const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let cardW = 400, cur = 0, target = 0, last = 0, hovering = false, touching = false, holdUntil = 0, settle = 0, shown = -1, onScreen = true;
    let sx = 0, st = 0, dragged = false;
    const cards = photos.map((p, idx) => {
      const fig = document.createElement('figure');
      fig.className = 'reel-card';
      const im = document.createElement('img');
      im.alt = p.caption || ''; im.decoding = 'async';
      fig.appendChild(im);
      stage.appendChild(fig);
      fig.addEventListener('click', () => {
        if (dragged) return;
        const n = cards.length;
        let d = idx - cur; d -= Math.round(d / n) * n;
        if (Math.abs(d) < 0.5) LB.open(photos, idx);
        else { hovering = true; target = cur + d; }
      });
      return { fig, im, p, loaded: false };
    });
    const manual = () => hovering || touching || performance.now() < holdUntil;
    function layout() {
      const h = stage.clientHeight || 300;
      cardW = Math.min(h * o.fill * 1.5, stage.clientWidth * o.maxW);
      cards.forEach(c => { c.fig.style.width = cardW.toFixed(1) + 'px'; c.fig.style.height = (cardW / 1.5).toFixed(1) + 'px'; });
    }
    function place(fig, d, w) {
      const ad = Math.abs(d), sg = Math.sign(d);
      if (ad > o.visible) { fig.style.visibility = 'hidden'; return; }
      fig.style.visibility = 'visible';
      const x = sg * (Math.min(ad, 1) * o.spread + Math.max(0, ad - 1) * 0.24) * w;
      const z = -(Math.min(ad, 1) * 190 + Math.max(0, ad - 1) * 95);
      const ry = -sg * Math.min(ad, 1.5) * 38;
      const sc = Math.max(0.7, 1 - Math.min(ad, 1) * 0.06 - Math.max(0, ad - 1) * 0.06);
      fig.style.transform = `translate(-50%, -50%) translate3d(${x.toFixed(1)}px, 0, ${z.toFixed(1)}px) rotateY(${ry.toFixed(2)}deg) scale(${sc.toFixed(3)})`;
      fig.style.opacity = Math.max(0, Math.min(1, 1 - Math.max(0, ad - 1.3) * 0.4)).toFixed(3);
      fig.style.zIndex = String(200 - Math.round(ad * 10));
    }
    function tick(now) {
      const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
      last = now;
      const n = cards.length;
      if (manual()) { cur += (target - cur) * 0.18; if (Math.abs(target - cur) < 0.0005) cur = target; }
      else if (!REDUCED && onScreen && !document.hidden) { cur += o.speed * dt; target = cur; }
      cards.forEach((c, i) => {
        let d = i - cur; d -= Math.round(d / n) * n;
        if (!c.loaded && Math.abs(d) <= o.visible + 1) { c.loaded = true; c.im.src = c.p.src; }
        place(c.fig, d, cardW);
      });
      const f = ((Math.round(cur) % n) + n) % n;
      if (f !== shown && o.cap) { shown = f; o.cap.textContent = photos[f].caption || ''; }
      requestAnimationFrame(tick);
    }
    root.addEventListener('mouseenter', () => { hovering = true; target = Math.round(cur); });
    root.addEventListener('mouseleave', () => { hovering = false; });
    root.addEventListener('wheel', e => {
      if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
      e.preventDefault(); hovering = true; target += e.deltaX / 240;
      clearTimeout(settle); settle = setTimeout(() => { target = Math.round(target); }, 160);
    }, { passive: false });
    root.addEventListener('pointerdown', e => { if (e.pointerType !== 'touch') return; touching = true; dragged = false; sx = e.clientX; st = target = cur; });
    root.addEventListener('pointermove', e => { if (!touching) return; const dx = e.clientX - sx; if (Math.abs(dx) > 8) dragged = true; target = st - dx / (cardW * 0.62); });
    const end = () => { if (!touching) return; touching = false; holdUntil = performance.now() + 2500; target = Math.round(target); setTimeout(() => { dragged = false; }, 50); };
    root.addEventListener('pointerup', end); root.addEventListener('pointercancel', end);
    if ('IntersectionObserver' in window) new IntersectionObserver(es => { onScreen = es[0].isIntersecting; }).observe(root);
    layout();
    addEventListener('resize', () => requestAnimationFrame(layout));
    if ('ResizeObserver' in window) new ResizeObserver(() => layout()).observe(stage);
    requestAnimationFrame(tick);
  }

  const data = document.getElementById('wwm-reel');
  const root = document.getElementById('reel');
  if (data && root) {
    let picks = [];
    try { picks = JSON.parse(data.textContent); } catch (e) { picks = []; }
    const photos = picks.map(p => ({ src: url(p.key), caption: p.caption || '' }));
    if (photos.length) Reel(root, photos, { cap: document.getElementById('cap'), fill: 0.92, maxW: innerWidth < 760 ? 0.72 : 0.5 });
  }
})();
