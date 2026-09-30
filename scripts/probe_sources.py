#!/usr/bin/env python3
"""Find which ATS each candidate employer actually uses, and verify it.

    python3 scripts/probe_sources.py scripts/candidates.txt > examples/sources.json

A seed list written from memory is a list of guesses. This tries every
candidate against every supported ATS, keeps only boards that answer with a
real job list, and records WHY each miss missed. That distinction matters:

    ok          board found and it returned jobs
    empty       board found, currently advertising nothing
    not_found   no board under any slug variant we tried (probably a
                different ATS, or an in-house careers site)

Candidates file: one employer per line, `Name | category | slug1,slug2`.
Slugs are optional; without them we derive a few variants from the name.

Different ATS hosts are probed in parallel, one request per second per host,
matching crawl.py's courtesy.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = "jev-offcycle/0.4 source-probe (+https://github.com/Peeps52/jev-offcycle)"

ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{s}/jobs", "jobs"),
    "lever":      ("https://api.lever.co/v0/postings/{s}?mode=json", None),
    "ashby":      ("https://api.ashbyhq.com/posting-api/job-board/{s}", "jobs"),
    "workable":   ("https://apply.workable.com/api/v1/widget/accounts/{s}", "jobs"),
    "recruitee":  ("https://{s}.recruitee.com/api/offers/", "offers"),
    "personio":   ("https://{s}.jobs.personio.de/search.json", None),
    "teamtailor": ("https://{s}.teamtailor.com/jobs.json", "jobs"),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=100", "content"),
}

_last: dict[str, float] = {}
_lock = threading.Lock()


def _get(url: str):
    host = url.split("/")[2]
    with _lock:
        wait = 1.0 - (time.monotonic() - _last.get(host, 0))
        _last[host] = time.monotonic() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            if b"<html" in body[:400].lower():
                return None          # redirected to a marketing page, not a board
            return json.loads(body)
    except Exception:
        return None


def variants(name: str, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    out = [base.replace(" ", ""), base.replace(" ", "-")]
    return list(dict.fromkeys(v for v in out if v))


def probe_one(ats: str, slug: str):
    url, key = ENDPOINTS[ats]
    d = _get(url.format(s=slug))
    if d is None:
        return None
    jobs = d if key is None else (d.get(key) if isinstance(d, dict) else None)
    if not isinstance(jobs, list):
        return None
    return len(jobs)


def probe(line: str) -> dict:
    parts = [p.strip() for p in line.split("|")]
    name = parts[0]
    cat = parts[1] if len(parts) > 1 else ""
    explicit = [s.strip() for s in parts[2].split(",")] if len(parts) > 2 and parts[2] else []
    best = None
    for ats in ENDPOINTS:
        for slug in variants(name, explicit):
            n = probe_one(ats, slug)
            if n is not None and (best is None or n > best["jobs"]):
                best = {"ats": ats, "slug": slug, "jobs": n}
    status = "not_found" if best is None else ("ok" if best["jobs"] else "empty")
    return {"name": name, "category": cat, "status": status, **(best or {})}


def main() -> int:
    lines = [l for l in Path(sys.argv[1]).read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    with ThreadPoolExecutor(max_workers=24) as ex:
        results = list(ex.map(probe, lines))
    ok = [r for r in results if r["status"] == "ok"]
    json.dump([{"ats": r["ats"], "slug": r["slug"], "name": r["name"],
                "category": r["category"]} for r in ok], sys.stdout, indent=2)
    report = Path(sys.argv[1]).with_suffix(".probe.json")
    report.write_text(json.dumps(results, indent=2))
    counts = {s: sum(r["status"] == s for r in results) for s in ("ok", "empty", "not_found")}
    print(f"\n{counts}  full report: {report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
