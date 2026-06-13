#!/usr/bin/env python3
"""
cleanup_dup_edits.py  —  audit & clean duplicate edit files in the Edits library

When a trip's photos get re-edited (sometimes years later) the new export lands in the
same trip folder as the original, and Lightroom appends a suffix to avoid a name clash
(-2, -3, -Enhanced-NR, -Enhanced-NR-2, -Edit, ...). The result is two+ files of the SAME
shot side by side — wasted space, and the map shows the photo twice.

This scans the library, then opens an interactive report in your browser (served from a
local web server). For each duplicate set you see the photos side by side; you can:
  • click any thumbnail for a full-size lightbox (← / → arrow keys to flip through all)
  • choose which copy(ies) to keep: "keep older" checkbox (2-copy sets) or per-photo keep
    checkboxes (3+ copies — you can keep more than one); or press K in the lightbox. Default
    keeper = the newest edit; everything not kept is dropped.
  • click "Clean" on a row to quarantine just that set
  • click "Clean folder" to quarantine every set in a folder (honouring each row's choice)
Quarantined files are MOVED to <root>/.dup_trash (reversible), never hard-deleted.

Detection:
  1. Group files in the SAME folder by "core ID" = filename with edit suffixes stripped
     (reuses sync_post_edits.core_id, e.g. RM17972-Enhanced-NR.jpg -> RM17972).
  2. CONFIRM a group is the same shot by EXIF capture identity
     (DateTimeOriginal + SubSec + SerialNumber + Model). A group whose members have
     DIFFERENT capture times (Sony filename recycling / counter rollover) is NOT a
     duplicate -> listed under REVIEW, never cleaned.
Default keeper = the NEWEST edit (EXIF ModifyDate; tie-break birthtime -> size -> path);
in the UI you can pick any copy in a set as the keeper instead.

EXIF + sizes are cached to <root>/.dup_cache.json (keyed by path+mtime+size).

USAGE
  python3 cleanup_dup_edits.py                 # whole library
  python3 cleanup_dup_edits.py --folder "2019 NYC"   # one trip folder
  python3 cleanup_dup_edits.py --port 8800     # use a different local port
"""

import argparse
import collections
import datetime
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from sync_post_edits import (
    DEFAULT_ROOT, core_id, export_time, fmt, load_json, log, parse_dt, save_json,
)

CACHE_NAME = ".dup_cache.json"      # EXIF + size, keyed by path -> [mtime, size, dto, sub, ser, model, modify]
TRASH_DIR = ".dup_trash"
DEFAULT_PORT = 8765

# Top-level folders that are not trips (aggregations, exports, staging) — never scanned.
# ("UK" is a local-only aggregation not referenced by any trip in trips.json, but it's still
# worth deduping, so it is NOT excluded — cleaning it just doesn't affect any manifest/R2.)
EXCLUDE_TOP = {"Lightroom Cat", "Mixed edits", "Posts", "Reels", "Reports", "Videos"}
SKIP_DIRS = {"Posts", "Compressed"}  # never descend into these anywhere deeper in the tree
IMG_EXTS = {".jpg"}                  # all collision groups are same-ext jpg in practice


# ----------------------------------------------------------------------------- EXIF cache
def read_meta(paths, cache):
    """Return {path: dict(dto, sub, ser, model, modify, size)} for each path, refreshing the
    cache for any new/changed file. One exiftool call for all stale paths. Mutates `cache`;
    caller saves it."""
    meta_stat = {}
    stale = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        meta_stat[p] = (int(st.st_mtime), st.st_size)
        c = cache.get(p)
        if not c or c[0] != meta_stat[p][0] or c[1] != meta_stat[p][1] or len(c) < 7:
            stale.append(p)
    if stale:
        log("reading EXIF for %d new/changed files..." % len(stale))
        cmd = ["exiftool", "-j", "-n", "-DateTimeOriginal", "-SubSecTimeOriginal",
               "-CreateDate", "-SubSecCreateDate", "-SerialNumber", "-Model",
               "-ModifyDate"] + stale
        res = subprocess.run(cmd, capture_output=True, text=True)
        try:
            data = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            data = []
        got = {d.get("SourceFile"): d for d in data}
        for p in stale:
            d = got.get(p, {})
            dto = d.get("DateTimeOriginal") or d.get("CreateDate")
            sub = d.get("SubSecTimeOriginal") or d.get("SubSecCreateDate") or ""
            ser = d.get("SerialNumber") or ""
            model = d.get("Model") or ""
            modify = d.get("ModifyDate") or ""
            mt, sz = meta_stat[p]
            cache[p] = [mt, sz, str(dto) if dto else None, str(sub), str(ser),
                        str(model), str(modify)]
    out = {}
    for p in paths:
        c = cache.get(p)
        if c and len(c) >= 7:
            out[p] = {"dto": c[2], "sub": c[3], "ser": c[4], "model": c[5],
                      "modify": c[6], "size": meta_stat.get(p, (0, c[1]))[1]}
        else:
            out[p] = {"dto": None, "sub": "", "ser": "", "model": "",
                      "modify": "", "size": meta_stat.get(p, (0, 0))[1]}
    return out


