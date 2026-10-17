#!/usr/bin/env python3
"""Check whether the site (and what it loads) is reachable from mainland China.

Two free public services do the testing from inside China; this script only
asks them and prints what they saw. See docs/china-access.md for the method
and past results.

  1. DNS: ViewDNS.info's Chinese firewall test looks the domain up from five
     mainland cities. A poisoned (Great Firewall) answer shows up as a wrong
     or missing IP.
  2. HTTPS: Globalping (globalping.io) fetches a URL from volunteer probes in
     China. "eyeball" probes sit on consumer ISPs (China Mobile, Unicom,
     Telecom): the networks a phone opening a WeChat link is on.

Usage:
  python3 tools/china_access_check.py                   # site + every host it loads
  python3 tools/china_access_check.py --only site --runs 3
  python3 tools/china_access_check.py --probes 20 --any-network

No API key needed; Globalping allows a few hundred anonymous probe results
per hour. Fetches /login because every other page redirects there while the
site password is on.
"""
import argparse
import json
import re
import statistics
import time
import urllib.request

DOMAIN = "ryanmcl.com"
UA = {"User-Agent": "Mozilla/5.0 (china_access_check)"}

# What a visitor's browser has to reach. Keep in step with the hosts the pages
# load from (grep -rhoE "https://[a-z.]+" web/*.html web/js).
TARGETS = {
    "site": (DOMAIN, "/login"),
    "map tiles (Esri)": ("server.arcgisonline.com",
                         "/ArcGIS/rest/services/World_Street_Map/MapServer/tile/4/6/12"),
    "lightbox (jsDelivr, no longer used)": ("cdn.jsdelivr.net",
                                           "/npm/photoswipe@4.1.3/dist/photoswipe.min.js"),
    "Leaflet (unpkg, no longer used)": ("unpkg.com", "/leaflet@1.9.4/dist/leaflet.js"),
}


def http_json(url, body=None):
    req = urllib.request.Request(url, headers={**UA, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def dns_check(domain):
    req = urllib.request.Request(f"https://viewdns.info/chinesefirewall/?domain={domain}", headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    rows = re.findall(r'"location":\s*"([^"]+)",\s*"resultvalue":\s*"([^"]*)",\s*"resultstatus":\s*"([^"]+)"', html)
    verdict = re.search(r"((?:All|Some|No) servers? (?:were|was)[^.<]*\.)", html)
    return rows, verdict.group(1).strip() if verdict else "no verdict found on the page"


def http_check(host, path, probes, eyeball):
    loc = {"magic": "CN", "limit": probes}
    if eyeball:
        loc["tags"] = ["eyeball-network"]
    m = http_json("https://api.globalping.io/v1/measurements", {
        "type": "http", "target": host, "locations": [loc],
        "measurementOptions": {"protocol": "HTTPS", "request": {"method": "GET", "path": path}},
    })
    for _ in range(30):
        time.sleep(3)
        d = http_json(f"https://api.globalping.io/v1/measurements/{m['id']}")
        if d["status"] == "finished":
            return d["results"]
    return d["results"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["site", "dns", "all"], default="all")
    ap.add_argument("--runs", type=int, default=1, help="repeat the site fetch (failures are intermittent)")
    ap.add_argument("--probes", type=int, default=10)
    ap.add_argument("--any-network", action="store_true", help="include data-centre probes")
    a = ap.parse_args()

    if a.only in ("all", "dns"):
        rows, verdict = dns_check(DOMAIN)
        print(f"DNS from China ({DOMAIN})")
        for loc, ips, status in rows:
            if status != "xx":  # ViewDNS masks some rows on the free page
                print(f"  {loc:<22} {status:<4} {ips}")
        print(f"  {verdict}\n")
        if a.only == "dns":
            return

    targets = {"site": TARGETS["site"]} if a.only == "site" else TARGETS
    for name, (host, path) in targets.items():
        for run in range(a.runs if name == "site" else 1):
            results = http_check(host, path, a.probes, not a.any_network)
            ok = [r for r in results if r["result"].get("statusCode")]
            times = [r["result"]["timings"]["total"] for r in ok]
            med = f"median {statistics.median(times):.0f} ms" if times else ""
            print(f"HTTPS {name}: {len(ok)}/{len(results)} loaded {med}")
            for r in results:
                p, x = r["probe"], r["result"]
                t = (x.get("timings") or {}).get("total")
                why = "" if x.get("statusCode") else (x.get("rawOutput") or x.get("status") or "")[:55]
                print(f"  {p['city'][:11]:<11} {(p.get('network') or '')[:30]:<30} "
                      f"{x.get('statusCode') or 'FAIL':<5} {t or '-':>6} ms  {why}")
            print()


if __name__ == "__main__":
    main()
