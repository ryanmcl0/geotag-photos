#!/usr/bin/env python3
"""
compression_compare.py — visual + storage comparison for image compression choices.

Encodes a handful of real photos at several format/quality/resolution variants, builds a
browser comparison grid (judge quality in the actual rendering engine, not Finder — which
mis-renders AVIF), and projects each variant's total R2 usage. Use when deciding the
display encoding (e.g. weighing AVIF quality vs resolution against the ~10 GB R2 ceiling).

Variant syntax: "fmt:quality:longest_px"  e.g.  avif:80:1920

Usage:
  python3 compression_compare.py --src "/Volumes/RYAN/Edits/2026:02 China CNY" --serve
  python3 compression_compare.py --src <dir> --n 8 \
      --variant webp:90:2160 --variant avif:80:1920 --variant avif:82:1600
  python3 compression_compare.py --src <dir> --total 14000   # photos for R2 projection
"""

import glob
import os
import subprocess

import click
from PIL import Image

DEFAULT_VARIANTS = ("webp:90:2160", "avif:63:2160", "avif:80:2160",
                    "avif:80:1920", "avif:82:1600")


def _fit(im, longest):
    L = max(im.size)
    if L <= longest:
        return im
    return im.resize((round(im.width * longest / L), round(im.height * longest / L)),
                     Image.LANCZOS)