def capture_key(m):
    """Identity of the underlying shot. None when there's no capture time (can't confirm)."""
    if not m["dto"]:
        return None
    return (m["dto"], m["sub"], m["ser"], m["model"])


def keeper_sort_key(p, m):
    """Higher = preferred (keep). Newest edit first: ModifyDate, then birthtime, then size."""
    md = parse_dt(m["modify"])
    return (md or datetime.datetime.min, export_time(p), m["size"], p)


# ----------------------------------------------------------------------------- scan
def find_groups(root, only_folder):
    """Walk the library; yield (dirpath, [paths]) for every same-folder core-ID group of
    size >= 2."""
    for dp, dn, fn in os.walk(root):
        rel = os.path.relpath(dp, root)
        top = rel.split(os.sep)[0] if rel != "." else ""
        if top in EXCLUDE_TOP or top.startswith("."):
            dn[:] = []
            continue
        if only_folder and rel != "." and top != only_folder:
            dn[:] = []
            continue
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
        by_core = collections.defaultdict(list)
        for f in fn:
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() not in IMG_EXTS:
                continue
            by_core[core_id(f)].append(os.path.join(dp, f))
        for paths in by_core.values():
            if len(paths) >= 2:
                yield dp, sorted(paths)


def classify(root, only_folder, cache):
    """Return (dup_sets, review, meta) where each dup_set is (keeper, [drops]) ordered
    newest->oldest, and review is a list of (dirpath, [paths], reason)."""
    groups = list(find_groups(root, only_folder))
    all_paths = sorted({p for _, ps in groups for p in ps})
    log("found %d candidate groups (%d files); confirming by EXIF..."
        % (len(groups), len(all_paths)))
    meta = read_meta(all_paths, cache)

    dup_sets, review = [], []
    for dp, paths in groups:
        by_key = collections.defaultdict(list)
        for p in paths:
            by_key[capture_key(meta[p])].append(p)

        confirmed_any = False
        for key, members in by_key.items():
            if key is None or len(members) < 2:
                continue
            confirmed_any = True
            ordered = sorted(members, key=lambda p: keeper_sort_key(p, meta[p]), reverse=True)
            dup_sets.append((ordered[0], ordered[1:]))

        if len(by_key) > 1 or any(k is None for k in by_key) or not confirmed_any:
            unconfirmed = [p for k, ms in by_key.items() for p in ms
                           if k is None or len(ms) < 2]
            if len(unconfirmed) >= 2 or (unconfirmed and confirmed_any):
                review.append((dp, sorted(unconfirmed), "same core ID, different/unknown capture time"))
    return dup_sets, review, meta


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit)
        n /= 1024


# ----------------------------------------------------------------------------- data model
def build_data(root, dup_sets, review, meta):
    """Build the JSON the UI renders + a server-side {set_id: [abs paths newest->oldest]}
    map used to perform cleans."""
    by_dir = collections.defaultdict(list)
    for keeper, drops in dup_sets:
        by_dir[os.path.dirname(keeper)].append([keeper] + list(drops))

    def img(p):
        m = meta[p]
        return {"url": quote(os.path.relpath(p, root)), "name": os.path.basename(p),
                "modify": m["modify"] or "?", "size": human(m["size"])}

    sets_map, folders, sid = {}, [], 0
    n_drops = 0
    for dp in sorted(by_dir):
        sets_json = []
        for paths in sorted(by_dir[dp]):
            key = "s%d" % sid
            sid += 1
            sets_map[key] = paths
            n_drops += len(paths) - 1
            sets_json.append({"id": key, "images": [img(p) for p in paths]})
        folders.append({"name": fmt(root, dp), "sets": sets_json})

    # Review groups are also registered in sets_map so they can be cleaned from the UI on
    # demand (default in the UI = keep all, so they stay untouched unless the user chooses).
    n_dup = len(sets_map)
    review_json = []
    for dp, paths, reason in sorted(review):
        key = "s%d" % sid
        sid += 1
        sets_map[key] = paths
        review_json.append({"id": key, "name": fmt(root, dp), "reason": reason,
                            "images": [img(p) for p in paths]})

    data = {"root": root, "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "folders": folders, "review": review_json,
            "n_sets": n_dup, "n_drops": n_drops}
    return data, sets_map


