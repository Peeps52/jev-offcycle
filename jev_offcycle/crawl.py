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


FETCH_ERRORS = [0]   # incremented on any HTTP/network failure, so "0 listings"
                     # can be split into "failed to retrieve" vs "nothing open"
BLOCKED_HOSTS: set[str] = set()   # hosts whose robots.txt forbids us; reported, never silent


def _allowed(url: str) -> bool:
    """Honour robots.txt per RFC 9309.

    Python's RobotFileParser treats a 401/403 on robots.txt as "disallow
    everything". RFC 9309 says the opposite: a 4xx robots response means no
    rules apply. api.ashbyhq.com answers robots.txt with 401, so the stdlib
    behaviour silently switched off every Ashby board -- ~25 sources, reported
    as "0 listings", indistinguishable from boards with no vacancies.

      2xx          parse and obey
      4xx          no rules: allowed          (RFC 9309 s2.3.1.3)
      5xx/network  treated as allowed, as before -- but see BLOCKED_HOSTS for
                   explicit disallows, which are recorded and reported
    """
    parts = urllib.parse.urlparse(url)
    host = f"{parts.scheme}://{parts.netloc}"
    rp = _robots.get(host)
    if rp is None:
        rp = False
        try:
            req = urllib.request.Request(f"{host}/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(r.read().decode("utf-8", "ignore").splitlines())
                rp = parser
        except Exception:
            rp = False          # 4xx, 5xx or unreachable: no rules to apply
        _robots[host] = rp
    if rp is False:
        return True
    try:
        ok = rp.can_fetch(UA, url)
    except Exception:
        return True
    if not ok:
        BLOCKED_HOSTS.add(parts.netloc)
    return ok


@dataclass
class Source:
    """One company's job board."""

    ats: str        # greenhouse | lever | ashby
    slug: str       # the board identifier
    name: str = ""  # display name; defaults to the slug
    category: str = ""  # vc | pe | ib | strategy | trading | tech -- for reporting

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
        FETCH_ERRORS[0] += 1
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


def _smartrecruiters(s: Source) -> list[Listing]:
    """Public, unauthenticated. Common with European employers."""
    out, offset = [], 0
    while offset < 500:
        d = _get(f"https://api.smartrecruiters.com/v1/companies/{s.slug}/postings"
                 f"?limit=100&offset={offset}")
        if not isinstance(d, dict):
            break
        rows = d.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            out.append(Listing(
                title=j.get("name", ""), organisation=s.label(),
                location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                description=_clean(str((j.get("jobAd") or {}).get("sections", "")))[:6000],
                duration=(j.get("typeOfEmployment") or {}).get("label", ""),
                url=f"https://jobs.smartrecruiters.com/{s.slug}/{j.get('id', '')}",
                source=f"smartrecruiters:{s.slug}"))
        if len(rows) < 100:
            break
        offset += 100
    # List payloads omit the description. Fetch it only for what survives the
    # free filters -- detail calls for 500 engineering roles would be waste.
    for l in out:
        if prefilter([l]) and not l.description:
            d = _get(f"https://api.smartrecruiters.com/v1/companies/{s.slug}/postings/"
                     f"{l.url.rsplit('/', 1)[-1]}")
            if isinstance(d, dict):
                secs = ((d.get("jobAd") or {}).get("sections") or {})
                l.description = _clean(" ".join(
                    (v or {}).get("text", "") for v in secs.values()
                    if isinstance(v, dict)))[:6000]
    return out


def _workday(s: Source) -> list[Listing]:
    """Workday is where most banks, large PE firms and asset managers post.

    slug format: "tenant|wdN|site", e.g. "lazard|wd5|Lazard". The list
    endpoint is a POST that returns titles and locations only; descriptions
    are fetched per job, and only for postings that pass the free filters.
    """
    try:
        tenant, wd, site = s.slug.split("|")
    except ValueError:
        return []
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    out, offset = [], 0
    while offset < 400:
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset,
                           "searchText": ""}).encode()
        url = f"{base}/jobs"
        if not _allowed(url):
            break
        _throttle(url)
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": UA, "Accept": "application/json",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read())
        except Exception:
            break
        rows = d.get("jobPostings", [])
        for j in rows:
            path = j.get("externalPath", "")
            out.append(Listing(
                title=j.get("title", ""), organisation=s.label(),
                location=j.get("locationsText", ""),
                url=f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
                source=f"workday:{s.slug}", description=path))
        if len(rows) < 20:
            break
        offset += 20
    for l in out:
        path, l.description = l.description, ""
        if prefilter([l]) and location_tier(l.location) < 9:
            d = _get(f"{base}{path}")
            if isinstance(d, dict):
                info = d.get("jobPostingInfo") or {}
                l.description = _clean(info.get("jobDescription", ""))[:6000]
                # The list endpoint often says "2 Locations" instead of naming
                # them; the detail page has the real ones.
                locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
                named = ", ".join(x for x in locs if x)
                if named:
                    l.location = named
                # NOT info["startDate"]: on Workday that is the date the
                # posting went live. Mapping it to start_date made every
                # Workday listing "state" a 2026 start, and the availability
                # gate rejected Blackstone, PJT, Guggenheim and Houlihan
                # Lokey's 2027 programmes -- February-2027 off-cycles included.
                # timeType ("Full time") is not a duration either.
    return out


