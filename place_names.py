"""City and spot names for a coordinate, worldwide and offline.

Two levels, for the Places pages and site search:

  city  In China, the prefecture-level city whose boundary holds the point
        (丽江市 Lijiang covers the old town, Jade Dragon and the Jinsha bridges);
        the municipalities and SARs are one city each. Elsewhere, the largest
        town of CITY_POP+ people within CITY_KM (London, Paris, Seoul: what a
        visitor calls the city, not its districts), else, out in the country,
        the nearest town's GeoNames area (Bolzano, Highland, Naryn).
  spot  The nearest GeoNames town or neighbourhood (1,000+ people) within
        SPOT_KM: Shangri-La, Sesto, Spitalfields; out in the mountains, the
        nearest village. Remote points get no spot.

Data (git-ignored, downloaded once; see place_names.fetch()):
  config/geo/china_prefectures.geojson   DataV prefecture boundaries (as the provinces)
  .cache/geonames/cities1000.txt          GeoNames towns: coords, admin codes, names
  .cache/geonames/admin2Codes.txt, admin1CodesASCII.txt   area names

Keys are stable across runs so local data can point at the pages: a city is
"cn-<adcode>", "<cc>-g<geonameid>" (a town) or "<cc>-<admin1>[-<admin2>]", a
spot is "g<geonameid>".
"""
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PREFECTURES = ROOT / 'config' / 'geo' / 'china_prefectures.geojson'
GEONAMES = ROOT / '.cache' / 'geonames'
SOURCES = {
    PREFECTURES: 'https://geo.datav.aliyun.com/areas_v3/bound/100000_full_city.json',
    GEONAMES / 'cities1000.zip': 'https://download.geonames.org/export/dump/cities1000.zip',
    GEONAMES / 'admin2Codes.txt': 'https://download.geonames.org/export/dump/admin2Codes.txt',
    GEONAMES / 'admin1CodesASCII.txt': 'https://download.geonames.org/export/dump/admin1CodesASCII.txt',
}

SPOT_KM = 20      # a spot is the nearest town this close (remote places: the nearest village)
CITY_KM = 20      # a city is the largest town this close ...
CITY_POP = 50000  # ... of at least this many people
AREA_KM = 60      # within this of a town, its second-level area is the city ...
REGION_KM = 300   # ... further out, its first-level region; beyond this, nothing (open sea)

# The municipalities and the SARs: DataV lists their districts, a visitor
# thinks of the city. → (English, Chinese, country code). Taiwan has no city
# breakdown there (one province shape, skipped), so GeoNames names its cities.
MUNICIPALITIES = {110000: ('Beijing', '北京', 'CN'), 120000: ('Tianjin', '天津', 'CN'),
                  310000: ('Shanghai', '上海', 'CN'), 500000: ('Chongqing', '重庆', 'CN'),
                  810000: ('Hong Kong', '香港', 'HK'), 820000: ('Macau', '澳门', 'MO')}

# GeoNames' English for prefectures carries the administrative form and the
# autonomous nationalities ("Haixi Mongolzu Zangzu Zizhizhou"); the short name
# is what's left once those are gone.
_EN_FORM = re.compile(r'\s+(Shi|Diqu|Zizhizhou|Meng|Prefecture|Autonomous Prefecture|'
                      r'Autonomous Region|City|League|Region)$', re.I)
_EN_ETHNIC = re.compile(r'\s+(\w+zu|Tibetan|Mongol|Mongolian|Kazakh|Kirghiz|Kyrgyz|Hui|Yi|Bai|Dai|'
                        r'Miao|Dong|Buyei|Bouyei|Tujia|Zhuang|Qiang|Korean|Hani|Lisu|Jingpo|Uygur|'
                        r'Uyghur|and)$', re.I)
_ZH_FORM = re.compile(r'(市|地区|盟|自治州|特别行政区)$')
_ZH_ETHNIC = re.compile(r'(藏族|彝族|回族|蒙古族|蒙古|哈萨克|柯尔克孜|傣族|景颇族|白族|哈尼族|苗族|侗族|'
                        r'布依族|土家族|壮族|傈僳族|羌族|朝鲜族|侗|苗|壮)$')
