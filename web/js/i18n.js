/* Chinese / English language layer.
 *
 * Loaded synchronously at the top of <head> on every page. In Chinese mode it
 * translates the page in place: the static HTML as it is parsed, and every node
 * a script inserts later (MutationObserver). Nothing else on the site needs to
 * know about languages.
 *
 * Where the Chinese comes from:
 *   - js/i18n-zh.js         interface strings (public, tracked), loaded only in zh mode
 *   - <file>.zh.json        a sidecar next to each data JSON (trips, manifests,
 *                           collections, blogs) holding the Chinese for the names and
 *                           text in that file. Written by i18n_zh.py at build time
 *                           from the private config/i18n_zh_data.json, so each sidecar
 *                           is gated exactly like the file it describes. Loaded by the
 *                           fetch() hook below whenever a page fetches the JSON.
 *   - window.I18N_PAGE      a per-page table a generator embedded in <head> (blog
 *                           titles, video titles) for text that never came from JSON.
 *
 * Dictionary keys are the exact trimmed text of one text node or attribute.
 * Keys may hold placeholders: {n} a number, {s} text that is itself translated,
 * {x} text kept as-is. Strings joined with " · " are translated part by part.
 *
 * Language choice: ?lang=zh|en (sticky, so links shared on Chinese platforms open
 * in Chinese), else the saved choice, else the browser language. The nav toggle
 * saves the choice and reloads.
 *
 * Elements (or their ancestors) with translate="no" or class "notranslate" are
 * left alone.
 */
