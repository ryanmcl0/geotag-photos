#!/usr/bin/env python3
"""Resolve the people roster into a concrete per-photo exclusion set.

Face clustering lives entirely in local_browse/ (face_index.sqlite → clusters.json)
and never ships. This tool is the bridge: it turns "person X is switched off" into
a flat {slug: [photo ids]} map that photo_privacy.py can consume at build/deploy
time without any face data present, the same way analyze_bridge_visits.py feeds
bridge_visits.json into the privacy sync.

    tools/people_index.py            # rebuild config/people_private.json
    tools/people_index.py --seed     # create config/people.json from clusters
    tools/people_index.py --report   # show what each person would hide

Roster (config/people.json, gitignored — it names real people):
    {"people": {"<key>": {
        "label": "Display Name",
        "clusters": ["p1", "p4"],          # face cluster ids from clusters.json
        "hide": false,                     # false | "gated" | "blocked"
        "keep_public": {"<slug>": ["<id>"]}   # per-photo escape hatch
    }}}

`hide` tiers:
    false     — nothing happens.
    "gated"   — photo leaves the public manifest.json for manifest.all.json and the
                /photos proxy 404s it without the See All cookie. Still viewable by
                you. Identical treatment to a force_private photo.
    "blocked" — photo is stripped from manifest.all.json too and the proxy 404s it
                even WITH See All. Only the R2 object survives.

Output (config/people_private.json, gitignored) carries a digest of the roster's
hide-relevant state; photo_privacy.py aborts if that digest is stale, so a roster
edit can never silently deploy without a rebuild here.
"""
import hashlib
import json
import sys
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / 'web' / 'trips'
CLUSTERS = ROOT / 'local_browse' / 'clusters.json'
LOCAL_PEOPLE = ROOT / 'local_browse' / 'people.json'
ROSTER = ROOT / 'config' / 'people.json'
OUT = ROOT / 'config' / 'people_private.json'
SITE_OUT = ROOT / 'config' / 'people_site.json'      # deployed: camera photos only
LOCAL_OUT = ROOT / 'config' / 'people_local.json'    # localhost: + the phone library
PHONE_TRIPS = ROOT / 'web' / 'phone' / 'trips'

HIDE_TIERS = (False, 'gated', 'blocked')


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_roster() -> dict:
    """key → {label, clusters, hide, keep_public}. Missing file = empty roster."""
    data = _load(ROSTER)
    if data is None:
        return {}
    out = {}
    for key, ent in (data.get('people') or {}).items():
        if key.startswith('_') or not isinstance(ent, dict):
            continue
        hide = ent.get('hide', False)
        if hide not in HIDE_TIERS:
            raise SystemExit(f"✗ config/people.json: person '{key}' has hide={hide!r}; "
                             f"expected one of {HIDE_TIERS}")
        out[key] = {
            'label': ent.get('label') or key,
            'clusters': [str(c) for c in (ent.get('clusters') or [])],
            'hide': hide,
            'keep_public': {s: set(v) for s, v in (ent.get('keep_public') or {}).items()},
        }
    return out


def roster_digest(roster: dict) -> str:
    """Stable hash of everything that changes the OUTPUT of this tool. Label edits
    don't count; cluster membership, tier and escape hatches do."""
    norm = {
        k: {
            'clusters': sorted(v['clusters']),
            'hide': v['hide'],
            'keep_public': {s: sorted(ids) for s, ids in sorted(v['keep_public'].items())},
        }
        for k, v in sorted(roster.items()) if v['hide']
    }
    return hashlib.sha256(json.dumps(norm, sort_keys=True).encode()).hexdigest()[:16]


