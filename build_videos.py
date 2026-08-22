#!/usr/bin/env python3
"""Build web/videos.html from config/videos.json.

The eskilite source page grouped videos by location; this re-sorts them by YEAR.
The year is never hand-entered: it is derived from data already in the project.

  - A video tied to a `building` takes the most recent of that building's climb
    years, read from web/collections/rooftopping.json (built by build_collections.py).
  - A montage / non-building clip tied to a `trip` takes the year prefix of the
    trip id (e.g. "2019-nyc" -> 2019).

Each video is embedded in a tile (click-to-play), matching the rest of the site.
The page is served only to unlocked visitors (see functions/_middleware.ts +
photo_privacy.py private_pages), so the nav link is gated like Urbex.

Usage:  python3 build_videos.py
"""

import html
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEOS_JSON = ROOT / "config" / "videos.json"
ROOFTOPPING_JSON = ROOT / "web" / "collections" / "rooftopping.json"
OUT = ROOT / "web" / "videos.html"


def load_building_years() -> dict:
    """title(lowercased) -> sorted list of climb years, from the rooftopping collection."""
    data = json.loads(ROOFTOPPING_JSON.read_text())
    out = {}
    for tile in data.get("tiles", []):
        for section in tile.get("sections", []):
            for st in section.get("subtiles", []):
                title = st.get("title")
                years = st.get("years") or []
                if title and years:
                    out[title.strip().lower()] = sorted(int(y) for y in years)
    return out


def resolve_year(v: dict, building_years: dict) -> int:
    """Derive a video's year from project data. Exactly one of building/trip is used."""
    if v.get("building"):
        key = v["building"].strip().lower()
        years = building_years.get(key)
        if not years:
            raise SystemExit(
                f"ERROR: video {v['title']!r} references building {v['building']!r}, "
                f"which is not in {ROOFTOPPING_JSON.relative_to(ROOT)}."
            )
        return years[-1]  # most recent climb year
    if v.get("trip"):
        prefix = str(v["trip"])[:4]
        if not prefix.isdigit():
            raise SystemExit(f"ERROR: trip id {v['trip']!r} for {v['title']!r} has no year prefix.")
        return int(prefix)
    raise SystemExit(f"ERROR: video {v['title']!r} has neither `building` nor `trip`.")


NAV = '''    <nav class="topnav">
        <div class="nav-links">
            <a href="index.html">Home</a>
            <a href="map.html">Map</a>
            <a href="china.html">China</a>
            <a href="blogs.html">Blogs</a>
            <a href="rooftopping.html" data-gated>Urbex</a>
            <a href="galleries.html">Galleries</a>
            <div class="nav-more">
                <button class="nav-more-toggle active" type="button">More</button>
                <div class="nav-more-menu">
                    <a href="videos.html" class="active">Videos</a>
                    <a href="plans/" data-gated>Plans</a>
                </div>
            </div>
            <a href="about.html">About</a>
        </div>
        <a class="nav-name" href="index.html">Ryan McLoughlin</a>
        <span class="nav-spacer"></span>
        <div class="nav-links"><a id="seeall-link" href="#">See All</a></div>
    </nav>'''


def tile_html(v: dict) -> str:
    title = html.escape(v["title"])
    loc = html.escape(v.get("location", ""))
    if v.get("youtube"):
        vid = html.escape(v["youtube"], quote=True)
        return f'''            <div class="tile video-tile" data-embed="youtube" data-vid="{vid}" data-title="{title}" role="button" tabindex="0" aria-label="Play {title}">
                <img class="tile-img" loading="lazy" src="https://i.ytimg.com/vi/{vid}/hqdefault.jpg" alt="">
                <span class="video-play" aria-hidden="true"></span>
                <span class="tile-overlay">
                    <span class="tile-title">{title}</span>
                    <span class="tile-sub">{loc}</span>
                </span>
            </div>'''
    if v.get("drive"):
        vid = html.escape(v["drive"], quote=True)
        return f'''            <div class="tile video-tile video-tile--drive" data-embed="drive" data-vid="{vid}" data-title="{title}" role="button" tabindex="0" aria-label="Play {title}">
                <span class="video-play" aria-hidden="true"></span>
                <span class="tile-overlay">
                    <span class="tile-title">{title}</span>
                    <span class="tile-sub">{loc}</span>
                </span>
            </div>'''
    # no video linked yet -> placeholder tile
    return f'''            <div class="tile tile--pending video-tile--pending">
                <div class="tile-inner">
                    <div class="tile-title">{title}</div>
                    <div class="tile-sub">{loc}</div>
                    <div class="pending-tag">Video coming soon</div>
                </div>
            </div>'''


def build() -> None:
    doc = json.loads(VIDEOS_JSON.read_text())
    building_years = load_building_years()

    by_year: dict[int, list] = defaultdict(list)
    for v in doc.get("videos", []):
        by_year[resolve_year(v, building_years)].append(v)

    total = sum(len(vs) for vs in by_year.values())
    sections = []
    for year in sorted(by_year, reverse=True):
        vids = by_year[year]
        tiles = "\n".join(tile_html(v) for v in vids)
        count = f"{len(vids)} video{'s' if len(vids) != 1 else ''}"
        sections.append(f'''        <section class="video-year">
            <div class="section-head"><h2>{year}</h2><span class="count">{count}</span></div>
            <div class="tiles tiles--dense">
{tiles}
            </div>
        </section>''')
    body = "\n".join(sections)

    page = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Videos · Ryan's Travels</title>
    <link rel="stylesheet" href="css/site.css"/>
</head>
<body>
{NAV}

    <main class="wrap">
        <div class="china-masthead">
            <h1>Videos</h1>
        </div>
        <p class="powered-by" style="text-align:left;margin:14px 0 30px;">A small amount of the terabytes of footage I've been able to edit over the years.</p>
{body}
    </main>

    <script src="js/unlock.js"></script>
    <script src="js/posts.js"></script>
    <script src="js/phone-mode.js"></script>
    <script src="js/videos.js"></script>
</body>
</html>
'''
    OUT.write_text(page)
    years = ", ".join(str(y) for y in sorted(by_year, reverse=True))
    print(f"✓ {OUT.relative_to(ROOT)}: {total} videos across years {years}")


if __name__ == "__main__":
    try:
        build()
    except FileNotFoundError as e:
        sys.exit(f"ERROR: missing input file: {e}")
