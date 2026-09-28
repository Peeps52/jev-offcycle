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

`jev_ultrafast` remains the fallback for sites with no API at all, but reach
for it last: it needs Chrome, a TypeSafe or OpenRouter key, and roughly 310ms
of thinking per click. A JSON endpoint costs one request and nothing else.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .classify import Listing

UA = "jev-offcycle/0.2 (+https://github.com/Peeps52/jev-offcycle)"
TIMEOUT = 20


@dataclass
class Source:
    """One company's job board."""

    ats: str        # greenhouse | lever | ashby
    slug: str       # the board identifier
    name: str = ""  # display name; defaults to the slug

    def label(self) -> str:
        return self.name or self.slug


def _get(url: str) -> dict | list | None:
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


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


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
