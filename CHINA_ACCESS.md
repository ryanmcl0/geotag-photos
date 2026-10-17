# China access

Many visitors from China arrive from a link shared on WeChat, Xiaohongshu or
Bilibili and read the site in that app's built-in browser. That browser has no
translate button, and Google Translate is blocked in mainland China, so for
them the site has to load from China and make sense without machine
translation. This file records how reachability is tested and what was found.
It is the groundwork for a built-in Chinese version of the site.

## How to test from China

You can't test from the UK. Two free public services run the requests from
machines inside China, and `tools/china_access_check.py` asks them and prints
the results:

```
python3 tools/china_access_check.py                # DNS + site + every outside host it loads
python3 tools/china_access_check.py --only site --runs 3
python3 tools/china_access_check.py --only dns
```

1. **DNS: [ViewDNS Chinese firewall test](https://viewdns.info/chinesefirewall/?domain=ryanmcl.com).**
   Looks the domain up from five mainland cities. The Great Firewall's DNS
   blocking shows up as a wrong or missing IP address. The free page shows
   the IPs for two of the five cities, and a verdict for all five.
2. **HTTPS: [Globalping](https://globalping.io).** A free network of
   volunteer-hosted probes, including on Chinese networks. The script asks
   for probes tagged `eyeball-network`, which are consumer ISPs (China Mobile,
   Unicom, Telecom): the networks a phone opening a WeChat link is on. Each
   result gives the city, ISP, status code and timings, including TLS
   handshake time. A handshake that times out is the usual sign of
   interference or a dropped international route. The same check is
   available as a web form on globalping.io (location: China).

The site check fetches `/login`, because every other path redirects there
while the site password is on. It still goes through the same DNS, TLS (SNI)
and Cloudflare route as a real first visit.

Limits: these are a few dozen requests from volunteer machines, not real
visitors, so read the numbers as rough. `/login` is 7 KB; a gallery full of
photos is slower. Blocking in China changes over time, so rerun before a
link is shared somewhere big.

## Results, 2026-10-17

| What | Result |
|---|---|
| DNS, 5 cities | Not poisoned; same Cloudflare IPs as from the UK |
| Site over HTTPS, 34 fetches | 30 loaded, mostly in 0.6 to 1.5 s. 4 TLS handshake timeouts: China Unicom Shanghai (2), Unicom Tianjin, China Mobile Changsha |
| Esri map tiles | 10/10 |
| unpkg (Leaflet) | 9/10 |
| jsDelivr (PhotoSwipe) | **3/10** |

The domain is reachable and not blocked. The site is on Cloudflare's free
plan, which has no servers inside China, so traffic leaves the country.
Expect slower loads and the occasional dropped connection, not a block.

jsDelivr lost its ICP licence in China in 2021 and is unreliable there. It
served the PhotoSwipe lightbox, so most Chinese visitors could see a gallery
but tapping a photo did nothing.

## What was changed

Leaflet, Leaflet.markercluster and PhotoSwipe are now served from the site
itself, in `web/vendor/<package>@<version>/`. They are copied unmodified from
the npm registry; Leaflet's files match the integrity hashes the pages already
carried. PhotoSwipe's package has no `.min.css` files (jsDelivr made those on
the fly), so the plain `.css` files are used. The version is in the path, so
an upgrade is a new folder, not a silent change.

`web/_routes.json` excludes `/vendor/*` from Pages Functions, so these files
are served as plain static files. They don't run the middleware or count
toward the Workers Free daily request limit, and they don't need the site
password (they are public open-source files).

Still loaded from outside the site:

- **Esri map tiles** (`server.arcgisonline.com`): 10/10 from China.
- **YouTube** thumbnails and embeds on the Videos page: blocked in China.

## Built-in Chinese vs browser translation

For visitors on WeChat, Xiaohongshu or Bilibili, browser translation doesn't
exist: the in-app browsers can't translate, and Google Translate is blocked.
A built-in Chinese version also gets place, bridge and road names right (the
China data already carries `name_zh`), covers text added by scripts (map
pop-ups, tooltips), and lets Chinese search engines index the pages.
