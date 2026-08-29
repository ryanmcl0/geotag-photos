/* Video Browser front-end. Talks to serve.py's tiny JSON API. */
'use strict';

const $ = id => document.getElementById(id);
const DEVICE_META = {
  drone:  { label: 'Drone',      icon: '🛸' },
  camera: { label: 'Camera',     icon: '📷' },
  action: { label: 'Action cam', icon: '🎥' },
  '360':  { label: '360',        icon: '🌐' },
  phone:  { label: 'Phone',      icon: '📱' },
  other:  { label: 'Other',      icon: '🎞' },
};

let CLIPS = [];
let CUTS = { cuts: [] };
let activeCutId = localStorage.getItem('vb_active_cut') || null;
let filtered = [];
let lbIndex = -1;

const state = {
  trip: '', year: '', day: '', building: '', search: '', sort: 'chrono',
  devices: new Set(), regions: new Set(),
};

// ── data ────────────────────────────────────────────────────────────────────

async function loadAll() {
  const [idx, cuts] = await Promise.all([
    fetch('/api/index').then(r => r.json()),
    fetch('/api/cuts').then(r => r.json()),
  ]);
  CLIPS = idx.clips || [];
  CUTS = cuts && cuts.cuts ? cuts : { cuts: [] };
  if (!activeCutId && CUTS.cuts.length) activeCutId = CUTS.cuts[0].id;
  buildFilters();
  render();
  renderCuts();
}

let saveTimer = null;
function saveCuts() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fetch('/api/cuts', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(CUTS) });
  }, 400);
}

// ── filters ─────────────────────────────────────────────────────────────────

function uniq(arr) { return [...new Set(arr)].filter(Boolean); }

function buildFilters() {
  const trips = uniq(CLIPS.map(c => c.trip));
  fillSelect($('f-trip'), ['All trips', ...trips], state.trip);
  const years = uniq(CLIPS.map(c => c.year)).sort().reverse();
  fillSelect($('f-year'), ['All years', ...years], state.year);
  buildDayFilter();
  buildBuildingFilter();
  const devs = uniq(CLIPS.map(c => c.device));
  const dRow = $('f-devices');
  dRow.innerHTML = '';
  for (const d of ['drone', 'camera', 'action', '360', 'phone']) {
    if (!devs.includes(d)) continue;
    const el = document.createElement('span');
    el.className = 'chip' + (state.devices.has(d) ? ' on' : '');
    el.textContent = `${DEVICE_META[d].icon} ${DEVICE_META[d].label}`;
    el.onclick = () => { toggleSet(state.devices, d); el.classList.toggle('on'); render(); };
    dRow.appendChild(el);
  }
  const regions = uniq(CLIPS.flatMap(c => c.regions || [])).sort();
  const rRow = $('f-regions');
  rRow.innerHTML = '';
  for (const r of regions) {
    const el = document.createElement('span');
    el.className = 'chip' + (state.regions.has(r) ? ' on' : '');
    el.textContent = r;
    el.onclick = () => { toggleSet(state.regions, r); el.classList.toggle('on'); render(); };
    rRow.appendChild(el);
  }
}

function buildDayFilter() {
  const pool = CLIPS.filter(c => !state.trip || c.trip === state.trip);
  const days = uniq(pool.map(c => c.day)).sort((a, b) => a - b);
  const labels = { '': 'All days' };
  for (const d of days) {
    const c = pool.find(x => x.day === d);
    labels[d] = `Day ${d}` + (c && c.label ? ` · ${c.label.slice(0, 30)}` : '');
  }
  const sel = $('f-day');
  sel.innerHTML = '';
  for (const [val, lab] of Object.entries(labels)) {
    const o = document.createElement('option');
    o.value = val; o.textContent = lab;
    sel.appendChild(o);
  }
  sel.value = state.day;
}

/* Buildings come from the Urbex rosters (config/*_roofs.json), matched at
   ingest time. Hidden entirely on trips with no climbs. */
function buildBuildingFilter() {
  const sel = $('f-building');
  const pool = CLIPS.filter(c => !state.trip || c.trip === state.trip);
  const counts = {};
  for (const c of pool) if (c.building) counts[c.building] = (counts[c.building] || 0) + 1;
  const names = Object.keys(counts).sort();
  sel.style.display = names.length ? '' : 'none';
  if (!names.length) { state.building = ''; return; }
  sel.innerHTML = '';
  const all = document.createElement('option');
  all.value = ''; all.textContent = `All buildings (${names.length})`;
  sel.appendChild(all);
  for (const n of names) {
    const o = document.createElement('option');
    o.value = n; o.textContent = `${n} (${counts[n]})`;
    sel.appendChild(o);
  }
  if (!names.includes(state.building)) state.building = '';
  sel.value = state.building;
}