# Consider portfolio boards: seniorities we can drop for free, and regions we
# keep. Everything else about a Consider listing is rich structured metadata
# rather than prose, so it is folded into a synthetic description below.
_CONSIDER_SENIOR = {"senior", "lead", "principal", "director", "executive", "vp"}
_CONSIDER_REGIONS = {"europe", "uk", "united kingdom", "emea", "remote"}


def _consider(s: Source) -> list[Listing]:
    """VC portfolio job boards on Consider (e.g. careers.balderton.com).

    This is how you reach the startups a fund backs -- the smaller employers
    that no seed list of named companies would find. Balderton's board alone
    lists ~2,400 roles across its portfolio.

    slug: the board's domain. The board id and a CSRF token are read from the
    page itself, and the token only works with the cookie from the same load,
    so both requests share one cookie jar.
    """
    import http.cookiejar
    domain = s.slug.strip("/")
    home = f"https://{domain}/jobs"
    if not _allowed(home):
        return []
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        _throttle(home)
        html = op.open(urllib.request.Request(home, headers={"User-Agent": UA}),
                       timeout=TIMEOUT).read().decode("utf-8", "ignore")
    except Exception:
        return []
    tok = re.search(r'csrfToken":"([^"]+)"', html)
    board = re.search(r'"board":\{"id":"([^"]+)"', html)
    if not tok or not board:
        return []

    out, seq, seen = [], None, 0
    api = f"https://{domain}/api-boards/search-jobs"
    while seen < 3000:
        meta = {"size": 100}
        if seq:
            meta["sequence"] = seq
        body = json.dumps({"meta": meta, "board": {"id": board.group(1), "isParent": True},
                           "query": {"promoteFeatured": True}}).encode()
        _throttle(api)
        try:
            d = json.loads(op.open(urllib.request.Request(api, data=body, method="POST", headers={
                "Content-Type": "application/json", "x-csrf-token": tok.group(1),
                "User-Agent": UA}), timeout=TIMEOUT).read())
        except Exception:
            break
        jobs = d.get("jobs") or []
        for j in jobs:
            sen = {x.lower() for x in (j.get("jobSeniorityIds") or [])}
            regions = {(r.get("label") or "").lower() for r in (j.get("regions") or [])}
            if sen and sen <= _CONSIDER_SENIOR:
                continue                          # free drop: explicitly senior
            locs = ", ".join(j.get("locations") or [])
            if regions and not (regions & _CONSIDER_REGIONS) and location_tier(locs) >= 9:
                continue                          # free drop: outside Europe
            lbl = lambda k: ", ".join(x.get("label", "") for x in (j.get(k) or []))
            desc = (
                f"Company: {j.get('companyName')} ({j.get('companyStaffCount')} staff; "
                f"stage {lbl('stages')}; markets {lbl('markets')}). "
                f"Functions: {lbl('jobFunctions')}. Departments: {', '.join(j.get('departments') or [])}. "
                f"Seniority: {lbl('jobSeniorities') or 'not stated'}. "
                f"Minimum years of experience: {j.get('minYearsExp') or 'not stated'}. "
                f"Required skills: {lbl('requiredSkills')}. "
                f"NOTE: portfolio-board listing; the full description is at the apply link.")
            out.append(Listing(
                title=j.get("title", ""), organisation=j.get("companyName") or s.label(),
                location=locs, description=desc[:6000], url=j.get("url") or j.get("applyUrl", ""),
                source=f"consider:{domain}"))
        seen += len(jobs)
        seq = (d.get("meta") or {}).get("sequence")
        if not jobs or not seq:
            break
    return out


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
            "workable": _workable, "recruitee": _recruitee,
            "personio": _personio, "teamtailor": _teamtailor,
            "smartrecruiters": _smartrecruiters, "workday": _workday,
            "consider": _consider}


