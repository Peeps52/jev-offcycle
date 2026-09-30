"""Crawler regressions. Offline -- network calls are stubbed."""

from __future__ import annotations

import json
import urllib.error

from jev_offcycle import crawl
from jev_offcycle.classify import Listing


def test_robots_4xx_means_allowed(monkeypatch):
    """api.ashbyhq.com answers robots.txt with 401. Python's RobotFileParser
    treats that as disallow-all, which silently switched off ~25 Ashby boards
    and reported them as '0 listings'. RFC 9309: 4xx means no rules apply."""
    crawl._robots.clear()

    def boom(*a, **k):
        raise urllib.error.HTTPError("https://x/robots.txt", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(crawl.urllib.request, "urlopen", boom)
    assert crawl._allowed("https://api.ashbyhq.com/posting-api/job-board/ramp") is True


def test_explicit_disallow_is_recorded_not_silent():
    crawl._robots.clear()
    crawl.BLOCKED_HOSTS.clear()
    rp = crawl.urllib.robotparser.RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /"])
    crawl._robots["https://blocked.example"] = rp
    assert crawl._allowed("https://blocked.example/jobs") is False
    assert "blocked.example" in crawl.BLOCKED_HOSTS


def test_retrieval_failure_is_not_reported_as_empty(monkeypatch):
    """A 404 and a board with no vacancies both used to read '0 listings'."""
    def failing(s):
        crawl.FETCH_ERRORS[0] += 1
        return []
    monkeypatch.setitem(crawl.FETCHERS, "greenhouse", failing)
    _, stats = crawl.fetch([crawl.Source("greenhouse", "gone", "Gone")], verbose=False)
    assert stats["failed"] == 1 and stats["empty"] == 0


def test_location_priority_and_drops():
    t = crawl.location_tier
    assert t("London, UK") == 0 and t("Milano, Italy") == 0
    assert t("Paris, France") == 1
    assert t("Remote - Europe") == 2
    assert t("") == 3                         # unknown is kept, not dropped
    assert t("Washington, D.C.") == 9 and t("Singapore") == 9


def test_rank_puts_london_before_board_order():
    """--limit used to take the first N in board order: 40 Palantir US roles
    were scored while London listings further down never were."""
    ls = [Listing(title="Intern", organisation="A", location="Washington, D.C."),
          Listing(title="Intern", organisation="B", location="Paris"),
          Listing(title="Intern", organisation="C", location="London")]
    ranked = crawl.rank_by_location(ls)
    assert [l.organisation for l in ranked] == ["C", "B"]


def test_prefilter_drops_senior_and_keeps_italian_internships():
    keep = crawl.prefilter([
        Listing(title="Stage Investment Analyst", organisation="X"),
        Listing(title="Senior Associate", organisation="X"),
        Listing(title="Associate Product Manager", organisation="X"),
        Listing(title="Software Engineer", organisation="X"),
    ])
    assert [l.title for l in keep] == ["Stage Investment Analyst", "Associate Product Manager"]


def test_workday_posting_date_is_not_a_start_date(monkeypatch):
    # Workday's detail `startDate` is when the posting went live. Treating it
    # as the role's start rejected every 2027 Workday programme as "too early".
    src = crawl.Source(ats="workday", slug="hl|wd1|Campus", name="Houlihan Lokey")
    import io
    page = json.dumps({"total": 1, "jobPostings": [{
        "title": "2027 Off-Cycle Internship (February)",
        "locationsText": "London, UK", "externalPath": "/job/x"}]}).encode()
    monkeypatch.setattr(crawl, "_allowed", lambda url: True)
    monkeypatch.setattr(crawl, "_throttle", lambda url: None)
    monkeypatch.setattr(crawl.urllib.request, "urlopen",
                        lambda req, timeout=0: io.BytesIO(page))
    monkeypatch.setattr(crawl, "_get", lambda url: {"jobPostingInfo": {
        "jobDescription": "Takes place in February 2027.", "location": "London, UK",
        "startDate": "2026-08-10", "timeType": "Full time"}})
    [l] = crawl._workday(src)
    assert l.start_date == "" and l.duration == ""
    assert "February 2027" in l.description