def _pick(src, n):
    js = sorted(glob.glob(os.path.join(src, "*.jpg")) + glob.glob(os.path.join(src, "*.JPG")))
    if not js:
        raise SystemExit(f"No .jpg under {src}")
    step = max(1, len(js) // n)
    return js[::step][:n]


@click.command()
@click.option("--src", required=True, help="Directory of source photos to sample")
@click.option("--n", default=6, help="How many photos to sample (spread across the dir)")
@click.option("--variant", "variants", multiple=True,
              help="fmt:quality:longest (repeatable). Default: a webp + AVIF q/res spread")
@click.option("--out", default="/tmp/compression_compare", help="Output dir for the page")
@click.option("--total", default=14000, help="Photo count for projected R2 total (display only)")
@click.option("--serve/--no-serve", default=False, help="Start a localhost server + open the page")
@click.option("--port", default=8799)
def main(src, n, variants, out, total, serve, port):
    variants = list(variants) or list(DEFAULT_VARIANTS)
    try:  # mark the variant matching the pipeline's current encoding as "(current)"
        from process_trip import DEFAULT_FORMAT, DEFAULT_QUALITY, DEFAULT_DISPLAY_LONGEST
        current = f"{DEFAULT_FORMAT}:{DEFAULT_QUALITY}:{DEFAULT_DISPLAY_LONGEST}"
    except Exception:
        current = "webp:90:2160"
    specs = []
    for v in variants:
        fmt, q, lng = v.split(":")
        label = v + (" (current)" if v == current else "")
        specs.append((label, fmt.lower(), int(q), int(lng)))
    os.makedirs(out, exist_ok=True)
    picks = _pick(src, n)

    sizes = {v[0]: [] for v in specs}
    rows = []
    for i, sp in enumerate(picks):
        im0 = Image.open(sp).convert("RGB")
        cells = []
        for label, fmt, q, lng in specs:
            im = _fit(im0, lng)
            ext = "webp" if fmt == "webp" else ("jpg" if fmt == "jpeg" else "avif")
            pil = {"webp": "WEBP", "jpeg": "JPEG", "avif": "AVIF"}[fmt]
            opt = ({"quality": q, "method": 6} if fmt == "webp"
                   else {"quality": q, "optimize": True} if fmt == "jpeg"
                   else {"quality": q, "speed": 6})
            mark = "_CURRENT" if "(current)" in label else ""
            fn = f"{i}_{fmt}_{q}_{lng}{mark}.{ext}"
            im.save(os.path.join(out, fn), pil, **opt)
            kb = os.path.getsize(os.path.join(out, fn)) / 1024
            sizes[label].append(kb)
            cells.append((label, fn, kb, f"{im.width}×{im.height}"))
        rows.append((os.path.basename(sp), cells))

    def proj_gb(label):
        avg = sum(sizes[label]) / len(sizes[label])
        return avg * total / 1024 / 1024

    import json
    data = [{"name": name, "cells": [{"label": l, "fn": fn, "kb": round(kb), "dim": dim}
                                     for l, fn, kb, dim in cells]} for name, cells in rows]

    h = ["<html><head><meta charset=utf8><style>",
         "body{background:#111;color:#ddd;font:13px system-ui;margin:0;padding:12px}",
         "table{border-collapse:collapse}td{padding:6px;text-align:center;vertical-align:top}",
         "img{max-width:330px;height:auto;display:block;border:1px solid #333;cursor:zoom-in}",
         ".h{position:sticky;top:0;background:#000;padding:8px;font-weight:600;z-index:2}",
         ".sz{color:#9c9;font-size:12px}small{color:#888}",
         "#lb{display:none;position:fixed;inset:0;background:#000;z-index:9;overflow:auto}",
         "#lb img{max-width:none;border:0;cursor:zoom-in;margin:auto}",
         "#lb.fit img{max-width:100vw;max-height:100vh;cursor:zoom-out}",
         "#bar{position:fixed;top:0;left:0;right:0;background:rgba(0,0,0,.8);padding:8px 14px;"
         "font:14px system-ui;color:#fff;z-index:10;display:flex;gap:16px;align-items:center}",
         "#bar b{color:#9cf}kbd{background:#333;padding:1px 6px;border-radius:3px}</style></head><body>",
         "<h2>Compression comparison — click a photo to open full-screen; "
         "<kbd>←</kbd>/<kbd>→</kbd> flip variants at the same zoom, click to toggle 100%, <kbd>Esc</kbd> close</h2>",
         "<table><tr><td class=h>photo</td>"]
    for label, _, _, _ in specs:
        h.append(f"<td class=h>{label}<br><small>proj. R2 display ≈ {proj_gb(label):.1f} GB</small></td>")
    h.append("</tr>")
    for pi, (name, cells) in enumerate(rows):
        h.append(f"<tr><td class=h>{name}</td>")
        for vi, (label, fn, kb, dim) in enumerate(cells):
            h.append(f"<td><img src='{fn}' loading=lazy onclick='openLB({pi},{vi})'>"
                     f"<div class=sz>{kb:.0f} KB · {dim}</div></td>")
        h.append("</tr>")
    h.append(f"</table><p><small>Projected R2 = avg display size × {total:,} photos "
             "(display only; thumbnails extra & unchanged).</small></p>")
    h.append("<div id=lb class=fit><div id=bar></div><img id=lbimg></div>")
    h.append("<script>const DATA=" + json.dumps(data) + ";")
    h.append("""
let pi=0,vi=0; const lb=document.getElementById('lb'),img=document.getElementById('lbimg'),bar=document.getElementById('bar');
function frac(){return lb.classList.contains('fit')?null:[lb.scrollLeft/(lb.scrollWidth-lb.clientWidth||1),lb.scrollTop/(lb.scrollHeight-lb.clientHeight||1)];}
function applyFrac(f){if(f){lb.scrollLeft=f[0]*(lb.scrollWidth-lb.clientWidth);lb.scrollTop=f[1]*(lb.scrollHeight-lb.clientHeight);}}
function render(keep){const c=DATA[pi].cells[vi];const f=keep?keep:frac();img.onload=()=>applyFrac(f);img.src=c.fn;
 bar.innerHTML=`${DATA[pi].name} &nbsp; <b>${c.label}</b> &nbsp; ${c.kb} KB · ${c.dim} &nbsp;&nbsp; <small>${vi+1}/${DATA[pi].cells.length} — ←/→ to compare</small>`;}
function openLB(p,v){pi=p;vi=v;lb.style.display='block';lb.classList.add('fit');render();}
img.onclick=e=>{e.stopPropagation();const wasFit=lb.classList.contains('fit');const f=frac();lb.classList.toggle('fit');render(wasFit?null:f);};
lb.onclick=()=>lb.style.display='none';
document.onkeydown=e=>{if(lb.style.display!=='block')return;const f=frac();
 if(e.key==='ArrowRight'){vi=(vi+1)%DATA[pi].cells.length;render(f);}
 else if(e.key==='ArrowLeft'){vi=(vi-1+DATA[pi].cells.length)%DATA[pi].cells.length;render(f);}
 else if(e.key==='ArrowDown'){pi=(pi+1)%DATA.length;vi=Math.min(vi,DATA[pi].cells.length-1);render(f);}
 else if(e.key==='ArrowUp'){pi=(pi-1+DATA.length)%DATA.length;vi=Math.min(vi,DATA[pi].cells.length-1);render(f);}
 else if(e.key==='Escape')lb.style.display='none';};
</script></body></html>""")
    open(os.path.join(out, "index.html"), "w").write("\n".join(h))

    click.echo(f"\nbuilt {out}/index.html — projected R2 (display only, {total:,} photos):")
    for label, *_ in specs:
        avg = sum(sizes[label]) / len(sizes[label])
        click.echo(f"  {label:22} {proj_gb(label):5.1f} GB   (avg {avg:.0f} KB)")

    if serve:
        subprocess.Popen(["python3", "-m", "http.server", str(port)], cwd=out,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://localhost:{port}/"
        click.echo(f"\nserving {url}")
        subprocess.run(["open", url])


if __name__ == "__main__":
    main()
