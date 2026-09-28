"""Find listings. Applicant-tracking JSON first, browser agent only if needed.

Most job boards are a React shell around a JSON API. Trackr returns an empty
page to any HTTP fetch, which is what sent me looking -- and the answer turned
out not to be a better scraper but a different door. Greenhouse, Lever and
Ashby all expose public, unauthenticated, structured endpoints, and that is
where startups actually post. No browser, no login, no parsing HTML that
changes next week.

    greenhouse  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
    lever       https://api.lever.co/v0/postings/{slug}?mode=json
    ashby       https://api.ashbyhq.com/posting-api/job-board/{slug}
    workable    https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
    recruitee   https://{slug}.recruitee.com/api/offers/
    personio    https://{slug}.jobs.personio.de/search.json
    teamtailor  https://{slug}.teamtailor.com/jobs.json

The last four came from reading jobleft (MIT, Blueturboguy07/jobleft), which
covers eight platforms to my original three. Personio and Teamtailor matter
disproportionately here: both are European, and a US-shaped job tool tends to
skip them.

Crawling politely is not optional. One request per second per host, a real
User-Agent, and robots.txt respected -- also from jobleft, and the right
default regardless of whether anyone is watching.

`jev_ultrafast` remains the fallback for sites with no API at all, but reach
for it last: it needs Chrome, a TypeSafe or OpenRouter key, and roughly 310ms
of thinking per click. A JSON endpoint costs one request and nothing else.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass

from .classify import Listing

UA = "jev-offcycle/0.3 (+https://github.com/Peeps52/jev-offcycle)"
TIMEOUT = 20
RATE_LIMIT_S = 1.0          # per host, matching jobleft's courtesy

_last_hit: dict[str, float] = {}
_robots: dict[str, object] = {}


def _throttle(url: str) -> None:
    host = urllib.parse.urlparse(url).netloc
    wait = RATE_LIMIT_S - (time.monotonic() - _last_hit.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def _allowed(url: str) -> bool:
    """Honour robots.txt. Fails OPEN on an unreachable robots file, which is
    the convention -- but fails CLOSED on an explicit disallow."""
    parts = urllib.parse.urlparse(url)
    host = f"{parts.scheme}://{parts.netloc}"
    rp = _robots.get(host)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{host}/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = False          # unreachable: treat as permitted
        _robots[host] = rp
    if rp is False:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


@dataclass
class Source:
    """One company's job board."""

    ats: str        # greenhouse | lever | ashby
    slug: str       # the board identifier
    name: str = ""  # display name; defaults to the slug

    def label(self) -> str:
        return self.name or self.slug