# ----------------------------------------------------------------------------- clean
def quarantine(root, dst_base, src):
    """Move src into dst_base mirroring its path relative to root. Returns the new path."""
    rel = os.path.relpath(src, root)
    dst = os.path.join(dst_base, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        stem, ext = os.path.splitext(dst)
        i = 1
        while os.path.exists("%s.%d%s" % (stem, i, ext)):
            i += 1
        dst = "%s.%d%s" % (stem, i, ext)
    shutil.move(src, dst)
    return dst


def clean_sets(ctx, items):
    """items: [{id, keep}]. `keep` is a list of indices (into the set's newest->oldest list)
    of the copies to KEEP (default [0] = newest); you may keep more than one. Quarantine the
    rest and drop the set from the live map. Returns a result dict for the UI."""
    root, sets_map, logpath = ctx["root"], ctx["sets_map"], ctx["logpath"]
    trash = os.path.join(root, TRASH_DIR)
    os.makedirs(trash, exist_ok=True)
    removed, errors, freed = [], [], 0
    new = not os.path.exists(logpath)
    with open(logpath, "a") as lf:
        if new:
            lf.write("# cleanup_dup_edits session %s  mode=QUARANTINE\n"
                     % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        for it in items:
            sid = it.get("id")
            paths = sets_map.get(sid)
            if not paths:
                errors.append({"id": sid, "msg": "already cleaned"})
                continue
            ki = it.get("keep", [0])
            if isinstance(ki, int):
                ki = [ki]
            keep_idx = {i for i in ki if isinstance(i, int) and 0 <= i < len(paths)} or {0}
            keepers = [paths[i] for i in sorted(keep_idx)]
            drops = [p for i, p in enumerate(paths) if i not in keep_idx]
            if not drops:
                continue  # every copy kept — nothing to quarantine; leave the set in place
            keep_label = ", ".join(os.path.basename(k) for k in keepers)
            ok = True
            for d in drops:
                try:
                    sz = os.path.getsize(d)
                    dst = quarantine(root, trash, d)
                    lf.write("MOVE\t%s\t->\t%s\tkeep=%s\n"
                             % (fmt(root, d), fmt(root, dst), keep_label))
                    freed += sz
                except OSError as e:
                    ok = False
                    errors.append({"id": sid, "msg": "%s: %s" % (os.path.basename(d), e)})
                    lf.write("ERROR\t%s\t%s\n" % (fmt(root, d), e))
            if ok:
                del sets_map[sid]
                removed.append(sid)
    return {"removed": removed, "errors": errors, "freed_h": human(freed),
            "remaining": len(sets_map)}


# ----------------------------------------------------------------------------- web UI
PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Duplicate edits</title>
<style>
*{box-sizing:border-box}
body{font:14px -apple-system,Helvetica,Arial,sans-serif;margin:1.5rem;background:#161616;color:#ddd}
h1{font-size:1.4rem;margin:0 0 .3rem}h2{margin-top:2rem;border-bottom:1px solid #333;padding-bottom:.3rem}
h3{font-size:1rem;margin:0}code{background:#2a2a2a;padding:.1rem .3rem;border-radius:3px}
.muted{color:#888}.sum{font-weight:700;margin:.4rem 0 1rem}
.fhead{display:flex;align-items:center;gap:1rem;margin:1.8rem 0 .3rem}
.ftoggle{cursor:pointer;user-select:none;display:flex;align-items:center;gap:.45rem}
.caret{display:inline-block;transition:transform .12s;font-size:.8rem;color:#888}
.folder.collapsed .caret{transform:rotate(-90deg)}
.folder.collapsed .sets,.folder.collapsed .cards{display:none}
.set{display:flex;align-items:flex-start;gap:1rem;padding:1rem 0;border-bottom:1px solid #2c2c2c}
.set .cards{flex:1}
.cards{display:flex;flex-wrap:wrap;gap:1rem}
.card{position:relative;width:240px}
.card img{width:240px;height:160px;object-fit:cover;border-radius:6px;border:3px solid #333;display:block;background:#222;cursor:zoom-in}
.card.keep img{border-color:#2ea043}.card.drop img{border-color:#d23b4a;opacity:.8}
.badge{position:absolute;top:7px;left:7px;font-size:.68rem;font-weight:700;padding:.12rem .45rem;border-radius:4px;color:#fff}
.card.keep .badge{background:#2ea043}.card.drop .badge{background:#d23b4a}
.keepsel{position:absolute;top:6px;right:6px;display:flex;gap:.3rem;align-items:center;cursor:pointer;
 background:rgba(0,0,0,.62);color:#fff;font-size:.72rem;font-weight:600;padding:.15rem .45rem;border-radius:5px}
.keepsel input{cursor:pointer;margin:0}
.cap{font-size:.78rem;margin-top:.25rem;line-height:1.3}.cap .name{font-weight:600;word-break:break-all}
.ctl{display:flex;flex-direction:column;gap:.55rem;min-width:130px}
.btn{cursor:pointer;border:1px solid #555;background:#2a2a2a;color:#eee;padding:.4rem .7rem;border-radius:6px;font-size:.85rem}
.btn:hover{background:#3a3a3a}.cleanfolder{background:#3a2b2b;border-color:#7a4a4a}
.inv{font-size:.82rem;display:flex;gap:.4rem;align-items:center;color:#ccc;cursor:pointer}
.rnote{font-size:.74rem;color:#999;max-width:150px}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.93);display:flex;align-items:center;justify-content:center;z-index:100}
#lb.hidden{display:none}#lbimg{max-width:92vw;max-height:86vh;object-fit:contain}
.pill{color:#fff;padding:.15rem .55rem;border-radius:5px;font-weight:700;margin-right:.6rem;font-size:.85rem}
.pill.kp{background:#2ea043}.pill.dp{background:#d23b4a}
.nav,#lbclose{position:absolute;color:#fff;cursor:pointer;user-select:none;text-shadow:0 0 6px #000}
#lbprev{left:2vw}#lbnext{right:2vw}.nav{font-size:4rem;top:50%;transform:translateY(-50%)}
#lbclose{top:1rem;right:1.6rem;font-size:2.4rem}
#lbcap{position:absolute;bottom:1.1rem;width:100%;text-align:center;color:#ddd;font-size:.9rem}
#toast{position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#2ea043;color:#fff;
 padding:.6rem 1rem;border-radius:8px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:200}
#toast.show{opacity:1}
</style></head><body>
<div id=app></div>
<div id=lb class=hidden>
 <span id=lbclose onclick="closeLB()">&times;</span>
 <span id=lbprev class=nav onclick="moveLB(-1)">&#10094;</span>
 <img id=lbimg>
 <span id=lbnext class=nav onclick="moveLB(1)">&#10095;</span>
 <div id=lbcap></div>
</div>
<div id=toast></div>
<script>
const DATA = __DATA__;
const STATE = {};                       // set id -> keeper index (into newest->oldest list)

function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function card(im,sid,idx,showChk,checked){
  const sel=showChk?`<label class=keepsel title="keep this copy (you can keep more than one)"><input type=checkbox class=keepchk data-idx="${idx}"${checked?' checked':''}> keep</label>`:'';
  return `<figure class=card data-name="${esc(im.name)}" data-meta="${esc(im.modify+' · '+im.size)}">
  <span class=badge></span>
  <img class=thumb loading=lazy decoding=async src="${im.url}">
  ${sel}
  <figcaption class=cap><div class=name>${esc(im.name)}</div><div class=muted>${esc(im.modify)} · ${esc(im.size)}</div></figcaption>
 </figure>`;}
function setRow(s){STATE[s.id]=new Set([0]);const multi=s.images.length>2;
  const ctl=(multi?'':'<label class=inv><input type=checkbox class=invbox> keep older</label>')
            +'<button class="btn clean">Clean &#9656;</button>';
  return `<div class=set data-id="${s.id}">
  <div class=cards>${s.images.map((im,i)=>card(im,s.id,i,multi,i===0)).join('')}</div>
  <div class=ctl>${ctl}</div></div>`;}
function reviewRow(r){STATE[r.id]=new Set(r.images.map((_,i)=>i));   // default: keep ALL
  return `<div class="set review" data-id="${r.id}">
  <div class=cards>${r.images.map((im,i)=>card(im,r.id,i,true,true)).join('')}</div>
  <div class=ctl><div class=rnote>${esc(r.reason)}</div><button class="btn clean">Clean &#9656;</button></div>
  </div>`;}

function render(){
  let h=`<h1>Duplicate edits</h1><p class=muted>Root: <code>${esc(DATA.root)}</code> · ${esc(DATA.generated)} · click a photo to zoom (&larr;/&rarr; to flip) · choose which copy(ies) to keep with the "keep older" box (2 copies) or the keep checkboxes on each photo (3+, keep one or more), or press K in zoom</p>`;
  h+=`<p class=sum id=summary></p>`;
  DATA.folders.forEach(f=>{
    h+=`<section class=folder><div class=fhead>
      <h3 class=ftoggle><span class=caret>&#9662;</span>${esc(f.name)}</h3>
      <button class="btn cleanfolder">Clean folder (${f.sets.length})</button></div>
      <div class=sets>${f.sets.map(setRow).join('')}</div></section>`;
  });
  if(DATA.review.length){
    h+=`<h2>Review <span class=muted>— same name, different/unknown capture time · default keeps all, untick a copy to drop it</span></h2>`;
    DATA.review.forEach(r=>{h+=`<section class=folder><div class=fhead>
      <h3 class=ftoggle><span class=caret>&#9662;</span>${esc(r.name)}</h3></div>
      <div class=sets>${reviewRow(r)}</div></section>`;});
  }
  document.getElementById('app').innerHTML=h;
  document.querySelectorAll('.set').forEach(updateBadges);
  updateSummary();
}
function keepSet(id,n){let k=STATE[id];if(!(k instanceof Set)||!k.size){k=new Set([0]);STATE[id]=k;}return k;}
function updateBadges(setEl){
  const cards=[...setEl.querySelectorAll('.card')];
  const keep=keepSet(setEl.dataset.id,cards.length);
  cards.forEach((c,i)=>{const k=keep.has(i);c.classList.toggle('keep',k);c.classList.toggle('drop',!k);
    c.querySelector('.badge').textContent=k?'KEEP':'DROP';});
  // keep the controls in sync (e.g. when toggled via the lightbox K key)
  setEl.querySelectorAll('.keepchk').forEach(chk=>{chk.checked=keep.has(parseInt(chk.dataset.idx));});
  const inv=setEl.querySelector('.invbox'); if(inv) inv.checked=keep.has(cards.length-1);
}
function toggleKeeper(card){const s=card.closest('.set');if(!s)return;
  const cards=[...s.querySelectorAll('.card')],i=cards.indexOf(card),keep=keepSet(s.dataset.id,cards.length);
  if(cards.length<=2){STATE[s.dataset.id]=new Set([i]);}        // 2-copy: single keeper
  else if(keep.has(i)){if(keep.size>1)keep.delete(i);}          // never remove the last kept
  else keep.add(i);
  updateBadges(s);updateSummary();}
function updateSummary(){
  const dupSets=document.querySelectorAll('.set:not(.review)');
  let drops=0;document.querySelectorAll('.set').forEach(s=>drops+=s.querySelectorAll('.card.drop').length);
  document.getElementById('summary').textContent=`${dupSets.length} duplicate sets, ${drops} files to drop`;
  document.querySelectorAll('.folder').forEach(sec=>{
    const b=sec.querySelector('.cleanfolder');const n=sec.querySelectorAll('.set:not(.review)').length;
    if(b)b.textContent=`Clean folder (${n})`;});
}

document.addEventListener('change',e=>{
  const s=e.target.closest('.set'); if(!s)return;
  if(e.target.classList.contains('invbox')){
    STATE[s.dataset.id]=new Set([e.target.checked?1:0]);updateBadges(s);updateSummary();
  }else if(e.target.classList.contains('keepchk')){
    const idx=parseInt(e.target.dataset.idx)||0,keep=keepSet(s.dataset.id);
    if(e.target.checked)keep.add(idx);
    else{keep.delete(idx);if(!keep.size){keep.add(idx);toast('keep at least one copy');}}
    updateBadges(s);updateSummary();
  }
});

document.addEventListener('click',async e=>{
  const ftog=e.target.closest('.ftoggle');
  if(ftog){ftog.closest('.folder').classList.toggle('collapsed');return;}
  if(e.target.classList.contains('thumb')){openLB(e.target);return;}
  const cleanBtn=e.target.closest('.clean');
  if(cleanBtn){const s=cleanBtn.closest('.set');
    await doClean([{id:s.dataset.id,keep:[...keepSet(s.dataset.id)]}],[s]);return;}
  const fBtn=e.target.closest('.cleanfolder');
  if(fBtn){const sec=fBtn.closest('.folder');const sets=[...sec.querySelectorAll('.set')];
    if(!sets.length)return;
    if(!confirm(`Quarantine the non-kept copies in ${sets.length} set(s) in this folder?`))return;
    await doClean(sets.map(s=>({id:s.dataset.id,keep:[...keepSet(s.dataset.id)]})),sets,sec);return;}
});

async function doClean(items,setEls,sectionEl){
  try{
    const r=await fetch('/api/clean',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})});
    const j=await r.json();const removed=new Set(j.removed);
    setEls.forEach(s=>{if(removed.has(s.dataset.id)){const sec=s.closest('.folder');s.remove();if(sec&&!sec.querySelector('.set'))sec.remove();}});
    if(sectionEl&&!sectionEl.querySelector('.set'))sectionEl.remove();
    updateSummary();
    if(j.errors&&j.errors.length)alert('Some files could not be moved:\n'+j.errors.map(x=>x.msg).join('\n'));
    toast(removed.size?`Quarantined — freed ${j.freed_h}`:'Nothing dropped — untick a copy to drop it');
  }catch(err){alert('Clean failed: '+err);}
}

let LB=[],LBi=0;
function openLB(img){LB=[...document.querySelectorAll('.thumb')];LBi=LB.indexOf(img);showLB();
  document.getElementById('lb').classList.remove('hidden');}
function showLB(){const t=LB[LBi],c=t.closest('.card'),img=document.getElementById('lbimg');img.src=t.src;
  const k=c.classList.contains('keep'),d=c.classList.contains('drop');
  const pill=k?'<span class="pill kp">KEEP</span>':d?'<span class="pill dp">DROP</span>':'';
  document.getElementById('lbcap').innerHTML=`${pill}${esc(c.dataset.name)} — ${esc(c.dataset.meta)}   (${LBi+1}/${LB.length})`;}
function moveLB(d){if(LB.length){LBi=(LBi+d+LB.length)%LB.length;showLB();}}
function closeLB(){document.getElementById('lb').classList.add('hidden');}
document.getElementById('lb').addEventListener('click',e=>{if(e.target.id==='lb')closeLB();});
document.addEventListener('keydown',e=>{if(document.getElementById('lb').classList.contains('hidden'))return;
  if(e.key==='ArrowRight')moveLB(1);else if(e.key==='ArrowLeft')moveLB(-1);else if(e.key==='Escape')closeLB();
  else if(e.key==='k'||e.key==='K'){toggleKeeper(LB[LBi].closest('.card'));showLB();}});

let toastT;function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');
  clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('show'),2500);}

render();
</script></body></html>"""


def make_handler(ctx):
    page = PAGE.replace("__DATA__", json.dumps(ctx["data"]).replace("</", "<\\/"))
    root_abs = os.path.abspath(ctx["root"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # quiet

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            # otherwise serve a file under the Edits root (the photos)
            full = os.path.normpath(os.path.join(root_abs, unquote(path).lstrip("/")))
            if not full.startswith(root_abs) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, data, ctype)

        def do_POST(self):
            if urlparse(self.path).path != "/api/clean":
                return self._send(404, b"not found", "text/plain")
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"error": "bad json"}))
            result = clean_sets(ctx, body.get("items", []))
            return self._send(200, json.dumps(result))

    return Handler


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Interactive duplicate-edit auditor (opens a browser UI).")
    ap.add_argument("--folder", help="Limit to one top-level trip folder (optional).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="Local server port (default %d)." % DEFAULT_PORT)
    args = ap.parse_args()

    root = DEFAULT_ROOT
    if not os.path.isdir(root):
        sys.exit("Edits root not found: %s (is the drive mounted?)" % root)

    t0 = time.time()
    cache = load_json(root, CACHE_NAME)
    dup_sets, review, meta = classify(root, args.folder, cache)
    save_json(root, CACHE_NAME, cache)
    data, sets_map = build_data(root, dup_sets, review, meta)
    print("%d duplicate sets, %d files to drop, %d groups to review  (%.1fs)"
          % (data["n_sets"], data["n_drops"], len(review), time.time() - t0))

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx = {"root": root, "data": data, "sets_map": sets_map,
           "logpath": os.path.join(root, TRASH_DIR, "cleanup-%s.log" % ts)}

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(ctx))
    url = "http://127.0.0.1:%d/" % args.port
    print("Serving UI at %s   (Ctrl-C to stop)" % url)
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
