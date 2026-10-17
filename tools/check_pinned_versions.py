#!/usr/bin/env python3
"""Report pinned dependencies that have a newer release. Changes nothing.

Finds the pins itself, so a new one is picked up without editing this file:
  - web/vendor/<package>@<version>/   self-hosted npm libraries
  - <cdn>/<package>@<version>/ URLs   unpkg / jsDelivr / cdnjs in tracked files
  - uses: <owner>/<repo>@<ref>        GitHub Actions in .github/workflows

and compares each against the registry (npm, or GitHub releases for actions).
requirements.txt only sets minimum versions (>=), so it never falls behind
and isn't checked.

Run weekly by .github/workflows/pinned-versions.yml, which turns the report
into a GitHub issue. Locally:

  python3 tools/check_pinned_versions.py              # print the report
  python3 tools/check_pinned_versions.py --out r.md   # also write it, for the workflow

Exit code is 0 either way; the report's first line says whether anything is out of date.
"""
import argparse
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CDN_RE = re.compile(r"(?:unpkg\.com|cdn\.jsdelivr\.net/npm|cdnjs\.cloudflare\.com/ajax/libs)/"
                    r"((?:@[\w.-]+/)?[\w.-]+)@(\d[\w.-]*)")
USES_RE = re.compile(r"^\s*-?\s*uses:\s*([\w.-]+/[\w.-]+)(?:/[\w./-]+)?@([\w.-]+)", re.M)


def get_json(url):
    headers = {"User-Agent": "check_pinned_versions", "Accept": "application/json"}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.load(r)


def vtuple(v):
    """'v4.2.1' -> (4, 2, 1); None for pre-releases like 2.0.0-alpha.1."""
    v = v.lstrip("v")
    if not re.fullmatch(r"\d+(\.\d+)*", v):
        return None
    return tuple(int(x) for x in v.split("."))


def find_pins():
    pins = {}  # (kind, name, version) -> set of files
    vendor = ROOT / "web" / "vendor"
    for d in sorted(vendor.iterdir()) if vendor.exists() else []:
        if d.is_dir() and "@" in d.name[1:]:
            name, ver = d.name.rsplit("@", 1)
            pins.setdefault(("npm", name, ver), set()).add(f"web/vendor/{d.name}/")
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    for f in tracked:
        if not f.endswith((".html", ".js", ".py", ".ts", ".md", ".yml", ".yaml")):
            continue
        text = (ROOT / f).read_text(errors="replace")
        if f.startswith(".github/workflows/"):
            for repo, ref in USES_RE.findall(text):
                pins.setdefault(("action", repo, ref), set()).add(f)
        for name, ver in CDN_RE.findall(text):
            pins.setdefault(("npm", name, ver), set()).add(f)
    return pins


def latest_npm(name):
    d = get_json(f"https://registry.npmjs.org/{name}")
    stable = [v for v in d.get("versions", {}) if vtuple(v)]
    return d.get("dist-tags", {}).get("latest"), stable


def latest_action(repo):
    tags = [t["name"] for t in get_json(f"https://api.github.com/repos/{repo}/tags?per_page=100")]
    stable = [t for t in tags if vtuple(t)]
    return max(stable, key=vtuple, default=None), stable


def check(kind, name, ver):
    """-> (latest overall, latest with the same major, or None if up to date)."""
    latest, stable = latest_npm(name) if kind == "npm" else latest_action(name)
    cur = vtuple(ver)
    if not latest or not cur:
        return latest, None
    # A floating action ref like v4 tracks every v4.x, so only a new major counts.
    depth = len(cur) if kind == "npm" else len(ver.lstrip("v").split("."))
    newer = [v for v in stable if vtuple(v)[:depth] > cur]
    same_major = max((v for v in newer if vtuple(v)[0] == cur[0]), key=vtuple, default=None)
    return (latest if newer and vtuple(latest) > cur else None), same_major


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write the markdown report here")
    a = ap.parse_args()

    rows, errors = [], []
    for (kind, name, ver), files in sorted(find_pins().items()):
        try:
            latest, same_major = check(kind, name, ver)
        except Exception as e:  # one registry hiccup shouldn't hide the rest
            errors.append(f"- `{name}@{ver}`: {e}")
            continue
        if latest or same_major:
            major = latest and vtuple(latest)[0] > vtuple(ver)[0]
            rows.append(f"| `{name}` | {ver} | {same_major or '-'} | {latest or '-'}"
                        f"{' (new major)' if major else ''} | {', '.join(sorted(files))} |")

    if rows:
        body = [f"{len(rows)} pinned dependenc{'y has' if len(rows) == 1 else 'ies have'} a newer release.", "",
                "| Package | Pinned | Newer, same major | Latest | Where |", "|---|---|---|---|---|", *rows, "",
                "Nothing has been changed. A new major version usually has breaking changes: "
                "read its changelog before upgrading. To upgrade a self-hosted library, add a "
                "web/vendor/<package>@<new version>/ folder and point the pages at it."]
    else:
        body = ["All pinned dependencies are up to date."]
    if errors:
        body += ["", "Could not check:", *errors]
    report = "\n".join(body) + "\n"
    print(report)
    if a.out:
        Path(a.out).write_text(report)


if __name__ == "__main__":
    main()
