#!/usr/bin/env python3
"""Scan a list of raw files for one person's face.

Reads each raw's embedded JPEG preview (no full raw decode, so it stays quick
over a network share), brightens dark frames, detects faces at high resolution
with permissive thresholds (ultrawide shots put faces small and underexposed),
embeds them with ArcFace and scores every face against a reference embedding
(a normalised mean of the person's labelled faces, saved as .npy).

    tools/face_scan_raws.py LIST --reference me.npy --db scan.sqlite \\
        [--previews DIR]

LIST is a text file of raw paths, one per line. Results go to the sqlite
database, one row per file plus one per face. The scan is resumable: every file
is recorded, including ones with no faces, and a rerun skips them.

Previews are only for reviewing hits by eye, never needed by the scan itself.
They are written only for frames scoring at least --floor, into --previews
(default: a "previews" folder next to the database). Keep that on local disk:
creating thousands of files in one folder on a network share costs seconds
each and used to dominate the run.
"""
import argparse
import hashlib
import queue
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

MIN_FACE = 14
READERS = 6


def db_init(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, prev TEXT, n_faces INT, best_sim REAL,
        w INT, h INT, err TEXT, done_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS faces(
        id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT,
        x INT, y INT, w INT, h INT, det REAL, sim REAL)""")
    con.commit()
    return con


def extract_preview(path):
    """Largest embedded JPEG from a raw file."""
    for tag in ("-JpgFromRaw", "-PreviewImage"):
        r = subprocess.run(["exiftool", "-b", tag, path],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and len(r.stdout) > 20000:
            return r.stdout
    return None


def brighten(img):
    """Boost dark frames so shadowed faces survive detection."""
    gray_mean = img.mean()
    if gray_mean < 55:
        gamma = 0.5 if gray_mean < 30 else 0.65
        lut = ((np.arange(256) / 255.0) ** gamma * 255).astype(np.uint8)
        img = cv2.LUT(img, lut)
    return img


def preview_name(path):
    """Stable across runs (Python's hash() is salted per process)."""
    return hashlib.sha1(path.encode()).hexdigest()[:16] + ".jpg"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("list", type=Path, help="text file of raw paths, one per line")
    ap.add_argument("--reference", type=Path, required=True,
                    help="reference face embedding (.npy, L2-normalised)")
    ap.add_argument("--db", type=Path, required=True, help="results database (sqlite)")
    ap.add_argument("--previews", type=Path,
                    help="where to save previews of hits (default: <db folder>/previews)")
    ap.add_argument("--floor", type=float, default=0.35,
                    help="similarity at which a frame counts as a hit (default 0.35)")
    ap.add_argument("--det-thresh", type=float, default=0.35,
                    help="face detector threshold (default 0.35, permissive)")
    args = ap.parse_args()
    previews = args.previews or args.db.parent / "previews"

    files = [l for l in args.list.read_text().splitlines() if l]
    con = db_init(args.db)
    done = {r[0] for r in con.execute("SELECT path FROM files")}
    todo = [f for f in files if f not in done]
    print(f"{len(files)} raws, {len(todo)} to scan", flush=True)
    if not todo:
        return
    previews.mkdir(parents=True, exist_ok=True)

    centroid = np.load(args.reference)

    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"],
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=-1, det_size=(1600, 1600), det_thresh=args.det_thresh)

    q = queue.Queue(maxsize=24)

    def reader(paths):
        for p in paths:
            try:
                q.put((p, extract_preview(p)))
            except Exception:
                q.put((p, None))
        q.put(None)

    for chunk in (todo[i::READERS] for i in range(READERS)):
        threading.Thread(target=reader, args=(chunk,), daemon=True).start()

    t0 = time.time()
    ended = n = hits = 0
    while ended < READERS:
        item = q.get()
        if item is None:
            ended += 1
            continue
        path, data = item
        n += 1
        prev = None
        best_sim = -1.0
        n_faces = w = h = 0
        err = None
        if data is None:
            err = "no preview"
        else:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                err = "decode failed"
            else:
                h, w = img.shape[:2]
                img = brighten(img)
                try:
                    faces = app.get(img)
                except Exception as e:
                    faces, err = [], f"detect: {e}"
                rows = []
                for f in faces:
                    x1, y1, x2, y2 = [int(v) for v in f.bbox]
                    fw, fh = x2 - x1, y2 - y1
                    if min(fw, fh) < MIN_FACE:
                        continue
                    sim = float(f.normed_embedding @ centroid)
                    rows.append((path, x1, y1, fw, fh, float(f.det_score), sim))
                    best_sim = max(best_sim, sim)
                n_faces = len(rows)
                if rows:
                    con.executemany(
                        "INSERT INTO faces(path,x,y,w,h,det,sim) VALUES(?,?,?,?,?,?,?)", rows)
                if best_sim >= args.floor:
                    prev = preview_name(path)
                    cv2.imwrite(str(previews / prev), img, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if best_sim >= args.floor:
            hits += 1
        con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)",
                    (path, prev, n_faces, best_sim, w, h, err, time.time()))
        if n % 25 == 0:
            con.commit()
        if n % 200 == 0:
            rate = n / (time.time() - t0)
            eta = (len(todo) - n) / rate / 60
            print(f"{n}/{len(todo)} {rate:.1f}/s hits={hits} ETA {eta:.0f} min", flush=True)
    con.commit()
    print(f"DONE: {n} scanned, {hits} hits in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
