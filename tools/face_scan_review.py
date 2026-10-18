#!/usr/bin/env python3
"""Publish a face_scan_raws.py database: import list, review page and an
optional Lightroom smart collection.

    tools/face_scan_review.py --db scan.sqlite --list import.txt \\
        [--page review.html] [--previews DIR] \\
        [--keep keep.txt] [--reject reject.txt] \\
        [--copy-list-to PATH] [--smart-collection FILE.lrsmcol --name NAME]

Re-run after every scan; it brings every output back in line with the database.

The import list is confident hits (similarity >= --confident) plus hand-picked
keeps, minus hand-marked rejects. Keeps and rejects are plain files, one path
per line, so manual decisions survive every rescan. The review page is only
needed to make those decisions: open it, click tiles, copy the paths into the
keep or reject file and re-run. Skip --page if you do not review by eye.

The import list feeds tools/lr_import_list.lrplugin, which adds the photos to
the Lightroom catalog in place, skips any already there and puts them all in
one collection named after the list file. With --append the list only ever
grows, so re-importing the same list after each new dump tops up that same
collection.
"""
import argparse
import html
import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_list(path):
    if not path or not path.exists():
        return []
    return [l.strip() for l in path.read_text().splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, required=True, help="face_scan_raws.py database")
    ap.add_argument("--list", type=Path, required=True, help="import list to write")
    ap.add_argument("--keep", type=Path, help="paths to add to the list by hand")
    ap.add_argument("--reject", type=Path, help="paths to exclude by hand")
    ap.add_argument("--page", type=Path, help="review page to write (optional)")
    ap.add_argument("--previews", type=Path,
                    help="preview folder the page shows (default: <db folder>/previews)")
    ap.add_argument("--copy-list-to", type=Path,
                    help="also copy the import list here (e.g. next to the photos)")
    ap.add_argument("--smart-collection", type=Path, help=".lrsmcol file to write")
    ap.add_argument("--name", default="Face scan", help="smart collection / page title")
    ap.add_argument("--date-cache", type=Path,
                    help="capture-date cache for the smart collection "
                         "(default: <db folder>/.face_scan_dates.json)")
    ap.add_argument("--append", action="store_true",
                    help="never drop a path already in --list unless it is rejected, "
                         "so one Lightroom collection keeps growing across dumps")
    ap.add_argument("--confident", type=float, default=0.5,
                    help="similarity that goes into the list automatically (default 0.5)")
    ap.add_argument("--floor", type=float, default=0.35)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    rows = con.execute("""SELECT path, prev, best_sim FROM files
                          WHERE best_sim >= ? ORDER BY best_sim DESC""",
                       (args.floor,)).fetchall()
    scanned = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    by_bucket = defaultdict(list)
    for path, prev, sim in rows:
        by_bucket["confident" if sim >= args.confident else "borderline"].append((path, prev, sim))

    keeps, rejects = read_list(args.keep), set(read_list(args.reject))
    previous_order = read_list(args.list)
    previous = set(previous_order)

    # confident + hand-picked keeps, minus hand-marked false positives. With
    # --append the existing list comes first and only a reject removes from it.
    selected, seen = [], set()
    carried = previous_order if args.append else []
    for p in carried + [p for p, _, _ in by_bucket["confident"]] + keeps:
        if p in rejects or p in seen:
            continue
        seen.add(p)
        selected.append(p)

    args.list.write_text("\n".join(selected) + ("\n" if selected else ""))
    added = [p for p in selected if p not in previous]
    gone = [p for p in previous if p not in seen]

    print(f"{scanned} raws scanned | {len(by_bucket['confident'])} confident, "
          f"{len(by_bucket['borderline'])} borderline")
    print(f"import list: {len(selected)} photos "
          f"(+{len(keeps)} kept by hand, -{len(rejects)} rejected) → {args.list}")
    if added:
        print(f"  {len(added)} new since the last run")
    if gone:
        print(f"  {len(gone)} dropped since the last run")

    if args.page:
        previews = args.previews or args.db.parent / "previews"
        write_page(args, by_bucket, scanned, selected, rejects, previews)

    if args.copy_list_to:
        if not args.copy_list_to.parent.exists():
            print(f"⚠️  {args.copy_list_to.parent} not found, list not copied")
        else:
            shutil.copyfile(args.list, args.copy_list_to)
            print(f"copied the list → {args.copy_list_to}")
    if args.smart_collection:
        write_smart_collection(args, selected)


