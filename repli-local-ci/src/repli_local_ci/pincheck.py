"""Compare runner-pin.json with the official release metadata. Explicit command; never runs implicitly."""
from __future__ import annotations

import json
import os
import re
import urllib.request

from .runner_install import Pin

API = "https://api.github.com/repos/actions/runner/releases"


def parse_release_sha(body: str, asset: str):
    m = re.search(r"<!--\s*BEGIN SHA (osx-arm64)\s*-->([0-9a-f]{64})<!--\s*END SHA \1\s*-->", body or "")
    if m:
        return m.group(2)
    for line in (body or "").splitlines():
        if asset in line:
            h = re.findall(r"\b[0-9a-f]{64}\b", line)
            if len(h) == 1:
                return h[0]
    return None


def default_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "repli-local-ci"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")  # only ever sent to api.github.com
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        return json.load(r)


def check_pin(pin: Pin, fetch_json=default_json):
    """Returns (problems, notes). Any problem means the pin no longer matches the official release."""
    problems, notes = [], []
    if not pin.pinned:
        problems.append("sha256 not populated in runner-pin.json")
    rel = fetch_json(f"{API}/tags/v{pin.version}")
    if rel.get("tag_name") != f"v{pin.version}":
        problems.append(f"release tag mismatch: {rel.get('tag_name')!r}")
    if rel.get("draft") or rel.get("prerelease"):
        problems.append("release is a draft/prerelease")
    urls = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets", [])}
    if urls.get(pin.asset) != pin.url:
        problems.append("asset missing or its download URL differs from the pin")
    official = parse_release_sha(rel.get("body", ""), pin.asset)
    if official is None:
        problems.append("could not find the official SHA-256 for osx-arm64 in the release notes")
    elif official != pin.sha256:
        problems.append(f"SHA-256 mismatch: pin {pin.sha256} != official {official}")
    latest = fetch_json(f"{API}/latest").get("tag_name")
    if latest and latest != f"v{pin.version}":
        notes.append(f"a newer official release exists ({latest}); update the pin through a reviewed PR (never at runtime)")
    return problems, notes
