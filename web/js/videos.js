/**
 * Videos page: click a tile to embed the player in place (lazy — no iframe loads
 * until the visitor asks for it, so 18 tiles stay light). YouTube via a privacy
 * embed, Google Drive via its /preview player.
 */
(function () {
    function frameFor(kind, id) {
        const f = document.createElement('iframe');
        f.className = 'video-frame';
        f.setAttribute('allowfullscreen', '');
        if (kind === 'drive') {
            f.src = 'https://drive.google.com/file/d/' + id + '/preview';
            f.allow = 'autoplay; fullscreen';
        } else {
            f.src = 'https://www.youtube-nocookie.com/embed/' + id +
                '?autoplay=1&rel=0&modestbranding=1';
            f.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
        }
        f.title = 'Video player';
        return f;
    }

    function play(tile) {
        if (tile.dataset.playing) return;
        tile.dataset.playing = '1';
        const frame = frameFor(tile.dataset.embed, tile.dataset.vid);
        tile.innerHTML = '';
        tile.appendChild(frame);
        tile.classList.add('is-playing');
        tile.removeAttribute('role');
        tile.removeAttribute('tabindex');
    }

    document.querySelectorAll('.video-tile').forEach(tile => {
        if (!tile.dataset.vid) return;
        tile.addEventListener('click', () => play(tile));
        tile.addEventListener('keydown', e => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); play(tile); }
        });
    });
})();
