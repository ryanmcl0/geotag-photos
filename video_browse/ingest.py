#!/usr/bin/env python3
"""Video library ingest for the local video browser.

Two phases, both incremental:

  python3 video_browse/ingest.py            # scan + probe metadata -> local_videos/index.json
  python3 video_browse/ingest.py --proxies  # generate 480p proxies + posters + filmstrips

Trips come from config/video_browse.json. Probe results are cached by
(path, size, mtime) so re-runs only touch new/changed files. Proxies are
written atomically (tmp + rename) so an interrupted run just resumes.
"""
import argparse
import bisect
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / 'config' / 'video_browse.json'
LIB = ROOT / 'local_videos'


def _media_dir():
    # Proxy/poster/filmstrip storage. config media_dir may point at the NAS or
    # an external SSD; defaults to local_videos/media.
    try:
        d = json.loads(CONFIG_PATH.read_text()).get('media_dir')
        if d:
            return Path(d)
    except (OSError, json.JSONDecodeError):
        pass
    return LIB / 'media'


MEDIA = _media_dir()
VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.mts', '.avi', '.insv', '.360'}

# Proxy target: long side <= 854, short side <= 480 (i.e. 480p-class either orientation)
PROXY_LONG, PROXY_SHORT = 854, 480
PROXY_VBITRATE = '1400k'
PROXY_MAXFPS = 30.0
STRIP_FRAMES = 10
STRIP_TILE_W, STRIP_TILE_H = 214, 120

DAY_RE = re.compile(r'^Day (\d+)\s*(?:\[([^\]]+)\])?\s*[:\-]?\s*(.*)$')
ISO6709_RE = re.compile(r'([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)')
DAY_ITINERARY = re.compile(r'^\s*day\s*\d', re.I)

# Climbed-building rosters, the same ones the Urbex page is built from. A clip's
# folder (the raws-style dir under the trip root) is matched against them exactly
# as build_collections.facet_roofs matches a photo's `building` field.
ROOF_ROSTERS = ('config/china_roofs.json', 'config/world_roofs.json')


def load_config():
    return json.loads(CONFIG_PATH.read_text())


def clip_id(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:16]


# ── scan ─────────────────────────────────────────────────────────────────────

def scan_trip(trip):
    base = Path(trip['path'])
    excl = set(trip.get('exclude', []))
    files = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in excl]
        for fn in filenames:
            if fn.startswith('.'):
                continue
            if Path(fn).suffix.lower() in VIDEO_EXTS:
                files.append(Path(dirpath) / fn)
    return sorted(files)


# ── probe ────────────────────────────────────────────────────────────────────

