#!/usr/bin/env python3
"""
sync_post_edits.py  —  propagate re-edited Posts/ photos back over their originals
==================================================================================

Workflow:
  /Volumes/RYAN/Edits/**         master edit library (per-trip folders)
  /Volumes/RYAN/Edits/Posts/**   re-edited copies exported for posting

Posts photos get re-edited (usually better) but drift away from their original
location. This finds each original and overwrites it with the Posts version.
Posts files are NEVER deleted or modified.

Matching (robust to Lightroom renames + Sony filename recycling):
  1. PRE-FILTER by "core ID" = filename with edit suffixes stripped
     (-Enhanced-NR/-SR, -Edit, trailing -2/-3 virtual copies). Bridges the AI
     denoise rename in BOTH directions (X-Enhanced-NR.jpg <-> X.jpg).
  2. CONFIRM by EXIF capture identity (DateTimeOriginal + SubSec + Serial).
     Sony recycles filenames across trips, so name alone is unsafe; capture
     identity is what truly pins the shot.
  3. RESOLVE which master file(s) to overwrite among EXIF-confirmed candidates:
       - one basename (maybe duplicated across folders) -> replace all (keeps
         duplicate library locations in sync).
       - several distinct basenames (virtual copies): prefer the exact-name one,
         else AMBIGUOUS -> skip & report (don't guess which edit).

NEW files (no original anywhere) get an inferred home folder via nearest
capture-time clustering (+ filename corroboration); only HIGH-confidence ones
are filed by --move-new.

EXIF is cached to <root>/.sync_cache.json keyed by path+mtime+size, so only
new/changed files are ever re-read.

USAGE
  python3 sync_post_edits.py                 # DRY RUN: replacements by folder + inferred
                                             #   home folders for NEW files (HIGH/MED/NONE)
  python3 sync_post_edits.py --move          # apply replacements AND file HIGH-confidence
                                             #   new edits into their inferred folders
  python3 sync_post_edits.py --move-new      # ONLY file HIGH-confidence new edits
  python3 sync_post_edits.py --move --backup  # snapshot each original before overwrite
"""

import argparse, bisect, collections, datetime, hashlib, json, os, re, shutil, subprocess, sys, time

DEFAULT_ROOT = "/Volumes/RYAN/Edits"
SKIP_TOP = {"Lightroom Cat", "Videos", "Reports", "Reels", "Mixed edits"}  # not trip dirs (NFT lives under Mixed edits)
CACHE_NAME = ".sync_cache.json"
HASH_NAME = ".sync_hashes.json"     # content hashes, keyed by path+size+mtime
CLUSTER_DAYS = 5            # window for capture-time clustering of NEW files
HIGH_CLUSTER = 3           # >=this many neighbours in window -> high confidence

SUFFIX_RE = re.compile(
    r'(-(Enhanced-NR|Enhanced-SR|Enhanced|Edit|NR|SR|HDR|Pano)|-\d+)+$', re.I)
NUM_RE = re.compile(r'^(_?[A-Za-z]+)(\d+)')


def core_id(filename):
    return SUFFIX_RE.sub('', os.path.splitext(filename)[0])