def fetch(sources: list[Source], verbose: bool = True) -> tuple[list[Listing], dict]:
    """Pull every listing from every source. Never raises on one bad board."""
    listings: list[Listing] = []
    stats = {"ok": 0, "empty": 0, "failed": 0, "blocked": 0}
    for s in sources:
        fn = FETCHERS.get(s.ats)
        if not fn:
            stats["failed"] += 1
            continue
        before, errs = set(BLOCKED_HOSTS), FETCH_ERRORS[0]
        got = fn(s)
        if BLOCKED_HOSTS - before and not got:
            stats["blocked"] += 1
            if verbose:
                print(f"  {s.ats:<10} {s.label():<28} BLOCKED by robots.txt")
            continue
        if got:
            stats["ok"] += 1
            listings.extend(got)
        elif FETCH_ERRORS[0] > errs:
            # The request itself failed (404, timeout, bad JSON). This is a
            # RETRIEVAL failure, not an employer with nothing open.
            stats["failed"] += 1
            if verbose:
                print(f"  {s.ats:<10} {s.label():<28} RETRIEVAL FAILED")
            continue
        else:
            # Cannot distinguish "board exists, hiring nobody" from "slug is
            # wrong" without another request. Counted together and reported,
            # so a seed list quietly rotting is visible rather than silent.
            stats["empty"] += 1
        if verbose:
            print(f"  {s.ats:<10} {s.label():<28} {len(got):>3} listings")
    return listings, stats


# Keyword prefilter. NOT a classifier -- it only avoids paying Jev to read
# posts that are plainly out of scope. Deliberately loose: a false positive
# costs $0.00005, a false negative costs a role you never see.
#
# Italian terms included because Milan is a priority: "stage"/"stagista" is an
# internship, "neolaureato" a recent graduate, "tirocinio" a traineeship.
INTERESTING = re.compile(
    r"\b(intern|internship|analyst|off[- ]?cycle|placement|graduate|grad|"
    r"spring\s?week|trainee|apprentice|junior|associate|new\s?grad|"
    r"early[- ]career|entry[- ]level|rotational|apm|"
    r"stage|stagista|tirocinio|tirocinante|neolaureat[oai]|praktikum|werkstudent|"
    r"alternance|stagiaire)\b", re.I)

# Titles that are senior regardless of what else they say. "Associate
# Director" contains "associate", so this runs after INTERESTING.
SENIOR = re.compile(
    r"\b(senior|sr\.?|lead|head|principal|director|staff|vp|vice president|"
    r"partner|chief|manager of|expert|specialist ii|iii)\b", re.I)


def prefilter(listings: list[Listing]) -> list[Listing]:
    out = []
    for l in listings:
        t = f"{l.title} {l.duration}"
        if INTERESTING.search(t) and not SENIOR.search(l.title):
            out.append(l)
    return out


# Location priority. London and Milan first, then European hubs. Listings
# outside Europe are dropped before any paid call: the first real run spent
# 45 of 56 Jev calls rejecting Palantir roles in Washington and Honolulu on
# visa grounds, which a string match could have done for free.
TIER0 = re.compile(r"\b(london|milan|milano|remote.{0,12}(uk|united kingdom))\b", re.I)
TIER1 = re.compile(
    r"\b(uk|united kingdom|england|italy|italia|rome|roma|turin|torino|"
    r"paris|berlin|munich|m[uü]nchen|amsterdam|zurich|z[uü]rich|madrid|"
    r"barcelona|stockholm|dublin|lisbon|lisboa|copenhagen|frankfurt|"
    r"luxembourg|brussels|vienna|wien|geneva|oslo|helsinki|warsaw|prague|"
    r"hamburg|cologne|edinburgh|manchester|cambridge|oxford|bristol)\b", re.I)
TIER2 = re.compile(r"\b(europe|emea|eu\b|remote)", re.I)
NON_EUROPE = re.compile(
    r"\b(usa?|united states|new york|nyc|san francisco|sf|palo alto|seattle|"
    r"boston|chicago|austin|denver|los angeles|washington|d\.c\.|miami|"
    r"honolulu|atlanta|toronto|vancouver|canada|india|bangalore|bengaluru|"
    r"singapore|hong kong|tokyo|sydney|melbourne|australia|dubai|"
    r"s[aã]o paulo|mexico|brazil|israel|tel aviv|korea|seoul|china|shanghai)\b", re.I)


def location_tier(loc: str) -> int:
    """0 London/Milan · 1 other European city · 2 Europe-wide/remote ·
    3 unstated (kept: unknown is not wrong) · 9 outside Europe (dropped)."""
    loc = loc or ""
    if TIER0.search(loc):
        return 0
    if TIER1.search(loc):
        return 1
    if NON_EUROPE.search(loc):
        return 9
    if TIER2.search(loc):
        return 2
    # Anything unrecognised ("Hybrid", "Tallinn") is kept at the lowest
    # priority rather than dropped: unknown is not wrong, and the visa gate
    # will still catch a non-European role at the cost of one call.
    return 3


def rank_by_location(listings: list[Listing]) -> list[Listing]:
    kept = [(location_tier(l.location), i, l) for i, l in enumerate(listings)]
    return [l for t, i, l in sorted(k for k in kept if k[0] < 9)]


def load_sources(path: str) -> list[Source]:
    """Read a seed file: [{"ats": "lever", "slug": "...", "name": "..."}]"""
    with open(path) as f:
        return [Source(**d) for d in json.load(f)]