(function () {
    'use strict';

    var STORE_KEY = 'site_lang';
    function saved() { try { return localStorage.getItem(STORE_KEY); } catch (e) { return null; } }
    function save(v) { try { localStorage.setItem(STORE_KEY, v); } catch (e) { /* private mode */ } }

    var q = null;
    try { q = new URLSearchParams(location.search).get('lang'); } catch (e) { /* old browser */ }
    var lang;
    if (q === 'zh' || q === 'en') { lang = q; save(q); }
    else lang = saved() || (/^zh\b/i.test(navigator.language || '') ? 'zh' : 'en');
    if (lang !== 'zh') lang = 'en';

    function setLang(next) {
        save(next);
        var u = new URL(location.href);
        if (u.searchParams.has('lang')) u.searchParams.set('lang', next);
        location.replace(u.toString());
    }

    var I18N = window.I18N = { lang: lang, setLang: setLang, t: function (s) { return s; } };

    // ---------------------------------------------------------------- toggle
    // English pages get a 中文 link, Chinese pages an EN link: in the top nav when
    // there is one, else the map sidebar, else a small fixed button.
    function addToggle() {
        if (document.querySelector('.lang-toggle')) return;
        var a = document.createElement('a');
        a.href = '#';
        a.className = 'lang-toggle';
        a.setAttribute('translate', 'no');
        a.setAttribute('lang', lang === 'zh' ? 'en' : 'zh-CN');
        a.textContent = lang === 'zh' ? 'EN' : '中文';
        a.title = lang === 'zh' ? 'English' : '切换到中文';
        a.addEventListener('click', function (e) { e.preventDefault(); setLang(lang === 'zh' ? 'en' : 'zh'); });
        var seeAll = document.getElementById('seeall-link');
        var sidebarNav = document.querySelector('.sidebar-nav');
        if (seeAll && seeAll.parentNode) seeAll.parentNode.insertBefore(a, seeAll);
        else if (document.querySelector('.topnav .nav-spacer')) {
            var wrap = document.createElement('div');
            wrap.className = 'nav-links';
            wrap.appendChild(a);
            document.querySelector('.topnav').appendChild(wrap);
        } else if (sidebarNav) { a.classList.add('lang-toggle--sidebar'); sidebarNav.appendChild(a); }
        else { a.classList.add('lang-toggle--float'); document.body.appendChild(a); }
    }
    var TOGGLE_CSS =
        '.lang-toggle{cursor:pointer}' +
        '.lang-toggle--sidebar{margin-left:auto;font-size:13px;padding:4px 8px;border:1px solid currentColor;border-radius:4px;opacity:.75;color:inherit;text-decoration:none}' +
        '.lang-toggle--sidebar:hover{opacity:1}' +
        '.lang-toggle--float{position:fixed;left:12px;bottom:12px;z-index:9999;padding:6px 10px;border-radius:6px;background:rgba(17,17,17,.85);color:#fff;font:13px/1 -apple-system,BlinkMacSystemFont,sans-serif;text-decoration:none}';

    function injectStyle(css) {
        var st = document.createElement('style');
        st.textContent = css;
        (document.head || document.documentElement).appendChild(st);
    }
    injectStyle(TOGGLE_CSS);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addToggle);
    else addToggle();

    if (lang !== 'zh') return;

    // ---------------------------------------------------------------- zh mode
    document.documentElement.lang = 'zh-CN';
    document.documentElement.classList.add('lang-zh');
    // The dictionary must be in place before the body is parsed, so the page never
    // shows English first. A parser-inserted same-origin script does that.
    document.write('<script src="/js/i18n-zh.js"><\/script>');

    // The site's fonts (Bebas Kai, Futura PT) are Latin only. Chinese falls back to
    // the device's own fonts; never a web font (Google Fonts is blocked in China).
    // Uppercase display styles get tighter letter spacing, which suits CJK better.
    var CJK = '"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC","Source Han Sans SC",sans-serif';
    injectStyle(
        ':root{--head:"Bebas Kai","Oswald",' + CJK + ';--body:"Futura PT","Futura",-apple-system,BlinkMacSystemFont,' + CJK + '}' +
        'html.lang-zh body{font-family:var(--body),' + CJK + '}' +
        'html.lang-zh .nav-links a,html.lang-zh .tile-title,html.lang-zh .tile-sub,html.lang-zh .stat-label,html.lang-zh .hero h1,' +
        'html.lang-zh .china-masthead h1,html.lang-zh .page-heading,html.lang-zh .day-eyebrow,html.lang-zh .day-chip,' +
        'html.lang-zh .day-km,html.lang-zh .blog-meta,html.lang-zh .section-head h2{letter-spacing:.04em}' +
        // China data shows the Chinese name beside the English one; once the English
        // is translated that would print the same name twice.
        'html.lang-zh .tile-zh,html.lang-zh .bridge-zh,html.lang-zh .ss-card__zh,html.lang-zh .zh-dup{display:none}'
    );

    var HAS_LETTER = /[A-Za-z]/;
    var exact = new Map();     // English → Chinese (interface + data + page)
    var patterns = [];         // keys with {n}/{s}/{x}
    var missCache = new Map();
    var uiLoaded = false;

    function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
    function addEntry(key, val) {
        if (typeof val !== 'string' || !val) return;
        if (/\{[nsx]\}/.test(key)) {   // "{s} →" stays useful when key === val
            var types = [];
            var src = key.split(/(\{[nsx]\})/).map(function (part) {
                var m = /^\{([nsx])\}$/.exec(part);
                if (!m) return escRe(part);
                types.push(m[1]);
                return m[1] === 'n' ? '([-+~]?\\d[\\d,.]*)' : '(.+?)';
            }).join('');
            try { patterns.push({ re: new RegExp('^' + src + '$'), types: types, val: val, len: key.length }); }
            catch (e) { /* bad key */ }
        } else if (key !== val) exact.set(key, val);
    }
    function addTable(tbl) {
        if (!tbl || typeof tbl !== 'object') return;
        Object.keys(tbl).forEach(function (k) { addEntry(k, tbl[k]); });
        // Longer, more specific patterns first.
        patterns.sort(function (a, b) { return b.len - a.len; });
        missCache.clear();
    }
    // The page table can be defined after the first lookups (it follows this
    // script in <head>), so it is checked on every call, not just once.
    function ensureUI() {
        if (!uiLoaded && window.I18N_ZH) { addTable(window.I18N_ZH); uiLoaded = true; }
        if (window.I18N_PAGE) { addTable(window.I18N_PAGE); window.I18N_PAGE = null; }
    }

    var MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12 };
    function mon(s) { return MONTHS[s.toLowerCase().slice(0, s.toLowerCase().startsWith('sept') ? 4 : 3)]; }
    var MON_RE = '(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)';
    var DATE_RULES = [
        // 7 Mar 2024 / Mar 7, 2024 / March 2024
        [new RegExp('^(\\d{1,2}) ' + MON_RE + ',? (\\d{4})$'), function (m) { return m[3] + '年' + mon(m[2]) + '月' + (+m[1]) + '日'; }],
        [new RegExp('^' + MON_RE + ' (\\d{1,2}),? (\\d{4})$'), function (m) { return m[3] + '年' + mon(m[1]) + '月' + (+m[2]) + '日'; }],
        [new RegExp('^' + MON_RE + ',? (\\d{4})$'), function (m) { return m[2] + '年' + mon(m[1]) + '月'; }],
        // 7–12 Mar 2024
        [new RegExp('^(\\d{1,2})\\s*[–-]\\s*(\\d{1,2}) ' + MON_RE + ',? (\\d{4})$'), function (m) { return m[4] + '年' + mon(m[3]) + '月' + (+m[1]) + '日至' + (+m[2]) + '日'; }],
        [new RegExp('^(\\d{1,2}) ' + MON_RE + '$'), function (m) { return mon(m[2]) + '月' + (+m[1]) + '日'; }],
        [new RegExp('^' + MON_RE + ' (\\d{1,2})$'), function (m) { return mon(m[1]) + '月' + (+m[2]) + '日'; }],
        [new RegExp('^' + MON_RE + '$'), function (m) { return mon(m[1]) + '月'; }],
    ];

    function fill(p, m) {
        var caps = { n: [], s: [], x: [] };
        p.types.forEach(function (t, i) { caps[t].push(m[i + 1]); });
        var failed = false;
        var out = p.val.replace(/\{([nsx])\}/g, function (_, t) {
            var v = caps[t].shift();
            if (v === undefined) return '';
            if (t === 's') {
                var r = tr(v);
                if (r == null && HAS_LETTER.test(v)) failed = true;
                return r == null ? v : r;
            }
            return v;
        });
        return failed ? null : out;
    }

    // → Chinese for one trimmed string, or null when there is none.
    function tr(text, depth) {
        if (!text) return null;
        var hit = exact.get(text);
        if (hit !== undefined) return hit;
        if (missCache.has(text)) return missCache.get(text);
        depth = depth || 0;
        var out = null;
        if (depth < 4) {
            // A pattern whose {s} part has no Chinese doesn't count: try the next.
            for (var i = 0; i < patterns.length && out == null; i++) {
                var m = patterns[i].re.exec(text);
                if (m) {
                    out = fill(patterns[i], m);
                    if (out === text) out = null;
                }
            }
            if (out == null) {
                for (var j = 0; j < DATE_RULES.length && out == null; j++) {
                    var dm = DATE_RULES[j][0].exec(text);
                    if (dm) out = DATE_RULES[j][1](dm);
                }
            }
            if (out == null) out = splitJoin(text, ' · ', depth) || splitJoin(text, ', ', depth) || splitRange(text, depth);
        }
        if (missCache.size > 20000) missCache.clear();
        missCache.set(text, out);
        return out;
    }
    // A list ("Seoul · Busan", "Amsterdam, Luxembourg") is translated only when
    // every wordy part is: a half-Chinese list reads worse than English.
    function splitJoin(text, sep, depth) {
        if (text.indexOf(sep) < 0) return null;
        var ok = true;
        var parts = text.split(sep).map(function (p) {
            var t = p.trim();
            if (!t || !HAS_LETTER.test(t) || /^[\d.,~]+\s*(m|km)$/.test(t)) return p;
            var r = tr(t, depth + 1);
            if (r == null) { ok = false; return p; }
            return p.replace(t, r);
        });
        return ok ? parts.join(sep === ', ' ? '、' : sep) : null;
    }
    // "Mar 2024 – Apr 2024", "7 Mar – 12 Mar 2024"
    function splitRange(text, depth) {
        var m = /^(.+?)\s+[–-]\s+(.+)$/.exec(text);
        if (!m || !/\d/.test(text)) return null;
        var a = tr(m[1], depth + 1), b = tr(m[2], depth + 1);
        if (a == null && b == null) return null;
        return (a == null ? m[1] : a) + ' 至 ' + (b == null ? m[2] : b);
    }
    I18N.t = function (s) {
        ensureUI();
        if (typeof s !== 'string') return s;
        var t = s.trim();
        var r = tr(t);
        return r == null ? s : s.replace(t, r);
    };
    I18N.add = addTable;

    // ---------------------------------------------------------------- DOM
    var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, CODE: 1, PRE: 1, svg: 0 };
    var ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];

    function skipped(el) {
        for (var e = el; e && e.nodeType === 1; e = e.parentNode) {
            if (SKIP_TAGS[e.tagName]) return true;
            if (e.getAttribute('translate') === 'no' || (e.classList && e.classList.contains('notranslate'))) return true;
            if (e.isContentEditable) return true;
        }
        return false;
    }
    function doText(node) {
        var v = node.nodeValue;
        if (!v || !HAS_LETTER.test(v)) return;
        var t = v.trim();
        var r = tr(t);
        if (r == null || r === t) return;
        if (node.parentNode && skipped(node.parentNode)) return;
        node.nodeValue = v.replace(t, function () { return r; });
    }
    // Search highlighting splits a name around <mark>s ("Puli <mark>Bridge</mark>"),
    // so no single piece matches. Translate the whole run; the highlight is lost.
    function doMarked(el) {
        var kids = el.childNodes, hasMark = false;
        for (var i = 0; i < kids.length; i++) {
            var k = kids[i];
            if (k.nodeType === 1 && k.tagName === 'MARK') hasMark = true;
            else if (k.nodeType !== 3) return;
        }
        if (!hasMark) return;
        var t = el.textContent.trim(), r = HAS_LETTER.test(t) ? tr(t) : null;
        if (r != null && r !== t) el.textContent = r;
    }
    function doAttrs(el) {
        for (var i = 0; i < ATTRS.length; i++) {
            var v = el.getAttribute(ATTRS[i]);
            if (v && HAS_LETTER.test(v)) {
                var r = tr(v.trim());
                if (r != null && r !== v) el.setAttribute(ATTRS[i], r);
            }
        }
        if (el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type) && el.value && HAS_LETTER.test(el.value)) {
            var rv = tr(el.value.trim());
            if (rv != null) el.value = rv;
        }
    }
    function walk(root) {
        ensureUI();
        if (!root) return;
        if (root.nodeType === 3) { doText(root); return; }
        if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
        if (root.nodeType === 1) {
            if (skipped(root)) return;
            doAttrs(root);
        }
        var w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
            acceptNode: function (n) {
                if (n.nodeType === 1) {
                    if (SKIP_TAGS[n.tagName] || n.getAttribute('translate') === 'no' ||
                        n.classList.contains('notranslate') || n.isContentEditable) return NodeFilter.FILTER_REJECT;
                    return NodeFilter.FILTER_ACCEPT;
                }
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        var n;
        while ((n = w.nextNode())) {
            if (n.nodeType === 3) {
                var v = n.nodeValue;
                if (v && HAS_LETTER.test(v)) {
                    var t = v.trim(), r = tr(t);
                    if (r != null && r !== t) n.nodeValue = v.replace(t, function () { return r; });
                }
            } else { doAttrs(n); if (n.firstChild) doMarked(n); }
        }
    }

    new MutationObserver(function (records) {
        ensureUI();
        for (var i = 0; i < records.length; i++) {
            var rec = records[i];
            if (rec.type === 'childList') {
                for (var j = 0; j < rec.addedNodes.length; j++) {
                    var n = rec.addedNodes[j];
                    if (n.nodeType === 3) doText(n);
                    else {
                        if (n.tagName === 'MARK' && rec.target.nodeType === 1) doMarked(rec.target);
                        walk(n);
                    }
                }
            } else if (rec.type === 'characterData') doText(rec.target);
            else if (rec.type === 'attributes' && rec.target.nodeType === 1 && !skipped(rec.target)) doAttrs(rec.target);
        }
    }).observe(document.documentElement, {
        childList: true, subtree: true, characterData: true,
        attributes: true, attributeFilter: ATTRS.concat(['value'])
    });

    // A full pass once new data arrives, for text drawn before its sidecar landed.
    var passTimer = null;
    function schedulePass() {
        if (passTimer) return;
        passTimer = setTimeout(function () { passTimer = null; walk(document.documentElement); }, 60);
    }
    document.addEventListener('DOMContentLoaded', function () { walk(document.documentElement); });

    // ---------------------------------------------------------------- data sidecars
    // When a page fetches /trips/…/x.json, /collections/x.json or /blogs/x.json,
    // fetch x.zh.json alongside and register it before the page sees the data.
    var SIDECAR_DIRS = /^\/(trips|collections|blogs)\//;
    var sidecars = {};
    var origFetch = window.fetch;
    function sidecarFor(url) {
        var u;
        try { u = new URL(url, location.href); } catch (e) { return null; }
        if (u.origin !== location.origin || !SIDECAR_DIRS.test(u.pathname)) return null;
        if (!/\.json$/.test(u.pathname) || /\.zh\.json$/.test(u.pathname)) return null;
        var path = u.pathname.replace(/\.json$/, '.zh.json');
        if (!sidecars[path]) {
            sidecars[path] = origFetch.call(window, path, { credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (tbl) { if (tbl) { addTable(tbl); schedulePass(); } })
                .catch(function () { /* missing, gated, or not JSON */ });
        }
        return sidecars[path];
    }
    if (origFetch) {
        window.fetch = function (input) {
            var p = origFetch.apply(this, arguments);
            var url = typeof input === 'string' ? input : (input && input.url) || String(input || '');
            var side = sidecarFor(url);
            if (!side) return p;
            return p.then(function (res) {
                ['json', 'text'].forEach(function (k) {
                    var orig = res[k].bind(res);
                    try {
                        res[k] = function () { return Promise.all([orig(), side]).then(function (a) { return a[0]; }); };
                    } catch (e) { /* frozen response */ }
                });
                return res;
            });
        };
    }

    // Public files that list locked items (the trip index) carry no Chinese for
    // them; unlocked visitors get it from a gated table instead.
    if (/(^|;\s*)all_access=[^;]+/.test(document.cookie)) sidecarFor('/collections/locked_names.json');

    // Native dialogs aren't DOM: translate line by line.
    ['alert', 'confirm', 'prompt'].forEach(function (k) {
        var orig = window[k];
        if (typeof orig !== 'function') return;
        window[k] = function (msg) {
            var args = Array.prototype.slice.call(arguments);
            if (typeof msg === 'string') args[0] = msg.split('\n').map(function (l) { return I18N.t(l); }).join('\n');
            return orig.apply(window, args);
        };
    });

    // Dates the scripts format themselves come out in Chinese.
    var LOCALE_FNS = [[Date.prototype, 'toLocaleDateString'], [Date.prototype, 'toLocaleString'], [Date.prototype, 'toLocaleTimeString']];
    LOCALE_FNS.forEach(function (pair) {
        var orig = pair[0][pair[1]];
        pair[0][pair[1]] = function (locale, opts) {
            var l = (!locale || /^en\b/i.test(String(locale))) ? 'zh-CN' : locale;
            return orig.call(this, l, opts);
        };
    });
    // Country names the scripts get from Intl.DisplayNames(['en']).
    if (window.Intl && Intl.DisplayNames) {
        var OrigDN = Intl.DisplayNames;
        var DN = function (locales, opts) {
            var l = (!locales || /^en\b/i.test(String([].concat(locales)[0]))) ? 'zh-CN' : locales;
            return new OrigDN(l, opts);
        };
        DN.prototype = OrigDN.prototype;
        DN.supportedLocalesOf = OrigDN.supportedLocalesOf;
        try { Intl.DisplayNames = DN; } catch (e) { /* read-only */ }
    }
    if (window.Intl && Intl.DateTimeFormat) {
        var OrigDTF = Intl.DateTimeFormat;
        var DTF = function (locale, opts) {
            var l = (!locale || /^en\b/i.test(String(locale))) ? 'zh-CN' : locale;
            return new OrigDTF(l, opts);
        };
        DTF.prototype = OrigDTF.prototype;
        DTF.supportedLocalesOf = OrigDTF.supportedLocalesOf;
        try { Intl.DateTimeFormat = DTF; } catch (e) { /* read-only */ }
    }
})();
