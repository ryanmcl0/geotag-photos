#!/usr/bin/env python3
"""
Extract readable text from a saved blog HTML file.
Strips scripts, styles, nav, and prints the body text.
Useful for pulling out day-by-day route narrative.

Usage:
    python reconstruction/blog_extract.py "/path/to/blog.html"
    python reconstruction/blog_extract.py "/path/to/blog.html" | grep -i "day\|drove\|train\|arrived"
"""
import sys
from html.parser import HTMLParser
from pathlib import Path


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head', 'nav'):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head', 'nav'):
            self._skip = False
        if tag in ('p', 'h1', 'h2', 'h3', 'h4', 'li', 'br', 'div'):
            self.chunks.append('\n')

    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s:
                self.chunks.append(s)


def main():
    if len(sys.argv) < 2:
        print("Usage: blog_extract.py <blog.html>")
        sys.exit(1)

    html = Path(sys.argv[1]).read_text(encoding='utf-8', errors='ignore')
    p = TextExtractor()
    p.feed(html)
    text = ' '.join(p.chunks)
    # Collapse whitespace runs
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    print(text)


if __name__ == '__main__':
    main()
