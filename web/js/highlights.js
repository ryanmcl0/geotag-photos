/**
 * Highlights — scroll-driven 3D coverflow galleries built from post drafts.
 *
 * Data comes from GET /api/highlights, which filters privately server-side:
 * locked visitors get only the public photos (and never the private refs at
 * all), all-access gets every carousel in full. Unlocking reloads the page
 * (unlock.js), so the refetch happens naturally.
 *
 * Each post is a section tall enough to give every photo a slice of scroll;
 * a 100vh sticky stage pins while the carousel spins at the pace the visitor
 * scrolls. When the last photo lands, the section runs out and the page moves
 * on to the next gallery. A gentle time-based sway keeps the stage alive even
 * before any scrolling, so the first carousel is never a still image.
 */
(function () {
    const app = document.getElementById('hl-app');
    if (!app) return;

    const PER_PHOTO_VH = 55;   // scroll distance (in vh) that advances one photo
    const AMBIENT = 0.12;      // idle sway amplitude, in photo-index units
    const REDUCED = window.matchMedia
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    /** @type {{el:HTMLElement, cards:HTMLElement[], imgs:HTMLImageElement[],
                photos:object[], n:number, cur:number, phase:number,
                count:HTMLElement, w:number[]}[]} */
    const sections = [];

    fetch('/api/highlights', { cache: 'no-store' })
        .then(r => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))))
        .then(build)
        .catch(() => {
            app.innerHTML = '<p class="hl-empty">Could not load highlights. Try refreshing.</p>';
        });

    function build(data) {
        const posts = (data && data.posts) || [];
        if (!posts.length) {
            app.innerHTML = '<p class="hl-empty">Nothing here yet.</p>';
            return;
        }
        posts.forEach((post, si) => {
            const n = post.photos.length;
            const sec = document.createElement('section');
            sec.className = 'hl-section';
            sec.style.height = (100 + n * PER_PHOTO_VH) + 'vh';

            const sticky = document.createElement('div');
            sticky.className = 'hl-sticky';

            const title = document.createElement('h2');
            title.className = 'hl-title';
            title.textContent = post.title;

            const stage = document.createElement('div');
            stage.className = 'hl-stage';

            const imgs = [];
            const cards = post.photos.map((ph, i) => {
                const fig = document.createElement('figure');
                fig.className = 'hl-card';
                const img = document.createElement('img');
                img.alt = post.title;
                img.decoding = 'async';
                img.loading = 'lazy';
                img.src = Gallery.photoUrl(ph, 'thumbnails');
                img.dataset.display = Gallery.photoUrl(ph, 'display');
                fig.appendChild(img);
                fig.addEventListener('click', () => Gallery.openLightbox(post.photos, i));
                imgs.push(img);
                stage.appendChild(fig);
                return fig;
            });

            const count = document.createElement('div');
            count.className = 'hl-count';
            count.textContent = '1 / ' + n;

            sticky.append(title, stage, count);
            sec.appendChild(sticky);
            app.appendChild(sec);
            sections.push({ el: sec, cards, imgs, photos: post.photos,
                            n, cur: 0, phase: si * 1.7, count, w: [] });
        });

        layout();
        window.addEventListener('resize', () => {
            cancelAnimationFrame(layout._raf);
            layout._raf = requestAnimationFrame(layout);
        });

        // Swap thumbnails for full display images once a section approaches the
        // viewport; preload first so the sharpening never blanks the card.
        const io = new IntersectionObserver(entries => {
            entries.forEach(en => {
                if (!en.isIntersecting) return;
                io.unobserve(en.target);
                const s = sections.find(x => x.el === en.target);
                if (s) s.imgs.forEach(img => {
                    const hi = new Image();
                    hi.onload = () => { img.src = hi.src; };
                    hi.src = img.dataset.display;
                });
            });
        }, { rootMargin: '75% 0px' });
        sections.forEach(s => io.observe(s.el));

        requestAnimationFrame(tick);
    }

    // Size every card from its aspect ratio: portrait sets share a height,
    // wide frames give up height rather than overflow the stage.
    function layout() {
        const vh = window.innerHeight, vw = window.innerWidth;
        const maxH = vh * (vw < 700 ? 0.5 : 0.56);
        const maxW = Math.min(vw * 0.78, 980);
        sections.forEach(s => {
            s.w = s.photos.map((ph, i) => {
                const ar = ph.ar || 1.5;
                let h = maxH, w = h * ar;
                if (w > maxW) { w = maxW; h = w / ar; }
                s.cards[i].style.width = w.toFixed(1) + 'px';
                s.cards[i].style.height = h.toFixed(1) + 'px';
                return w;
            });
        });
    }

    // Coverflow placement for a card sitting d photo-slots from the focus:
    // first neighbours peek beside the centre photo, farther ones stack away.
    function place(card, d, w) {
        const ad = Math.abs(d), sg = Math.sign(d);
        if (ad > 4.2) { card.style.visibility = 'hidden'; return; }
        card.style.visibility = '';
        const x = sg * (Math.min(ad, 1) * 0.62 + Math.max(0, ad - 1) * 0.24) * w;
        const z = -(Math.min(ad, 1) * 190 + Math.max(0, ad - 1) * 95);
        const ry = -sg * Math.min(ad, 1.5) * 38;
        const sc = Math.max(0.7, 1 - Math.min(ad, 1) * 0.06 - Math.max(0, ad - 1) * 0.06);
        card.style.transform = 'translate(-50%, -50%)'
            + ' translate3d(' + x.toFixed(1) + 'px, 0, ' + z.toFixed(1) + 'px)'
            + ' rotateY(' + ry.toFixed(2) + 'deg)'
            + ' scale(' + sc.toFixed(3) + ')';
        card.style.opacity = Math.max(0, Math.min(1, 1 - Math.max(0, ad - 1.3) * 0.4)).toFixed(3);
        card.style.zIndex = String(200 - Math.round(ad * 10));
    }

    function tick(now) {
        const vh = window.innerHeight;
        sections.forEach(s => {
            const rect = s.el.getBoundingClientRect();
            // skip stages nowhere near the viewport
            if (rect.bottom < -vh * 0.5 || rect.top > vh * 1.5) return;
            const scrollable = rect.height - vh;
            const p = scrollable > 0 ? Math.min(1, Math.max(0, -rect.top / scrollable)) : 0;
            const target = p * (s.n - 1);
            s.cur += (target - s.cur) * 0.16;
            if (Math.abs(target - s.cur) < 0.0005) s.cur = target;
            const sway = REDUCED ? 0 : Math.sin(now / 2400 + s.phase) * AMBIENT;
            const f = s.cur + sway;
            s.cards.forEach((card, i) => place(card, i - f, s.w[i] || 600));
            s.count.textContent =
                Math.min(s.n, Math.max(1, Math.round(s.cur) + 1)) + ' / ' + s.n;
        });
        requestAnimationFrame(tick);
    }
})();