COUNTRY_NAMES = {
    'AD': 'Andorra', 'AE': 'United Arab Emirates', 'AL': 'Albania', 'AT': 'Austria',
    'BA': 'Bosnia and Herzegovina', 'BE': 'Belgium', 'BG': 'Bulgaria', 'BH': 'Bahrain',
    'CH': 'Switzerland', 'CN': 'China', 'CY': 'Cyprus', 'CZ': 'Czechia', 'DE': 'Germany',
    'DK': 'Denmark', 'EE': 'Estonia', 'EG': 'Egypt', 'EH': 'Western Sahara', 'ES': 'Spain',
    'FI': 'Finland', 'FR': 'France', 'GB': 'United Kingdom', 'GR': 'Greece', 'HK': 'Hong Kong',
    'HR': 'Croatia', 'HU': 'Hungary', 'ID': 'Indonesia', 'IE': 'Ireland', 'IL': 'Israel',
    'IN': 'India', 'IS': 'Iceland', 'IT': 'Italy', 'JP': 'Japan', 'KG': 'Kyrgyzstan',
    'KR': 'South Korea', 'KZ': 'Kazakhstan', 'LI': 'Liechtenstein', 'LT': 'Lithuania',
    'LU': 'Luxembourg', 'LV': 'Latvia', 'MA': 'Morocco', 'MC': 'Monaco', 'ME': 'Montenegro',
    'MK': 'North Macedonia', 'MN': 'Mongolia', 'MO': 'Macau', 'MR': 'Mauritania',
    'MY': 'Malaysia', 'NL': 'Netherlands', 'NO': 'Norway', 'PH': 'Philippines', 'PL': 'Poland',
    'PT': 'Portugal', 'RO': 'Romania', 'RS': 'Serbia', 'SE': 'Sweden', 'SG': 'Singapore',
    'SI': 'Slovenia', 'SK': 'Slovakia', 'SM': 'San Marino', 'TH': 'Thailand', 'TN': 'Tunisia',
    'TR': 'Türkiye', 'TW': 'Taiwan', 'US': 'United States', 'VA': 'Vatican City',
    'VN': 'Vietnam',
}

# Mongolia's provinces by their own names, not GeoNames' translations ("Middle Govĭ" is Dundgovi,
# "Central Aimak" is Töv): what a map, a sign or a local says.
ADMIN1_NATIVE = {
    'MN.01': 'Arkhangai', 'MN.02': 'Bayankhongor', 'MN.03': 'Bayan-Ölgii', 'MN.06': 'Dornod',
    'MN.07': 'Dornogovi', 'MN.08': 'Dundgovi', 'MN.09': 'Zavkhan', 'MN.10': 'Govi-Altai', 'MN.11': 'Khentii',
    'MN.12': 'Khovd', 'MN.13': 'Khövsgöl', 'MN.14': 'Ömnögovi', 'MN.15': 'Övörkhangai', 'MN.16': 'Selenge',
    'MN.17': 'Sükhbaatar', 'MN.18': 'Töv', 'MN.19': 'Uvs', 'MN.20': 'Ulaanbaatar', 'MN.21': 'Bulgan',
    'MN.23': 'Darkhan-Uul', 'MN.24': 'Govisümber', 'MN.25': 'Orkhon',
}

# Towns GeoNames knows by an older English name.
TOWN_NATIVE = {'Ulan Bator': 'Ulaanbaatar'}

# The usual English for prefectures GeoNames gives in pinyin; the pinyin stays
# a search alias.
EN_COMMON = {'Lupanshui': 'Liupanshui', 'Kashi': 'Kashgar', 'Hetian': 'Hotan', 'Tulufan': 'Turpan', 'Kezilesu': 'Kizilsu',
             'Aletai': 'Altay', 'Yili': 'Ili', 'Kelamayi': 'Karamay', 'Tacheng': 'Tacheng',
             'Bayinguoleng': 'Bayingolin', 'Boertala': 'Bortala', 'Deqen': 'Diqing',
             'Yichangshi': 'Yichang', 'Wulumuqi': 'Urumqi', 'Hami': 'Kumul', 'Changji': 'Changji',
             'Rikaze': 'Shigatse', 'Linzhi': 'Nyingchi', 'Shannan': 'Lhoka', 'Naqu': 'Nagqu',
             'Ali': 'Ngari', 'Changdu': 'Chamdo', 'Lasa': 'Lhasa', 'Guoluo': 'Golog',
             'Abazhou': 'Ngawa', 'Ganzi': 'Garze', 'Huangnan': 'Huangnan'}