def _get(url: str) -> dict | list | None:
    if not _allowed(url):
        return None
    _throttle(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        # A board that 404s is a board that moved or never existed. Returning
        # None lets the caller count it and carry on; raising would abandon
        # the other 199 companies over one dead slug.
        return None


def _clean(html: str) -> str:
    """ATS descriptions are HTML. Strip it -- Jev reads the words, not the markup,
    and markup inflates the token count you pay for."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</li>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", t)).strip()


def _greenhouse(s: Source) -> list[Listing]:
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{s.slug}/jobs?content=true")
    if not isinstance(d, dict):
        return []
    out = []
    for j in d.get("jobs", []):
        out.append(Listing(
            title=j.get("title", ""), organisation=s.label(),
            location=(j.get("location") or {}).get("name", ""),
            description=_clean(j.get("content", ""))[:6000],
            url=j.get("absolute_url", ""), source=f"greenhouse:{s.slug}"))
    return out


def _lever(s: Source) -> list[Listing]:
    d = _get(f"https://api.lever.co/v0/postings/{s.slug}?mode=json")
    if not isinstance(d, list):
        return []
    out = []
    for j in d:
        cats = j.get("categories") or {}
        body = j.get("descriptionPlain") or _clean(j.get("description", ""))
        for lst in j.get("lists", []) or []:
            body += "\n" + _clean(lst.get("text", ""))
        out.append(Listing(
            title=j.get("text", ""), organisation=s.label(),
            location=cats.get("location", ""), description=body[:6000],
            duration=cats.get("commitment", ""), url=j.get("hostedUrl", ""),
            source=f"lever:{s.slug}"))
    return out


def _ashby(s: Source) -> list[Listing]:
    d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{s.slug}?includeCompensation=false")
    if not isinstance(d, dict):
        return []
    out = []
    for j in d.get("jobs", []):
        out.append(Listing(
            title=j.get("title", ""), organisation=s.label(),
            location=j.get("location", ""),
            description=_clean(j.get("descriptionHtml") or j.get("descriptionPlain", ""))[:6000],
            duration=j.get("employmentType", ""), url=j.get("jobUrl", ""),
            source=f"ashby:{s.slug}"))
    return out


def _workable(s: Source) -> list[Listing]:
    d = _get(f"https://apply.workable.com/api/v1/widget/accounts/{s.slug}?details=true")
    if not isinstance(d, dict):
        return []
    return [Listing(
        title=j.get("title", ""), organisation=s.label(),
        location=", ".join(x for x in (j.get("city"), j.get("country")) if x),
        description=_clean(j.get("description", "") + " " + j.get("requirements", ""))[:6000],
        duration=j.get("employment_type", ""), url=j.get("url", ""),
        source=f"workable:{s.slug}") for j in d.get("jobs", [])]


def _recruitee(s: Source) -> list[Listing]:
    d = _get(f"https://{s.slug}.recruitee.com/api/offers/")
    if not isinstance(d, dict):
        return []
    return [Listing(
        title=j.get("title", ""), organisation=s.label(),
        location=", ".join(x for x in (j.get("city"), j.get("country")) if x),
        description=_clean(j.get("description", "") + " " + j.get("requirements", ""))[:6000],
        duration=j.get("employment_type_code", ""), url=j.get("careers_url", ""),
        source=f"recruitee:{s.slug}") for j in d.get("offers", [])]


def _personio(s: Source) -> list[Listing]:
    d = _get(f"https://{s.slug}.jobs.personio.de/search.json")
    if not isinstance(d, list):
        return []
    out = []
    for j in d:
        desc = j.get("jobDescriptions") or j.get("job_descriptions") or ""
        if isinstance(desc, list):
            desc = " ".join(x.get("value", "") if isinstance(x, dict) else str(x) for x in desc)
        out.append(Listing(
            title=j.get("name", "") or j.get("subcompany", ""), organisation=s.label(),
            location=j.get("office", ""), description=_clean(str(desc))[:6000],
            duration=j.get("employmentType", ""),
            url=j.get("url", "") or f"https://{s.slug}.jobs.personio.de/",
            source=f"personio:{s.slug}"))
    return out


def _teamtailor(s: Source) -> list[Listing]:
    d = _get(f"https://{s.slug}.teamtailor.com/jobs.json")
    if not isinstance(d, (dict, list)):
        return []
    jobs = d.get("jobs", []) if isinstance(d, dict) else d
    return [Listing(
        title=j.get("title", ""), organisation=s.label(),
        location=(j.get("location") or {}).get("city", "") if isinstance(j.get("location"), dict)
                 else str(j.get("location") or ""),
        description=_clean(j.get("body", "") or j.get("pitch", ""))[:6000],
        url=j.get("careersite-job-url", "") or j.get("url", ""),
        source=f"teamtailor:{s.slug}") for j in jobs]


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
            "workable": _workable, "recruitee": _recruitee,
            "personio": _personio, "teamtailor": _teamtailor}


def fetch(sources: list[Source], verbose: bool = True) -> tuple[list[Listing], dict]:
    """Pull every listing from every source. Never raises on one bad board."""
    listings: list[Listing] = []
    stats = {"ok": 0, "empty": 0, "failed": 0}
    for s in sources:
        fn = FETCHERS.get(s.ats)
        if not fn:
            stats["failed"] += 1
            continue
        got = fn(s)
        if got:
            stats["ok"] += 1
            listings.extend(got)
        else:
            # Cannot distinguish "board exists, hiring nobody" from "slug is
            # wrong" without another request. Counted together and reported,
            # so a seed list quietly rotting is visible rather than silent.
            stats["empty"] += 1
        if verbose:
            print(f"  {s.ats:<10} {s.label():<28} {len(got):>3} listings")
    return listings, stats


# Keyword prefilter. NOT a classifier -- it only avoids paying Jev to read
# software-engineering posts. Deliberately loose: a false positive costs
# $0.00005, a false negative costs a role you never see.
INTERESTING = re.compile(
    r"\b(intern|internship|analyst|off[- ]?cycle|placement|graduate|"
    r"spring\s?week|summer|trainee|apprentice|junior|associate)\b", re.I)


def prefilter(listings: list[Listing]) -> list[Listing]:
    return [l for l in listings if INTERESTING.search(f"{l.title} {l.duration}")]


def load_sources(path: str) -> list[Source]:
    """Read a seed file: [{"ats": "lever", "slug": "...", "name": "..."}]"""
    with open(path) as f:
        return [Source(**d) for d in json.load(f)]