def cluster_photos() -> tuple[dict, dict]:
    """(cluster id → {(slug, photo_id)}, stats) over CAMERA photos only.

    Face refs look like 'camera:<slug>/display/<stem>.webp' and <stem> is the
    manifest photo id. Phone-library refs are dropped: that library is local-only,
    so there is nothing on the site to hide.
    """
    data = _load(CLUSTERS)
    if data is None:
        raise SystemExit(f"✗ {CLUSTERS.relative_to(ROOT)} missing or unreadable — "
                         "run local_browse/cluster_faces.py first")
    manifests = {}

    def ids_for(slug):
        if slug not in manifests:
            # manifest.full.json FIRST. The blocked tier physically removes photos from
            # the live manifests, so reading those would make an already-blocked photo
            # look like it no longer exists — the resolver would drop it, emit an empty
            # blocked set, and the next privacy sync would put the person back on the
            # site. The stash is the pre-strip copy photo_privacy keeps for exactly this.
            man = (_load(TRIPS / slug / 'manifest.full.json')
                   or _load(TRIPS / slug / 'manifest.all.json')
                   or _load(TRIPS / slug / 'manifest.json'))
            manifests[slug] = {p['id'] for p in (man or {}).get('photos', [])} if man else None
        return manifests[slug]

    out, stats = {}, {'refs': 0, 'stale': set(), 'no_manifest': set()}
    for c in data.get('clusters', []):
        members = set()
        for ref in c.get('photos', []):
            if not ref.startswith('camera:'):
                continue
            rest = ref.split(':', 1)[1]
            slug = rest.split('/')[0]
            stem = rest.split('/')[-1].rsplit('.', 1)[0]
            stats['refs'] += 1
            known = ids_for(slug)
            if known is None:
                # blog-<slug> pseudo-trips hold a blog's non-trip images; they have no
                # manifest and are not reachable through the per-photo privacy model.
                stats['no_manifest'].add(slug)
                continue
            if stem not in known:
                # Re-edited/renamed since the face index ran (RM102034 → …-Enhanced-NR).
                stats['stale'].add(f'{slug}/{stem}')
                continue
            members.add((slug, stem))
        if members:
            out[c['id']] = members
    return out, stats


def resolve(roster: dict) -> tuple[dict, dict]:
    """(payload for config/people_private.json, per-person detail for reporting)."""
    by_cluster, stats = cluster_photos()
    gated, blocked, detail = {}, {}, {}

    for key, person in sorted(roster.items()):
        photos = set()
        for cid in person['clusters']:
            photos |= by_cluster.get(cid, set())
        kept = {(s, i) for s, ids in person['keep_public'].items() for i in ids}
        effective = photos - kept
        detail[key] = {
            'label': person['label'], 'hide': person['hide'],
            'clusters': person['clusters'], 'n_photos': len(photos),
            'n_kept': len(photos & kept), 'n_hidden': len(effective) if person['hide'] else 0,
            'photos': sorted(effective),
        }
        if not person['hide']:
            continue
        target = blocked if person['hide'] == 'blocked' else gated
        for slug, pid in effective:
            target.setdefault(slug, set()).add(pid)

    # A photo naming a blocked person AND a merely-gated one is blocked: the
    # stricter tier wins, and leaving it in both maps would be contradictory.
    for slug, ids in blocked.items():
        if slug in gated:
            gated[slug] -= ids
            if not gated[slug]:
                del gated[slug]

    payload = {
        'roster_digest': roster_digest(roster),
        'gated': {s: sorted(v) for s, v in sorted(gated.items())},
        'blocked': {s: sorted(v) for s, v in sorted(blocked.items())},
        'by_person': {k: {kk: vv for kk, vv in d.items() if kk != 'photos'}
                      for k, d in detail.items()},
    }
    return payload, {'detail': detail, 'stats': stats}


def cluster_photos_phone() -> dict:
    """cluster id → {(phone-<slug>, photo_id)} over the LOCAL phone library.

    Kept separate from cluster_photos() on purpose: nothing here is deployed, so
    these can never take part in the hiding rules — they exist only so the People
    page shows a whole person when you are looking at it on localhost. The trip id
    carries the 'phone-' prefix that Gallery.photoUrl routes to /phone/trips.
    """
    data = _load(CLUSTERS)
    if data is None:
        return {}
    manifests = {}

    def ids_for(slug):
        if slug not in manifests:
            man = _load(PHONE_TRIPS / f'phone-{slug}' / 'manifest.json')
            manifests[slug] = {p['id'] for p in (man or {}).get('photos', [])} if man else None
        return manifests[slug]

    out = {}
    for c in data.get('clusters', []):
        members = set()
        for ref in c.get('photos', []):
            if not ref.startswith('phone:'):
                continue
            rest = ref.split(':', 1)[1]
            slug = rest.split('/')[0]
            stem = rest.split('/')[-1].rsplit('.', 1)[0]
            known = ids_for(slug)
            if known is not None and stem in known:
                members.add((f'phone-{slug}', stem))
        if members:
            out[c['id']] = members
    return out