function fillSelect(sel, options, current) {
  sel.innerHTML = '';
  options.forEach((v, i) => {
    const o = document.createElement('option');
    o.value = i === 0 ? '' : String(v);
    o.textContent = String(v);
    sel.appendChild(o);
  });
  sel.value = current;
}

function toggleSet(set, v) { set.has(v) ? set.delete(v) : set.add(v); }

function applyFilters() {
  const q = state.search.toLowerCase();
  let out = CLIPS.filter(c =>
    (!state.trip || c.trip === state.trip) &&
    (!state.year || String(c.year) === state.year) &&
    (!state.day || String(c.day) === state.day) &&
    (!state.building || c.building === state.building) &&
    (!state.devices.size || state.devices.has(c.device)) &&
    (!state.regions.size || (c.regions || []).some(r => state.regions.has(r))) &&
    (!q || (c.name + ' ' + (c.label || '') + ' ' + (c.building || '') + ' '
      + (c.regions || []).join(' ')).toLowerCase().includes(q))
  );
  if (state.sort === 'newest') {
    out = out.slice().sort((a, b) => (b.utc || '').localeCompare(a.utc || ''));
  } else if (state.sort === 'longest') {
    out = out.slice().sort((a, b) => b.duration - a.duration);
  }
  return out;
}

// ── grid ────────────────────────────────────────────────────────────────────