# Area names elsewhere that lead or end with their administrative form.
_AREA_AFFIX = re.compile(r'^(Provincia (autonoma )?(di|del|della|de|dell\')\s*|Città metropolitana di |'
                         r'Bashkia |Politischer Bezirk |Bezirk |Statutarstadt |Rrethi i |Landkreis |'
                         r'Kreisfreie Stadt |Regierungsbezirk |Département (de |du |de la |des )|'
                         r'Comarca de |Municipality of |Okres |Obshtina |Grad |Sveitarfélagið |'
                         r'Kabupaten |Kota |Amphoe |Khet |Huyện |Quận |Thành Phố |Délégation de |'
                         r'Ville de |WP\. )|'
                         r'(\s+(District|County|Municipality|Kommune|kommune|Province|Region|Oblast|'
                         r'Prefecture|Governorate|Rayon|Raion|Stadt|sub-region|Shi|Division))$')
_HAN = re.compile(r'^[一-鿿]+$')


def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def _strip(rx, s):
    prev = None
    while prev != s:
        prev, s = s, rx.sub('', s).strip()
    return s


def en_prefecture(name):
    return _strip(_EN_ETHNIC, _strip(_EN_FORM, name)).split(' ')[0] if ' ' in name else name


def zh_prefecture(name):
    return _strip(_ZH_ETHNIC, _strip(_ZH_FORM, name))


def clean_area(name):
    return _strip(_AREA_AFFIX, name)


def fetch(echo=print):
    """Download any missing data file (they are git-ignored)."""
    import urllib.request
    import zipfile
    for path, url in SOURCES.items():
        target = path.with_suffix('.txt') if path.suffix == '.zip' else path
        if target.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        echo(f"  downloading {url}")
        urllib.request.urlretrieve(url, path)
        if path.suffix == '.zip':
            with zipfile.ZipFile(path) as z:
                z.extractall(path.parent)