def photo_meta() -> dict:
    """(display_trip_id, photo_id) -> (iso_timestamp, country_code).

    Country comes from the photo's cluster in the manifest, falling back to the
    trip's first country — the same resolution build_collections uses, so the
    People page agrees with the map. Keyed by the trip id as it appears in the
    page's photo refs ('2025-china-cny', 'phone-2025-01-25-china') so curated sets
    and roster people can share one lookup.
    """
    meta = {}
    need_geo = []

    def ingest(trip_dir, display_id, fallback_country):
        man = (_load(trip_dir / 'manifest.full.json') or _load(trip_dir / 'manifest.all.json')
               or _load(trip_dir / 'manifest.json'))
        if not man:
            return
        by_photo = {}
        for c in man.get('clusters', []):
            cc = c.get('country')
            if not cc:
                continue
            for pid in c.get('photo_ids', []):
                by_photo[pid] = cc
        for ph in man.get('photos', []):
            ts = ph.get('timestamp') or ''
            cc = by_photo.get(ph['id']) or fallback_country
            if not cc and ph.get('lat') is not None and ph.get('lon') is not None:
                # Only 16 of 40 phone trips carry a country, and their clusters
                # often don't either — but the photos have coordinates, so resolve
                # from those rather than guessing from the trip name (which would
                # pick one country for trips like 'iceland-italy').
                need_geo.append(((display_id, ph['id']), (ph['lat'], ph['lon'])))
            meta[(display_id, ph['id'])] = (ts or None, cc)

    idx = _load(TRIPS / 'index.json') or {}
    trip_country = {t['id']: (t.get('countries') or [None])[0] for t in idx.get('trips', [])}
    for d in TRIPS.iterdir():
        if d.is_dir():
            ingest(d, d.name, trip_country.get(d.name))
    if PHONE_TRIPS.is_dir():
        pidx = _load(PHONE_TRIPS / 'index.json') or {}
        pc = {t['id']: (t.get('countries') or [None])[0] for t in pidx.get('trips', [])}
        for d in PHONE_TRIPS.iterdir():
            if d.is_dir():
                ingest(d, d.name, pc.get(d.name))

    if need_geo:
        try:
            import reverse_geocoder as rg
            hits = rg.search([c for _, c in need_geo], mode=1)
            for (key, _), hit in zip(need_geo, hits):
                y, _ = meta[key]
                meta[key] = (y, hit.get('cc') or None)
        except Exception as e:                    # noqa: BLE001 — optional enrichment
            print(f"  ⚠ coordinate country lookup unavailable ({e}); "
                  f"{len(need_geo)} photos stay uncategorised", file=sys.stderr)
    fill_countries_by_time(meta)
    return meta


NEAR_HOURS = 24        # how far a photo may borrow a country from another photo


def fill_countries_by_time(meta: dict) -> None:
    """Give the GPS-less photos a country, borrowed from what was photographed
    around the same time on a device that did record where it was.

    Whole phone trips carry no coordinates at all — phone-2024-asia-24-pt2 has 0
    of 2850 — so they landed under 'Unknown' and the People page's country filter
    could not reach them: 195 photos of a China trip sitting outside China. A
    photo taken within NEAR_HOURS of one whose country IS known was in the same
    country. Beyond that the answer is left as unknown rather than guessed, which
    is what keeps a travel day from tagging the country you had just left.
    """
    def epoch(ts):
        try:
            return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
        except (AttributeError, ValueError):
            return None

    known = sorted((e, cc) for ts, cc in meta.values()
                   if ts and cc and (e := epoch(ts)) is not None)
    if not known:
        return
    times = [e for e, _ in known]
    filled = 0
    for key, (ts, cc) in meta.items():
        if cc or not ts:
            continue
        t = epoch(ts)
        if t is None:
            continue
        i = bisect_left(times, t)
        best = min((c for c in (i - 1, i) if 0 <= c < len(known)),
                   key=lambda c: abs(times[c] - t), default=None)
        if best is not None and abs(times[best] - t) <= NEAR_HOURS * 3600:
            meta[key] = (ts, known[best][1])
            filled += 1
    if filled:
        print(f"  ↳ {filled} photos took a country from another photo "
              f"within {NEAR_HOURS}h")


def public_ids(slug: str) -> set:
    """Ids that survive the privacy filter into the trip's PUBLIC manifest.json.
    Empty for a private trip (nothing there is public)."""
    man = _load(TRIPS / slug / 'manifest.json')
    if not man:
        return set()
    idx = _load(TRIPS / 'index.json') or {}
    public_trips = {t['id'] for t in idx.get('trips', []) if t.get('public')}
    if slug not in public_trips:
        return set()
    return {p['id'] for p in man.get('photos', [])}