function fmtDur(s) {
  s = Math.round(s);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
function resTag(c) {
  const m = Math.max(c.w, c.h);
  if (m >= 5000) return '5K+'; if (m >= 3800) return '4K';
  if (m >= 2500) return '2.7K'; if (m >= 1900) return 'HD'; return 'SD';
}

function render() {
  filtered = applyFilters();
  const grid = $('grid');
  grid.innerHTML = '';
  const activeCut = getActiveCut();
  const inCut = new Set(activeCut ? activeCut.clips : []);
  let lastKey = null;
  const frag = document.createDocumentFragment();
  filtered.forEach((c, i) => {
    if (state.sort === 'chrono') {
      // Trips without Day folders (climbs, city trips) group by their folder
      // instead, so each building gets its own header rather than one big block.
      const key = c.trip + '|' + (c.day != null ? c.day : (c.building || c.label || ''));
      if (key !== lastKey) {
        lastKey = key;
        const h = document.createElement('div');
        h.className = 'day-head';
        const title = c.day != null ? `Day ${c.day}` : (c.building || c.label || c.trip);
        h.innerHTML = title +
          `<small>${[(c.regions || []).join(', '), c.day != null ? c.label : '', c.date]
            .filter(Boolean).join(' · ')}</small>`;
        frag.appendChild(h);
      }
    }
    frag.appendChild(makeCard(c, i, inCut));
  });
  grid.appendChild(frag);
  const totalDur = filtered.reduce((s, c) => s + c.duration, 0);
  $('count').textContent =
    `${filtered.length} clips · ${(totalDur / 3600).toFixed(1)} h`;
}

function makeCard(c, i, inCut) {
  const card = document.createElement('div');
  card.className = 'card' + (inCut.has(c.id) ? ' incut' : '');
  card.dataset.i = i;
  if (c.poster) {
    const img = document.createElement('img');
    img.className = 'poster'; img.loading = 'lazy';
    img.src = `/media/${c.id}.jpg`;
    card.appendChild(img);
  } else {
    const ph = document.createElement('div');
    ph.className = 'noposter';
    ph.textContent = DEVICE_META[c.device].icon;
    card.appendChild(ph);
  }
  if (c.strip) {
    const strip = document.createElement('div');
    strip.className = 'strip';
    card.appendChild(strip);
    card.addEventListener('mouseenter', () => {
      if (!strip.style.backgroundImage) {
        strip.style.backgroundImage = `url(/media/${c.id}.strip.jpg)`;
      }
      strip.classList.add('ready');
    }, { passive: true });
    card.addEventListener('mousemove', e => {
      const r = card.getBoundingClientRect();
      const f = Math.min(9, Math.max(0, Math.floor((e.clientX - r.left) / r.width * 10)));
      strip.style.backgroundPosition = `${(f / 9) * 100}% 0`;
    }, { passive: true });
  }
  card.insertAdjacentHTML('beforeend',
    `<span class="badge b-dev">${DEVICE_META[c.device].icon}</span>` +
    `<span class="badge b-res">${resTag(c)}</span>` +
    (!c.proxy ? '<span class="badge b-nop">no proxy</span>' : '') +
    `<div class="cap"><span class="b-dur2">${fmtDur(c.duration)}</span>` +
    `${c.name}<br>${[c.date, c.building, (c.regions || []).join(', ')]
      .filter(Boolean).join(' · ')}</div>` +
    `<span class="badge b-dur">${fmtDur(c.duration)}</span>`);
  const add = document.createElement('button');
  add.className = 'add'; add.textContent = '＋'; add.title = 'Add to video';
  add.onclick = e => { e.stopPropagation(); addToCut(c.id); };
  card.appendChild(add);
  card.onclick = () => openLightbox(i);
  return card;
}

// ── lightbox ────────────────────────────────────────────────────────────────

function openLightbox(i) {
  lbIndex = i;
  const c = filtered[i];
  if (!c) return;
  const v = $('lb-video');
  v.src = c.proxy ? `/media/${c.id}.mp4` : `/original/${c.id}`;
  $('lb-orig').style.display = c.proxy ? '' : 'none';
  $('lb-info').textContent =
    `${c.name} · ${DEVICE_META[c.device].label} (${c.model || '?'}) · ${resTag(c)} ` +
    `${c.fps ? Math.round(c.fps) + 'fps' : ''} · ${fmtDur(c.duration)} · ` +
    `${c.local ? c.local.replace('T', ' ') : 'no time'} · ` +
    `${c.day != null ? 'Day ' + c.day + ' · ' : ''}` +
    `${c.building ? '🏙 ' + c.building + ' · ' : ''}${(c.regions || []).join(', ')}` +
    `${c.lat != null ? ` · 📍 ${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}` : ''}` +
    `${c.proxy ? '' : ' · streaming original from NAS'}`;
  $('lightbox').classList.remove('hidden');
  v.play().catch(() => {});
}

function closeLightbox() {
  $('lightbox').classList.add('hidden');
  const v = $('lb-video');
  v.pause(); v.removeAttribute('src'); v.load();
  lbIndex = -1;
}

// ── cuts ────────────────────────────────────────────────────────────────────

function getActiveCut() {
  return CUTS.cuts.find(c => c.id === activeCutId) || null;
}

function newCut() {
  const name = prompt('Name for the new video:');
  if (!name) return;
  const cut = { id: 'cut_' + Date.now().toString(36), name,
    created: new Date().toISOString(), clips: [] };
  CUTS.cuts.push(cut);
  activeCutId = cut.id;
  localStorage.setItem('vb_active_cut', activeCutId);
  saveCuts(); renderCuts(); render();
  openCutsPanel(true);
}

function addToCut(clipId) {
  let cut = getActiveCut();
  if (!cut) { newCut(); cut = getActiveCut(); if (!cut) return; }
  if (!cut.clips.includes(clipId)) cut.clips.push(clipId);
  saveCuts(); renderCuts(); render();
  openCutsPanel(true);
}

function renderCuts() {
  const sel = $('cut-select');
  sel.innerHTML = '';
  if (!CUTS.cuts.length) {
    const o = document.createElement('option');
    o.textContent = 'no videos yet';
    sel.appendChild(o);
  }
  for (const c of CUTS.cuts) {
    const o = document.createElement('option');
    o.value = c.id; o.textContent = `${c.name} (${c.clips.length})`;
    sel.appendChild(o);
  }
  if (activeCutId) sel.value = activeCutId;

  const list = $('cut-clips');
  list.innerHTML = '';
  const cut = getActiveCut();
  const byId = Object.fromEntries(CLIPS.map(c => [c.id, c]));
  let total = 0;
  if (cut) {
    cut.clips.forEach((cid, pos) => {
      const c = byId[cid];
      if (!c) return;
      total += c.duration;
      const item = document.createElement('div');
      item.className = 'cut-item';
      item.draggable = true;
      item.dataset.pos = pos;
      item.innerHTML =
        (c.poster ? `<img src="/media/${c.id}.jpg" loading="lazy">`
                  : `<img alt="">`) +
        `<span class="ci-name">${pos + 1}. ${c.name} · ${fmtDur(c.duration)}</span>`;
      const x = document.createElement('button');
      x.className = 'ci-x'; x.textContent = '✕';
      x.onclick = () => { cut.clips.splice(pos, 1); saveCuts(); renderCuts(); render(); };
      item.appendChild(x);
      item.addEventListener('dragstart', e => {
        item.classList.add('dragging');
        e.dataTransfer.setData('text/plain', String(pos));
      });
      item.addEventListener('dragend', () => item.classList.remove('dragging'));
      item.addEventListener('dragover', e => e.preventDefault());
      item.addEventListener('drop', e => {
        e.preventDefault();
        const from = parseInt(e.dataTransfer.getData('text/plain'), 10);
        const to = pos;
        if (isNaN(from) || from === to) return;
        const [moved] = cut.clips.splice(from, 1);
        cut.clips.splice(to, 0, moved);
        saveCuts(); renderCuts();
      });
      list.appendChild(item);
    });
  }
  $('cut-stats').textContent = cut
    ? `${cut.clips.length} clips · ${fmtDur(total)} total`
    : 'Create a video, then add clips with ＋';
}

async function exportCut() {
  const cut = getActiveCut();
  if (!cut || !cut.clips.length) return;
  const out = $('export-result');
  out.textContent = 'Exporting…';
  try {
    const r = await fetch('/api/export', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cut_id: cut.id }) }).then(x => x.json());
    if (r.error) { out.innerHTML = `<span class="warn">${r.error}</span>`; return; }
    let html = `FCPXML: ${r.fcpxml}\n`;
    if (r.resolve && r.resolve.ok) {
      // Say what actually happened: a re-export appends the new clips to the
      // timeline you have been editing rather than building a fresh one.
      const v = r.resolve;
      const what = v.created
        ? `created with ${v.appended} clip${v.appended === 1 ? '' : 's'}`
        : v.appended
          ? `${v.appended} clip${v.appended === 1 ? '' : 's'} appended` +
            (v.already_present ? `, ${v.already_present} already there` : '')
          : 'already up to date';
      html += `<span class="ok">✓ Resolve project "${v.project}" ` +
              `timeline "${v.timeline}" ${what}</span>`;
    } else if (r.resolve) {
      html += `<span class="warn">Resolve API: ${r.resolve.why}</span>`;
    }
    out.innerHTML = html;
  } catch (e) {
    out.innerHTML = `<span class="warn">${e}</span>`;
  }
}

