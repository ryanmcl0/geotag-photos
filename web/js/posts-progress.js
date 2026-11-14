/*
 * Posts ▸ Reels ▸ In progress (localhost only, owner 2026-11-14: "Is there smth running rn? Can you add an in progress
 * section to my reels tab … that summarise short form content job status. And also the html pipeline dashboard for
 * vlogs can go there too so I can see all this status when I just launch the desktop localhost app", and: "it should
 * make it clear what is AI and what's machine").
 *
 * It only reads a report: first the local helper's GET /reels/status (tools/reels_local.py asks the video project's
 * tools/vlog/reel_status.py on demand), else web/phone/reels/status.json (local-only, like usage.json), which that tool
 * writes at the end of each generator turn from what its jobs leave behind. Nothing here starts, stops or changes a job. The report's
 * shape (version 2):
 *   made (local time), disk_ok, note?, ai_note, render_running, filed_count, held_count
 *   now[]: {who, text}                              what is running this minute
 *   redo: {state, who, who_note, done, failed, left, total, current, since, failed_names[], last[]}
 *   generator: {state, who, who_note, batch, page, finished, waiting, failed, second_mac, plan, note, as_of,
 *               this_mac[], second_mac_lines[]}
 *   built[]: {batch, state, who, who_note, queued, waiting[{name}], rendering[{name, where}], failed[],
 *             filed[{name, place, note}], held[{name, note}], last[]}
 *   vlog: {who_note, dashboard, dashboard_up, trips[{trip, days[{day, stage, kind, step_who, waiting_on, msg, at}],
 *          running[{day, stage, who, detail, done, total}]}]}
 * "who" is "machine" (scripts and renders, no AI), "ai" (Claude in a session) or "you" (waiting for the owner).
 *
 * On the page: when the report was made (a warning when it is old), the legend, what is running now, one card per
 * job with a Machine / AI tag, the reels filed and held, and the vlog days with the vlog dashboard (status.py serve,
 * port 8770, started by site_launcher.sh) in a frame under them. The section re-reads the file every 30 s while it
 * is showing.
 */