def site_payload(roster: dict, detail: dict, include_phone: bool = False) -> dict:
    """The owner-only People page document.

    Lists every photo the face index reached, grouped by roster person, with unnamed
    clusters kept separate so the page doubles as the place to work out who is who.
    Blocked photos are omitted entirely — "hidden from every tier" has to mean this
    page too, and the local picker is where you unblock.

    include_phone adds the local-only phone library, marked g=2. That variant is
    written to a SEPARATE file that only serve.sh reads, so the document uploaded to
    R2 physically cannot carry phone-library references. One page, two documents:
    on localhost you see the whole person, on the deployed site only what is
    deployed.
    """
    by_cluster, _ = cluster_photos()
    phone_by_cluster = cluster_photos_phone() if include_phone else {}
    meta = photo_meta()
    assigned = {c for p in roster.values() for c in p['clusters']}
    blocked_pairs = {pair for k, d in detail.items() if d['hide'] == 'blocked'
                     for pair in d['photos']}
    pub_cache = {}

    def entries(pairs, phone_pairs=()):
        out = []
        for slug, pid in sorted(pairs):
            if (slug, pid) in blocked_pairs:
                continue
            if slug not in pub_cache:
                pub_cache[slug] = public_ids(slug)
            d, cc = meta.get((slug, pid), (None, None))
            out.append({'t': slug, 'i': pid, 'g': 0 if pid in pub_cache[slug] else 1,
                        'd': d, 'c': cc})
        # g=2: local phone library. Never public, never gated, never deployed.
        for slug, pid in sorted(phone_pairs):
            d, cc = meta.get((slug, pid), (None, None))
            out.append({'t': slug, 'i': pid, 'g': 2, 'd': d, 'c': cc})
        # Newest first: opening a person on their oldest trip (alphabetical by
        # slug) buried anything recent hundreds of rows down.
        out.sort(key=lambda e: (e.get('d') or '', e['t'], e['i']), reverse=True)
        return out

    def pairs_for(clusters):
        return ({p for c in clusters for p in by_cluster.get(c, set())},
                {p for c in clusters for p in phone_by_cluster.get(c, set())})

    people = []
    for key, person in sorted(roster.items(),
                              key=lambda kv: -sum(len(s) for s in pairs_for(kv[1]['clusters']))):
        cam, phone = pairs_for(person['clusters'])
        ph = entries(cam, phone)
        people.append({'key': key, 'label': person['label'], 'hide': person['hide'] or False,
                       'clusters': person['clusters'], 'n': len(ph), 'photos': ph})

    unnamed = []
    for cid in sorted(set(by_cluster) | set(phone_by_cluster),
                      key=lambda c: -(len(by_cluster.get(c, ())) + len(phone_by_cluster.get(c, ())))):
        if cid in assigned:
            continue
        ph = entries(by_cluster.get(cid, set()), phone_by_cluster.get(cid, set()))
        if ph:
            unnamed.append({'key': cid, 'label': cid, 'n': len(ph), 'photos': ph})

    return {'people': people, 'unnamed': unnamed,
            'n_blocked_hidden': len(blocked_pairs),
            'has_phone': include_phone}


def seed():
    """Create config/people.json from the local labels in local_browse/people.json
    (its `groups` map plus `me`). Everyone starts visible."""
    if ROSTER.exists():
        raise SystemExit(f"✗ {ROSTER.relative_to(ROOT)} already exists — edit it by hand "
                         "(or delete it first to re-seed)")
    local = _load(LOCAL_PEOPLE) or {}
    people = {}
    if local.get('me'):
        people['me'] = {'label': 'Ryan', 'clusters': sorted(local['me']), 'hide': False}
    for name, clusters in sorted((local.get('groups') or {}).items()):
        key = ''.join(ch if ch.isalnum() else '-' for ch in name.lower()).strip('-')
        people[key] = {'label': name, 'clusters': sorted(clusters), 'hide': False,
                       'keep_public': {}}
    ROSTER.parent.mkdir(parents=True, exist_ok=True)
    ROSTER.write_text(json.dumps({
        '_comment': "Who is hidden from the site. Set 'hide' per person (see _hide_options), "
                    "then run tools/people_index.py and rebuild + deploy. "
                    "tools/people_privacy_picker.py does all of that for you and shows what "
                    "each tier would hide first.",
        '_hide_options': {
            'false': 'VISIBLE. On the site as normal. Nothing is hidden.',
            'gated': 'GATED. Drops out of the public manifest.json into manifest.all.json, and '
                     'the /photos proxy 404s the image without the See All cookie. You can '
                     'still see these yourself. Same treatment a force_private photo gets.',
            'blocked': 'BLOCKED. Everything gated does, plus stripped from manifest.all.json '
                       'and refused by the image proxy even WITH See All. Nobody sees it on any '
                       'tier. The R2 object is left alone, and setting hide back restores the '
                       'manifests exactly.',
        },
        '_keep_public': 'Per-person escape hatch: {"<trip-slug>": ["<photo-id>"]}. Those photos '
                        'stay on the site even while the person is hidden — use it for wrong '
                        'face matches. It exempts from the PEOPLE rule only: a photo that is '
                        'already gated by the roof/bridge/section rules stays gated.',
        '_note': 'A person exclusion outranks force_public in config/photo_privacy.json, '
                 'including "*". Hiding someone is a decision about another person, so a '
                 'whitelist written for roof/bridge reasons does not override it — use '
                 'keep_public instead.',
        'people': people,
    }, ensure_ascii=False, indent=2) + '\n')
    print(f"✓ seeded {ROSTER.relative_to(ROOT)} with {len(people)} people (all visible)")