function deleteCut() {
  const cut = getActiveCut();
  if (!cut) return;
  if (!confirm(`Delete "${cut.name}"? (does not touch any files)`)) return;
  CUTS.cuts = CUTS.cuts.filter(c => c.id !== cut.id);
  activeCutId = CUTS.cuts.length ? CUTS.cuts[0].id : null;
  localStorage.setItem('vb_active_cut', activeCutId || '');
  saveCuts(); renderCuts(); render();
}

function openCutsPanel(open) {
  $('cuts').classList.toggle('hidden', !open);
  document.body.classList.toggle('cuts-open', open);
}

// ── progress ────────────────────────────────────────────────────────────────

async function pollProgress() {
  try {
    const p = await fetch('/api/progress').then(r => r.json());
    if (p && p.total) {
      $('progress').textContent = p.done < p.total
        ? `⚙ proxies ${p.done}/${p.total}` : `✓ proxies ready`;
    }
  } catch (e) { /* server gone */ }
}

// ── wiring ──────────────────────────────────────────────────────────────────

$('f-trip').onchange = e => {
  state.trip = e.target.value; state.day = '';
  buildDayFilter(); buildBuildingFilter(); render();
};
$('f-year').onchange = e => { state.year = e.target.value; render(); };
$('f-day').onchange = e => { state.day = e.target.value; render(); };
$('f-building').onchange = e => { state.building = e.target.value; render(); };
$('f-sort').onchange = e => { state.sort = e.target.value; render(); };
let searchTimer = null;
$('f-search').oninput = e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = e.target.value; render(); }, 200);
};
$('toggle-cuts').onclick = () => openCutsPanel($('cuts').classList.contains('hidden'));
$('cut-new').onclick = newCut;
$('cut-select').onchange = e => {
  activeCutId = e.target.value;
  localStorage.setItem('vb_active_cut', activeCutId);
  renderCuts(); render();
};
$('cut-export').onclick = exportCut;
$('cut-delete').onclick = deleteCut;
$('lb-close').onclick = closeLightbox;
$('lb-backdrop').onclick = closeLightbox;
$('lb-add').onclick = () => { if (lbIndex >= 0) addToCut(filtered[lbIndex].id); };
$('lb-reveal').onclick = () => {
  if (lbIndex < 0) return;
  fetch('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: filtered[lbIndex].id }) });
};
$('lb-orig').onclick = () => {
  if (lbIndex < 0) return;
  const v = $('lb-video');
  v.src = `/original/${filtered[lbIndex].id}`;
  v.play().catch(() => {});
};
document.addEventListener('keydown', e => {
  if ($('lightbox').classList.contains('hidden')) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowRight' && lbIndex < filtered.length - 1) openLightbox(lbIndex + 1);
  else if (e.key === 'ArrowLeft' && lbIndex > 0) openLightbox(lbIndex - 1);
  else if (e.key === 'a' && lbIndex >= 0) addToCut(filtered[lbIndex].id);
});

loadAll();
pollProgress();
setInterval(pollProgress, 5000);
setInterval(async () => {   // refresh proxy availability as the worker progresses
  const idx = await fetch('/api/index').then(r => r.json()).catch(() => null);
  if (!idx) return;
  const before = CLIPS.filter(c => c.proxy).length;
  CLIPS = idx.clips || CLIPS;
  if (CLIPS.filter(c => c.proxy).length !== before) render();
}, 30000);
