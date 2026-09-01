/**
 * Owner-only Instagram post drafts ("Posts").
 *
 * Included on every photo page but completely inert unless the posts_auth
 * cookie is present (set by /auth-posts, a separate owner-only password):
 * no DOM, no network, no CSS. The server re-checks the cookie on every
 * /api/posts call, so a forged cookie only ever produces 404s.
 *
 * Surfaces when unlocked:
 *  - Sitewide "Posts mode" pill (bottom-left) linking to /posts, with an ✕
 *    that exits posts mode by clearing the cookie.
 *  - "+ Post" button in every PhotoSwipe top bar (hooked via attachLightbox).
 *  - Select mode on photo grids (hooked via onGridRender from gallery.js).
 *  - The /posts manager page (initPostsPage, called by posts.html).
 *
 * All state lives server-side in one versioned JSON doc; every mutation is a
 * re-appliable function so a 409 (edited on another device) can merge cleanly.
 */
window.Posts = (function () {
    function unlocked() {
        return document.cookie.split(';').some(c => {
            const t = c.trim();
            return t.startsWith('posts_auth=') && t.length > 'posts_auth='.length;
        });
    }

    // ---------- Locked: no-op stubs + the /posts password prompt ----------

    function renderPasswordForm(root) {
        root.innerHTML = `
            <div class="posts-login">
                <h1>Posts</h1>
                <input type="password" id="posts-pw" placeholder="Password" autocomplete="current-password">
                <p class="posts-login-error" id="posts-pw-error"></p>
                <button type="button" id="posts-pw-submit">Enter</button>
            </div>`;
        const input = root.querySelector('#posts-pw');
        const error = root.querySelector('#posts-pw-error');
        async function submit() {
            const fd = new FormData();
            fd.append('password', input.value);
            try {
                const r = await fetch('/auth-posts', { method: 'POST', body: fd });
                if (r.ok) location.reload();
                else error.textContent = 'Incorrect password';
            } catch (err) {
                error.textContent = 'Error: ' + err.message;
            }
        }
        input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
        root.querySelector('#posts-pw-submit').addEventListener('click', submit);
        input.focus();
    }

    if (!unlocked()) {
        return {
            enabled: false,
            attachLightbox() {},
            onGridRender() {},
            initPostsPage() {
                const root = document.getElementById('posts-app');
                if (root) renderPasswordForm(root);
            }
        };
    }

    // ---------- Unlocked ----------

    const cssLink = document.createElement('link');
    cssLink.rel = 'stylesheet';
    cssLink.href = '/css/posts.css';
    document.head.appendChild(cssLink);

    // Sitewide "Posts mode" pill: shows on every page while the posts_auth
    // cookie is present, links to the /posts manager, and its ✕ exits posts
    // mode by clearing the cookie (it is not HttpOnly, so JS can).
    function exitPostsMode() {
        if (!confirm('Exit posts mode? You will need the posts password to get back in.')) return;
        document.cookie = 'posts_auth=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;'
            + (location.protocol === 'https:' ? ' Secure;' : '');
        location.reload();
    }

    // Current-post chip inside the pill: shows which post "+ Post" / "Add"
    // target, with name and count; clicking it opens the post switcher.
    // Empty (hidden by CSS) until the posts doc has loaded.
    let pillTarget = null;

    (function addModePill() {
        const pill = document.createElement('div');
        pill.id = 'posts-pill';
        const link = document.createElement('a');
        link.href = '/posts';
        link.textContent = 'Posts mode';
        link.title = 'Open the posts manager';
        pillTarget = document.createElement('button');
        pillTarget.type = 'button';
        pillTarget.className = 'posts-pill-target';
        pillTarget.addEventListener('click', () => openPicker([], null, { selectOnly: true }));
        const exit = document.createElement('button');
        exit.type = 'button';
        exit.title = 'Exit posts mode';
        exit.textContent = '✕';
        exit.addEventListener('click', exitPostsMode);
        pill.append(link, pillTarget, exit);
        document.body.appendChild(pill);
    })();

    // "Posts" and "People" entries in the nav "More" dropdown while posts mode is
    // on. The script loads at the end of body, so the nav is already in the DOM.
    // People rides the same posts_auth tier (it is owner-only for the same reason),
    // so it belongs to the same unlock rather than the See All gate.
    document.querySelectorAll('.nav-more-menu').forEach(menu => {
        [['/posts', 'Posts'], ['/people', 'People']].forEach(([href, label]) => {
            if (menu.querySelector(`a[href="${href}"]`)) return;
            const a = document.createElement('a');
            a.href = href;
            a.textContent = label;
            menu.appendChild(a);
        });
    });

    let doc = null;           // manual drafts { version, posts }; null on failure
    let autoDoc = null;       // auto-curated suggestions doc (/posts page only)
    let saveChain = Promise.resolve();

    const ready = fetch('/api/posts', { cache: 'no-store' })
        .then(r => (r.ok ? r.json() : null))
        .then(d => { doc = d; return !!d; })
        .catch(() => false);

    // The auto set (machine-generated by tools/auto_curate_posts.py) is a
    // second independent doc; only the /posts manager needs it, so it loads
    // lazily there.
    let autoReadyP = null;
    function autoReady() {
        if (!autoReadyP) {
            autoReadyP = fetch('/api/posts?set=auto', { cache: 'no-store' })
                .then(r => (r.ok ? r.json() : null))
                .then(d => { autoDoc = d; return !!d; })
                .catch(() => false);
        }
        return autoReadyP;
    }

    // Every mutation is a function of (posts) that can be re-applied to a fresh
    // server copy after a 409, so concurrent edits from another device merge
    // instead of clobbering. Mutations are serialized through saveChain.
    function mutateSet(set, fn) {
        const get = () => (set === 'auto' ? autoDoc : doc);
        const run = async () => {
            if (!get()) throw new Error('posts unavailable');
            fn(get().posts, get());
            const url = '/api/posts' + (set === 'auto' ? '?set=auto' : '');
            // Doc-level settings ride along on every save (the API keeps the
            // stored ones when the field is absent, so this only matters for
            // mutations that change them).
            const put = () => fetch(url, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ baseVersion: get().version, posts: get().posts,
                                       settings: get().settings || {} })
            });
            let r = await put();
            if (r.status === 409) {
                const fresh = await r.json();
                if (set === 'auto') autoDoc = fresh; else doc = fresh;
                fn(get().posts, get());
                r = await put();
            }
            if (!r.ok) throw new Error('save failed (' + r.status + ')');
            get().version = (await r.json()).version;
        };
        saveChain = saveChain
            .then(run)
            .then(() => { stampPostBadges(); return true; },
                  err => { toast('Save failed: ' + err.message); return false; });
        return saveChain;
    }
    function mutate(fn) { return mutateSet('main', fn); }

    // Location cards, rendered on pull by tools/map_card.py. null = no card.
    const MAP_STYLES = [null, 'route', 'pin', 'china'];
    const MAP_LABELS = {
        route: 'the drive through the frame',
        pin: 'photo pinned to the spot',
        china: 'photo pinned, plus a China locator',
    };
    const MAP_SHORT = { route: 'route', pin: 'pin', china: 'China' };

    // The IG accounts a post can be marked for (the card chip cycles these).
    const IG_ACCOUNTS = ['ryanmcl0', 'urbex'];

    // Per-platform carousel caps. On Xiaohongshu the phone items are part of
    // the carousel so they count toward the cap; on Instagram the phone
    // bucket is behind-the-scenes material and exempt.
    const CAPS = { ig: 20, xhs: 18 };
    const platformOf = post => (post.platform === 'xhs' ? 'xhs' : 'ig');
    const platLabel = post => (platformOf(post) === 'xhs' ? 'XHS' : 'IG');
    const capOf = post => CAPS[platformOf(post)];
    // The waitlist is a holding bay OUTSIDE the carousel — demoted photos stay
    // attached to the post but are not part of it, so they never count toward
    // the cap on either platform (unlike phone items, which do on XHS).
    const countOf = post => post.photos.length
        + (platformOf(post) === 'xhs' ? (post.phone || []).length : 0);
    const waitOf = post => post.waitlist || [];
    const WAITLIST_MAX = 40;

    // Orientation lock: a carousel must not mix portrait and landscape; the
    // first photo with a known aspect ratio sets the post's orientation.
    const orientOf = ar => (typeof ar === 'number' ? (ar >= 1 ? 'landscape' : 'portrait') : null);
    function postOrientation(post) {
        for (const ph of post.photos) {
            const o = orientOf(ph.ar);
            if (o) return o;
        }
        if (platformOf(post) === 'xhs') {
            for (const ph of (post.phone || [])) {
                const o = orientOf(ph.ar);
                if (o) return o;
            }
        }
        return null;
    }

    // How a post is named in warnings/toasts: account (or platform) included,
    // so "already in ..." tells you WHICH account's post has the photo.
    const postLabel = p => `"${p.name}"`
        + (p.account ? ` (@${p.account})` : platformOf(p) === 'xhs' ? ' (XHS)' : '');

    const keyOf = ref => `${ref.trip}::${ref.id || ref.file}`;
    const plural = n => `${n} photo${n === 1 ? '' : 's'}`;
    const newId = () => 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    // Human-facing post number (#N on the card, ./post.py pull N): next above
    // the highest ever used, so deleting a post never re-issues its number.
    const nextNum = posts => posts.reduce((m, p) => Math.max(m, p.num || 0), 0) + 1;

    function defaultName(posts) {
        let n = posts.length + 1;
        while (posts.some(p => p.name === `Post ${n}`)) n++;
        return `Post ${n}`;
    }

    // Strip to the fields the API accepts (grid/lightbox refs can carry extras).
    function cleanRef(ref) {
        const out = { trip: ref.trip, id: ref.id };
        if (typeof ref.ar === 'number') out.ar = ref.ar;
        return out;
    }

    // Phone items are {trip, id} photos or {trip, file} videos from the
    // local-only phone library.
    function cleanPhoneRef(ref) {
        const out = { trip: ref.trip };
        if (ref.id) out.id = ref.id; else out.file = ref.file;
        if (typeof ref.ar === 'number') out.ar = ref.ar;
        return out;
    }

    function addRefsToPost(post, refs) {
        const cap = capOf(post);
        const xhs = platformOf(post) === 'xhs';
        // A waitlisted photo already belongs to this post, so re-adding it is a
        // duplicate, not a new photo (promote it from the waitlist instead).
        const have = new Set(post.photos.map(keyOf).concat(waitOf(post).map(keyOf)));
        const havePhone = new Set((post.phone || []).map(keyOf));
        let added = 0, dupes = 0, hitCap = false, phoneAdded = 0;
        refs.forEach(ref => {
            // Phone-library items live in their own bucket: on IG they are
            // behind-the-scenes companions exempt from the cap, on XHS they
            // join the carousel and count toward it.
            if (ref.trip && ref.trip.startsWith('phone-')) {
                if (havePhone.has(keyOf(ref))) { dupes++; return; }
                if (xhs && countOf(post) >= cap) { hitCap = true; return; }
                if (!post.phone) post.phone = [];
                post.phone.push(cleanPhoneRef(ref));
                havePhone.add(keyOf(ref));
                unhistory(post, keyOf(ref));   // it's back in: drop it from History
                added++; phoneAdded++;
                return;
            }
            if (have.has(keyOf(ref))) { dupes++; return; }
            if (countOf(post) >= cap) { hitCap = true; return; }
            post.photos.push(cleanRef(ref));
            have.add(keyOf(ref));
            unhistory(post, keyOf(ref));   // it's back in: drop it from History
            added++;
        });
        return { added, dupes, hitCap, phoneAdded, count: countOf(post), cap };
    }

    // The "current post": the last post photos were added to (or picked in the
    // sheet). "+ Post" and "Add to post" go straight here without re-asking.
    const TARGET_KEY = 'posts_target_id';
    function targetPost() {
        if (!doc) return null;
        let id = null;
        try { id = localStorage.getItem(TARGET_KEY); } catch (e) { /* private mode */ }
        return (id && doc.posts.find(p => p.id === id)) || null;
    }
    function setTarget(id) {
        try { localStorage.setItem(TARGET_KEY, id); } catch (e) { /* private mode */ }
    }

    // ---------- Small UI primitives ----------

    let toastTimer;
    /** action = {label, fn}: renders a button in the toast (e.g. Undo) and
     *  keeps it up longer so there is time to press it. */
    function toast(msg, action) {
        let el = document.getElementById('posts-toast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'posts-toast';
            document.body.appendChild(el);
        }
        el.textContent = msg;
        el.classList.toggle('has-action', !!action);
        if (action) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'posts-toast-action';
            btn.textContent = action.label;
            btn.addEventListener('click', () => {
                el.classList.remove('visible');
                clearTimeout(toastTimer);
                action.fn();
            });
            el.appendChild(btn);
        }
        el.classList.add('visible');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('visible'), action ? 6000 : 2200);
    }

    /* ---------- removal history ----------
     *
     * Every photo taken out of a draft is remembered on the post itself
     * (post.history, newest first, synced with the doc), so the card's
     * History dropdown can offer it back later. Undo in the removal toast is
     * the fast path; adding the photo back through ANY add flow also clears
     * its history entry. The full ref is kept (ar/blur/map survive), so a
     * restored photo comes back with its marks intact.
     */
    const HISTORY_MAX = 100;
    function rememberRemoval(post, ref, isPhone) {
        if (!post.history) post.history = [];
        const k = keyOf(ref);
        const i = post.history.findIndex(h => keyOf(h) === k);
        if (i !== -1) post.history.splice(i, 1);
        const entry = { ...ref, removed: new Date().toISOString() };
        if (isPhone) entry.phone = true;
        post.history.unshift(entry);
        if (post.history.length > HISTORY_MAX) post.history.length = HISTORY_MAX;
    }
    function unhistory(post, key) {
        if (!post.history) return;
        const i = post.history.findIndex(h => keyOf(h) === key);
        if (i !== -1) post.history.splice(i, 1);
        if (!post.history.length) delete post.history;
    }

    /** Refs that already live in ANOTHER post (drafts, not auto suggestions):
     *  [{ref, post}] pairs, so the add flow can warn before double-posting. */
    function conflictsFor(refs, targetId) {
        const out = [];
        if (!doc) return out;
        refs.forEach(ref => {
            const k = keyOf(ref);
            doc.posts.forEach(p => {
                if (p.id === targetId) return;
                if (p.photos.some(ph => keyOf(ph) === k) ||
                    (p.phone || []).some(ph => keyOf(ph) === k)) {
                    out.push({ ref, post: p });
                } else if (waitOf(p).some(ph => keyOf(ph) === k)) {
                    out.push({ ref, post: p, waitlist: true });
                }
            });
        });
        return out;
    }

    /** Orientation gate: the first photo of a post locks its orientation;
     *  mixed-orientation adds get an add-anyway/cancel confirm. Phone refs
     *  only participate on XHS, where they join the carousel. */
    function orientationOk(postId, refs, platform) {
        const existing = doc && doc.posts.find(p => p.id === postId);
        const plat = existing ? platformOf(existing) : (platform === 'xhs' ? 'xhs' : 'ig');
        let base = existing ? postOrientation(existing) : null;
        let mismatched = 0, other = null;
        refs.forEach(ref => {
            if (ref.trip && ref.trip.startsWith('phone-') && plat !== 'xhs') return;
            const o = orientOf(ref.ar);
            if (!o) return;
            if (!base) { base = o; return; }
            if (o !== base) { mismatched++; other = o; }
        });
        if (!mismatched) return true;
        const msg = refs.length === 1
            ? `This post is ${base} (set by its first photo) but this photo is ${other}. Add anyway?`
            : `This post is ${base} (set by its first photo) but ${mismatched} selected photo${mismatched > 1 ? 's are' : ' is'} ${other}. Add anyway?`;
        return confirm(msg);
    }

    /** Add refs to a post (creating it if postId is new), remember it as the
     *  current post, and toast the outcome. Duplicates are skipped and named.
     *  Photos already used by a different post get an add-anyway/cancel
     *  confirm naming that post, so nothing is double-posted by accident. */
    function addToPost(postId, refs, onDone, platform) {
        if (!orientationOk(postId, refs, platform)) return;
        const conflicts = conflictsFor(refs, postId);
        if (conflicts.length) {
            const names = [...new Set(conflicts.map(c =>
                postLabel(c.post) + (c.waitlist ? ' (waitlist)' : '')))];
            const nDup = new Set(conflicts.map(c => keyOf(c.ref))).size;
            // Name the TARGET too: "already in X" alone reads as if X is where
            // the photo is about to go.
            const target = doc && doc.posts.find(p => p.id === postId);
            const addTo = target ? `Add to ${postLabel(target)} anyway?` : 'Add anyway?';
            const msg = refs.length === 1
                ? `This photo is already in ${names[0]}. ${addTo}`
                : `${nDup} of these photos are already in other posts (${names.join(', ')}). ${addTo}`;
            if (!confirm(msg)) return;
        }
        const result = {};
        mutate(posts => {
            let post = posts.find(p => p.id === postId);
            if (!post) {
                post = { id: postId, num: nextNum(posts), name: defaultName(posts),
                         created: new Date().toISOString(), photos: [] };
                if (platform === 'xhs') post.platform = 'xhs';
                posts.unshift(post);   // new drafts go to the top of the list
            }
            result.name = post.name;
            result.label = post.name + (post.account ? ` (@${post.account})` : '');
            Object.assign(result, addRefsToPost(post, refs));
        }).then(okSave => {
            if (!okSave) return;
            setTarget(postId);
            updateSelectUi();
            restampSelections();
            const capTxt = `${result.count}/${result.cap}`;
            if (result.phoneAdded && result.added === result.phoneAdded && result.count !== undefined && result.cap === 20) {
                toast(`Added ${result.phoneAdded > 1 ? result.phoneAdded + ' ' : ''}to ${result.label} (Phone section, no cap)`);
            } else if (result.hitCap && result.added) {
                toast(`Added ${result.added}, ${result.label} is now full (${capTxt})`);
            } else if (result.hitCap) {
                toast(`${result.label} is full (${capTxt})`);
                return;   // nothing was added, keep the selection
            } else if (!result.added) {
                toast(refs.length === 1
                    ? `Already in ${result.label} (${capTxt})`
                    : `All ${refs.length} already in ${result.label} (${capTxt})`);
                return;   // nothing was added, keep the selection
            } else if (result.dupes) {
                toast(`Added ${result.added} to ${result.label} (${capTxt}), ${result.dupes} already in it`);
            } else {
                toast(`Added to ${result.label} (${capTxt})`);
            }
            if (onDone) onDone();
        });
    }

    /** Bottom sheet listing posts. Default: adds refs to the chosen post.
     *  With opts.selectOnly it just switches the current post (no adding). */
    function openPicker(refs, onDone, opts) {
        const selectOnly = !!(opts && opts.selectOnly);
        ready.then(ok => {
            if (!ok) { toast('Posts unavailable, try re-entering the password at /posts'); return; }
            const overlay = document.createElement('div');
            overlay.className = 'posts-picker-overlay';
            const sheet = document.createElement('div');
            sheet.className = 'posts-picker';
            const title = document.createElement('h3');
            title.textContent = selectOnly ? 'Set current post'
                : refs.length === 1 ? 'Add photo to' : `Add ${refs.length} photos to`;
            sheet.appendChild(title);

            const close = () => overlay.remove();
            const choose = (postId, platform) => {
                close();
                if (!selectOnly) { addToPost(postId, refs, onDone, platform); return; }
                const existing = doc.posts.find(p => p.id === postId);
                if (existing) {
                    setTarget(postId);
                    updateSelectUi();
                    restampSelections();
                    toast(`Current post: ${existing.name}`);
                    if (onDone) onDone();
                    return;
                }
                const result = {};
                mutate(posts => {
                    if (!posts.find(p => p.id === postId)) {
                        const post = { id: postId, num: nextNum(posts), name: defaultName(posts),
                                       created: new Date().toISOString(), photos: [] };
                        if (platform === 'xhs') post.platform = 'xhs';
                        posts.unshift(post);   // new drafts go to the top of the list
                        result.name = post.name;
                    }
                }).then(okSave => {
                    if (!okSave) return;
                    setTarget(postId);
                    updateSelectUi();
                    restampSelections();
                    toast(`Current post: ${result.name}`);
                    if (onDone) onDone();
                });
            };

            const current = targetPost();
            const ordered = doc.posts.map((p, i) => [p, i])
                .sort((a, b) => (!!a[0].posted - !!b[0].posted) || (a[1] - b[1]))
                .map(([p]) => p);
            ordered.forEach(p => {
                const row = document.createElement('button');
                row.type = 'button';
                row.className = 'posts-picker-row';
                if (countOf(p) >= capOf(p)) row.classList.add('posts-picker-full');
                if (p.posted) row.classList.add('posts-picker-posted');
                if (current && current.id === p.id) row.classList.add('posts-picker-current');
                row.innerHTML = `<span class="posts-picker-name"></span><span class="posts-picker-count"></span>`;
                row.querySelector('.posts-picker-name').textContent = (p.num ? `#${p.num} ` : '') + p.name;
                row.querySelector('.posts-picker-count').textContent = (current && current.id === p.id ? 'current · ' : '')
                    + (p.posted ? 'posted · ' : '')
                    + (p.account ? `@${p.account} · ` : '')
                    + `${platLabel(p)} ${countOf(p)}/${capOf(p)}`;
                row.addEventListener('click', () => choose(p.id));
                sheet.appendChild(row);
            });

            [['ig', '+ New Instagram post'], ['xhs', '+ New Xiaohongshu post']].forEach(([plat, label]) => {
                const create = document.createElement('button');
                create.type = 'button';
                create.className = 'posts-picker-row posts-picker-new';
                create.textContent = label;
                create.addEventListener('click', () => choose(newId(), plat));
                sheet.appendChild(create);
            });

            const cancel = document.createElement('button');
            cancel.type = 'button';
            cancel.className = 'posts-picker-cancel';
            cancel.textContent = 'Cancel';
            cancel.addEventListener('click', close);
            sheet.appendChild(cancel);

            overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
            overlay.appendChild(sheet);
            document.body.appendChild(overlay);
        });
    }

    // ---------- Lightbox button ----------

    let currentGallery = null;
    let lightboxPostId = null;   // per-lightbox override of the current post (phone companion)

    function attachLightbox(gallery, pswpEl, opts) {
        currentGallery = gallery;
        // A lightbox opened from a post's phone-companion overlay adds to THAT
        // post by default, not the global current post.
        lightboxPostId = (opts && opts.defaultPostId) || null;
        gallery.listen('destroy', () => {
            if (currentGallery === gallery) { currentGallery = null; lightboxPostId = null; }
        });
        const bar = pswpEl && pswpEl.querySelector('.pswp__top-bar');
        if (!bar || bar.querySelector('.posts-add-btn')) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'pswp__button posts-add-btn';
        btn.title = 'Add to post';
        btn.textContent = '+ Post';
        btn.addEventListener('click', e => {
            e.stopPropagation();
            const item = currentGallery && currentGallery.currItem;
            if (!item || !item.ref) { toast('This photo cannot be added'); return; }
            ready.then(ok => {
                if (!ok) { toast('Posts unavailable, try re-entering the password at /posts'); return; }
                const override = lightboxPostId && doc.posts.find(p => p.id === lightboxPostId);
                const t = override || targetPost();
                if (t) addToPost(t.id, [item.ref]);   // straight into the current post
                else openPicker([item.ref]);          // no current post yet: ask once
            });
        });
        const pick = document.createElement('button');
        pick.type = 'button';
        pick.className = 'pswp__button posts-pick-btn';
        pick.title = 'Choose which post "+ Post" adds to';
        pick.textContent = '▾';
        pick.addEventListener('click', e => {
            e.stopPropagation();
            // An explicit pick beats the phone-companion default from then on.
            openPicker([], () => { lightboxPostId = null; }, { selectOnly: true });
        });
        const closeBtn = bar.querySelector('.pswp__button--close');
        bar.insertBefore(btn, closeBtn || null);
        bar.insertBefore(pick, closeBtn || null);

        // Same "already in a post" pill as the grids. The map has no thumbnails
        // of its own, so the viewer is the only place its photos can carry one.
        let pill = pswpEl.querySelector('.posts-badge--lightbox');
        if (!pill) {
            pill = document.createElement('span');
            pill.className = 'posts-badge posts-badge--lightbox';
            pswpEl.appendChild(pill);
        }
        const refresh = () => {
            const item = gallery.currItem;
            const info = item && item.ref
                ? badgeFor(postsByPhoto().get(keyOf(item.ref)))
                : null;
            pill.style.display = info ? '' : 'none';
            if (info) {
                pill.dataset.state = info.state;
                pill.textContent = info.text;
                pill.title = info.title;
                // Clear the caption when there is one, sit low when there isn't.
                const cap = pswpEl.querySelector('.pswp__caption__center');
                pill.classList.toggle('is-low', !(cap && cap.textContent.trim()));
            }
        };
        gallery.listen('afterChange', refresh);
        gallery.listen('close', () => { pill.style.display = 'none'; });
        ready.then(ok => { if (ok) refresh(); });
    }

    // ---------- Grid select mode ----------

    let selectMode = false;
    const selected = new Map();   // key -> ref
    const refByKey = new Map();   // key -> ref, for every grid photo seen on this page
    let fab = null, actionBar = null, actionCount = null, actionTarget = null, actionAdd = null;

    function ensureSelectUi() {
        if (fab) return;
        fab = document.createElement('button');
        fab.type = 'button';
        fab.id = 'posts-fab';
        fab.textContent = 'Select';
        fab.title = 'Select photos for a post';
        fab.addEventListener('click', () => setSelectMode(!selectMode));
        document.body.appendChild(fab);

        actionBar = document.createElement('div');
        actionBar.id = 'posts-actionbar';
        actionCount = document.createElement('span');
        actionCount.className = 'posts-action-count';
        actionTarget = document.createElement('button');
        actionTarget.type = 'button';
        actionTarget.className = 'posts-action-target';
        actionTarget.title = 'Change which post photos are added to';
        actionTarget.addEventListener('click', () => openPicker([], null, { selectOnly: true }));
        actionAdd = document.createElement('button');
        actionAdd.type = 'button';
        actionAdd.className = 'posts-action-add';
        actionAdd.textContent = 'Add';
        actionAdd.addEventListener('click', () => {
            if (!selected.size) { toast('Nothing selected'); return; }
            const refs = [...selected.values()];
            const t = targetPost();
            if (t) addToPost(t.id, refs, () => setSelectMode(false));  // straight into the current post
            else openPicker(refs, () => setSelectMode(false));         // no current post yet: ask once
        });
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'posts-action-cancel';
        cancel.textContent = 'Cancel';
        cancel.addEventListener('click', () => setSelectMode(false));
        actionBar.append(actionCount, actionTarget, actionAdd, cancel);
        document.body.appendChild(actionBar);
    }

    function setSelectMode(on) {
        selectMode = on;
        if (!on) selected.clear();
        document.body.classList.toggle('posts-selecting', on);
        updateSelectUi();
        restampSelections();
    }

    function updatePill() {
        if (!pillTarget || !doc) return;
        const t = targetPost();
        pillTarget.textContent = t
            ? `${t.name}${t.account ? ' @' + t.account : ''} · ${countOf(t)}/${capOf(t)}`
            : 'Choose post';
        pillTarget.title = t
            ? `Adding to "${t.name}" (${t.account ? '@' + t.account + ', ' : ''}${platLabel(t)}, ${plural(countOf(t))}). Click to change post`
            : 'Choose which post photos are added to';
    }

    function updateSelectUi() {
        updatePill();
        if (!fab) return;
        fab.classList.toggle('active', selectMode);
        fab.textContent = selectMode ? 'Selecting...' : 'Select';
        actionBar.classList.toggle('visible', selectMode);
        const t = targetPost();
        const cap = t ? capOf(t) : CAPS.ig;
        actionCount.textContent = selected.size > cap
            ? `${selected.size} selected (max ${cap} per post)`
            : `${selected.size} selected`;
        actionTarget.textContent = t
            ? `${t.account ? '@' + t.account : platLabel(t)} · ${t.name} · ${countOf(t)}/${cap}`
            : 'Choose post';
        actionAdd.textContent = t ? 'Add' : 'Add to post';
    }

    /* ---------- "already in a post" badges ----------
     *
     * While browsing in posts mode, any photo already committed to a post gets a
     * pill saying so, on every surface: grids (galleries, blogs, China, People)
     * and the full-screen viewer, which is the only place map photos are seen.
     * The point is recognising at a glance that a shot is already spoken for,
     * instead of finding out at the moment of adding it.
     */

    /** key -> [{name, posted, waitlist}], rebuilt from doc on demand (a few
     *  hundred refs). Waitlisted photos are included so browsing shows they
     *  are spoken for, marked so the badge can say which shelf they sit on. */
    function postsByPhoto() {
        const map = new Map();
        const add = (ref, entry) => {
            const key = keyOf(ref);
            if (!map.has(key)) map.set(key, []);
            map.get(key).push(entry);
        };
        for (const post of (doc && doc.posts) || []) {
            const label = post.name || (post.num ? `#${post.num}` : 'Untitled');
            for (const ref of post.photos || []) {
                add(ref, { name: label, posted: !!post.posted });
            }
            for (const ref of waitOf(post)) {
                add(ref, { name: label, posted: false, waitlist: true });
            }
        }
        return map;
    }

    /** "Posted · Norway bridge" / "Planned · Asia v1 +1" /
     *  "Planned · Asia v1 (waitlist)", plus a full title listing every post the
     *  photo sits in.
     *
     *  Precedence for the one-line label: posted (already out) beats being in a
     *  post's carousel, which beats merely sitting on a waitlist. A waitlisted
     *  photo is named with "(waitlist)" after the post, so browsing shows the
     *  shot is spoken for while making clear it is not in the carousel yet. */
    function badgeFor(entries) {
        if (!entries || !entries.length) return null;
        const posted = entries.filter(e => e.posted);
        const main = entries.filter(e => !e.posted && !e.waitlist);
        const shown = posted.length ? posted : (main.length ? main : entries);
        const waitOnly = !posted.length && !main.length;
        const rest = entries.length - shown.length;
        const extra = shown.length - 1 + rest;
        const named = e => e.name + (e.waitlist ? ' (waitlist)' : '');
        return {
            state: posted.length ? 'posted' : waitOnly ? 'waitlist' : 'planned',
            text: (posted.length ? 'Posted' : 'Planned') + ' · ' + named(shown[0])
                  + (extra > 0 ? ` +${extra}` : ''),
            title: entries.map(e => (e.posted ? 'Posted' : 'Planned') + ' · ' + named(e)).join('\n'),
        };
    }

    function stampPostBadges(root) {
        if (!doc) return;
        const map = postsByPhoto();
        (root || document).querySelectorAll('.photo-cell[data-trip]').forEach(cell => {
            const info = badgeFor(map.get(`${cell.dataset.trip}::${cell.dataset.id}`));
            let pill = cell.querySelector(':scope > .posts-badge');
            if (!info) {
                if (pill) pill.remove();
                cell.classList.remove('posts-has-badge');
                return;
            }
            if (!pill) {
                pill = document.createElement('span');
                pill.className = 'posts-badge';
                cell.appendChild(pill);
            }
            // Cells are only position:relative while selecting; the badge shows
            // whenever posts mode is unlocked, so it needs its own anchor.
            cell.classList.add('posts-has-badge');
            pill.dataset.state = info.state;
            pill.textContent = info.text;
            pill.title = info.title;
        });
    }

    function restampSelections() {
        const t = selectMode ? targetPost() : null;
        const inTarget = t ? new Set(t.photos.map(keyOf).concat(waitOf(t).map(keyOf))) : null;
        document.querySelectorAll('.photo-cell[data-trip]').forEach(cell => {
            const key = `${cell.dataset.trip}::${cell.dataset.id}`;
            cell.classList.toggle('posts-selected', selectMode && selected.has(key));
            cell.classList.toggle('posts-in-target', !!inTarget && inTarget.has(key));
        });
    }

    // Capture phase: runs before the cell's own click handler, so in select
    // mode a tap toggles selection instead of opening the lightbox.
    document.addEventListener('click', e => {
        if (!selectMode) return;
        const cell = e.target.closest && e.target.closest('.photo-cell');
        if (!cell || !cell.dataset.trip) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        const key = `${cell.dataset.trip}::${cell.dataset.id}`;
        if (selected.has(key)) {
            selected.delete(key);
        } else {
            const t = targetPost();
            if (t && t.photos.some(ph => keyOf(ph) === key)) {
                toast(`Already in ${t.name}${t.account ? ` (@${t.account})` : ''}`);
                return;
            }
            if (t && waitOf(t).some(ph => keyOf(ph) === key)) {
                toast(`Already on the waitlist of ${t.name}${t.account ? ` (@${t.account})` : ''}`);
                return;
            }
            const ref = refByKey.get(key) || { trip: cell.dataset.trip, id: cell.dataset.id };
            selected.set(key, ref);
        }
        cell.classList.toggle('posts-selected', selected.has(key));
        updateSelectUi();
    }, true);

    /** Called by gallery.js after every grid (re)layout. */
    function onGridRender(grid, order) {
        (order || []).forEach(ref => {
            if (ref && ref.trip && ref.id) refByKey.set(keyOf(ref), ref);
        });
        ready.then(ok => { if (ok) { ensureSelectUi(); stampPostBadges(grid); } });
        restampSelections();   // relayout rebuilds cells, so re-apply check marks
    }

    // ---------- /posts manager page ----------

    function initPostsPage() {
        const root = document.getElementById('posts-app');
        if (!root) return;
        Promise.all([ready, autoReady()]).then(([ok]) => {
            if (!ok) {
                // Cookie present but the API rejects it (stale after a password
                // change): fall back to the prompt instead of a dead page.
                renderPasswordForm(root);
                return;
            }
            // One-time backfill: give pre-numbering drafts their #N (in stored
            // order). Renders even if the save fails — numbers are then local.
            if (doc.posts.some(p => !p.num)) {
                mutate(posts => posts.forEach(p => { if (!p.num) p.num = nextNum(posts); }))
                    .then(() => renderManager(root));
                return;
            }
            renderManager(root);
        });
    }

    // Posts whose Phone <details> is open: renderManager rebuilds every card
    // on each edit, so without this a ⇩/✕ inside the section would collapse it.
    const phoneOpen = new Set();

    // Which manager tab is showing: 'ig' / 'xhs' (hand-made drafts by
    // platform) or 'auto' (suggestions from tools/auto_curate_posts.py).
    let activeTab = 'ig';
    try {
        activeTab = localStorage.getItem('posts_tab') || 'ig';
        if (activeTab === 'main') activeTab = 'ig';   // pre-platform value
    } catch (e) { /* private mode */ }

    function renderManager(root) {
        updatePill();
        root.innerHTML = '';
        const head = document.createElement('div');
        head.className = 'posts-head';
        const h1 = document.createElement('h1');
        h1.textContent = 'Posts';
        head.appendChild(h1);
        if (activeTab !== 'auto') {
            const plat = activeTab;
            const newBtn = document.createElement('button');
            newBtn.type = 'button';
            newBtn.className = 'posts-new-btn';
            newBtn.textContent = plat === 'xhs' ? '+ New XHS post' : '+ New IG post';
            newBtn.addEventListener('click', () => {
                // A freshly created post becomes the current one, so "+ Post"
                // anywhere on the site adds straight to it.
                const id = newId();
                mutate(posts => {
                    if (posts.find(p => p.id === id)) return;
                    const post = { id, num: nextNum(posts), name: defaultName(posts),
                                   created: new Date().toISOString(), photos: [] };
                    if (plat === 'xhs') post.platform = 'xhs';
                    posts.unshift(post);   // new drafts go to the top of the list
                }).then(okSave => {
                    if (okSave) setTarget(id);
                    renderManager(root);
                });
            });
            head.appendChild(newBtn);
        }
        root.appendChild(head);
        renderPostedBuildings(root);

        const tabs = document.createElement('div');
        tabs.className = 'posts-tabs';
        const igCount = doc.posts.filter(p => platformOf(p) === 'ig').length;
        const xhsCount = doc.posts.filter(p => platformOf(p) === 'xhs').length;
        const autoCount = autoDoc ? autoDoc.posts.length : 0;
        [['ig', `Instagram (${igCount})`],
         ['xhs', `Xiaohongshu (${xhsCount})`],
         ['auto', `Auto curated (${autoCount})`]].forEach(([id, label]) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'posts-tab' + (activeTab === id ? ' active' : '');
            b.textContent = label;
            b.addEventListener('click', () => {
                activeTab = id;
                try { localStorage.setItem('posts_tab', id); } catch (e) { /* private mode */ }
                renderManager(root);
            });
            tabs.appendChild(b);
        });
        root.appendChild(tabs);

        if (activeTab === 'auto') {
            renderAutoList(root);
            return;
        }

        // Highlights feature controls. The master switch is the whole feature:
        // while off, the site has no Highlights page at all (middleware 404s it
        // and strips its nav links; the API 404s). Only once it is on do the
        // per-platform switch (absent = on) and the per-post "No Highlights"
        // ticks apply, so they only render then.
        {
            const st = doc.settings || {};
            const controls = document.createElement('div');
            controls.className = 'posts-hl-controls';
            const check = (label, checked, title, onChange) => {
                const row = document.createElement('label');
                row.className = 'posts-hl-platform';
                if (title) row.title = title;
                const sw = document.createElement('input');
                sw.type = 'checkbox';
                sw.checked = checked;
                sw.addEventListener('change', () => onChange(sw.checked));
                row.append(sw, document.createTextNode(' ' + label));
                return row;
            };
            const setSetting = (key, val) => mutate((posts, d) => {
                const s = d.settings || (d.settings = {});
                if (val === undefined) delete s[key]; else s[key] = val;
                if (!Object.keys(s).length) delete d.settings;
            }).then(() => renderManager(root));
            controls.appendChild(check('Enable Highlights page',
                st.highlightsEnabled === true,
                'Public page of coverflow galleries built from these posts. ' +
                'Off: the page does not exist anywhere on the site. ' +
                'Flips may take up to 5 minutes to reach every visitor.',
                on => setSetting('highlightsEnabled', on ? true : undefined)));
            if (st.highlightsEnabled === true) {
                const key = activeTab === 'xhs' ? 'highlightsXhs' : 'highlightsIg';
                controls.appendChild(check(
                    `Show ${activeTab === 'xhs' ? 'Xiaohongshu' : 'Instagram'} posts on it`,
                    st[key] !== false, '',
                    on => setSetting(key, on ? undefined : false)));
            }
            root.appendChild(controls);
        }

        // Unposted first (what still needs work), posted ones collapsed below;
        // the stored order is untouched, this is display order only.
        const list = doc.posts.filter(p => platformOf(p) === activeTab)
            .map((p, i) => [p, i])
            .sort((a, b) => (!!a[0].posted - !!b[0].posted) || (a[1] - b[1]))
            .map(([p]) => p);
        if (!list.length) {
            const empty = document.createElement('p');
            empty.className = 'posts-empty';
            empty.textContent = activeTab === 'xhs'
                ? 'No Xiaohongshu drafts yet. Create one here, or pick "+ New Xiaohongshu post" when adding photos from the site.'
                : 'No post drafts yet. Browse anywhere on the site and use "+ Post" in the photo viewer, or "Select" on any photo grid.';
            root.appendChild(empty);
            return;
        }

        list.forEach(post => root.appendChild(renderCard(root, post, 'main')));
    }

    /* ---------- Posted Buildings banner ----------
     *
     * Which climbed buildings have already gone out, so a building doesn't get
     * posted twice. The building a photo belongs to is only known to the Urbex
     * collection (build_collections.facet_roofs does the roster matching), so
     * this reads collections/rooftopping.json and inverts it into
     * "<trip>|<id>" → building name. That file is behind the all-access gate;
     * if it isn't unlocked the banner just stays hidden rather than erroring.
     */
    let roofMapP = null;
    function roofMap() {
        if (!roofMapP) {
            roofMapP = fetch('collections/rooftopping.json', { cache: 'no-store' })
                .then(r => (r.ok ? r.json() : null))
                .then(doc => {
                    const map = new Map();
                    if (!doc) return map;
                    for (const tile of doc.tiles || []) {
                        for (const sec of tile.sections || []) {
                            for (const sub of sec.subtiles || []) {
                                for (const ph of sub.photos || []) {
                                    map.set(`${ph.trip}|${ph.id}`, sub.title);
                                }
                            }
                        }
                    }
                    return map;
                })
                .catch(() => new Map());
        }
        return roofMapP;
    }

    function renderPostedBuildings(root) {
        roofMap().then(map => {
            if (!map.size) return;   // locked, or no roofs data
            // building → the posted drafts it appears in (any platform: this is
            // "have I shared this building", not a per-account view).
            const byBuilding = new Map();
            for (const p of doc.posts) {
                if (!p.posted) continue;
                for (const ref of p.photos || []) {
                    const b = map.get(`${ref.trip}|${ref.id}`);
                    if (!b) continue;
                    if (!byBuilding.has(b)) byBuilding.set(b, new Set());
                    byBuilding.get(b).add(p.name || `#${p.num}`);
                }
            }
            const existing = document.getElementById('posts-buildings');
            if (existing) existing.remove();
            if (!byBuilding.size) return;

            const names = [...byBuilding.keys()].sort((a, b) => a.localeCompare(b));
            const box = document.createElement('details');
            box.id = 'posts-buildings';
            box.className = 'posts-buildings';
            const sum = document.createElement('summary');
            sum.textContent = `Posted Buildings (${names.length})`;
            box.appendChild(sum);
            const ul = document.createElement('ul');
            for (const n of names) {
                const li = document.createElement('li');
                const posts = [...byBuilding.get(n)];
                li.innerHTML = `<span class="pb-name">${n}</span>`
                    + `<span class="pb-posts">${posts.join(', ')}</span>`;
                ul.appendChild(li);
            }
            box.appendChild(ul);
            // Under the title, above the tabs.
            const head = root.querySelector('.posts-head');
            if (head && head.nextSibling) root.insertBefore(box, head.nextSibling);
            else root.appendChild(box);
        });
    }

    const THEME_LABELS = {
        custom: 'Custom curated', story: 'Behind the scenes', province: 'By province',
        place: 'Places', industrial: 'Industry', nature: 'Nature', wildlife: 'Wildlife'
    };

    // Local-only curation server (tools/curate_server.py): free-text queries
    // become curated posts. Probed once; the box only appears when it runs.
    let curateBaseP = null;
    function curateBase() {
        if (!curateBaseP) {
            const base = `http://${location.hostname}:8799`;
            let signal;
            try { signal = AbortSignal.timeout(1500); } catch (e) { signal = undefined; }
            curateBaseP = fetch(base + '/health', { signal })
                .then(r => (r.ok ? base : null))
                .catch(() => null);
        }
        return curateBaseP;
    }

    function renderCurateBar(root, container) {
        curateBase().then(base => {
            if (!base) return;
            const bar = document.createElement('div');
            bar.className = 'posts-curate-bar';
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'posts-curate-input';
            input.placeholder = 'Curate by prompt, e.g. "truck stops in china" or "central asia landscapes"';
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'posts-copy-btn';
            btn.textContent = 'Curate';
            async function run() {
                const q = input.value.trim();
                if (!q) return;
                btn.disabled = true;
                btn.textContent = 'Curating...';
                try {
                    const r = await fetch(base + '/curate?q=' + encodeURIComponent(q));
                    const data = await r.json();
                    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
                    if (!data.posts.length) {
                        toast(`No good matches for "${q}"`);
                        return;
                    }
                    await mutateSet('auto', posts => {
                        const ids = new Set(data.posts.map(p => p.id));
                        for (let i = posts.length - 1; i >= 0; i--) {
                            if (ids.has(posts[i].id)) posts.splice(i, 1);
                        }
                        posts.unshift(...data.posts);
                    });
                    toast(`Curated ${data.posts.length} post${data.posts.length > 1 ? 's' : ''} for "${q}"`);
                    renderManager(root);
                } catch (e) {
                    toast('Curate failed: ' + e.message);
                } finally {
                    btn.disabled = false;
                    btn.textContent = 'Curate';
                }
            }
            input.addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
            btn.addEventListener('click', run);
            bar.append(input, btn);
            container.appendChild(bar);
        });
    }

    function renderAutoList(root) {
        const curateSlot = document.createElement('div');
        root.appendChild(curateSlot);
        renderCurateBar(root, curateSlot);
        if (!autoDoc) {
            const p = document.createElement('p');
            p.className = 'posts-empty';
            p.textContent = 'Auto-curated posts are unavailable (API error).';
            root.appendChild(p);
            return;
        }
        if (!autoDoc.posts.length) {
            const p = document.createElement('p');
            p.className = 'posts-empty';
            p.textContent = 'No auto-curated suggestions yet. Run tools/auto_curate_posts.py --push to generate them.';
            root.appendChild(p);
            return;
        }
        let lastTheme = null;
        autoDoc.posts.forEach(post => {
            const theme = post.theme || 'other';
            if (theme !== lastTheme) {
                lastTheme = theme;
                const h = document.createElement('h2');
                h.className = 'posts-theme-head';
                h.textContent = THEME_LABELS[theme] || theme;
                root.appendChild(h);
            }
            root.appendChild(renderCard(root, post, 'auto'));
        });
    }

    // ---------- fuzzy song matching ----------
    // "Already used this song?" check: case, punctuation and word order are
    // ignored (artist/title can be swapped), small typos tolerated (edit
    // distance scales with token length), and while typing the last token
    // matches as a prefix so suggestions appear early.

    const normSong = s => s.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-z0-9\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]+/g, ' ').trim();

    function editDist(a, b) {
        if (a === b) return 0;
        let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
        for (let i = 1; i <= a.length; i++) {
            const cur = [i];
            for (let j = 1; j <= b.length; j++) {
                cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1,
                    prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
            }
            prev = cur;
        }
        return prev[b.length];
    }

    /** 0..1: how much of `typed` is found in `existing`, token-wise. */
    function songSimilarity(typed, existing, allowPrefix) {
        const a = normSong(typed).split(' ').filter(Boolean);
        const b = normSong(existing).split(' ').filter(Boolean);
        if (!a.length || !b.length) return 0;
        const taken = new Set();
        let hit = 0;
        a.forEach((t, i) => {
            const last = allowPrefix && i === a.length - 1;
            const maxD = t.length >= 6 ? 2 : t.length >= 4 ? 1 : 0;
            for (let j = 0; j < b.length; j++) {
                if (taken.has(j)) continue;
                const c = b[j];
                if (c === t || (last && t.length >= 2 && c.startsWith(t)) ||
                    (maxD && Math.abs(c.length - t.length) <= maxD && editDist(t, c) <= maxD)) {
                    taken.add(j);
                    hit++;
                    break;
                }
            }
        });
        return hit / a.length;
    }

    // Per-session expand/collapse overrides: posted drafts default collapsed,
    // unposted ones default expanded; these hold the ids the user has flipped.
    const expandedPosted = new Set();
    const collapsedUnposted = new Set();
    const cardOpen = post => (post.posted ? expandedPosted.has(post.id) : !collapsedUnposted.has(post.id));

    /** Move a draft one slot up/down among the cards around it. Display order
     *  within a same-platform, same posted-state group IS stored order, so the
     *  move swaps with the neighbouring card the user actually sees. */
    function movePost(root, post, dir) {
        mutate(posts => {
            const group = posts.filter(p => platformOf(p) === platformOf(post) && !!p.posted === !!post.posted);
            const gi = group.findIndex(p => p.id === post.id);
            const neighbor = gi === -1 ? null : group[gi + dir];
            if (!neighbor) return;
            const from = posts.findIndex(p => p.id === post.id);
            const [moved] = posts.splice(from, 1);
            const to = posts.findIndex(p => p.id === neighbor.id) + (dir > 0 ? 1 : 0);
            posts.splice(to, 0, moved);
        }).then(() => renderManager(root));
    }

    /** Drop a dragged card onto another: the moved draft takes the target's
     *  place (before it when dragging up, after it when dragging down). Only
     *  within the same platform + posted-state group, where display order is
     *  stored order — a cross-group drop would not visibly change anything. */
    function movePostTo(root, fromId, target) {
        mutate(posts => {
            const moved = posts.find(p => p.id === fromId);
            const tp = posts.find(p => p.id === target.id);
            if (!moved || !tp || moved === tp) return;
            if (platformOf(moved) !== platformOf(tp) || !!moved.posted !== !!tp.posted) return;
            const from = posts.indexOf(moved);
            const wasBefore = from < posts.indexOf(tp);
            posts.splice(from, 1);
            posts.splice(posts.indexOf(tp) + (wasBefore ? 1 : 0), 0, moved);
        }).then(() => renderManager(root));
    }

    function renderCard(root, post, set) {
        set = set || 'main';
        const M = fn => mutateSet(set, fn);
        const card = document.createElement('section');
        card.className = 'posts-card';

        const bar = document.createElement('div');
        bar.className = 'posts-card-bar';
        if (set === 'main' && post.num) {
            const num = document.createElement('span');
            num.className = 'posts-num';
            num.textContent = `#${post.num}`;
            num.title = `./post.py pull ${post.num}`;
            bar.appendChild(num);
        }
        const name = document.createElement('input');
        name.className = 'posts-name';
        name.value = post.name;
        name.addEventListener('change', () => {
            const v = name.value.trim() || post.name;
            name.value = v;
            M(posts => {
                const p = posts.find(x => x.id === post.id);
                if (p) p.name = v;
            }).then(() => updatePill());
        });
        const count = document.createElement('span');
        count.className = 'posts-count';
        const phonePhotos = (post.phone || []).filter(r => r.id).length;
        const phoneVideos = (post.phone || []).length - phonePhotos;
        count.textContent = (set === 'auto'
            ? `${post.photos.length} photos`
            : `${platLabel(post)} · ${countOf(post)}/${capOf(post)} photos`)
            + (phonePhotos ? ` · ${phonePhotos} phone` : '')
            + (phoneVideos ? ` · ${phoneVideos} video${phoneVideos === 1 ? '' : 's'}` : '')
            + (set === 'main' && waitOf(post).length ? ` · ${waitOf(post).length} waitlisted` : '');
        if (countOf(post) >= capOf(post)) count.classList.add('posts-count-full');
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'posts-delete';
        del.textContent = set === 'auto' ? 'Dismiss' : 'Delete';
        del.addEventListener('click', () => {
            const verb = set === 'auto' ? 'Dismiss' : 'Delete';
            if (!confirm(`${verb} "${post.name}"? The photos themselves are not affected.`)) return;
            M(posts => {
                const i = posts.findIndex(x => x.id === post.id);
                if (i !== -1) posts.splice(i, 1);
            }).then(() => renderManager(root));
        });
        bar.append(name, count);
        if (set === 'auto') {
            // Promote a suggestion into the hand-made drafts (which is what
            // ./post.py pull pulls); the suggestion itself stays until
            // dismissed. XHS copies obey the combined 18 cap: carousel photos
            // first, phone items fill any remaining slots.
            [['ig', 'Copy → IG'], ['xhs', 'Copy → XHS']].forEach(([plat, label]) => {
                const copy = document.createElement('button');
                copy.type = 'button';
                copy.className = 'posts-copy-btn';
                copy.textContent = label;
                copy.addEventListener('click', () => {
                    const dup = {
                        id: newId(), name: post.name, created: new Date().toISOString(),
                        photos: post.photos.map(cleanRef)
                    };
                    let phone = (post.phone || []).map(cleanPhoneRef);
                    if (plat === 'xhs') {
                        dup.platform = 'xhs';
                        dup.photos = dup.photos.slice(0, CAPS.xhs);
                        phone = phone.slice(0, Math.max(0, CAPS.xhs - dup.photos.length));
                    }
                    if (phone.length) dup.phone = phone;
                    const trimmed = post.photos.length + (post.phone || []).length
                        - dup.photos.length - (dup.phone || []).length;
                    mutate(posts => {
                        if (!posts.some(p => p.name === dup.name && platformOf(p) === plat)) {
                            dup.num = nextNum(posts);
                            posts.push(dup);
                        }
                    }).then(okSave => {
                        if (okSave) {
                            toast(`Copied "${post.name}" to ${plat === 'xhs' ? 'Xiaohongshu' : 'Instagram'}`
                                + (trimmed > 0 ? ` (${trimmed} trimmed for the 18 cap)` : ''));
                        }
                        renderManager(root);
                    });
                });
                bar.appendChild(copy);
            });
        }
        if (set === 'main') {
            // Drag the card by its handle to reorder. The card only becomes
            // draggable while the handle is pressed, so text in the name and
            // caption fields stays selectable; a distinct data type keeps
            // card drops apart from the thumbs' own drag-to-reorder.
            const handle = document.createElement('span');
            handle.className = 'posts-drag-handle';
            handle.textContent = '⠿';
            handle.title = 'Drag to reorder';
            handle.addEventListener('mousedown', () => { card.draggable = true; });
            bar.insertBefore(handle, bar.firstChild);
            card.addEventListener('dragstart', e => {
                if (!card.draggable) return;   // a thumb drag bubbling up
                e.dataTransfer.setData('text/posts-card', post.id);
                e.dataTransfer.effectAllowed = 'move';
            });
            card.addEventListener('dragend', () => { card.draggable = false; });
            card.addEventListener('dragover', e => {
                if (!e.dataTransfer.types.includes('text/posts-card')) return;
                e.preventDefault();
                card.classList.add('drag-over');
            });
            card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
            card.addEventListener('drop', e => {
                if (!e.dataTransfer.types.includes('text/posts-card')) return;
                e.preventDefault();
                card.classList.remove('drag-over');
                movePostTo(root, e.dataTransfer.getData('text/posts-card'), post);
            });

            // published tick: posted drafts collapse to just their bar
            const postedLbl = document.createElement('label');
            postedLbl.className = 'posts-posted-toggle';
            const tick = document.createElement('input');
            tick.type = 'checkbox';
            tick.checked = !!post.posted;
            tick.addEventListener('change', () => {
                const on = tick.checked;
                // reset to the new default state (posted closed, unposted open)
                expandedPosted.delete(post.id);
                collapsedUnposted.delete(post.id);
                M(posts => {
                    const p = posts.find(x => x.id === post.id);
                    if (!p) return;
                    if (on) p.posted = true; else delete p.posted;
                }).then(() => renderManager(root));
            });
            // Which IG account this post is for: a chip cycling through
            // unset → @ryanmcl0 → @urbex → unset. IG drafts only.
            if (platformOf(post) === 'ig') {
                const idx = IG_ACCOUNTS.indexOf(post.account);
                const acct = document.createElement('button');
                acct.type = 'button';
                acct.className = 'posts-account' + (idx !== -1 ? ` posts-account-${idx}` : '');
                acct.textContent = post.account ? '@' + post.account : '@ account?';
                acct.title = 'Which IG account this post is for — click to change';
                acct.addEventListener('click', () => {
                    M(posts => {
                        const p = posts.find(x => x.id === post.id);
                        if (!p) return;
                        const next = IG_ACCOUNTS[IG_ACCOUNTS.indexOf(p.account) + 1];
                        if (next) p.account = next;
                        else delete p.account;   // past the last handle: back to unset
                    }).then(() => renderManager(root));
                });
                bar.appendChild(acct);
            }

            // Duplicate this draft onto the other platform, keeping order,
            // blur/map marks, caption and song. XHS copies obey the combined
            // 18 cap: carousel photos first, phone items fill what remains.
            const otherPlat = platformOf(post) === 'xhs' ? 'ig' : 'xhs';
            const dupBtn = document.createElement('button');
            dupBtn.type = 'button';
            dupBtn.className = 'posts-copy-btn';
            dupBtn.textContent = otherPlat === 'xhs' ? 'Copy → XHS' : 'Copy → IG';
            dupBtn.title = otherPlat === 'xhs'
                ? 'Duplicate this draft as a Xiaohongshu post'
                : 'Duplicate this draft as an Instagram post';
            dupBtn.addEventListener('click', () => {
                const dup = {
                    id: newId(), name: post.name, created: new Date().toISOString(),
                    photos: post.photos.map(ph => ({ ...ph }))
                };
                if (post.caption) dup.caption = post.caption;
                if (post.song) dup.song = post.song;
                if (waitOf(post).length) dup.waitlist = waitOf(post).map(ph => ({ ...ph }));
                let phone = (post.phone || []).map(ph => ({ ...ph }));
                if (otherPlat === 'xhs') {
                    dup.platform = 'xhs';
                    dup.photos = dup.photos.slice(0, CAPS.xhs);
                    phone = phone.slice(0, Math.max(0, CAPS.xhs - dup.photos.length));
                }
                if (phone.length) dup.phone = phone;
                const trimmed = post.photos.length + (post.phone || []).length
                    - dup.photos.length - (dup.phone || []).length;
                mutate(posts => {
                    if (!posts.some(p => p.id === dup.id)) {
                        dup.num = nextNum(posts);
                        posts.unshift(dup);
                    }
                }).then(okSave => {
                    if (okSave) {
                        toast(`Copied "${post.name}" to ${otherPlat === 'xhs' ? 'Xiaohongshu' : 'Instagram'}`
                            + (trimmed > 0 ? ` (${trimmed} trimmed for the 18 cap)` : ''));
                    }
                    renderManager(root);
                });
            });
            bar.appendChild(dupBtn);
            postedLbl.append(tick, document.createTextNode(' Posted'));
            bar.appendChild(postedLbl);
            // Highlights opt-out: ticked drafts never feed the public
            // Highlights page (/api/highlights reads the flag server-side).
            // Only shown while the feature's master switch is on.
            if ((doc.settings || {}).highlightsEnabled === true) {
                const hlLbl = document.createElement('label');
                hlLbl.className = 'posts-posted-toggle posts-nohl-toggle';
                hlLbl.title = 'Exclude this post from the Highlights page';
                const hlTick = document.createElement('input');
                hlTick.type = 'checkbox';
                hlTick.checked = !!post.noHighlight;
                hlTick.addEventListener('change', () => {
                    const on = hlTick.checked;
                    M(posts => {
                        const p = posts.find(x => x.id === post.id);
                        if (!p) return;
                        if (on) p.noHighlight = true; else delete p.noHighlight;
                    }).then(() => renderManager(root));
                });
                hlLbl.append(hlTick, document.createTextNode(' No Highlights'));
                bar.appendChild(hlLbl);
            }
            // Reorder arrows: move the card among its visible neighbours.
            const group = doc.posts.filter(p => platformOf(p) === platformOf(post) && !!p.posted === !!post.posted);
            const gi = group.findIndex(p => p.id === post.id);
            [['▲', -1, 'Move up'], ['▼', 1, 'Move down']].forEach(([sym, dir, label]) => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'posts-expand posts-move';
                b.textContent = sym;
                b.title = label;
                b.disabled = gi === -1 || (dir === -1 ? gi === 0 : gi === group.length - 1);
                b.addEventListener('click', () => movePost(root, post, dir));
                bar.appendChild(b);
            });
            const expand = document.createElement('button');
            expand.type = 'button';
            expand.className = 'posts-expand';
            const isOpen = cardOpen(post);
            expand.textContent = isOpen ? '▾' : '▸';
            expand.title = isOpen ? 'Collapse' : 'Show photos';
            expand.addEventListener('click', () => {
                if (post.posted) {
                    if (isOpen) expandedPosted.delete(post.id); else expandedPosted.add(post.id);
                } else {
                    if (isOpen) collapsedUnposted.add(post.id); else collapsedUnposted.delete(post.id);
                }
                renderManager(root);
            });
            bar.appendChild(expand);
        }
        bar.appendChild(del);
        // Local-only phone-library companion: shows phone photos taken around
        // the same time as this post's camera picks. No-op when the phone
        // library (or the script) is absent.
        if (window.PhoneCompanion) PhoneCompanion.decorateCard(bar, post);
        card.appendChild(bar);
        if (set === 'auto' && post.note) {
            const note = document.createElement('p');
            note.className = 'posts-note';
            note.textContent = post.note;
            card.appendChild(note);
        }

        // Collapsed drafts show just their bar (posted ones start collapsed).
        if (set === 'main' && !cardOpen(post)) {
            card.classList.add('posts-card-collapsed');
            if (post.posted) card.classList.add('posts-card-posted');
            return card;
        }

        const strip = document.createElement('div');
        strip.className = 'posts-strip';
        post.photos.forEach((ref, idx) => strip.appendChild(renderThumb(root, post, ref, idx, set)));
        if (!post.photos.length) {
            const hint = document.createElement('p');
            hint.className = 'posts-empty';
            hint.textContent = 'Empty. Add photos from anywhere on the site.';
            strip.appendChild(hint);
        }
        card.appendChild(strip);

        // Caption + song for the post; ./post.py pull writes both into the
        // post folder as caption.txt. Saved on blur, empty clears the field.
        // The song input fuzzy-matches against every other draft's song and
        // warns when it looks already used; a cross-platform twin (a post
        // with the same name, i.e. a Copy → IG/XHS of this one) is expected
        // to share the song and is not flagged.
        if (set === 'main') {
            const meta = document.createElement('div');
            meta.className = 'posts-meta';

            const songWrap = document.createElement('div');
            songWrap.className = 'posts-song-wrap';
            const song = document.createElement('input');
            song.type = 'text';
            song.className = 'posts-song';
            song.placeholder = 'Song';
            song.value = post.song || '';
            const drop = document.createElement('div');
            drop.className = 'posts-song-suggest';
            const warn = document.createElement('p');
            warn.className = 'posts-song-warn';

            const matchesFor = (v, allowPrefix) => (v.trim().length < 3 ? [] :
                doc.posts
                    .filter(p => p.id !== post.id && p.song &&
                        p.name.trim().toLowerCase() !== post.name.trim().toLowerCase())
                    .map(p => ({ song: p.song, name: p.name,
                                 score: songSimilarity(v, p.song, allowPrefix) }))
                    .filter(m => m.score >= 0.6)
                    .sort((x, y) => y.score - x.score)
                    .slice(0, 3));

            function refreshWarn() {
                const top = matchesFor(song.value, false)[0];
                warn.textContent = !top ? ''
                    : normSong(top.song) === normSong(song.value)
                        ? `⚠ Already used in "${top.name}"`
                        : `⚠ Looks already used in "${top.name}": ${top.song}`;
                warn.style.display = top ? 'block' : 'none';
            }
            function refreshDrop() {
                drop.innerHTML = '';
                const matches = matchesFor(song.value, true);
                matches.forEach(m => {
                    const row = document.createElement('button');
                    row.type = 'button';
                    row.className = 'posts-song-suggest-row';
                    const s = document.createElement('span');
                    s.className = 'posts-song-suggest-song';
                    s.textContent = m.song;
                    const n = document.createElement('span');
                    n.className = 'posts-song-suggest-used';
                    n.textContent = `already used in ${m.name}`;
                    row.append(s, n);
                    // mousedown (not click) so it wins over the input's blur
                    row.addEventListener('mousedown', e => {
                        e.preventDefault();
                        song.value = m.song;
                        drop.style.display = 'none';
                        song.dispatchEvent(new Event('change'));
                    });
                    drop.appendChild(row);
                });
                drop.style.display = matches.length ? 'block' : 'none';
            }
            song.addEventListener('input', () => { refreshDrop(); warn.style.display = 'none'; });
            song.addEventListener('focus', refreshDrop);
            song.addEventListener('blur', () => { drop.style.display = 'none'; refreshWarn(); });
            song.addEventListener('change', () => {
                const v = song.value.trim();
                song.value = v;
                M(posts => {
                    const p = posts.find(x => x.id === post.id);
                    if (!p) return;
                    if (v) p.song = v; else delete p.song;
                });
                refreshWarn();
            });
            songWrap.append(song, drop);
            meta.append(songWrap, warn);
            refreshWarn();   // flag a duplicate song as soon as the card renders

            const caption = document.createElement('textarea');
            caption.className = 'posts-caption';
            caption.placeholder = 'Caption';
            caption.value = post.caption || '';
            caption.addEventListener('change', () => {
                const v = caption.value.trim();
                caption.value = v;
                M(posts => {
                    const p = posts.find(x => x.id === post.id);
                    if (!p) return;
                    if (v) p.caption = v; else delete p.caption;
                });
            });
            meta.appendChild(caption);
            card.appendChild(meta);
        }

        // Collapsible behind-the-scenes bucket from the local phone library
        // (uncapped, pulled into a Phone/ subfolder by ./post.py pull).
        if (post.phone && post.phone.length) {
            const det = document.createElement('details');
            det.style.cssText = 'margin-top:8px';
            det.open = phoneOpen.has(post.id);
            det.addEventListener('toggle', () => {
                if (det.open) phoneOpen.add(post.id);
                else phoneOpen.delete(post.id);
            });
            const sum = document.createElement('summary');
            sum.textContent = `Phone (${post.phone.length})`;
            sum.style.cssText = 'cursor:pointer;opacity:.75;font-size:13px';
            const pstrip = document.createElement('div');
            pstrip.className = 'posts-strip';
            // web/phone/ never deploys, so away from the local site every
            // thumbnail here would 404 into a broken-image icon: keep the
            // count in the summary but swap the strip for a note.
            const phoneLocal = !!(window.Gallery && Gallery.isLocal);
            const photoRefs = post.phone.filter(r => r.id);
            (phoneLocal ? post.phone : []).forEach(ref => {
                const cell = document.createElement('div');
                cell.style.cssText = 'position:relative;flex:0 0 auto';
                if (ref.id) {
                    const img = document.createElement('img');
                    img.src = window.Gallery ? Gallery.photoUrl(ref, 'thumbnails') : '';
                    img.loading = 'lazy';
                    img.style.cssText = 'height:96px;border-radius:5px;display:block;cursor:pointer';
                    img.addEventListener('click', () => {
                        if (window.Gallery) Gallery.openLightbox(photoRefs, photoRefs.indexOf(ref),
                            { defaultPostId: post.id });
                    });
                    cell.appendChild(img);
                } else {
                    const vid = document.createElement('a');
                    vid.href = `/phone/trips/${ref.trip}/videos/` +
                        ref.file.split('/').map(encodeURIComponent).join('/');
                    vid.target = '_blank';
                    // in-page player; plain href still works for cmd/middle-click new-tab
                    vid.addEventListener('click', ev => {
                        if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
                        ev.preventDefault();
                        if (window.Gallery) Gallery.openVideo(vid.href, ref.file.split('/').pop());
                    });
                    vid.style.cssText = 'height:96px;width:128px;border-radius:5px;background:#26262c;' +
                        'color:#ddd;display:flex;flex-direction:column;align-items:center;' +
                        'justify-content:center;gap:3px;text-decoration:none';
                    const play = document.createElement('span');
                    play.textContent = '▶';
                    play.style.cssText = 'font-size:22px';
                    const lbl = document.createElement('span');
                    lbl.textContent = ref.file.split('/').pop().slice(0, 16);
                    lbl.style.cssText = 'font-size:9px;opacity:.7';
                    vid.append(play, lbl);
                    cell.appendChild(vid);
                }
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.textContent = '✕';
                rm.title = 'Remove from Phone section';
                rm.style.cssText = 'position:absolute;top:3px;right:3px;background:rgba(0,0,0,.65);' +
                    'color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;padding:1px 5px';
                rm.addEventListener('click', () => {
                    const what = ref.id ? 'photo' : 'video';
                    if (!confirm(`Remove this ${what} from the Phone section of "${post.name}"?`
                        + (set !== 'auto' ? ` It stays in the card's History.` : ''))) return;
                    let at = -1;
                    M(posts => {
                        const pp = posts.find(x => x.id === post.id);
                        if (!pp || !pp.phone) return;
                        const i = pp.phone.findIndex(r => keyOf(r) === keyOf(ref));
                        if (i === -1) return;
                        at = i;
                        const gone = pp.phone.splice(i, 1)[0];
                        if (set !== 'auto') rememberRemoval(pp, gone, true);
                        if (!pp.phone.length) delete pp.phone;
                    }).then(okSave => {
                        renderManager(root);
                        if (!okSave) return;
                        toast(`Removed from "${post.name}" (Phone)`, { label: 'Undo', fn: () => {
                            M(posts => {
                                const pp = posts.find(x => x.id === post.id);
                                if (!pp) return;
                                if ((pp.phone || []).some(r => keyOf(r) === keyOf(ref))) return;
                                if (platformOf(pp) === 'xhs' && countOf(pp) >= capOf(pp)) return;
                                if (!pp.phone) pp.phone = [];
                                pp.phone.splice(Math.min(Math.max(at, 0), pp.phone.length), 0, { ...ref });
                                unhistory(pp, keyOf(ref));
                            }).then(() => renderManager(root));
                        } });
                    });
                });
                cell.appendChild(rm);
                // Same soft "take it out" as the carousel's ⇩: park the phone
                // photo on the waitlist instead of dropping it (photos only —
                // the waitlist renders thumbnails, which videos don't have).
                if (set === 'main' && ref.id) {
                    const demote = document.createElement('button');
                    demote.type = 'button';
                    demote.textContent = '⇩';
                    demote.title = 'Demote to the waitlist (keeps it on this post, out of the Phone section)';
                    demote.style.cssText = 'position:absolute;top:3px;right:30px;background:rgba(0,0,0,.65);' +
                        'color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;padding:1px 5px';
                    demote.addEventListener('click', () => {
                        let at = -1;
                        const result = {};
                        M(posts => {
                            const pp = posts.find(x => x.id === post.id);
                            if (!pp || !pp.phone) return;
                            const i = pp.phone.findIndex(r => keyOf(r) === keyOf(ref));
                            if (i === -1) return;
                            if (waitOf(pp).length >= WAITLIST_MAX) { result.full = true; return; }
                            at = i;
                            const moved = pp.phone.splice(i, 1)[0];
                            if (!pp.phone.length) delete pp.phone;
                            if (!pp.waitlist) pp.waitlist = [];
                            pp.waitlist.push(moved);
                            result.ok = true;
                        }).then(okSave => {
                            renderManager(root);
                            if (result.full) { toast(`Waitlist is full (${WAITLIST_MAX})`); return; }
                            if (!okSave || !result.ok) return;
                            toast(`Waitlisted in "${post.name}" (from Phone)`, { label: 'Undo', fn: () => {
                                M(posts => {
                                    const pp = posts.find(x => x.id === post.id);
                                    if (!pp) return;
                                    const wi = waitOf(pp).findIndex(r => keyOf(r) === keyOf(ref));
                                    if (wi === -1) return;
                                    if (platformOf(pp) === 'xhs' && countOf(pp) >= capOf(pp)) return;
                                    const back = pp.waitlist.splice(wi, 1)[0];
                                    if (!pp.waitlist.length) delete pp.waitlist;
                                    if (!pp.phone) pp.phone = [];
                                    pp.phone.splice(Math.min(at, pp.phone.length), 0, back);
                                }).then(() => renderManager(root));
                            } });
                        });
                    });
                    cell.appendChild(demote);
                }
                pstrip.appendChild(cell);
            });
            if (!phoneLocal) {
                const note = document.createElement('p');
                note.textContent = '📱 Phone photos are stored on the local machine only. ' +
                    'Open the local site to view and manage them.';
                note.style.cssText = 'opacity:.65;font-size:12.5px;margin:6px 0 2px';
                pstrip.appendChild(note);
            }
            det.append(sum, pstrip);
            card.appendChild(det);
        }

        // Waitlist: demoted photos, still attached to the post but out of the
        // carousel (and out of the cap). Promote to put one back.
        if (set === 'main' && waitOf(post).length) {
            const det = document.createElement('details');
            det.className = 'posts-waitlist';
            det.open = true;
            const sum = document.createElement('summary');
            sum.textContent = `Waitlist (${waitOf(post).length})`;
            sum.title = 'Demoted photos: kept on this post but not in the carousel, and not counted against the cap';
            const wstrip = document.createElement('div');
            wstrip.className = 'posts-strip posts-waitlist-strip';
            waitOf(post).forEach(ref => wstrip.appendChild(renderWaitCell(root, post, ref)));
            det.append(sum, wstrip);
            card.appendChild(det);
        }

        // Previously-removed photos, offered back (see rememberRemoval).
        if (set === 'main' && post.history && post.history.length) {
            const det = document.createElement('details');
            det.className = 'posts-history';
            const sum = document.createElement('summary');
            sum.textContent = `History (${post.history.length})`;
            sum.title = 'Photos previously removed from this post: add them back or forget them';
            const hstrip = document.createElement('div');
            hstrip.className = 'posts-strip posts-history-strip';
            post.history.forEach(entry => hstrip.appendChild(renderHistoryCell(root, post, entry)));
            det.append(sum, hstrip);
            card.appendChild(det);
        }
        return card;
    }

    /** One waitlist entry: thumb + promote ("⇧", back into the carousel at the
     *  end) and remove ("✕", confirm + History like the main strip). Keeps the
     *  blur/map marks visible so a demoted photo does not silently lose them. */
    function renderWaitCell(root, post, ref) {
        const cell = document.createElement('div');
        cell.className = 'posts-wait-cell';
        const isPhone = !!(ref.trip && ref.trip.startsWith('phone-'));
        if (isPhone && !(window.Gallery && Gallery.isLocal)) {
            // Phone thumbnails only exist on the local site; a 📱 tile beats
            // the generic 🔒 the error fallback would show.
            const ph = document.createElement('div');
            ph.textContent = '📱';
            ph.title = 'Waitlisted phone photo. Only viewable on the local site.';
            ph.style.cssText = 'width:100%;height:100%;border-radius:5px;background:#26262c;' +
                'border:1px dashed #55555f;display:flex;align-items:center;' +
                'justify-content:center;font-size:26px';
            cell.appendChild(ph);
        } else {
            const img = document.createElement('img');
            img.loading = 'lazy';
            img.decoding = 'async';
            img.alt = '';
            img.title = 'On the waitlist';
            img.src = window.Gallery ? Gallery.photoUrl(ref, 'thumbnails') : '';
            img.addEventListener('error', () => { if (window.Gallery) Gallery.lockedCover(img); });
            img.addEventListener('click', () => {
                if (window.Gallery) Gallery.openLightbox(waitOf(post), waitOf(post).indexOf(ref),
                    { defaultPostId: post.id });
            });
            cell.appendChild(img);
        }

        if (ref.blur || ref.map) {
            const mark = document.createElement('span');
            mark.className = 'posts-wait-marks';
            mark.textContent = (ref.blur ? '🙂🚫' : '') + (ref.map ? ' 🗺' : '');
            mark.title = [ref.blur ? 'Faces blurred on pull' : null,
                          ref.map ? `Map card: ${MAP_LABELS[ref.map]}` : null]
                         .filter(Boolean).join('\n');
            cell.appendChild(mark);
        }

        const actions = document.createElement('div');
        actions.className = 'posts-wait-actions';
        const up = document.createElement('button');
        up.type = 'button';
        up.textContent = '⇧';
        up.title = isPhone ? 'Promote back into the Phone section' : 'Promote back into the post';
        up.addEventListener('click', () => {
            const result = {};
            mutate(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const wi = waitOf(p).findIndex(ph => keyOf(ph) === keyOf(ref));
                if (wi === -1) return;
                // Phone refs return to the Phone bucket, which only counts
                // against the cap on XHS; carousel refs need a free slot.
                if (isPhone ? (platformOf(p) === 'xhs' && countOf(p) >= capOf(p))
                            : countOf(p) >= capOf(p)) { result.full = true; return; }
                const back = p.waitlist.splice(wi, 1)[0];
                if (!p.waitlist.length) delete p.waitlist;
                if (isPhone) {
                    if (!p.phone) p.phone = [];
                    p.phone.push(back);
                } else {
                    p.photos.push(back);
                }
                result.ok = true;
            }).then(okSave => {
                renderManager(root);
                if (result.full) toast(`"${post.name}" is full (${countOf(post)}/${capOf(post)})`);
                else if (okSave && result.ok) {
                    toast(isPhone ? `Promoted into "${post.name}" (Phone)` : `Promoted into "${post.name}"`);
                }
            });
        });
        const rm = document.createElement('button');
        rm.type = 'button';
        rm.textContent = '✕';
        rm.title = 'Remove from the waitlist';
        rm.addEventListener('click', () => {
            if (!confirm(`Remove this photo from the waitlist of "${post.name}"? `
                + `It stays in the card's History.`)) return;
            mutate(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const wi = waitOf(p).findIndex(ph => keyOf(ph) === keyOf(ref));
                if (wi === -1) return;
                const gone = p.waitlist.splice(wi, 1)[0];
                if (!p.waitlist.length) delete p.waitlist;
                rememberRemoval(p, { ...gone, waitlist: true }, isPhone);
            }).then(okSave => {
                renderManager(root);
                if (!okSave) return;
                toast(`Removed from the waitlist of "${post.name}"`, { label: 'Undo', fn: () => {
                    mutate(posts => {
                        const p = posts.find(x => x.id === post.id);
                        if (!p || waitOf(p).some(ph => keyOf(ph) === keyOf(ref))) return;
                        if (waitOf(p).length >= WAITLIST_MAX) return;
                        if (!p.waitlist) p.waitlist = [];
                        p.waitlist.push({ ...ref });
                        unhistory(p, keyOf(ref));
                    }).then(() => renderManager(root));
                } });
            });
        });
        actions.append(up, rm);
        cell.appendChild(actions);
        return cell;
    }

    /** One History entry: greyed thumb + "add back" / "forget". Restoring
     *  keeps the ref's marks (ar/blur/map) and returns phone items to the
     *  Phone bucket; caps are respected with a toast instead of a save. */
    function renderHistoryCell(root, post, entry) {
        const isPhone = !!entry.phone || (entry.trip && entry.trip.startsWith('phone-'));
        const wasWait = !!entry.waitlist;
        const cell = document.createElement('div');
        cell.className = 'posts-history-cell';
        const when = (entry.removed ? `Removed ${new Date(entry.removed).toLocaleDateString()}` : 'Removed')
            + (wasWait ? ' (from the waitlist)' : '');
        if (entry.file) {   // phone video: no thumbnail to show
            const vid = document.createElement('span');
            vid.className = 'posts-history-video';
            vid.textContent = '▶ ' + entry.file.split('/').pop().slice(0, 14);
            vid.title = when;
            cell.appendChild(vid);
        } else {
            const img = document.createElement('img');
            img.loading = 'lazy';
            img.decoding = 'async';
            img.alt = '';
            img.title = when;
            img.src = window.Gallery ? Gallery.photoUrl(entry, 'thumbnails') : '';
            img.addEventListener('error', () => { if (window.Gallery) Gallery.lockedCover(img); });
            cell.appendChild(img);
        }
        const actions = document.createElement('div');
        actions.className = 'posts-history-actions';
        const back = document.createElement('button');
        back.type = 'button';
        back.textContent = '+';
        back.title = isPhone ? 'Add back to the Phone section'
            : wasWait ? 'Add back to the waitlist' : 'Add back to the post';
        back.addEventListener('click', () => {
            const result = {};
            mutate(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const { removed, phone, waitlist, ...ref } = entry;
                const k = keyOf(ref);
                if (isPhone) {
                    if (!(p.phone || []).some(r => keyOf(r) === k)) {
                        if (platformOf(p) === 'xhs' && countOf(p) >= capOf(p)) { result.full = true; return; }
                        if (!p.phone) p.phone = [];
                        p.phone.push(ref);
                    }
                } else if (wasWait) {
                    // it left from the waitlist, so it goes back there
                    if (!waitOf(p).some(r => keyOf(r) === k)) {
                        if (waitOf(p).length >= WAITLIST_MAX) { result.waitFull = true; return; }
                        if (!p.waitlist) p.waitlist = [];
                        p.waitlist.push(ref);
                    }
                } else if (!p.photos.some(r => keyOf(r) === k)) {
                    if (countOf(p) >= capOf(p)) { result.full = true; return; }
                    p.photos.push(ref);
                }
                unhistory(p, k);
                result.ok = true;
            }).then(okSave => {
                renderManager(root);
                if (result.full) toast(`"${post.name}" is full (${countOf(post)}/${capOf(post)})`);
                else if (result.waitFull) toast(`Waitlist is full (${WAITLIST_MAX})`);
                else if (okSave && result.ok) {
                    toast(wasWait ? `Added back to the waitlist of "${post.name}"`
                                  : `Added back to "${post.name}"`);
                }
            });
        });
        const forget = document.createElement('button');
        forget.type = 'button';
        forget.textContent = '✕';
        forget.title = 'Forget (drop from History)';
        forget.addEventListener('click', () => {
            mutate(posts => {
                const p = posts.find(x => x.id === post.id);
                if (p) unhistory(p, keyOf(entry));
            }).then(() => renderManager(root));
        });
        actions.append(back, forget);
        cell.appendChild(actions);
        return cell;
    }

    function renderThumb(root, post, ref, idx, set) {
        const M = fn => mutateSet(set || 'main', fn);
        const cell = document.createElement('div');
        cell.className = 'posts-thumb';
        cell.draggable = true;

        const orderBadge = document.createElement('span');
        orderBadge.className = 'posts-order';
        orderBadge.textContent = idx + 1;

        const img = document.createElement('img');
        img.loading = 'lazy';
        img.decoding = 'async';
        img.alt = '';
        img.src = window.Gallery ? Gallery.photoUrl(ref, 'thumbnails') : '';
        img.addEventListener('error', () => { if (window.Gallery) Gallery.lockedCover(img); });
        img.addEventListener('click', () => {
            // Auto-suggestion sets are pseudo-posts whose id matches no draft;
            // the override lookup falls back to the current post for those.
            if (window.Gallery) Gallery.openLightbox(post.photos, idx, { defaultPostId: post.id });
        });

        const move = pos => M(posts => {
            const p = posts.find(x => x.id === post.id);
            if (!p) return;
            const from = p.photos.findIndex(ph => keyOf(ph) === keyOf(ref));
            if (from === -1) return;
            const to = Math.max(0, Math.min(p.photos.length - 1, pos));
            p.photos.splice(to, 0, p.photos.splice(from, 1)[0]);
        }).then(() => renderManager(root));

        const controls = document.createElement('div');
        controls.className = 'posts-thumb-controls';
        const left = document.createElement('button');
        left.type = 'button'; left.textContent = '◀'; left.title = 'Move earlier';
        left.disabled = idx === 0;
        left.addEventListener('click', () => move(idx - 1));
        const right = document.createElement('button');
        right.type = 'button'; right.textContent = '▶'; right.title = 'Move later';
        right.disabled = idx === post.photos.length - 1;
        right.addEventListener('click', () => move(idx + 1));
        // Face-blur mark: flags this photo so ./post.py pull copies a
        // face-pixelated version instead of the straight original.
        const blurBtn = document.createElement('button');
        blurBtn.type = 'button';
        blurBtn.textContent = '🙂';
        blurBtn.title = ref.blur ? 'Face blur ON — pull copies a censored version'
                                 : 'Blur faces when pulled';
        if (ref.blur) blurBtn.style.cssText = 'background:#1d4ed8;border-radius:4px';
        blurBtn.addEventListener('click', () => {
            M(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const ph = p.photos.find(x => keyOf(x) === keyOf(ref));
                if (!ph) return;
                if (ph.blur) delete ph.blur; else ph.blur = true;
            }).then(() => renderManager(root));
        });

        // Map-card mark: flags this photo so ./post.py pull renders a location
        // card into <post>/maps/, at the post's own shape. Clicking cycles the
        // styles, since which one carries the story changes with the photo.
        const mapBtn = document.createElement('button');
        mapBtn.type = 'button';
        mapBtn.textContent = '🗺';
        mapBtn.title = ref.map ? `Map card: ${MAP_LABELS[ref.map]} — click for the next style`
                               : `Add a map card (${MAP_LABELS[MAP_STYLES[1]]})`;
        if (ref.map) mapBtn.style.cssText = 'background:#1d4ed8;border-radius:4px';
        mapBtn.addEventListener('click', () => {
            M(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const ph = p.photos.find(x => keyOf(x) === keyOf(ref));
                if (!ph) return;
                const next = MAP_STYLES[(MAP_STYLES.indexOf(ph.map || null) + 1) % MAP_STYLES.length];
                if (next) ph.map = next; else delete ph.map;
            }).then(() => renderManager(root));
        });

        // Demote: park the photo on the post's waitlist instead of removing it.
        // It stays attached to the post (and keeps its blur/map marks) but
        // leaves the carousel, freeing a slot against the cap.
        const demote = document.createElement('button');
        demote.type = 'button';
        demote.textContent = '⇩';
        demote.title = 'Demote to the waitlist (keeps it on this post, out of the carousel)';
        demote.addEventListener('click', () => {
            const at = idx;
            const result = {};
            M(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const i = p.photos.findIndex(ph => keyOf(ph) === keyOf(ref));
                if (i === -1) return;
                if (waitOf(p).length >= WAITLIST_MAX) { result.full = true; return; }
                const moved = p.photos.splice(i, 1)[0];
                if (!p.waitlist) p.waitlist = [];
                p.waitlist.push(moved);
                result.ok = true;
            }).then(okSave => {
                renderManager(root);
                if (result.full) { toast(`Waitlist is full (${WAITLIST_MAX})`); return; }
                if (!okSave || !result.ok) return;
                toast(`Waitlisted in "${post.name}"`, { label: 'Undo', fn: () => {
                    M(posts => {
                        const p = posts.find(x => x.id === post.id);
                        if (!p) return;
                        const wi = waitOf(p).findIndex(ph => keyOf(ph) === keyOf(ref));
                        if (wi === -1) return;
                        if (countOf(p) >= capOf(p)) return;   // carousel filled up meanwhile
                        const back = p.waitlist.splice(wi, 1)[0];
                        if (!p.waitlist.length) delete p.waitlist;
                        p.photos.splice(Math.min(at, p.photos.length), 0, back);
                    }).then(() => renderManager(root));
                } });
            });
        });

        const rm = document.createElement('button');
        rm.type = 'button'; rm.textContent = '✕'; rm.title = 'Remove from post';
        rm.addEventListener('click', () => {
            if (!confirm(set === 'auto'
                ? `Remove this photo from "${post.name}"?`
                : `Remove this photo from "${post.name}"? It stays in the card's History.`)) return;
            const at = idx;
            M(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const i = p.photos.findIndex(ph => keyOf(ph) === keyOf(ref));
                if (i === -1) return;
                const gone = p.photos.splice(i, 1)[0];
                // Auto suggestions are regenerated wholesale, so no History there.
                if (set !== 'auto') rememberRemoval(p, gone, false);
            }).then(okSave => {
                renderManager(root);
                if (!okSave) return;
                toast(`Removed from "${post.name}"`, { label: 'Undo', fn: () => {
                    M(posts => {
                        const p = posts.find(x => x.id === post.id);
                        if (!p || p.photos.some(ph => keyOf(ph) === keyOf(ref))) return;
                        if (countOf(p) >= capOf(p)) return;   // filled up meanwhile
                        p.photos.splice(Math.min(at, p.photos.length), 0, { ...ref });
                        unhistory(p, keyOf(ref));
                    }).then(() => renderManager(root));
                } });
            });
        });
        controls.append(left, rm, right);

        // Desktop drag to reorder; phones use the arrow buttons.
        cell.addEventListener('dragstart', e => {
            e.dataTransfer.setData('text/plain', keyOf(ref));
            e.dataTransfer.effectAllowed = 'move';
        });
        cell.addEventListener('dragover', e => { e.preventDefault(); cell.classList.add('drag-over'); });
        cell.addEventListener('dragleave', () => cell.classList.remove('drag-over'));
        cell.addEventListener('drop', e => {
            e.preventDefault();
            cell.classList.remove('drag-over');
            const fromKey = e.dataTransfer.getData('text/plain');
            if (!fromKey || fromKey === keyOf(ref)) return;
            M(posts => {
                const p = posts.find(x => x.id === post.id);
                if (!p) return;
                const from = p.photos.findIndex(ph => keyOf(ph) === fromKey);
                const to = p.photos.findIndex(ph => keyOf(ph) === keyOf(ref));
                if (from === -1 || to === -1) return;
                p.photos.splice(to, 0, p.photos.splice(from, 1)[0]);
            }).then(() => renderManager(root));
        });

        controls.insertBefore(blurBtn, rm);
        controls.insertBefore(mapBtn, rm);
        // Demote sits next to remove: the softer of the two "take it out" actions.
        if (set === 'main') controls.insertBefore(demote, rm);
        if (ref.map) {
            const mark = document.createElement('span');
            mark.textContent = '🗺 ' + MAP_SHORT[ref.map];
            mark.title = `Map card on pull: ${MAP_LABELS[ref.map]}`;
            mark.style.cssText = 'position:absolute;top:4px;right:4px;font-size:11px;' +
                'background:rgba(0,0,0,.65);border-radius:4px;padding:1px 4px';
            cell.appendChild(mark);
        }
        if (ref.blur) {
            const mark = document.createElement('span');
            mark.textContent = '🙂🚫';
            mark.title = 'Faces will be blurred on pull';
            mark.style.cssText = 'position:absolute;bottom:4px;left:4px;font-size:12px;' +
                'background:rgba(0,0,0,.65);border-radius:4px;padding:1px 4px';
            cell.appendChild(mark);
        }
        cell.append(orderBadge, img, controls);
        return cell;
    }

    // Blogs render their grids synchronously from inline JSON before this
    // script loads, so their onGridRender call never happens — pick up any
    // already-rendered grid once the state is in.
    ready.then(ok => {
        if (!ok) return;
        updatePill();
        // Blogs render their grids before this script loads, so stamp whatever is
        // already on the page as well as anything rendered later.
        stampPostBadges();
        if (document.querySelector('.photo-cell[data-trip]')) ensureSelectUi();
    });

    return { enabled: true, attachLightbox, onGridRender, initPostsPage, addToPost };
})();
