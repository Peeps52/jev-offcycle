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
    cv_summary=(
        "Final-year BSc International Economics and Management at Bocconi "
        "University, Milan (Sep 2023 - Jan 2027 expected, expected 105/110). "
        "Coursework in multivariable calculus, probability, statistics, "
        "econometrics, corporate finance, accounting and computer science. "
        "Previously St Paul's School, London: A Levels in Mathematics, Further "
        "Mathematics, Economics and Physics; 11 A* at GCSE. "
        "Experience: venture capital summer analyst at BRV Capital Management "
        "(Venture Opportunities Team, Seoul, Jun-Aug 2025) - commercial and "
        "technical diligence on early-stage and growth investments, 15+ founder "
        "and management meetings, assessed an Oxford hyperspectral-imaging "
        "spin-out raising a $15m Series A+ including VDR, cap table and "
        "commercial traction, built top-down and bottom-up market models for "
        "semiconductor metrology, modelled ownership and dilution under "
        "alternative financing scenarios, produced sector research across "
        "semiconductors, AI data-centre infrastructure, robotics, medical AI "
        "and genomics for the CEO and investment committee. "
        "Investment banking intern at Equita (Milan, Jun 2023) - cybersecurity "
        "M&A research, comparable-company analysis, daily morning note. "
        "Marketing and strategy at Tod's (Milan, Jul 2024). "
        "BSc thesis on tacit coordination in prediction markets, building a "
        "transaction-level Polymarket dataset. "
        "Builds software: Python, SQL, data pipelines, LLM applications."
    ),
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
