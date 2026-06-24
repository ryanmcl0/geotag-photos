#!/usr/bin/env python3
"""
List all image filenames referenced in a saved blog HTML file.
PNG files and *_rw_1920* / *_rwc_* files are likely Google Maps Timeline screenshots.

Usage:
    python tools/reconstruction/blog_images.py "/path/to/blog.html"
    python tools/reconstruction/blog_images.py "/path/to/blog.html" --maps-only
"""
import sys, re, argparse
from html.parser import HTMLParser
from pathlib import Path


class ImgExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            attrs_dict = dict(attrs)
            src = attrs_dict.get('src', '') or attrs_dict.get('data-src', '')
            alt = attrs_dict.get('alt', '')
            fname = src.split('/')[-1].split('?')[0]
            if fname:
                self.images.append((fname, alt))


def is_likely_map(fname):
    return (fname.endswith('.png') or
            '_rw_1920' in fname or
            '_rwc_' in fname or
            'carw' in fname)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('html', help='Path to saved blog HTML')
    ap.add_argument('--maps-only', action='store_true', help='Only show likely map screenshots')
    args = ap.parse_args()

    html = Path(args.html).read_text(encoding='utf-8', errors='ignore')
    p = ImgExtractor()
    p.feed(html)

    for fname, alt in p.images:
        if args.maps_only and not is_likely_map(fname):
            continue
        marker = '* MAP?' if is_likely_map(fname) else '  '
        alt_str = f'  [{alt}]' if alt else ''
        print(f"{marker} {fname}{alt_str}")

    if args.maps_only:
        maps = [(f, a) for f, a in p.images if is_likely_map(f)]
        print(f"\n{len(maps)} likely map screenshots found.")
    else:
        print(f"\n{len(p.images)} total images.")


if __name__ == '__main__':
    main()
