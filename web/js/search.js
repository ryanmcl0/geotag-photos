/**
 * Site search (search.html): one box over everything on the site, results grouped
 * by where they live (trips, China provinces / bridges / roads / energy, blogs,
 * places on the map).
 *
 * The index comes from build_search.py, in the variant this visitor can load.
 * Everything visitor-specific (the results, extra sections, banner crops) is in
 * that index, never in this file.
 *
 * Typing a place (a province, a country) opens a place card on top: everything
 * filed under that place, counted per section, with links into each.
 */
(function () {
  'use strict';

  const root = document.getElementById('search-app');
  if (!root) return;

  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  // Lowercase, accents stripped (Évasion → evasion), punctuation to spaces.
  const fold = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, ' ').trim();

  const CUSTOM_COUNTRY_NAMES = { SCT: 'Scotland', WAL: 'Wales', ENG: 'England', HK: 'Hong Kong', MO: 'Macau' };
  const regionNames = new Intl.DisplayNames(['en'], { type: 'region' });
  function countryName(cc) {
    if (CUSTOM_COUNTRY_NAMES[cc]) return CUSTOM_COUNTRY_NAMES[cc];
    try { return regionNames.of(cc) || cc; } catch (e) { return cc; }
  }

  // Display order when sections score the same; `grid` sections render as image
  // cards, the rest as rows.
  const SECTIONS = [
    { k: 'trip', title: 'Trips', unit: ['trip', 'trips'], grid: true },
    { k: 'province', title: 'China · Provinces', unit: ['province', 'provinces'], grid: true },
    { k: 'bridge', title: 'China · Bridges', unit: ['bridge', 'bridges'], grid: true },
    { k: 'road', title: 'China · On the Roads', unit: ['road trip', 'road trips'], grid: true },
    { k: 'energy', title: 'China · Energy & Infrastructure', unit: ['site', 'sites'], grid: true },
    { k: 'blog', title: 'Blogs', unit: ['blog', 'blogs'] },
    { k: 'place', title: 'On the Map', unit: ['place', 'places'], chip: ['place on the map', 'places on the map'] },
  ];
  let SECTION = {};
  // Sections only some visitors have arrive with their index (data.sections),
  // slotted in before "On the Map".
  function setSections(extra) {
    const list = SECTIONS.slice(0, -1).concat(extra || [], SECTIONS.slice(-1));
    SECTION = Object.fromEntries(list.map((s, i) => [s.k, { ...s, order: i }]));
    return list;
  }
  let SECTION_LIST = setSections();
  const FIRST = { grid: 8, row: 6 };

  const unlocked = () => !!(window.Unlock && window.Unlock.unlocked());

  root.innerHTML = `
    <header class="ss-head">
      <h1>Search</h1>
      <div class="ss-box">
        <svg class="ss-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/></svg>
        <input type="search" id="ss-input" autocomplete="off" spellcheck="false"
          placeholder="A place, a province, a bridge, a building, a trip…" aria-label="Search the site">
        <span class="ss-count" aria-live="polite"></span>
      </div>
    </header>
    <div id="ss-results"></div>`;
  const input = root.querySelector('#ss-input');
  const out = root.querySelector('#ss-results');
  const countEl = root.querySelector('.ss-count');

  let DATA = null;
  let PLACES = [];

  /* ---------------- data ---------------- */

  async function load() {
    if (unlocked()) {
      try {
        const r = await fetch('collections/search.all.json?t=' + Date.now());
        if (r.ok) return await r.json();
      } catch (e) { /* fall through to the public index */ }
    }
    const r = await fetch('collections/search.json?t=' + Date.now());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  }

  function prepare(data) {
    // Chinese mode (js/i18n.js): blog text searches and shows in Chinese, and names
    // match their Chinese too.
    const zh = window.I18N && window.I18N.lang === 'zh';
    const both = s => (zh && s ? s + ' ' + window.I18N.t(s) : s);
    data.items.forEach(it => {
      if (zh && it.bz) it.b = it.bz;
      it.placeNames = [...(it.p || []), ...(it.cc || []).map(countryName)];
      it._t = fold(both(it.t));
      it._s = fold([both(it.s), it.x].join(' '));
      it._p = fold(it.placeNames.map(both).join(' | '));
      it._b = (it.b || []).map(fold);
      it._words = (it._t + ' ' + it._p + ' ' + it._s).split(' ').filter(Boolean);
    });
    // Places a whole query can name: provinces from the index, countries from the
    // items themselves (so the public page only knows public countries).
    const places = (data.places || []).map(p => ({ ...p }));
    const ccs = new Map();
    data.items.forEach(it => (it.cc || []).forEach(cc => ccs.set(cc, (ccs.get(cc) || 0) + 1)));
    ccs.forEach((n, cc) => places.push({ n: countryName(cc), k: 'country', cc }));
    places.forEach(p => { p._keys = [p.n, ...(p.a || [])].map(fold).filter(Boolean); });
    return places;
  }

  /* ---------------- matching ---------------- */

  const startsWord = (text, q) => text.startsWith(q) || text.includes(' ' + q);

  // Optimal string alignment distance, capped (same rule as the China page search).
  function within(a, b, max) {
    if (Math.abs(a.length - b.length) > max) return false;
    let prev2 = null, prev = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      let rowMin = i;
      for (let j = 1; j <= b.length; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        let v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        if (prev2 && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
          v = Math.min(v, prev2[j - 2] + 1);
        }
        cur.push(v);
        rowMin = Math.min(rowMin, v);
      }
      if (rowMin > max) return false;
      prev2 = prev; prev = cur;
    }
    return prev[b.length] <= max;
  }

  // Typo'd word start (1 edit from 4 letters, 2 from 7) or letters in order
  // within a tight span ("hjiang" → huajiang).
  function fuzzyWord(q, text, words) {
    if (q.length < 3) return false;
    const max = q.length >= 7 ? 2 : q.length >= 4 ? 1 : 0;
    if (max && words.some(w => within(q, w.slice(0, q.length), max))) return true;
    if (q.length >= 4) {
      for (let start = text.indexOf(q[0]); start >= 0; start = text.indexOf(q[0], start + 1)) {
        let i = start, k = 0;
        while (i < text.length && k < q.length && i - start <= q.length + 2) {
          if (text[i] === q[k]) k++;
          i++;
        }
        if (k === q.length) return true;
      }
    }
    return false;
  }

  // Every query word has to land somewhere in the item; where it lands sets the
  // score (title > place > subtitle > blog text) and the "why" shown on the result.
  function scoreExact(it, qs) {
    let total = 0;
    const via = new Set();
    for (const q of qs) {
      let sc = 0;
      if (it._t.includes(q)) {
        sc = startsWord(it._t, q) ? 40 : 22;
        via.add('title');
      } else if (it._p.includes(q)) {
        sc = startsWord(it._p, q) ? 20 : 10;
        via.add('place');
      } else if (it._s.includes(q)) {
        sc = startsWord(it._s, q) ? 14 : 8;
        via.add('sub');
      } else if (it._b.some(p => p.includes(q))) {
        sc = 4;
        via.add('text');
      }
      if (!sc) return null;
      total += sc;
    }
    if (it._t === qs.join(' ')) total += 40;
    return { score: total, via };
  }

  function scoreFuzzy(it, qs) {
    const text = it._t + ' ' + it._p + ' ' + it._s;
    if (!qs.every(q => text.includes(q) || fuzzyWord(q, text, it._words))) return null;
    return { score: 2, via: new Set(['fuzzy']) };
  }

  function search(query) {
    const qs = fold(query).split(' ').filter(Boolean);
    if (!qs.length || fold(query).length < 2) return { qs, hits: [], fuzzy: false };
    const run = fn => DATA.items.map(it => {
      const m = fn(it, qs);
      if (!m) return null;
      // Blog text: the paragraph holding the most query words is the snippet.
      let para = -1, mentions = 0;
      if (it._b.length && (m.via.has('text') || m.via.has('title') || m.via.has('place'))) {
        let best = 0;
        it._b.forEach((p, i) => {
          const n = qs.filter(q => p.includes(q)).length;
          if (n) mentions++;
          if (n > best) { best = n; para = i; }
        });
        m.score += Math.min(mentions, 6);
      }
      // A little weight for size, so the bigger of two equal matches leads.
      m.score += Math.min(Math.log10((it.n || 0) + 1), 3);
      return { it, ...m, para, mentions };
    }).filter(Boolean);
    let hits = run(scoreExact);
    let fuzzy = false;
    if (!hits.length) { hits = run(scoreFuzzy); fuzzy = true; }
    return { qs, hits, fuzzy };
  }

  function placeFor(query) {
    const q = fold(query);
    if (q.length < 2) return null;
    const exact = PLACES.find(p => p._keys.includes(q));
    if (exact) return exact;
    if (q.length < 4) return null;
    const pre = PLACES.filter(p => p._keys.some(k => k.startsWith(q)));
    return pre.length === 1 ? pre[0] : null;
  }

  /* ---------------- rendering ---------------- */

  function mark(text, qs) {
    let html = esc(text);
    const f = fold(text);
    if (f.length !== text.length) {
      // Folding changed the length (punctuation runs, accents): mark on the raw text.
      qs.forEach(q => {
        const re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig');
        html = html.replace(re, '<mark>$1</mark>');
      });
      return html;
    }
    const spans = [];
    qs.forEach(q => {
      for (let i = f.indexOf(q); i >= 0; i = f.indexOf(q, i + 1)) spans.push([i, i + q.length]);
    });
    if (!spans.length) return html;
    spans.sort((a, b) => a[0] - b[0]);
    let res = '', at = 0;
    spans.forEach(([a, b]) => {
      if (a < at) return;
      res += esc(text.slice(at, a)) + '<mark>' + esc(text.slice(a, b)) + '</mark>';
      at = b;
    });
    return res + esc(text.slice(at));
  }

  function snippet(text, qs, len = 220) {
    const f = fold(text);
    let i = -1;
    for (const q of qs) { i = f.indexOf(q); if (i >= 0) break; }
    if (i < 0 || f.length !== text.length) {
      return mark(text.length > len ? text.slice(0, len).replace(/\s\S*$/, '') + '…' : text, qs);
    }
    let a = Math.max(0, i - 80);
    if (a) a = text.indexOf(' ', a) + 1;
    let b = Math.min(text.length, a + len);
    if (b < text.length) b = text.lastIndexOf(' ', b);
    return (a ? '…' : '') + mark(text.slice(a, b), qs) + (b < text.length ? '…' : '');
  }


  function imgFor(c, kind) {
    if (!c) return '';
    const src = c.src ? esc(c.src) : Gallery.photoUrl(c, kind);
    const focus = c.f ? ` style="object-position:50% ${+c.f}%"` : '';   // banner crop from the index
    return `<img loading="lazy" alt="" src="${src}"${focus} onerror="Gallery.lockedCover(this)">`;
  }

  const plural = (n, unit) => `${n.toLocaleString()} ${n === 1 ? unit[0] : unit[1]}`;

  function whyLine(h, qs) {
    if (h.via.has('title') || h.via.has('fuzzy')) return '';
    if (h.via.has('place')) {
      const p = h.it.placeNames.find(n => qs.some(q => fold(n).includes(q)));
      if (p) return `in ${esc(p)}`;
    }
    if (h.via.has('text') && h.mentions) return `mentioned in ${h.mentions} ${h.mentions === 1 ? 'paragraph' : 'paragraphs'}`;
    return '';
  }

  function hrefFor(h, qs) {
    const it = h.it;
    if (it.k === 'blog' && h.para >= 0) {
      return `${it.u}?hl=${encodeURIComponent(qs.join(' '))}#p-${h.para}`;
    }
    return it.u;
  }

  function card(h, qs) {
    const it = h.it;
    const why = whyLine(h, qs);
    const meta = [it.n ? `${it.n.toLocaleString()} photos` : '', why].filter(Boolean).join(' · ');
    const mapBtn = it.m ? `<a class="ss-card__map" href="${esc(it.m)}">Map</a>` : '';
    return `<article class="ss-card">
      <a class="ss-card__link" href="${esc(hrefFor(h, qs))}">
        <div class="ss-card__img">${imgFor(it.c, 'thumbnails')}</div>
        <div class="ss-card__body">
          <div class="ss-card__title">${mark(it.t, qs)}</div>
          ${it.s ? `<div class="ss-card__sub">${mark(it.s, qs)}</div>` : ''}
          ${it.x ? `<div class="ss-card__zh">${esc(it.x)}</div>` : ''}
          ${meta ? `<div class="ss-card__meta">${meta}</div>` : ''}
        </div>
      </a>${mapBtn}
    </article>`;
  }

  function row(h, qs) {
    const it = h.it;
    const why = whyLine(h, qs);
    let body = '';
    if (it.k === 'blog') {
      body = h.para >= 0 ? `<div class="ss-row__snip">${snippet(it.b[h.para], qs)}</div>` : '';
    }
    const meta = [it.s ? mark(it.s, qs) : '', it.k === 'place' && it.n ? `${it.n} photos` : '',
      it.k === 'blog' ? '' : why].filter(Boolean).join(' · ');
    const blogMeta = it.k === 'blog' && why ? `<div class="ss-row__why">${why}</div>` : '';
    return `<a class="ss-row ss-row--${it.k}" href="${esc(hrefFor(h, qs))}">
      <div class="ss-row__img">${imgFor(it.c, 'thumbnails')}</div>
      <div class="ss-row__body">
        <div class="ss-row__title">${mark(it.t, qs)}</div>
        ${meta ? `<div class="ss-row__meta">${meta}</div>` : ''}
        ${body}${blogMeta}
      </div>
    </a>`;
  }

  function placeCard(place, groups, qs) {
    const counts = SECTION_LIST.map(sec => {
      const g = groups.get(sec.k);
      return g ? `<a class="ss-chip" href="#ss-${sec.k}">${plural(g.length, sec.chip || sec.unit)}</a>` : '';
    }).join('');
    let cover = null, photos = 0;
    let kicker = ['HK', 'MO'].includes(place.cc) ? 'Special Administrative Region of China' : 'Country';
    if (place.k === 'province') {
      kicker = 'Province of China';
      const tile = DATA.items.find(i => i.k === 'province' && fold(i.t) === fold(place.n));
      cover = tile && tile.c;
      photos = tile ? tile.n || 0 : 0;
    } else {
      const trips = DATA.items.filter(i => i.k === 'trip' && (i.cc || []).includes(place.cc));
      trips.sort((a, b) => (b.n || 0) - (a.n || 0));
      cover = trips[0] && trips[0].c;
      photos = trips.reduce((s, t) => s + (t.n || 0), 0);
    }
    if (!cover) {
      const any = (groups.get('trip') || groups.get('place') || [])[0];
      cover = any && any.it.c;
    }
    const links = [
      place.u ? `<a class="ss-btn" href="${esc(place.u)}">Open ${place.k === 'province' ? 'province' : 'gallery'}</a>` : '',
      place.m ? `<a class="ss-btn" href="${esc(place.m)}">Show on the map</a>` : '',
    ].join('');
    return `<section class="ss-place">
      ${cover ? `<div class="ss-place__img">${imgFor(cover, 'display')}</div>` : ''}
      <div class="ss-place__inner">
        <div class="ss-place__kicker">${esc(kicker)}${place.a && place.a[0] ? ` · ${esc(place.a[0])}` : ''}</div>
        <h2 class="ss-place__name">${esc(place.n)}</h2>
        ${photos ? `<div class="ss-place__stat">${photos.toLocaleString()} photos</div>` : ''}
        ${counts ? `<div class="ss-chips">${counts}</div>` : ''}
        ${links ? `<div class="ss-place__links">${links}</div>` : ''}
      </div>
    </section>`;
  }

  function suggestions() {
    const pick = (k, n, by = (a, b) => (b.n || 0) - (a.n || 0)) =>
      DATA.items.filter(i => i.k === k).sort(by).slice(0, n).map(i => i.t);
    const countries = PLACES.filter(p => p.k === 'country' && p.cc !== 'GB')
      .map(p => [p.n, DATA.items.filter(i => i.k === 'trip' && (i.cc || []).includes(p.cc)).length])
      .sort((a, b) => b[1] - a[1]).slice(0, 3).map(x => x[0]);
    const list = [...pick('province', 3), ...pick('bridge', 2), ...countries];
    return [...new Set(list)];
  }

  function renderEmpty() {
    const sug = suggestions();
    out.innerHTML = `<div class="ss-empty">
      <p>Site-wide search of all trips, blogs, countries, locations and maps</p>
      <div class="ss-chips">${sug.map(s => `<button type="button" class="ss-chip" data-q="${esc(s)}">${esc(s)}</button>`).join('')}</div>
    </div>`;
    countEl.textContent = '';
  }

  function render() {
    const query = input.value;
    if (fold(query).length < 2) return renderEmpty();
    // A place typed by another of its names (浙江) searches as its English name.
    const place = placeFor(query);
    const { qs, hits, fuzzy } = search(place && !place._keys[0].startsWith(fold(query)) ? place.n : query);

    const groups = new Map();
    hits.forEach(h => {
      if (!groups.has(h.it.k)) groups.set(h.it.k, []);
      groups.get(h.it.k).push(h);
    });
    groups.forEach(list => list.sort((a, b) => b.score - a.score || (b.it.n || 0) - (a.it.n || 0)));
    // Sections lead with their best match; equal bests keep the site's order.
    const order = [...groups.keys()].sort((a, b) =>
      (groups.get(b)[0].score - groups.get(a)[0].score) || (SECTION[a].order - SECTION[b].order));

    let html = place ? placeCard(place, groups, qs) : '';
    if (fuzzy && hits.length) html += `<p class="ss-note">No exact matches for “${esc(query.trim())}”. Showing close spellings.</p>`;
    order.forEach(k => {
      const sec = SECTION[k];
      const list = groups.get(k);
      const first = sec.grid ? FIRST.grid : FIRST.row;
      const body = list.map((h, i) =>
        `<div class="ss-item"${i >= first ? ' hidden' : ''}>${sec.grid ? card(h, qs) : row(h, qs)}</div>`).join('');
      html += `<section class="ss-sec" id="ss-${k}">
        <div class="ss-sec__head"><h3>${esc(sec.title)}</h3><span class="count">${plural(list.length, sec.unit)}</span></div>
        <div class="${sec.grid ? 'ss-grid' : 'ss-rows'}">${body}</div>
        ${list.length > first ? `<button type="button" class="ss-more">Show all ${list.length}</button>` : ''}
      </section>`;
    });
    if (!hits.length) html += `<p class="gallery-empty">Nothing matches “${esc(query.trim())}”.</p>`;
    out.innerHTML = html;
    countEl.textContent = hits.length ? `${hits.length} results` : '';
  }

  /* ---------------- wiring ---------------- */

  let timer = null;
  input.addEventListener('input', () => {
    const q = input.value.trim();
    history.replaceState(null, '', q ? `?q=${encodeURIComponent(q)}` : location.pathname);
    clearTimeout(timer);
    timer = setTimeout(render, 60);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape' && input.value) { input.value = ''; input.dispatchEvent(new Event('input')); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== input &&
        !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || '')) {
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
  out.addEventListener('click', e => {
    const chip = e.target.closest('[data-q]');
    if (chip) {
      input.value = chip.dataset.q;
      input.dispatchEvent(new Event('input'));
      input.focus();
      return;
    }
    const more = e.target.closest('.ss-more');
    if (more) {
      more.parentElement.querySelectorAll('.ss-item[hidden]').forEach(n => { n.hidden = false; });
      more.remove();
      return;
    }
    const jump = e.target.closest('a[href^="#ss-"]');
    if (jump) {
      e.preventDefault();
      const t = document.getElementById(jump.getAttribute('href').slice(1));
      if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
  });

  load().then(data => {
    DATA = data;
    SECTION_LIST = setSections(data.sections);
    PLACES = prepare(data);
    input.value = new URLSearchParams(location.search).get('q') || '';
    render();
    input.focus();
  }).catch(err => {
    out.innerHTML = `<p class="gallery-empty">Could not load the search index: ${esc(err.message)}</p>`;
  });
})();