def write(payload: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + '\n')


def main():
    if '--seed' in sys.argv:
        seed()
        return
    roster = load_roster()
    if not roster:
        print(f"no roster at {ROSTER.relative_to(ROOT)} — run --seed first "
              "(writing an empty index so the privacy sync stays consistent)")
        write({'roster_digest': roster_digest({}), 'gated': {}, 'blocked': {}, 'by_person': {}})
        return
    payload, extra = resolve(roster)
    detail, stats = extra['detail'], extra['stats']

    if '--report' in sys.argv:
        for key, d in sorted(detail.items(), key=lambda kv: -kv[1]['n_photos']):
            tier = d['hide'] or 'visible'
            print(f"{d['label']:<24} {tier:<8} {d['n_photos']:>5} photos"
                  + (f"  ({d['n_kept']} kept public)" if d['n_kept'] else ''))
        print()

    write(payload)

    def count(doc):
        return sum(p['n'] for p in doc['people']) + sum(u['n'] for u in doc['unnamed'])

    site = site_payload(roster, detail)
    SITE_OUT.write_text(json.dumps(site, separators=(',', ':')) + '\n')
    print(f"✓ {SITE_OUT.relative_to(ROOT)}: {len(site['people'])} named, "
          f"{len(site['unnamed'])} unnamed clusters, {count(site)} photos (deployed → R2)"
          + (f", {site['n_blocked_hidden']} blocked and omitted" if site['n_blocked_hidden'] else ""))

    # The localhost variant. Written whenever the phone library mirror is present;
    # otherwise removed, so serve.sh can't seed a stale copy.
    if PHONE_TRIPS.is_dir():
        local = site_payload(roster, detail, include_phone=True)
        # Hand-curated sets (tools/curate_photos_of_person.py) ride along as extra
        # entries on the People page. Local document ONLY — they can reference the
        # phone library and backup photos, none of which exist on the deployed site.
        cmeta = photo_meta()
        for name, entries in (_load(ROOT / 'config' / 'curated_sets.json') or {}).items():
            for e in entries:
                d, cc = cmeta.get((e['t'], e['i']), (None, None))
                e.setdefault('d', d)
                e.setdefault('c', cc)
            local['people'].insert(0, {'key': name.lower().replace(' ', '-'), 'label': name,
                                       'hide': False, 'clusters': [], 'curated_set': True,
                                       'n': len(entries), 'photos': entries})
        LOCAL_OUT.write_text(json.dumps(local, separators=(',', ':')) + '\n')
        print(f"✓ {LOCAL_OUT.relative_to(ROOT)}: {count(local)} photos "
              f"(+{count(local) - count(site)} from the local phone library, localhost only)")
    elif LOCAL_OUT.exists():
        LOCAL_OUT.unlink()

    ng = sum(len(v) for v in payload['gated'].values())
    nb = sum(len(v) for v in payload['blocked'].values())
    hidden = [d['label'] for d in detail.values() if d['hide']]
    print(f"✓ {OUT.relative_to(ROOT)}: {ng} gated, {nb} blocked "
          f"across {len(set(payload['gated']) | set(payload['blocked']))} trips"
          + (f" · hiding {', '.join(hidden)}" if hidden else " · nobody hidden"))
    # Never silently drop coverage: say what the face data pointed at but couldn't reach.
    if stats['stale']:
        print(f"  ⚠ {len(stats['stale'])} face refs no longer match a manifest id "
              f"(re-edited/renamed since the last face index) — not hideable, "
              f"e.g. {sorted(stats['stale'])[0]}")
    if stats['no_manifest']:
        print(f"  ⚠ {len(stats['no_manifest'])} blog image sets have faces but no trip "
              f"manifest, so per-photo privacy cannot reach them: "
              f"{', '.join(sorted(stats['no_manifest']))}")


if __name__ == '__main__':
    main()