def write_smart_collection(args, selected):
    """Smart collection matching the list on filename + capture date. Dates are
    cached: reading EXIF off a network drive is the slow part of a re-run."""
    sys.path.insert(0, str(ROOT / "tools"))
    import lr_smart_collection as lrsc

    cache_path = args.date_cache or args.db.parent / ".face_scan_dates.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    missing = [Path(p) for p in selected if p not in cache]
    if missing:
        print(f"reading capture dates for {len(missing)} new photos...")
        for f, d in lrsc.capture_dates(missing).items():
            cache[str(f)] = d
        cache_path.write_text(json.dumps(cache))

    # Feed the cached dates back in so write_lrsmcol does not re-read them.
    original = lrsc.capture_dates
    lrsc.capture_dates = lambda files: {f: cache.get(str(f)) for f in files}
    try:
        n, undated = lrsc.write_lrsmcol([Path(p) for p in selected], args.name,
                                        args.smart_collection)
    finally:
        lrsc.capture_dates = original
    print(f"smart collection: {n} rules → {args.smart_collection}"
          + (f" ({len(undated)} without a capture date)" if undated else ""))


def write_page(args, by_bucket, scanned, selected, rejects, previews):
    in_list = set(selected)
    # absolute(), not resolve(): a symlinked preview folder beside the page
    # (to serve it from the web root) must stay a sibling URL
    prev_url = os.path.relpath(previews.absolute(), args.page.parent.absolute())
    common = os.path.commonpath([p for p, _, _ in by_bucket["confident"] + by_bucket["borderline"]]
                                or ["/"])

    def cells(items):
        out = []
        for path, prev, sim in items:
            rel = os.path.relpath(path, common) if common != "/" else path
            state = "in" if path in in_list else ("out" if path in rejects else "")
            src = f"{prev_url}/{prev}" if prev else ""
            out.append(
                f'<a href="{html.escape(src)}" data-path="{html.escape(path)}" class="{state}" '
                f'title="{html.escape(rel)}">'
                + (f'<img loading="lazy" src="{html.escape(src)}">' if prev
                   else '<div class="noprev">no preview</div>')
                + f'<span class="badge">{sim:.2f}</span>'
                f'<span class="cap">{html.escape(rel[-48:])}</span></a>')
        return "\n".join(out)

    keep_name = args.keep.name if args.keep else "the keep file"
    reject_name = args.reject.name if args.reject else "the reject file"
    title = html.escape(args.name)
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}: review</title><style>
body{{background:#111;color:#eee;font:14px -apple-system,sans-serif;margin:0;padding:20px}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}}
.grid{{display:flex;flex-wrap:wrap;gap:8px}}
.grid a{{position:relative;display:block;color:#bbb;text-decoration:none;width:210px}}
.grid img{{width:210px;border-radius:5px;display:block}}
.noprev{{width:210px;height:140px;border-radius:5px;background:#222;display:flex;
align-items:center;justify-content:center;font-size:12px;opacity:.6}}
.grid a.in img{{outline:3px solid #22c55e;outline-offset:-3px}}
.grid a.out img{{opacity:.35;outline:3px solid #ef4444;outline-offset:-3px}}
.grid a.pick img{{outline:3px solid #f0b429;outline-offset:-3px}}
.badge{{position:absolute;top:4px;right:4px;background:rgba(0,0,0,.7);border-radius:4px;
padding:1px 6px;font-size:11px;color:#fff}}
.cap{{font-size:10px;opacity:.6;display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
.tag{{background:#1d4ed8;border-radius:99px;padding:2px 8px;font-size:12px;margin-left:8px}}
#bar{{position:sticky;top:0;background:#111;padding:10px 0;z-index:5;border-bottom:1px solid #333}}
#bar button{{background:#1d4ed8;border:none;color:#fff;border-radius:6px;padding:6px 12px;
cursor:pointer;font-size:13px;margin-right:6px}}
#bar button.ghost{{background:none;border:1px solid #555;color:#ddd}}
#out{{width:100%;height:110px;background:#181818;color:#ddd;border:1px solid #333;
border-radius:6px;margin-top:8px;display:none;font:12px monospace;padding:8px}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;align-items:center;
justify-content:center;z-index:10;cursor:zoom-out}}
#lb img{{max-width:96vw;max-height:94vh}}
</style></head><body>
<h1>{title}
<span class="tag">{len(by_bucket['confident'])} confident</span>
<span class="tag">{len(by_bucket['borderline'])} borderline</span>
<span class="tag">{len(selected)} in the import list</span></h1>
<p>{scanned} raws scanned via embedded previews. Confident = similarity ≥ {args.confident}
to the reference face; borderline {args.floor}–{args.confident} (review by eye).
Green = in the import list, red = rejected.</p>
<div id="bar">
  <span id="picked">0 selected</span>
  <button id="copy-keep">Copy as KEEP</button>
  <button id="copy-reject" class="ghost">Copy as REJECT</button>
  <button id="clear" class="ghost">Clear</button>
  <p style="font-size:12px;opacity:.65;margin:6px 0 0">Click tiles to select (cmd-click to
  enlarge), copy, then paste into <code>{html.escape(keep_name)}</code> or
  <code>{html.escape(reject_name)}</code> and re-run. Those files survive every future scan.</p>
  <textarea id="out" readonly></textarea>
</div>
<h2>Confident</h2><div class="grid">{cells(by_bucket['confident'])}</div>
<h2>Borderline, worth a skim</h2><div class="grid">{cells(by_bucket['borderline'])}</div>
<div id="lb"><img alt=""></div>
<script>
(function () {{
  var picked = new Set();
  var lb = document.getElementById('lb');
  lb.addEventListener('click', function () {{ lb.style.display = 'none'; }});
  function refresh() {{
    document.getElementById('picked').textContent = picked.size + ' selected';
  }}
  document.querySelectorAll('.grid a').forEach(function (a) {{
    a.addEventListener('click', function (e) {{
      e.preventDefault();
      if (e.metaKey || e.ctrlKey) {{
        if (a.getAttribute('href')) {{
          lb.querySelector('img').src = a.getAttribute('href');
          lb.style.display = 'flex';
        }}
        return;
      }}
      var p = a.dataset.path;
      if (picked.has(p)) {{ picked.delete(p); a.classList.remove('pick'); }}
      else {{ picked.add(p); a.classList.add('pick'); }}
      refresh();
    }});
  }});
  function dump() {{
    var t = document.getElementById('out');
    t.style.display = 'block';
    t.value = Array.from(picked).join('\\n');
    t.select();
    try {{ document.execCommand('copy'); }} catch (err) {{}}
  }}
  document.getElementById('copy-keep').addEventListener('click', dump);
  document.getElementById('copy-reject').addEventListener('click', dump);
  document.getElementById('clear').addEventListener('click', function () {{
    picked.clear();
    document.querySelectorAll('.grid a.pick').forEach(function (a) {{ a.classList.remove('pick'); }});
    document.getElementById('out').style.display = 'none';
    refresh();
  }});
}})();
</script>
</body></html>"""
    args.page.write_text(page)
    print(f"review page → {args.page}")


if __name__ == "__main__":
    main()
