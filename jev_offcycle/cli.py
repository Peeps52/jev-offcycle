"""CLI: python -m jev_offcycle examples/listings.json"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from .classify import Listing, classify
from .jev import JevClient, JevError
from .questions import CandidateProfile


def _load_cv() -> str:
    """Read the CV from the gitignored profile.local.py.

    Kept out of the repo deliberately: it is personal data, it is sent to the
    model on every listing, and a public repo is the wrong place for either.
    Absent, candidacy scoring simply switches off and role scoring still runs.
    """
    try:
        import importlib.util
        from pathlib import Path
        p = Path(__file__).parent.parent / "profile.local.py"
        if not p.exists():
            return ""
        spec = importlib.util.spec_from_file_location("profile_local", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "CV_SUMMARY", "")
    except Exception:
        return ""

# Edit this block, not the question text in questions.py.
DEFAULT_PROFILE = CandidateProfile(
    available_from="2027-01-22",
    availability_note=(
        "The candidate is a final-year undergraduate whose last examination is "
        "on 21 January 2027, and is not available to start before that date."
    ),
    right_to_work=["United Kingdom", "European Union"],
    target_sectors=[
        "venture capital",
        "early-stage technology investing",
        "startup accelerators",
    ],
    # Which programme types you want. Off-cycle is the search nothing else
    # serves; graduate schemes are included because a Jan-2027 finalist is
    # eligible for both and there is no reason to hide one.
    # Also available: "summer_internship", "placement_year", "spring_week".
    target_programmes=("offcycle_internship", "graduate_scheme"),
    # Condensed deliberately. Name, email, phone and address are absent --
    # they add nothing to the judgement and this string is sent on every call.
    cv_summary=_load_cv(),
    min_months=3,
    max_months=12,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jev-offcycle")
    ap.add_argument("listings", type=Path, help="JSON file: array of listing objects")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument(
        "--all", action="store_true", help="show rejects too (default hides them)"
    )
    args = ap.parse_args(argv)

    try:
        raw = json.loads(args.listings.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"could not read {args.listings}: {e}", file=sys.stderr)
        return 2

    try:
        client = JevClient()
    except JevError as e:
        print(f"{e}", file=sys.stderr)
        return 2

    known = set(Listing.__dataclass_fields__)
    results, total_cost, total_ms, latencies = [], 0.0, 0.0, []
    for item in raw:
        # Ignore unknown keys rather than dying on the whole run: real job
        # feeds carry extra fields, and one stray key should not cost you the
        # other 200 listings.
        extra = set(item) - known
        if extra:
            print(f"   (ignoring unknown fields: {', '.join(sorted(extra))})", file=sys.stderr)
        listing = Listing(**{k: v for k, v in item.items() if k in known})
        try:
            r = classify(listing, DEFAULT_PROFILE, client)
        except JevError as e:
            # Surface it. A filter that silently drops a listing on error is a
            # filter you cannot trust.
            print(f"!! {listing.title}: {e}", file=sys.stderr)
            continue
        results.append(r)
        total_cost += r.cost_usd
        total_ms += r.latency_ms
        latencies.append(r.latency_ms)

    results.sort(key=lambda r: r.score, reverse=True)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "title": r.listing.title,
                        "organisation": r.listing.organisation,
                        "url": r.listing.url,
                        "verdict": r.verdict,
                        "score": r.score,
                        "reasons": r.reasons,
                        "signals": r.signals,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        shown = [r for r in results if args.all or r.verdict != "reject"]
        for r in shown:
            print(r.line())
        hidden = len(results) - len(shown)
        if hidden:
            print(f"\n  ({hidden} rejected, --all to show)")

    print(
        f"\n{len(results)} listings · "
        f"{sum(r.verdict == 'shortlist' for r in results)} shortlist · "
        f"{sum(r.verdict == 'review' for r in results)} review · "
        f"{sum(r.verdict == 'reject' for r in results)} reject",
        file=sys.stderr,
    )
    if latencies:
        med = statistics.median(latencies)
        print(
            f"${total_cost:.6f} total · {med:.0f}ms median · "
            f"{total_cost / len(latencies):.6f}/listing",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def crawl_main(argv: list[str] | None = None) -> int:
    """python3 -m jev_offcycle.crawl_cli examples/sources.json"""
    import argparse
    from .crawl import fetch, load_sources, prefilter

    ap = argparse.ArgumentParser(prog="jev-offcycle crawl")
    ap.add_argument("sources", type=Path)
    ap.add_argument("--limit", type=int, default=0, help="score at most N listings")
    ap.add_argument("--out", type=Path, help="write fetched listings to JSON and stop")
    args = ap.parse_args(argv)

    from . import store as _store

    srcs = load_sources(str(args.sources))
    listings, stats = fetch(srcs)
    keep = prefilter(listings)

    con = _store.connect()
    seen = {f"{s.ats}:{s.slug}" for s in srcs}
    delta = _store.sync(con, keep, seen)
    fresh = _store.needs_scoring(con, keep)
    print(f"\n  {len(listings)} listings · {len(keep)} after prefilter · boards {stats}",
          file=sys.stderr)
    print(f"  store: {delta['new']} new · {delta['changed']} changed · "
          f"{delta['unchanged']} unchanged · {delta['closed']} closed", file=sys.stderr)
    print(f"  {len(fresh)} need scoring ({len(keep)-len(fresh)} already priced)",
          file=sys.stderr)
    keep = fresh

    if args.out:
        args.out.write_text(json.dumps([{
            "title": l.title, "organisation": l.organisation, "location": l.location,
            "description": l.description, "duration": l.duration, "url": l.url,
            "source": l.source} for l in keep], indent=2))
        print(f"  wrote {len(keep)} listings to {args.out}", file=sys.stderr)
        return 0

    if args.limit:
        keep = keep[: args.limit]
    client = JevClient()
    results, cost = [], 0.0
    for l in keep:
        try:
            r = classify(l, DEFAULT_PROFILE, client)
        except JevError as e:
            print(f"!! {l.title}: {e}", file=sys.stderr)
            continue
        results.append(r)
        _store.save_verdict(con, r)
        cost += r.cost_usd

    # The report shows EVERY live verdict, not only this run's -- otherwise a
    # second run would show an empty shortlist, having correctly skipped
    # everything it already knew.
    from .classify import Listing as _L, Result as _R
    for v in _store.load_verdicts(con):
        if any(r.listing.url == v["listing"].get("url") for r in results):
            continue
        results.append(_R(listing=_L(**v["listing"]), verdict=v["verdict"],
                          score=v["score"], reasons=v["reasons"],
                          signals=v["signals"], candidacy=v["candidacy"]))
    results.sort(key=lambda r: r.score, reverse=True)

    # A shortlist is something you read and click through, not something that
    # scrolls past. Write a page and open it.
    from .report import write as write_report
    out = Path.home() / "Desktop" / "shortlist.html"
    write_report(results, out, meta={"source": str(args.sources), "fetched": len(listings)})
    print(f"  report: {out}", file=sys.stderr)

    print(f"\n  {len(results)} scored · "
          f"{sum(r.verdict=='shortlist' for r in results)} shortlist · "
          f"{sum(r.verdict=='review' for r in results)} review · "
          f"{sum(r.verdict=='reject' for r in results)} reject · ${cost:.4f}",
          file=sys.stderr)
    return 0