def ffprobe(path: Path):
    out = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', str(path)],
        capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or not out.stdout:
        return None
    d = json.loads(out.stdout)
    v = next((s for s in d.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not v:
        return None
    fmt = d.get('format', {})
    tags = {k.lower(): val for k, val in (fmt.get('tags') or {}).items()}
    vtags = {k.lower(): val for k, val in (v.get('tags') or {}).items()}
    rotation = 0
    for sd in v.get('side_data_list', []) or []:
        if 'rotation' in sd:
            rotation = int(sd['rotation'])
    try:
        num, den = v.get('r_frame_rate', '0/1').split('/')
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        fps = 0.0
    has_audio = any(s.get('codec_type') == 'audio' for s in d.get('streams', []))
    return {
        'duration': float(fmt.get('duration') or 0),
        'size': int(fmt.get('size') or 0),
        'width': int(v.get('width') or 0),
        'height': int(v.get('height') or 0),
        'rotation': rotation,
        'fps': round(fps, 3),
        'codec': v.get('codec_name'),
        'transfer': v.get('color_transfer'),
        'has_audio': has_audio,
        'creation_time': tags.get('creation_time') or vtags.get('creation_time'),
        'model': (tags.get('encoder') or tags.get('com.apple.quicktime.model')
                  or vtags.get('encoder') or ''),
        'apple_date': tags.get('com.apple.quicktime.creationdate'),
        'iso6709': tags.get('com.apple.quicktime.location.iso6709'),
    }


# ── classification ───────────────────────────────────────────────────────────

DRONE_PAT = re.compile(r'mini|mavic|air\s?[23s]|avata|fpv|phantom|inspire|neo', re.I)
ACTION_PAT = re.compile(r'osmo\s?action|osmoaction|hero|gopro|osmo\s?pocket|osmopocket|pocket', re.I)
P360_PAT = re.compile(r'insta\s?360|one\s?x|theta', re.I)
APPLE_PAT = re.compile(r'iphone|apple', re.I)
SONY_CLIP_PAT = re.compile(r'^C\d{4}', re.I)

DEVICE_LABELS = {'drone': 'Drone', 'camera': 'Camera', 'action': 'Action cam',
                 '360': '360', 'phone': 'Phone', 'other': 'Other'}


def classify(path: Path, meta):
    ext = path.suffix.lower()
    model = meta.get('model') or ''
    if ext in ('.insv', '.360') or P360_PAT.search(model):
        return '360'
    if DRONE_PAT.search(model):
        return 'drone'
    if ACTION_PAT.search(model):
        return 'action'
    if APPLE_PAT.search(model) or meta.get('apple_date'):
        return 'phone'
    if SONY_CLIP_PAT.match(path.name) or 'sony' in model.lower():
        return 'camera'
    parts = {p.lower() for p in path.parts}
    if 'drone' in parts:
        return 'drone'
    if 'osmo' in parts or 'gopro' in parts or 'action' in parts:
        return 'action'
    if 'phone' in parts:
        return 'phone'
    if ext == '.mov':
        return 'phone'
    return 'camera'


# ── time + gpx ───────────────────────────────────────────────────────────────

def clock_offset(trip, device, model):
    """Hours to add to a clip's claimed creation_time to get true UTC.

    config clock_offsets keys are matched case/space-insensitively against the
    camera model first (e.g. "OsmoAction5": -8), then the device bucket.
    """
    offs = trip.get('clock_offsets') or {}
    mnorm = (model or '').lower().replace(' ', '')
    for k, v in offs.items():
        knorm = k.lower().replace(' ', '')
        if knorm not in ('drone', 'camera', 'action', '360', 'phone', 'other') \
                and knorm in mnorm:
            return v
    return offs.get(device, 0)


# Dates embedded in filenames: DJI_20260716094628_…, VID-20220428-WA0019,
# 20260712_155921, 2022-05-01 …. Used when a camera wrote no usable clock.
FILENAME_DATE_RE = re.compile(
    r'(?<!\d)(19[89]\d|20[0-4]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])'
    r'(?:[-_ T]?([01]\d|2[0-3])[-_.]?([0-5]\d)[-_.]?([0-5]\d))?(?!\d)')


def filename_dt(name):
    """Datetime embedded in a filename, or None. Treated as local wall clock."""
    m = FILENAME_DATE_RE.search(name)
    if not m:
        return None
    y, mo, d, hh, mi, ss = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0),
                        int(ss or 0), tzinfo=timezone.utc)
    except ValueError:
        return None


def trip_window(trip):
    """(start, end) UTC bounds a clip's timestamp must fall inside to be believed.

    Defaults to 2000-01-01..now, which rejects unset-clock cameras (GoPros with
    a dead coin cell stamp 1970-71). Set `dates` on the trip in
    config/video_browse.json to catch subtler cases, like a camera reset to its
    firmware's default year mid-trip.
    """
    d = trip.get('dates') or {}
    lo = datetime(2000, 1, 1, tzinfo=timezone.utc)
    hi = datetime.now(timezone.utc) + timedelta(days=1)
    if d.get('start'):
        lo = datetime.fromisoformat(d['start']).replace(tzinfo=timezone.utc) - timedelta(days=2)
    if d.get('end'):
        hi = datetime.fromisoformat(d['end']).replace(tzinfo=timezone.utc) + timedelta(days=2)
    return lo, hi