def _ring_contains(ring, x, y):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class PlaceIndex:
    def __init__(self):
        fetch()
        import numpy as np
        from scipy.spatial import cKDTree

        self.admin1 = {}
        for line in (GEONAMES / 'admin1CodesASCII.txt').read_text().splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                self.admin1[parts[0]] = ADMIN1_NATIVE.get(parts[0]) or parts[1] or parts[2]
        self.admin2 = {}
        self.cn_admin2 = {}   # "5307" (adcode // 100) → English
        for line in (GEONAMES / 'admin2Codes.txt').read_text().splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                self.admin2[parts[0]] = parts[1] or parts[2]
                if parts[0].startswith('CN.'):   # ASCII: "Deqen", not "Dêqên"
                    self.cn_admin2[parts[0].split('.')[-1]] = parts[2] or parts[1]

        self.towns = []   # (geonameid, name, cc, admin1, admin2, zh, population)
        self.town_ll = []  # (lat, lon) per town, same order
        coords = []
        with open(GEONAMES / 'cities1000.txt', encoding='utf-8') as f:
            for line in f:
                c = line.rstrip('\n').split('\t')
                if c[6] != 'P':
                    continue
                zh = next((a for a in c[3].split(',') if _HAN.match(a)), None) if c[3] else None
                self.towns.append((c[0], TOWN_NATIVE.get(c[1], c[1]), c[8], c[10], c[11], zh, int(c[14] or 0)))
                self.town_ll.append((float(c[4]), float(c[5])))
                lat, lon = math.radians(float(c[4])), math.radians(float(c[5]))
                coords.append((math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)))
        self.tree = cKDTree(np.array(coords))

        self.prefectures = []   # (key, en, zh, cc, [(ext, holes, bbox)])
        self.pref_meta = {}     # key → (province adcode, centroid lon/lat)
        self.aliases = {}       # city key → other names it is searched by
        for feat in json.loads(PREFECTURES.read_text()).get('features', []):
            props = feat.get('properties') or {}
            geom = feat.get('geometry') or {}
            if props.get('level') not in ('city', 'district') or not geom:
                continue
            adcode = props['adcode']
            parent = (props.get('parent') or {}).get('adcode')
            cc = 'CN'
            if parent in MUNICIPALITIES:
                key = f'cn-{parent}'
                en, zh, cc = MUNICIPALITIES[parent]
            else:
                key = f'cn-{adcode}'
                zh = zh_prefecture(props['name'])
                en = self.cn_admin2.get(str(adcode // 100))
                en = en_prefecture(en) if en else zh
                pinyin = en
                en = EN_COMMON.get(en, en)
                if en != pinyin:
                    self.aliases[key] = [pinyin]
            polys = []
            raw = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
            for poly in raw:
                if poly:
                    xs = [p[0] for p in poly[0]]
                    ys = [p[1] for p in poly[0]]
                    polys.append((poly[0], poly[1:], (min(xs), min(ys), max(xs), max(ys))))
            self.prefectures.append((key, en, zh, cc, polys))
            self.pref_meta.setdefault(key, (parent, props.get('centroid') or props.get('center')))
        self._cache = {}

    def _prefecture(self, lat, lon):
        if not (17 <= lat <= 54 and 73 <= lon <= 136):
            return None
        for key, en, zh, cc, polys in self.prefectures:
            for ext, holes, (x0, y0, x1, y1) in polys:
                if x0 <= lon <= x1 and y0 <= lat <= y1 and _ring_contains(ext, lon, lat) \
                        and not any(_ring_contains(h, lon, lat) for h in holes):
                    return key, en, zh, cc
        return None

    def towns_in(self, south, west, north, east, limit=40):
        """[(name, lat, lon, population)] of the towns inside a box, most populous first."""
        out = [(t[1], la, lo, t[6]) for t, (la, lo) in zip(self.towns, self.town_ll)
               if south <= la <= north and west <= lo <= east]
        return sorted(out, key=lambda x: -x[3])[:limit]

    def _big_town(self, la, lo):
        """Index of the most populous CITY_POP+ town within CITY_KM, or None."""
        chord = 2 * math.sin(CITY_KM / 6371 / 2)
        near = self.tree.query_ball_point(
            (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)), chord)
        big = [j for j in near if self.towns[j][6] >= CITY_POP]
        return max(big, key=lambda j: self.towns[j][6]) if big else None

    def _in_province(self, pref, province, lat, lon):
        """A hand-set province (config/province_overrides.json, by adcode) beats the
        boundary: a bridge pinned just over a border river belongs to the side it is
        filed under. → that province's prefecture nearest the point, else `pref`."""
        if not pref or not province or self.pref_meta[pref[0]][0] == province:
            return pref
        best = None
        for key, en, zh, cc, _ in self.prefectures:
            parent, c = self.pref_meta[key]
            if parent == province and c:
                d = (c[0] - lon) ** 2 + (c[1] - lat) ** 2
                if best is None or d < best[0]:
                    best = (d, (key, en, zh, cc))
        return best[1] if best else pref

    def lookup(self, lat, lon, province=None):
        """→ {cc, city_key, city, city_zh, town, town_zh, spot_key, spot, spot_zh, spot_pop} (town: the
        largest CITY_POP+ town within CITY_KM; spot_* None when no town is within SPOT_KM), or None
        far from everything. `province`: the
        adcode of a hand-set Chinese province the point must be filed under."""
        ck = (round(lat, 4), round(lon, 4), province)
        if ck in self._cache:
            return self._cache[ck]
        la, lo = math.radians(lat), math.radians(lon)
        chord, i = self.tree.query((math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)))
        km = 2 * 6371 * math.asin(min(1.0, chord / 2))
        gid, name, cc, a1, a2, zh, pop = self.towns[i]
        out = None
        pref = self._in_province(self._prefecture(lat, lon), province, lat, lon)
        if pref:
            key, en, pzh, pcc = pref
            out = {'cc': pcc, 'city_key': key, 'city': en, 'city_zh': pzh}
        elif (big := self._big_town(la, lo)) is not None:
            bg, bname, bcc, _, _, bzh, _ = self.towns[big]
            out = {'cc': bcc, 'city_key': f'{bcc}-g{bg}'.lower(), 'city': bname, 'city_zh': bzh}
        elif km <= REGION_KM:
            if km <= AREA_KM and a2 and f'{cc}.{a1}.{a2}' in self.admin2:
                out = {'cc': cc, 'city_key': f'{cc}-{a1}-{a2}'.lower(),
                       'city': clean_area(self.admin2[f'{cc}.{a1}.{a2}']), 'city_zh': None}
            elif f'{cc}.{a1}' in self.admin1:
                out = {'cc': cc, 'city_key': f'{cc}-{a1}'.lower(),
                       'city': clean_area(self.admin1[f'{cc}.{a1}']), 'city_zh': None}
        if out is not None:
            # the largest town around, whatever the city level said (a Chinese prefecture is
            # often named for one town and covers a desert): what a caption would call the place
            big = self._big_town(la, lo)
            out.update(town=self.towns[big][1] if big is not None else None,
                       town_zh=self.towns[big][5] if big is not None else None)
            near = km <= SPOT_KM
            out.update(spot_key=f'g{gid}' if near else None, spot=name if near else None,
                       spot_zh=zh if near else None, spot_pop=pop if near else None)
        self._cache[ck] = out
        return out
