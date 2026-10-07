/* Light deterrent against saving photos: no right-click menu, drag-out or iOS
   long-press callout on images. Screenshots and devtools still work, so this only
   stops the casual "Save image as". Owner tools (posts, people) don't load it. */
(() => {
    'use strict';
    const style = document.createElement('style');
    style.textContent = 'img{-webkit-touch-callout:none;-webkit-user-drag:none;-webkit-user-select:none;user-select:none}';
    document.head.append(style);

    const onImage = e => e.target instanceof Element && e.target.closest('img');
    for (const type of ['contextmenu', 'dragstart']) {
        document.addEventListener(type, e => { if (onImage(e)) e.preventDefault(); }, true);
    }
})();
