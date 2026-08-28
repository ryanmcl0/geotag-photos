#!/usr/bin/env python3
"""Export a cut to DaVinci Resolve.

Two routes, both attempted:
 1. FCPXML written to local_videos/exports/ (always works; File > Import > Timeline
    in any Resolve edition).
 2. Live import via the DaVinci Resolve scripting API when Resolve is running and
    external scripting is enabled (Preferences > System > General >
    External scripting using: Local). Creates/loads a project named after the cut,
    imports the full-res NAS clips into the media pool, and builds a timeline in
    the cut's order.
"""
import re
import subprocess
import sys
from datetime import datetime
from fractions import Fraction
from pathlib import Path

RESOLVE_MODULES = '/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules'


def _slug(name):
    return re.sub(r'[^A-Za-z0-9._ -]+', '', name).strip().replace(' ', '_') or 'cut'


def _rat(seconds, fps_num=30000, fps_den=1001):
    """Seconds -> frame-aligned rational time string for FCPXML."""
    frames = round(seconds * fps_num / (fps_den * 1))
    fr = Fraction(frames * fps_den, fps_num)
    return f'{fr.numerator}/{fr.denominator}s' if fr.denominator != 1 else f'{fr.numerator}s'


def write_fcpxml(cut, clips, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    name = _slug(cut.get('name') or 'cut')
    out = out_dir / f'{name}.fcpxml'
    resources, spine = [], []
    resources.append('<format id="r0" name="FFVideoFormat1080p2997" '
                     'frameDuration="1001/30000s" width="1920" height="1080"/>')
    offset = 0.0
    for i, c in enumerate(clips):
        rid = f'r{i + 1}'
        dur = max(c['duration'], 0.1)
        fps = c.get('fps') or 29.97
        fden, fnum = 1001, round(29.97 * 1001)
        try:
            fr = Fraction(fps).limit_denominator(1001)
            fnum, fden = fr.numerator, fr.denominator
        except (ValueError, ZeroDivisionError):
            pass
        src = 'file://' + str(Path(c['path'])).replace(' ', '%20')
        resources.append(
            f'<format id="f{i + 1}" frameDuration="{fden}/{fnum}s" '
            f'width="{c["w"]}" height="{c["h"]}"/>'
            f'<asset id="{rid}" name="{c["name"]}" start="0s" '
            f'duration="{_rat(dur, fnum, fden)}" hasVideo="1" '
            f'hasAudio="{1 if c.get("has_audio", True) else 0}" format="f{i + 1}">'
            f'<media-rep kind="original-media" src="{src}"/></asset>')
        spine.append(
            f'<asset-clip ref="{rid}" name="{c["name"]}" '
            f'offset="{_rat(offset)}" duration="{_rat(dur)}" start="0s"/>')
        offset += dur
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.9">\n'
        f'<resources>{"".join(resources)}</resources>\n'
        f'<library><event name="{name}"><project name="{name}">'
        f'<sequence format="r0"><spine>{"".join(spine)}</spine></sequence>'
        '</project></event></library>\n</fcpxml>\n')
    out.write_text(xml)
    return out


def try_resolve_api(cut, clips):
    sys.path.insert(0, RESOLVE_MODULES)
    try:
        import DaVinciResolveScript as dvr
    except ImportError as e:
        return {'ok': False, 'why': f'scripting module unavailable ({e})'}
    resolve = None
    try:
        resolve = dvr.scriptapp('Resolve')
    except Exception as e:
        return {'ok': False, 'why': f'could not connect ({e})'}
    if not resolve:
        return {'ok': False, 'why': 'Resolve not running or external scripting disabled '
                                    '(Preferences > System > General > External scripting: Local)'}
    try:
        pm = resolve.GetProjectManager()
        name = _slug(cut.get('name') or 'cut')
        proj = pm.LoadProject(name) or pm.CreateProject(name)
        if not proj:
            return {'ok': False, 'why': 'could not create/load project'}
        mp = proj.GetMediaPool()
        items = mp.ImportMedia([c['path'] for c in clips])
        if not items:
            return {'ok': False, 'why': 'media import returned nothing'}
        tl_name = f'{name} {datetime.now().strftime("%H%M%S")}'
        tl = mp.CreateEmptyTimeline(tl_name)
        if not tl:
            return {'ok': False, 'why': 'could not create timeline'}
        # ImportMedia may return items out of order; re-sort to the cut's order
        by_path = {}
        for it in items:
            try:
                by_path[it.GetClipProperty('File Path')] = it
            except Exception:
                pass
        ordered = [by_path.get(c['path']) for c in clips]
        ordered = [i for i in ordered if i] or items
        mp.AppendToTimeline(ordered)
        return {'ok': True, 'project': name, 'timeline': tl_name}
    except Exception as e:
        return {'ok': False, 'why': f'{type(e).__name__}: {e}'}


def resolve_running():
    r = subprocess.run(['pgrep', '-x', 'Resolve'], capture_output=True)
    return r.returncode == 0


def export_cut(cut, clips, out_dir: Path):
    fcpxml = write_fcpxml(cut, clips, out_dir)
    result = {'fcpxml': str(fcpxml), 'clips': len(clips)}
    if not resolve_running():
        subprocess.Popen(['open', '-a', 'DaVinci Resolve'])
        result['resolve'] = {'ok': False,
                             'why': 'Resolve was not running; launching it now. '
                                    'Import the FCPXML (File > Import > Timeline) or hit Export again once it is open.'}
        subprocess.Popen(['open', '-R', str(fcpxml)])
        return result
    result['resolve'] = try_resolve_api(cut, clips)
    if not result['resolve']['ok']:
        subprocess.Popen(['open', '-R', str(fcpxml)])
    return result