def parse_dt(meta, device, trip):
    """Return true-UTC datetime for a clip, or None."""
    apple = meta.get('apple_date')
    if apple:  # e.g. 2026-07-16T14:23:03+0800 -> already tz-aware truth
        try:
            return datetime.fromisoformat(apple.replace('Z', '+00:00')).astimezone(timezone.utc)
        except ValueError:
            pass
    ct = meta.get('creation_time')
    if not ct:
        return None
    try:
        dt = datetime.fromisoformat(ct.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    off = clock_offset(trip, device, meta.get('model') or '')
    return dt + timedelta(hours=off)


def resolve_dt(meta, device, trip, path):
    """(utc datetime, source) using the first source that lands in the trip window.

    Cameras lie in three ways seen in this library: a dead clock (GoPro stamping
    1971), a clock reset to the firmware default (2016 on a 2022 trip), and no
    timestamp at all (WhatsApp exports, which carry the date in the filename).
    """
    lo, hi = trip_window(trip)
    dt = parse_dt(meta, device, trip)
    if dt and lo <= dt <= hi:
        return dt, 'metadata'
    fdt = filename_dt(path.name)
    if fdt:
        fdt -= timedelta(hours=trip.get('tz_hours', 0))   # filename is local time
        if lo <= fdt <= hi:
            return fdt, 'filename'
    try:
        mt = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if lo <= mt <= hi:
            return mt, 'mtime'
    except (OSError, ValueError):
        pass
    # Nothing believable. Better no date than a 1971 one polluting the year
    # filter; these still sort by filename inside their folder.
    return None, ('rejected' if dt else 'none')


TRKPT_RE = re.compile(
    r'<trkpt[^>]*lat="([^"]+)"[^>]*lon="([^"]+)"[^>]*>(.*?)</trkpt>', re.S)
TIME_RE = re.compile(r'<time>([^<]+)</time>')


def load_gpx(gpx_path):
    """Parse trkpts from a gpx file or every .gpx in a directory. -> sorted [(epoch, lat, lon)]"""
    p = Path(gpx_path)
    files = sorted(p.glob('*.gpx')) if p.is_dir() else [p]
    pts = []
    for f in files:
        try:
            text = f.read_text(errors='replace')
        except OSError:
            continue
        for m in TRKPT_RE.finditer(text):
            tm = TIME_RE.search(m.group(3))
            if not tm:
                continue
            try:
                dt = datetime.fromisoformat(tm.group(1).replace('Z', '+00:00'))
                pts.append((dt.timestamp(), float(m.group(1)), float(m.group(2))))
            except ValueError:
                continue
    pts.sort()
    return pts


def gpx_locate(pts, epoch, max_gap=1800):
    if not pts:
        return None
    times = [p[0] for p in pts]
    i = bisect.bisect_left(times, epoch)
    if i == 0:
        return pts[0][1:] if times[0] - epoch <= max_gap else None
    if i >= len(pts):
        return pts[-1][1:] if epoch - times[-1] <= max_gap else None
    t0, la0, lo0 = pts[i - 1]
    t1, la1, lo1 = pts[i]
    if t1 - t0 > max_gap * 2 and min(epoch - t0, t1 - epoch) > max_gap:
        return None
    f = (epoch - t0) / (t1 - t0) if t1 > t0 else 0
    return (la0 + f * (la1 - la0), lo0 + f * (lo1 - lo0))


def parse_iso6709(s):
    if not s:
        return None
    m = ISO6709_RE.match(s)
    if not m:
        return None
    return (float(m.group(1)), float(m.group(2)))


# ── day-folder parsing ───────────────────────────────────────────────────────

def load_roof_tokens():
    """[(token_lower, building_name)] from the Urbex rosters, for longest-wins
    matching. Missing rosters just mean no building filter."""
    tokens = []
    for rel in ROOF_ROSTERS:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            roster = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        for b in roster.get('buildings', []):
            for t in b.get('match', [b['name']]):
                tokens.append((t.lower(), b['name']))
    return tokens


def match_building(folders, tokens):
    """Roster building for a clip, from the folder names above it, or None.

    Longest matching token wins, so 'Greenland Center' can't steal
    'Wuhan Greenland Center'. A folder that is a day itinerary ("Day 20 [Yunnan]
    Shangri-la - ...") only matches tokens that are themselves day strings,
    otherwise a town merely driven through claims a same-named tower elsewhere.
    Every folder above the clip is considered, since some trips nest the
    building under a Videos/ dir rather than putting it at the trip root.
    """
    if isinstance(folders, str):
        folders = [folders]
    best = None
    for folder in folders:
        fl = (folder or '').strip().lower()
        if not fl:
            continue
        itinerary = bool(DAY_ITINERARY.match(fl))
        for t, name in tokens:
            if itinerary and not DAY_ITINERARY.match(t):
                continue
            if t in fl and (best is None or len(t) > len(best[0])):
                best = (t, name)
    return best[1] if best else None


def day_info(path: Path, trip_base: Path):
    rel = path.relative_to(trip_base)
    for part in rel.parts[:-1]:
        m = DAY_RE.match(part)
        if m:
            regions = [r.strip() for r in (m.group(2) or '').split(',') if r.strip()]
            return int(m.group(1)), regions, (m.group(3) or '').strip()
    top = rel.parts[0] if len(rel.parts) > 1 else ''
    return None, [], top


def infer_folder_dates(clips, cfg):
    """Give clips whose camera clock was unusable the date of their folder.

    A GoPro with a dead clock still sat in the same folder as the drone and
    phone clips from that climb, so the folder's median date is right even
    though the file's own stamp is not. Only the DATE is inferred (time is left
    at midday local); these are marked time_src 'folder' so nothing pretends
    they are exact. Clips keep filename order within the folder.
    """
    tz_by_trip = {t['name']: t.get('tz_hours', 0) for t in cfg['trips']}
    known = {}
    for c in clips:
        if c['utc']:
            known.setdefault((c['trip'], c['folder']), []).append(c['utc'][:10])
    n = 0
    for c in clips:
        if c['utc']:
            continue
        dates = sorted(known.get((c['trip'], c['folder'])) or [])
        if not dates:
            continue
        mid = dates[len(dates) // 2]
        tz = tz_by_trip.get(c['trip'], 0)
        dt = datetime.fromisoformat(mid).replace(tzinfo=timezone.utc) \
            + timedelta(hours=12 - tz)
        local = dt + timedelta(hours=tz)
        c['utc'] = dt.strftime('%Y-%m-%dT%H:%M:%S')
        c['local'] = local.strftime('%Y-%m-%dT%H:%M:%S')
        c['date'] = local.strftime('%Y-%m-%d')
        c['year'] = local.year
        c['time_src'] = 'folder'
        n += 1
    if n:
        print(f"  inferred date from folder siblings for {n} clips")


# ── index build ──────────────────────────────────────────────────────────────

def build_index(cfg, workers=8):
    LIB.mkdir(exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    cache_path = LIB / 'probe_cache.json'
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            cache = {}

    clips = []
    for trip in cfg['trips']:
        base = Path(trip['path'])
        if not base.is_dir():
            print(f"!! trip path missing, skipping: {base}", file=sys.stderr)
            continue
        files = scan_trip(trip)
        print(f"[{trip['name']}] {len(files)} video files found")

        # figure out which need probing
        need, meta_by_path = [], {}
        for f in files:
            st = f.stat()
            key = str(f)
            c = cache.get(key)
            if c and c.get('size') == st.st_size and c.get('mtime') == int(st.st_mtime):
                meta_by_path[key] = c['meta']
            else:
                need.append((f, st))
        print(f"  probing {len(need)} new/changed files ({len(files)-len(need)} cached)")

        done = 0
        lock = threading.Lock()

        def probe_one(item):
            f, st = item
            meta = None
            try:
                meta = ffprobe(f)
            except Exception:
                meta = None
            return f, st, meta

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f, st, meta in ex.map(probe_one, need):
                key = str(f)
                if meta is None:
                    meta = {'error': True}
                cache[key] = {'size': st.st_size, 'mtime': int(st.st_mtime), 'meta': meta}
                meta_by_path[key] = meta
                with lock:
                    done += 1
                    if done % 100 == 0:
                        print(f"  probed {done}/{len(need)}")
                        cache_path.write_text(json.dumps(cache))
        cache_path.write_text(json.dumps(cache))

        roof_tokens = load_roof_tokens()
        gpx_pts = load_gpx(trip['gpx']) if trip.get('gpx') else []
        if trip.get('gpx'):
            print(f"  gpx points: {len(gpx_pts)}")
        tz = trip.get('tz_hours', 0)

        for f in files:
            meta = meta_by_path.get(str(f)) or {}
            if meta.get('error') or not meta.get('duration'):
                continue
            device = classify(f, meta)
            dt, time_src = resolve_dt(meta, device, trip, f)
            day_num, regions, day_label = day_info(f, base)
            folders = list(f.relative_to(base).parts[:-1])
            building = match_building(folders, roof_tokens)
            lat = lon = None
            loc_src = None
            ll = parse_iso6709(meta.get('iso6709'))
            if ll:
                lat, lon = ll
                loc_src = 'embedded'
            elif dt and gpx_pts:
                ll = gpx_locate(gpx_pts, dt.timestamp() + meta['duration'] / 2)
                if ll:
                    lat, lon = round(ll[0], 5), round(ll[1], 5)
                    loc_src = 'gpx'
            local = dt + timedelta(hours=tz) if dt else None
            w, h = meta['width'], meta['height']
            if meta.get('rotation') in (90, -90, 270):
                w, h = h, w
            clips.append({
                'id': clip_id(str(f)),
                'path': str(f),
                'name': f.name,
                'trip': trip['name'],
                'day': day_num,
                'regions': regions,
                'label': day_label,
                'folder': folders[0] if folders else '',
                'building': building,
                'time_src': time_src,
                'device': device,
                'model': meta.get('model') or '',
                'utc': dt.strftime('%Y-%m-%dT%H:%M:%S') if dt else None,
                'local': local.strftime('%Y-%m-%dT%H:%M:%S') if local else None,
                'date': local.strftime('%Y-%m-%d') if local else None,
                'year': local.year if local else None,
                'duration': round(meta['duration'], 2),
                'w': w, 'h': h,
                'fps': meta['fps'],
                'codec': meta['codec'],
                'transfer': meta.get('transfer'),
                'size': meta['size'],
                'lat': lat, 'lon': lon, 'loc_src': loc_src,
            })

    infer_folder_dates(clips, cfg)
    clips.sort(key=lambda c: (c['trip'], c['day'] if c['day'] is not None else 999,
                              c['utc'] or '9999', c['name']))
    index = {
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'clips': clips,
    }
    (LIB / 'index.json').write_text(json.dumps(index))
    print(f"index.json written: {len(clips)} clips")
    by_dev = {}
    for c in clips:
        by_dev[c['device']] = by_dev.get(c['device'], 0) + 1
    print('  by device:', by_dev)
    n_loc = sum(1 for c in clips if c['lat'] is not None)
    print(f"  located: {n_loc}/{len(clips)}")
    blds = {c['building'] for c in clips if c.get('building')}
    n_b = sum(1 for c in clips if c.get('building'))
    print(f"  buildings: {len(blds)} matched ({n_b} clips)")
    src = {}
    for c in clips:
        src[c.get('time_src')] = src.get(c.get('time_src'), 0) + 1
    print('  time source:', src)
    if src.get('rejected'):
        print(f"  !! {src['rejected']} clips had an out-of-window camera clock and no "
              f"usable fallback (set `dates` on the trip to tighten the window)")


# ── proxies ──────────────────────────────────────────────────────────────────

_HAS_ZSCALE = None


def has_zscale():
    """Whether this ffmpeg can tonemap (zscale needs libzimg, which the
    Homebrew build does not ship). Without it HDR clips are converted straight
    to SDR: HLG was designed to degrade gracefully that way and looks right,
    so it is not worth failing 376 iPhone clips over."""
    global _HAS_ZSCALE
    if _HAS_ZSCALE is None:
        try:
            out = subprocess.run(['ffmpeg', '-hide_banner', '-filters'],
                                 capture_output=True, text=True, timeout=30)
            _HAS_ZSCALE = bool(re.search(r'^\s*\S+\s+zscale\s', out.stdout, re.M))
        except (OSError, subprocess.SubprocessError):
            _HAS_ZSCALE = False
    return _HAS_ZSCALE


def proxy_dims(w, h):
    if not w or not h:
        return 854, 480
    f = min(PROXY_LONG / max(w, h), PROXY_SHORT / min(w, h), 1.0)
    return max(2, 2 * round(w * f / 2)), max(2, 2 * round(h * f / 2))


def make_proxy(clip, workdir=None):
    """Encode the proxy into workdir (local staging), returning its path.

    Staging locally matters when MEDIA is on the NAS: ffmpeg's +faststart pass
    rewrites the output file to move the moov atom, and doing that over SMB is
    slow and occasionally times out mid-write.
    """
    src = clip['path']
    pid = clip['id']
    workdir = Path(workdir) if workdir else MEDIA
    out = workdir / f'{pid}.mp4'
    tmp = workdir / f'{pid}.tmp.mp4'
    w, h = proxy_dims(clip['w'], clip['h'])
    vf = []
    if clip['fps'] and clip['fps'] > PROXY_MAXFPS + 1:
        vf.append(f'fps={PROXY_MAXFPS}')
    vf.append(f'scale={w}:{h}')
    if clip.get('transfer') in ('smpte2084', 'arib-std-b67') and has_zscale():
        vf.append('zscale=t=linear:npl=100,tonemap=hable:desat=0,'
                  'zscale=p=bt709:t=bt709:m=bt709:r=tv')
    vf.append('format=yuv420p')
    cmd = ['ffmpeg', '-y', '-v', 'error', '-hwaccel', 'videotoolbox', '-i', src,
           '-map', '0:v:0', '-map', '0:a?',
           '-vf', ','.join(vf),
           '-c:v', 'h264_videotoolbox', '-b:v', PROXY_VBITRATE, '-allow_sw', '1',
           '-c:a', 'aac', '-b:a', '96k', '-ac', '2',
           '-movflags', '+faststart', str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(r.stderr.strip()[-400:] or 'ffmpeg failed')
    tmp.rename(out)
    return out


def make_stills(clip, workdir=None):
    """Poster + hover filmstrip, generated from the already-built proxy (cheap)."""
    pid = clip['id']
    workdir = Path(workdir) if workdir else MEDIA
    proxy = workdir / f'{pid}.mp4'
    poster = workdir / f'{pid}.jpg'
    strip = workdir / f'{pid}.strip.jpg'
    dur = max(clip['duration'], 0.5)
    if not poster.exists():
        subprocess.run(['ffmpeg', '-y', '-v', 'error',
                        '-ss', f'{dur * 0.25:.2f}', '-i', str(proxy),
                        '-frames:v', '1', '-q:v', '4', str(poster)],
                       capture_output=True)
    if not strip.exists():
        rate = STRIP_FRAMES / dur
        vf = (f'fps={rate:.6f},'
              f'scale={STRIP_TILE_W}:{STRIP_TILE_H}:force_original_aspect_ratio=increase,'
              f'crop={STRIP_TILE_W}:{STRIP_TILE_H},tile={STRIP_FRAMES}x1')
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(proxy),
                        '-vf', vf, '-frames:v', '1', '-q:v', '5', str(strip)],
                       capture_output=True)


def run_proxies(workers=2):
    index = json.loads((LIB / 'index.json').read_text())
    clips = index['clips']
    staging = LIB / 'staging'
    staging.mkdir(parents=True, exist_ok=True)
    for f in staging.iterdir():  # clear anything a killed run left behind
        f.unlink(missing_ok=True)
    todo = [c for c in clips if not (MEDIA / f"{c['id']}.mp4").exists()]
    # fill in stills for proxies that exist but lack poster/strip
    for c in clips:
        if (MEDIA / f"{c['id']}.mp4").exists() \
                and not (MEDIA / f"{c['id']}.strip.jpg").exists():
            try:
                make_stills(c)
            except Exception:
                pass
    total = len(clips)
    progress_path = LIB / 'proxy_progress.json'
    err_log = LIB / 'proxy_errors.log'
    lock = threading.Lock()
    state = {'done': total - len(todo), 'total': total, 'current': None,
             'started': datetime.now(timezone.utc).strftime('%H:%M:%SZ'), 'errors': 0}

    def save():
        progress_path.write_text(json.dumps(state))

    save()
    print(f"proxies: {state['done']}/{total} done, {len(todo)} to go, {workers} workers")

    def work(clip):
        with lock:
            state['current'] = clip['name']
            save()
        try:
            # Encode + derive stills locally, then publish the finished trio to
            # MEDIA. Keeps every SMB write a single sequential copy.
            make_proxy(clip, staging)
            make_stills(clip, staging)
            for suffix in ('.mp4', '.jpg', '.strip.jpg'):
                s = staging / f"{clip['id']}{suffix}"
                if s.exists():
                    shutil.move(str(s), str(MEDIA / f"{clip['id']}{suffix}"))
            ok = True
        except Exception as e:
            for suffix in ('.mp4', '.jpg', '.strip.jpg', '.tmp.mp4'):
                (staging / f"{clip['id']}{suffix}").unlink(missing_ok=True)
            with lock:
                state['errors'] += 1
                with open(err_log, 'a') as fh:
                    fh.write(f"{clip['path']}\n  {e}\n")
            ok = False
        with lock:
            state['done'] += 1
            save()
            if state['done'] % 10 == 0:
                print(f"  {state['done']}/{total} ({state['errors']} errors)")
        return ok

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))
    state['current'] = None
    save()
    print(f"proxy pass complete: {state['done']}/{total}, {state['errors']} errors")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--proxies', action='store_true', help='generate proxies/posters/strips')
    ap.add_argument('--workers', type=int, default=None)
    args = ap.parse_args()
    cfg = load_config()
    if args.proxies:
        run_proxies(workers=args.workers or 2)
    else:
        build_index(cfg, workers=args.workers or 8)


if __name__ == '__main__':
    main()