window.PostsProgress = (function () {
    'use strict';

    const SRC = '/phone/reels/status.json';
    // the local helper makes the report on demand (tools/reels_local.py GET /reels/status); the file is the fallback
    const HOST = (typeof location !== 'undefined' && location.hostname) || 'localhost';
    const PORT = (() => {
        try { return parseInt(localStorage.getItem('reels_helper_port'), 10) || 8791; } catch (e) { return 8791; }
    })();
    const LIVE = `http://${HOST === 'localhost' ? '127.0.0.1' : HOST}:${PORT}/reels/status`;
    const EVERY = 30000;
    const STALE_S = 300;
    let report;                      // undefined = not loaded yet, null = no report
    let loadP = null;
    let panelEl = null;
    let timer = null;

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }

    const valid = d => !!d && typeof d === 'object' && typeof d.version === 'number' && typeof d.made === 'string';

    function load(force) {
        if (!loadP || force) {
            // a missing file comes back as the site's HTML page (the dev server's fallback), so parse it carefully
            const get = url => fetch(url, { cache: 'no-store' })
                .then(r => (r.ok ? r.text() : null))
                .then(t => { try { const d = t ? JSON.parse(t) : null; return valid(d) ? d : null; } catch (e) { return null; } })
                .catch(() => null);
            loadP = get(LIVE).then(d => d || get(SRC))
                .then(d => {
                    report = d;
                    try { document.dispatchEvent(new CustomEvent('reels:changed', { detail: 'progress' })); } catch (e) { /* no DOM */ }
                    return d;
                });
        }
        return loadP;
    }

    // ── small pieces ──
    const WHO = { machine: ['Machine', 'No AI: scripts and renders on the Macs'],
                  ai: ['AI', 'Claude, in a session, on the subscription'],
                  you: ['You', 'Waiting for you'],
                  mixed: ['AI + Machine', 'Part script, part Claude'],
                  'ai+machine': ['AI + Machine', 'Claude plans and reviews; the render is machine only'] };

    function whoTag(who) {
        const w = WHO[who] || WHO.machine;
        const t = el('span', 'ip-who ip-who-' + String(who || 'machine').replace(/\W/g, '-'), w[0]);
        t.title = w[1];
        return t;
    }

    const STATE = { running: 'Running', idle: 'Idle', waiting: 'Waiting for a render', finished: 'Finished',
                    stopped: 'Stopped', paused: 'Paused', 'not running': 'Not running',
                    'no disk space': 'No disk space' };

    function stateTag(state) {
        return el('span', 'ip-state ip-state-' + String(state || 'idle').replace(/\W/g, '-'), STATE[state] || state || '');
    }

    function ageSeconds(made, now) {
        const t = Date.parse(made);
        return isNaN(t) ? null : Math.round(((now || Date.now()) - t) / 1000);
    }

    function agoText(s) {
        if (s === null) return '';
        if (s < 90) return 'just now';
        if (s < 5400) return Math.round(s / 60) + ' min ago';
        return Math.round(s / 3600) + ' h ago';
    }

    function bar(done, total, failed) {
        const b = el('div', 'ip-bar');
        const pct = total ? Math.round(100 * done / total) : 0;
        const f = el('div', 'ip-bar-fill');
        f.style.width = pct + '%';
        b.appendChild(f);
        if (failed && total) {
            const x = el('div', 'ip-bar-fail');
            x.style.width = Math.round(100 * failed / total) + '%';
            b.appendChild(x);
        }
        b.title = `${done} of ${total}` + (failed ? `, ${failed} failed` : '');
        return b;
    }

    function lines(list, cls) {
        const box = el('div', cls || 'ip-lines');
        (list || []).forEach(x => box.appendChild(el('div', 'ip-line', x)));
        return box;
    }

    function fold(summary, body, open) {
        const d = el('details', 'ip-fold');
        if (open) d.open = true;
        d.appendChild(el('summary', null, summary));
        d.appendChild(body);
        return d;
    }

    function card(title, who, state, note) {
        const c = el('section', 'ip-card');
        const h = el('div', 'ip-card-head');
        h.appendChild(el('h3', 'ip-card-title', title));
        h.appendChild(whoTag(who));
        if (state) h.appendChild(stateTag(state));
        c.appendChild(h);
        if (note) c.appendChild(el('div', 'ip-note', note));
        return c;
    }

    function figures(pairs) {
        const row = el('div', 'ip-figs');
        pairs.forEach(([n, label]) => {
            if (n === undefined || n === null) return;
            const f = el('div', 'ip-fig');
            f.appendChild(el('div', 'ip-fig-n', String(n)));
            f.appendChild(el('div', 'ip-fig-l', label));
            row.appendChild(f);
        });
        return row;
    }

    // ── the cards ──
    function redoCard(r) {
        const c = card(r.title || 'Redo of old reels', r.who, r.state, r.who_note);
        c.appendChild(bar(r.done || 0, r.total || 0, r.failed || 0));
        c.appendChild(figures([[r.done, 'done'], [r.left, 'left'], [r.failed, 'failed'], [r.total, 'in all']]));
        if (r.current) c.appendChild(el('div', 'ip-current', `Now: ${r.current}` + (r.since ? ` (since ${r.since})` : '')));
        if ((r.failed_names || []).length) c.appendChild(fold(`${r.failed_names.length} failed`, lines(r.failed_names)));
        if ((r.last || []).length) c.appendChild(fold('Last lines', lines(r.last, 'ip-lines ip-mono')));
        return c;
    }

    function generatorCard(g) {
        const c = card(g.title || 'Machine-only generator', g.who, g.state, g.who_note);
        c.appendChild(figures([[g.finished, 'finished'], [g.waiting, 'planned, waiting'], [g.failed, 'failed'],
                               ...(g.second_mac_off ? [] : [[g.second_mac, 'sent to the second Mac']])]));
        if (g.batch) c.appendChild(el('div', 'ip-current', `Batch ${g.batch}` + (g.page ? ` · ${g.page}` : '')));
        if (g.plan) c.appendChild(el('div', 'ip-sub', `Plan: ${g.plan}`));
        if (g.note) c.appendChild(el('div', 'ip-warn', g.note));
        const both = el('div', 'ip-two');
        if ((g.this_mac || []).length) both.appendChild(fold('This Mac, last lines', lines(g.this_mac, 'ip-lines ip-mono')));
        if ((g.second_mac_lines || []).length) both.appendChild(fold('Second Mac, last lines', lines(g.second_mac_lines, 'ip-lines ip-mono')));
        if (both.childNodes.length) c.appendChild(both);
        return c;
    }

    function builtCard(b) {
        const c = card(b.title || 'Reels planned by the AI', b.who, b.state, b.who_note);
        c.appendChild(figures([[(b.rendering || []).length, 'rendering now'], [(b.waiting || []).length, 'waiting for a render'],
                               [(b.filed || []).length, 'filed in your folders'], [(b.held || []).length, 'held back'],
                               [(b.failed || []).length || null, 'failed']]));
        c.appendChild(el('div', 'ip-sub', `Batch ${b.batch}`));
        (b.rendering || []).forEach(it => {
            const row = el('div', 'ip-current');
            row.appendChild(whoTag('machine'));
            row.appendChild(document.createTextNode(` Rendering ${it.name} on ${it.where}`));
            c.appendChild(row);
        });
        if ((b.waiting || []).length) c.appendChild(fold(`${b.waiting.length} waiting for a render`, lines(b.waiting.map(x => x.name))));
        if ((b.filed || []).length) {
            const box = el('div', 'ip-lines');
            b.filed.slice().reverse().forEach(f => {
                const row = el('div', 'ip-line');
                row.appendChild(el('span', 'ip-strong', f.name));
                row.appendChild(el('span', 'ip-dim', '  ' + f.place));
                if (f.note) row.title = f.note;
                box.appendChild(row);
            });
            c.appendChild(fold(`${b.filed.length} filed (newest first)`, box));
        }
        if ((b.held || []).length) {
            const box = el('div', 'ip-lines');
            b.held.forEach(h => {
                const row = el('div', 'ip-line');
                row.appendChild(el('span', 'ip-strong', h.name));
                if (h.note) row.appendChild(el('span', 'ip-dim', '  ' + h.note));
                box.appendChild(row);
            });
            c.appendChild(fold(`${b.held.length} held back, and why`, box));
        }
        return c;
    }

    const DAY_STATE = { done: 'Done', wait: 'Waiting', busy: 'Running', failed: 'Failed', blocked: 'Blocked', stopped: 'Stopped' };

    function vlogCard(v) {
        const c = card(v.title || 'Vlog pipeline', v.who, null, v.who_note);
        (v.trips || []).forEach(t => {
            const waiting = (t.days || []).filter(d => d.kind === 'wait').length;
            const table = el('table', 'ip-table');
            (t.days || []).forEach(d => {
                const tr = el('tr');
                tr.appendChild(el('td', 'ip-day', 'Day ' + d.day));
                const st = el('td', 'ip-daystate');
                st.appendChild(el('span', 'ip-state ip-state-' + (d.kind === 'wait' ? 'waiting' : d.kind === 'busy' ? 'running' : d.kind === 'done' ? 'finished' : 'stopped'),
                                  DAY_STATE[d.kind] || d.kind || ''));
                if (d.waiting_on) { st.appendChild(document.createTextNode(' on ')); st.appendChild(whoTag(d.waiting_on)); }
                tr.appendChild(st);
                const stage = el('td', 'ip-stage');
                stage.appendChild(document.createTextNode((d.stage || '') + ' '));
                if (d.step_who && d.kind !== 'done') stage.appendChild(whoTag(d.step_who));
                tr.appendChild(stage);
                tr.appendChild(el('td', 'ip-msg', d.msg || ''));
                table.appendChild(tr);
            });
            (t.running || []).forEach(j => {
                const row = el('div', 'ip-current');
                row.appendChild(whoTag(j.who));
                row.appendChild(document.createTextNode(` Day ${j.day}: ${j.stage}` + (j.total ? ` (${j.done || 0} of ${j.total})` : '') + (j.detail ? ` · ${j.detail}` : '')));
                c.appendChild(row);
            });
            // closed unless something of this trip runs right now: the waiting days are in the summary line
            c.appendChild(fold(`${t.trip}: ${(t.days || []).length} days` + (waiting ? `, ${waiting} waiting` : ''), table,
                               (t.running || []).length > 0));
        });
        const dash = el('div', 'ip-dash');
        if (v.dashboard_up) {
            const a = el('a', 'ip-link', 'Open the vlog dashboard in its own tab');
            a.href = v.dashboard;
            a.target = '_blank';
            a.rel = 'noopener';
            dash.appendChild(a);
            const f = el('iframe', 'ip-frame');
            f.src = v.dashboard;
            f.title = 'Vlog pipeline dashboard';
            f.loading = 'lazy';
            dash.appendChild(f);
        } else {
            dash.appendChild(el('div', 'ip-sub', 'The vlog dashboard is not running. It starts with the site launcher when the work disk is connected.'));
        }
        c.appendChild(dash);
        return c;
    }

    // ── the whole section ──
    function draw(now) {
        const root = el('div', 'ip');
        const head = el('div', 'ip-headline');
        head.appendChild(el('h2', 'pq-h', 'In progress'));
        root.appendChild(head);
        if (report === undefined) { root.appendChild(el('div', 'ip-sub', 'Reading the status…')); return root; }
        if (!report) {
            root.appendChild(el('div', 'ip-sub', 'No status yet. The video project writes it once a minute while its generator is switched on.'));
            return root;
        }
        const age = ageSeconds(report.made, now);
        head.appendChild(el('span', 'ip-made', `Status of ${String(report.made).slice(11, 16)} (${agoText(age)}), refreshed every minute`));
        if (age !== null && age > STALE_S) {
            root.appendChild(el('div', 'ip-warn', `This status is ${agoText(age).replace(' ago', '')} old: the minute refresh is not running, so the figures below may be out of date.`));
        }
        if (report.disk_ok === false) { root.appendChild(el('div', 'ip-warn', report.note || 'The work disk is not connected.')); return root; }

        const legend = el('div', 'ip-legend');
        ['machine', 'ai', 'you'].forEach(w => {
            const item = el('span', 'ip-legend-item');
            item.appendChild(whoTag(w));
            item.appendChild(document.createTextNode(' ' + WHO[w][1]));
            legend.appendChild(item);
        });
        root.appendChild(legend);
        if (report.ai_note) root.appendChild(el('div', 'ip-note', report.ai_note));

        const nowBox = el('section', 'ip-card ip-now');
        nowBox.appendChild(el('h3', 'ip-card-title', 'Running right now'));
        if ((report.now || []).length) {
            report.now.forEach(x => {
                const row = el('div', 'ip-current');
                row.appendChild(whoTag(x.who));
                row.appendChild(document.createTextNode(' ' + x.text));
                nowBox.appendChild(row);
            });
        } else {
            nowBox.appendChild(el('div', 'ip-sub', 'Nothing is running right now.'));
        }
        root.appendChild(nowBox);

        if (report.redo) root.appendChild(redoCard(report.redo));
        if (report.generator) root.appendChild(generatorCard(report.generator));
        (report.built || []).slice().reverse().forEach(b => root.appendChild(builtCard(b)));
        if (report.vlog) root.appendChild(vlogCard(report.vlog));
        return root;
    }

    function redraw() {
        if (!panelEl) return;
        // keep the vlog frame (and any opened folds) when only the figures changed: redraw everything but reuse the frame
        const oldFrame = panelEl.querySelector('.ip-frame');
        const open = new Set(Array.from(panelEl.querySelectorAll('details[open] > summary')).map(s => s.textContent.replace(/\d+/g, '#')));
        const fresh = draw();
        fresh.querySelectorAll('details > summary').forEach(s => { if (open.has(s.textContent.replace(/\d+/g, '#'))) s.parentNode.open = true; });
        const newFrame = fresh.querySelector('.ip-frame');
        if (oldFrame && newFrame && oldFrame.src === newFrame.src) newFrame.parentNode.replaceChild(oldFrame, newFrame);
        panelEl.replaceChildren(fresh);
    }

    function tick() {
        if (!panelEl || !document.body.contains(panelEl)) { clearInterval(timer); timer = null; return; }
        load(true).then(redraw);
    }

    function panel() {
        panelEl = el('div', 'ip-wrap');
        panelEl.appendChild(draw());
        load(report === undefined ? false : true).then(redraw);
        if (timer) clearInterval(timer);
        timer = setInterval(tick, EVERY);
        return panelEl;
    }

    // the section button's number: how many things are running this minute, or null without a report
    function count() {
        if (!report) return null;
        return (report.now || []).length;
    }

    return { panel, load, count, draw, ageSeconds, agoText, valid };
})();
