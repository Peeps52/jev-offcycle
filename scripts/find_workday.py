#!/usr/bin/env python3
"""Discover a firm's Workday tenant/site by following the root redirect,
then confirm with a real jobs POST. Prints "tenant|wdN|site  jobs".
Usage: python3 scripts/find_workday.py lazard evercore ..."""
import json, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "jev-offcycle/0.4 workday-probe"
SERVERS = ["wd1", "wd3", "wd5", "wd12", "wd103", "wd108"]

def confirm(t, wd, site):
    url = f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs"
    body = json.dumps({"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read()).get("total")
    except Exception:
        return None

def find(t):
    for wd in SERVERS:
        try:
            req = urllib.request.Request(f"https://{t}.{wd}.myworkdayjobs.com/", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                final = r.geturl()
        except Exception:
            continue
        parts = [p for p in final.split("myworkdayjobs.com/", 1)[-1].split("/") if p]
        parts = [p for p in parts if not (len(p) == 5 and p[2] == "-")]  # drop en-US
        if parts:
            n = confirm(t, wd, parts[0])
            if n is not None:
                return f"{t}|{wd}|{parts[0]}", n
    return None, None

with ThreadPoolExecutor(8) as ex:
    for t, (slug, n) in zip(sys.argv[1:], ex.map(find, sys.argv[1:])):
        print(f"{t:<18} {slug or 'NOT FOUND':<40} {'' if n is None else n}")