def log(msg):
    """Progress to stderr (keeps stdout report clean if redirected)."""
    print("  … %s" % msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- caches
def load_json(root, name):
    try:
        with open(os.path.join(root, name)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(root, name, data):
    p = os.path.join(root, name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def load_cache(root):
    return load_json(root, CACHE_NAME)


def save_cache(root, cache):
    save_json(root, CACHE_NAME, cache)


def capture_ids(paths, cache):
    """Return {path: (dt|None, sub, ser, model)} using/refreshing the mtime cache.
    Mutates `cache`; caller is responsible for save_cache()."""
    stale = []
    meta = {}
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        meta[p] = (int(st.st_mtime), st.st_size)
        c = cache.get(p)
        if not c or c[0] != meta[p][0] or c[1] != meta[p][1] or len(c) < 6:
            stale.append(p)
    if stale:
        cmd = ["exiftool", "-j", "-n", "-DateTimeOriginal", "-SubSecTimeOriginal",
               "-CreateDate", "-SubSecCreateDate", "-SerialNumber", "-Model"] + stale
        res = subprocess.run(cmd, capture_output=True, text=True)
        try:
            data = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            data = []
        got = {d.get("SourceFile"): d for d in data}
        for p in stale:
            d = got.get(p, {})
            dt = d.get("DateTimeOriginal") or d.get("CreateDate")
            sub = d.get("SubSecTimeOriginal") or d.get("SubSecCreateDate") or ""
            ser = d.get("SerialNumber") or ""
            model = d.get("Model") or ""
            mt, sz = meta[p]
            cache[p] = [mt, sz, str(dt) if dt else None, str(sub), str(ser), str(model)]
    out = {}
    for p in paths:
        c = cache.get(p)
        out[p] = (c[2], c[3], c[4], c[5]) if c and len(c) >= 6 else (None, "", "", "")
    return out


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


# ----------------------------------------------------------------------------- indexing
def index_masters(root, posts):
    idx = collections.defaultdict(list)
    for dp, dn, fn in os.walk(root):
        top = os.path.relpath(dp, root).split(os.sep)[0]
        if top in SKIP_TOP or dp == posts or dp.startswith(posts + os.sep):
            dn[:] = []
            continue
        for f in fn:
            if f.lower().endswith('.jpg'):
                idx[core_id(f)].append(os.path.join(dp, f))
    return idx


def list_posts(posts):
    out = []
    for dp, dn, fn in os.walk(posts):
        for f in fn:
            if f.lower().endswith('.jpg'):
                out.append(os.path.join(dp, f))
    return out


def fmt(root, p):
    return p.replace(root + os.sep, "")


def file_hash(path, hcache):
    """SHA1 of a file, cached by path+size+mtime. Plain reads don't bump mtime on this
    drive, so a file is only read once per version; copies (which bump mtime) self-heal
    on the next run. Mutates hcache; caller saves it."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    size, mt = st.st_size, int(st.st_mtime)
    c = hcache.get(path)
    if c and c[0] == size and c[1] == mt:
        return c[2]
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    digest = h.hexdigest()
    hcache[path] = [size, mt, digest]
    return digest


def export_time(p):
    """When a Posts file was exported. Uses birthtime (creation), which this drive does NOT
    bump on copy, unlike mtime — so it's a stable 'most recent edit' signal. Falls back to
    mtime where birthtime is unavailable/zero."""
    try:
        st = os.stat(p)
    except OSError:
        return 0
    return getattr(st, "st_birthtime", 0) or st.st_mtime


def in_sync(src, tgt, hcache):
    """True if tgt is already byte-identical to src (edit applied on a previous run, or
    the Posts copy was never re-edited). Size is a cheap reject; otherwise compare cached
    content hashes — so repeated runs (e.g. dry-run then --move) don't re-read the drive."""
    try:
        if os.path.getsize(src) != os.path.getsize(tgt):
            return False
    except OSError:
        return False
    hs = file_hash(src, hcache)
    return hs is not None and hs == file_hash(tgt, hcache)


# ----------------------------------------------------------------------------- classify
def classify(root, posts, cache, hcache):
    log("scanning library...")
    idx = index_masters(root, posts)
    posts_files = list_posts(posts)
    need = set(posts_files)
    for pf in posts_files:
        need.update(idx.get(core_id(os.path.basename(pf)), []))
    log("reading capture times for %d files (cached after first run)..." % len(need))
    cap = capture_ids(sorted(need), cache)
    log("comparing %d Posts files against originals (skips already-synced)..." % len(posts_files))

    tod = lambda t: t[0][11:19] if t and t[0] and len(t[0]) >= 19 else None  # HH:MM:SS

    replace, ambiguous, new, clockshift, synced = [], [], [], set(), 0
    claims = collections.defaultdict(list)   # target -> Posts files that map to it
    for pf in posts_files:
        name = os.path.basename(pf)
        cands = idx.get(core_id(name), [])
        if not cands:
            new.append((pf, "no file with this core ID in library"))
            continue
        pid = cap.get(pf)
        if pid and pid[0]:
            matched = [c for c in cands if cap.get(c) == pid]
            if not matched:
                # clock-shift fallback: same core ID + identical time-of-day (to the
                # second) + same camera model, but a shifted date (wrong camera clock
                # fixed on one side only). Time-of-day + model + core ID is collision-safe.
                p_tod, p_model = tod(pid), pid[3]
                if p_tod:
                    matched = [c for c in cands
                               if tod(cap.get(c)) == p_tod and cap.get(c, ("",))[-1] == p_model]
                    if matched:
                        clockshift.add(pf)
        else:
            matched = [c for c in cands if os.path.basename(c) == name]
        if not matched:
            new.append((pf, "core ID exists but no capture-time match (different shot)"))
            continue
        by_name = collections.defaultdict(list)
        for c in matched:
            by_name[os.path.basename(c)].append(c)
        if name in by_name and len(by_name) > 1:
            # exact-name original present alongside OTHER distinct edits:
            # overwrite only the exact match, leave the other edits untouched.
            targets = by_name[name]
        else:
            # one edit (possibly duplicated across folders), OR several variants
            # of the same shot (e.g. -Enhanced-NR + -Enhanced-NR-2): overwrite all.
            targets = matched
        for t in targets:
            claims[t].append(pf)
        # idempotency: skip targets already identical to this Posts file (a previous
        # --move copied this edit onto the original). Re-editing a Posts file changes
        # its bytes, so it correctly re-applies.
        pending = [t for t in targets if not in_sync(pf, t, hcache)]
        if pending:
            replace.append((pf, pending))
        else:
            synced += 1

    # conflicts: one original claimed by >1 DIFFERENT Posts edit (same name/shot, different
    # bytes). Auto-resolve to the most recent export (birthtime, which this drive does NOT
    # bump on copy, unlike mtime); the winner stays/becomes the original, losers are dropped.
    resolved, winner_of = [], {}
    for t, pfs in claims.items():
        if len(pfs) > 1:
            distinct = {}
            for pf in pfs:
                distinct.setdefault(file_hash(pf, hcache), pf)
            if len(distinct) > 1:
                cands = list(distinct.values())
                win = max(cands, key=export_time)
                winner_of[t] = win
                resolved.append((t, win, [c for c in cands if c != win]))
    if winner_of:                          # keep target only for its winning Posts file
        replace = [(pf, [t for t in tg if winner_of.get(t, pf) == pf]) for pf, tg in replace]
        replace = [(pf, tg) for pf, tg in replace if tg]

    masters = [p for paths in idx.values() for p in paths]   # reuse this walk for inference
    return replace, ambiguous, new, clockshift, synced, resolved, masters


# ----------------------------------------------------------------------------- new-file folder inference
def suggest_folders(root, posts, new, cache, masters):
    """For each NEW file infer a home folder via capture-time clustering + filename.
    `masters` is the already-walked master list (shared with classify, avoids a 2nd
    full-tree walk). Returns {posts_path: (folder|None, confidence, detail)}."""
    newpaths = [pf for pf, _ in new]
    # Trust the cache for master timestamps (trip date-ranges don't change), so we avoid
    # re-statting ~11k files every run. Only files never seen before are scanned.
    missing = [m for m in masters if m not in cache]
    if missing:
        log("first-time capture-time scan of %d new library files..." % len(missing))
        capture_ids(missing, cache)
    capnew = capture_ids(newpaths, cache)                 # new files: always fresh (few)

    def mdt(p):                                           # master capture date, from cache
        c = cache.get(p)
        return c[2] if c and len(c) >= 6 else None

    folder_times = collections.defaultdict(list)         # folder -> [datetime]
    counters = collections.defaultdict(list)             # prefix -> [(num, folder)]
    for m in masters:
        folder = os.path.dirname(fmt(root, m))
        dt = parse_dt(mdt(m))
        if dt:
            folder_times[folder].append(dt)
        mm = NUM_RE.match(core_id(os.path.basename(m)))
        if mm:
            counters[mm.group(1)].append((int(mm.group(2)), folder))
    for v in folder_times.values():
        v.sort()
    for v in counters.values():
        v.sort()

    def nearest_name_folder(name):
        mm = NUM_RE.match(core_id(name))
        if not mm:
            return None
        pre, num = mm.group(1), int(mm.group(2))
        lst = counters.get(pre, [])
        if not lst:
            return None
        nums = [x[0] for x in lst]
        i = bisect.bisect_left(nums, num)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(lst):
                d = abs(lst[j][0] - num)
                if best is None or d < best[0]:
                    best = (d, lst[j][1])
        return best  # (delta, folder)

    win = datetime.timedelta(days=CLUSTER_DAYS)
    out = {}
    for pf, _ in new:
        name = os.path.basename(pf)
        dt = parse_dt(capnew.get(pf, (None,))[0])
        time_folder, time_count, min_delta = None, 0, None
        if dt:
            scored = []
            for folder, times in folder_times.items():
                cnt = sum(1 for t in times if abs(t - dt) <= win)
                if cnt:
                    md = min(abs(t - dt) for t in times)
                    scored.append((cnt, -md, folder))
            if scored:
                scored.sort(reverse=True)
                time_count, negmd, time_folder = scored[0]
                min_delta = -negmd
        nn = nearest_name_folder(name)
        name_folder = nn[1] if nn else None
        name_delta = nn[0] if nn else None

        if time_folder and (time_folder == name_folder or time_count >= HIGH_CLUSTER):
            conf = "HIGH"
            folder = time_folder
        elif time_folder:
            conf = "MED"
            folder = time_folder
        else:
            conf = "NONE"
            folder = None
        detail = "time:%s(%dn) name:%s(Δ%s)" % (
            time_folder or "-", time_count,
            name_folder or "-", name_delta if name_delta is not None else "-")
        out[pf] = (folder, conf, detail)
    return out


# ----------------------------------------------------------------------------- reporting
def print_report(root, posts, replace, ambiguous, new, suggestions, mode, clockshift=frozenset(), resolved=()):
    n_targets = sum(len(t) for _, t in replace)
    tcount = {pf: len(t) for pf, t in replace}                       # total targets per source
    dcount = {pf: len({os.path.basename(c) for c in t}) for pf, t in replace}  # distinct edit names
    by_folder = collections.defaultdict(list)
    for pf, targets in replace:
        for tg in targets:
            by_folder[os.path.dirname(fmt(root, tg))].append((pf, tg))

    print("=" * 74)
    print("SYNC POST EDITS  —  %s" % mode)
    print("root :", root)
    print("posts:", posts)
    print("=" * 74)
    print("\n### WILL REPLACE  (%d Posts files -> %d master files)\n" % (len(replace), n_targets))
    for folder in sorted(by_folder):
        print("  [%s]" % folder)
        for pf, tg in sorted(by_folder[folder]):
            pn, tn = os.path.basename(pf), os.path.basename(tg)
            note = "" if pn == tn else "   (Posts %s -> keep name %s)" % (pn, tn)
            if dcount[pf] > 1:
                note += "   [collapse: 1 of %d distinct edits <- same new file]" % dcount[pf]
            elif tcount[pf] > 1:
                note += "   [dup-sync: same file across %d folders]" % tcount[pf]
            if pf in clockshift:
                note += "   [clock-shift match: same time-of-day, wrong-date RAW]"
            print("      %s%s" % (tn, note))
            print("          <= Posts/%s" % fmt(posts, pf))
        print()

    if resolved:
        print("### AUTO-RESOLVED CONFLICTS (one original, >1 edit -> kept most recent export)  [%d]\n"
              % len(resolved))
        for tg, win, losers in resolved:
            print("  original: %s" % fmt(root, tg))
            print("      KEPT (newest): Posts/%s" % fmt(posts, win))
            for pf in losers:
                print("      dropped:       Posts/%s" % fmt(posts, pf))
            print()

    print("### NEW / NO ORIGINAL  [%d]\n" % len(new))
    buckets = {"HIGH": [], "MED": [], "NONE": []}
    for pf, _ in new:
        folder, conf, detail = suggestions[pf]
        buckets[conf].append((pf, folder, detail))
    for conf in ("HIGH", "MED", "NONE"):
        if not buckets[conf]:
            continue
        label = {"HIGH": "HIGH confidence (will be filed by --move / --move-new)",
                 "MED": "MEDIUM confidence (review before filing)",
                 "NONE": "NO confident match (file manually)"}[conf]
        print("  -- %s [%d]" % (label, len(buckets[conf])))
        for pf, folder, detail in buckets[conf]:
            dest = "-> [%s]" % folder if folder else "-> (?)"
            print("     Posts/%-42s %s" % (fmt(posts, pf), dest))
            print("           %s" % detail)
        print()


def print_next_actions(replace, ambiguous, new, suggestions, args, synced=0, resolved=()):
    hi = sum(1 for pf, _ in new if suggestions[pf][1] == "HIGH")
    print("=" * 74)
    print("SUMMARY: %d to replace  |  %d synced  |  %d new (%d high-conf)%s"
          % (len(replace), synced, len(new), hi,
             ("  |  %d auto-resolved" % len(resolved)) if resolved else ""))
    print("=" * 74)
    if not (args.move or args.move_new):
        print("\nNEXT ACTIONS:")
        print("  --move          overwrite the %d matched originals AND file the %d HIGH-confidence"
              % (len(replace), hi))
        print("                  new edits into their inferred folders (Posts kept; --backup snapshots)")
        print("  --move-new      ONLY file the %d high-confidence new edits (no replacements)" % hi)
        print("  --backup        with --move: snapshot each original before overwriting")
        print("\n  Non-high new files are never touched automatically.")
    print()


# ----------------------------------------------------------------------------- apply
def backup_dir(root, args):
    if not args.backup:
        return None
    d = os.path.join(root, ".sync_backups", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    print("Backing up originals to:", d)
    return d


def open_log(root, tag):
    d = os.path.join(root, ".sync_backups")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s-%s.tsv" % (tag, time.strftime("%Y%m%d-%H%M%S")))
    return open(p, "w"), p


def do_move(root, replace, args):
    bdir = backup_dir(root, args)
    logf, lp = open_log(root, "replace")
    logf.write("source_post\toverwritten_master\n")
    total = sum(len(t) for _, t in replace)
    log("overwriting %d originals%s..." % (total, " (with backup)" if bdir else ""))
    n = 0
    for pf, targets in replace:
        for tg in targets:
            if bdir:
                shutil.copy2(tg, os.path.join(bdir, fmt(root, tg).replace(os.sep, "__")))
            shutil.copy2(pf, tg)
            logf.write("%s\t%s\n" % (pf, tg))
            n += 1
            if n % 25 == 0:
                log("  ...%d/%d" % (n, total))
    logf.close()
    print("\nReplaced %d master files. Log: %s" % (n, lp))


def do_move_new(root, new, suggestions, args):
    logf, lp = open_log(root, "move-new")
    logf.write("source_post\tcopied_to\n")
    hi = sum(1 for pf, _ in new if suggestions[pf][1] == "HIGH" and suggestions[pf][0])
    log("filing %d high-confidence new edits into their folders..." % hi)
    n = 0
    for pf, _ in new:
        folder, conf, _ = suggestions[pf]
        if conf != "HIGH" or not folder:
            continue
        dest_dir = os.path.join(root, folder)
        if not os.path.isdir(dest_dir):
            print("  skip (folder missing): %s  [%s]" % (folder, os.path.basename(pf)))
            continue
        dest = os.path.join(dest_dir, os.path.basename(pf))
        if os.path.exists(dest):
            print("  skip (exists): %s" % fmt(root, dest))
            continue
        shutil.copy2(pf, dest)            # copy in; Posts copy preserved
        logf.write("%s\t%s\n" % (pf, dest))
        n += 1
    logf.close()
    print("\nFiled %d new edits into their folders. Log: %s" % (n, lp))


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Sync re-edited Posts/ photos over their originals.")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--posts", default=None)
    ap.add_argument("--move", action="store_true", help="overwrite the matched originals")
    ap.add_argument("--move-new", dest="move_new", action="store_true",
                    help="file HIGH-confidence new edits into their inferred folders")
    ap.add_argument("--backup", action="store_true", help="snapshot originals before overwrite")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    posts = os.path.abspath(args.posts) if args.posts else os.path.join(root, "Posts")
    cache = load_cache(root)
    hcache = load_json(root, HASH_NAME)
    t0 = time.time()

    replace, ambiguous, new, clockshift, synced, resolved, masters = classify(root, posts, cache, hcache)

    # new-file folder inference (skipped entirely when nothing is new); --move uses it
    if new:
        log("inferring home folders for %d new files..." % len(new))
        suggestions = suggest_folders(root, posts, new, cache, masters)
    else:
        suggestions = {}

    save_cache(root, cache)
    save_json(root, HASH_NAME, hcache)

    mode = "APPLY (--move)" if args.move else ("APPLY (--move-new)" if args.move_new else "DRY RUN")
    print_report(root, posts, replace, ambiguous, new, suggestions, mode, clockshift, resolved)
    print_next_actions(replace, ambiguous, new, suggestions, args, synced, resolved)
    print("(%.1fs)\n" % (time.time() - t0))

    if args.move:
        do_move(root, replace, args)
    if args.move or args.move_new:
        do_move_new(root, new, suggestions, args)


if __name__ == "__main__":
    main()
