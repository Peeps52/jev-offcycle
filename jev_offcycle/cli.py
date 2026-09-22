"""CLI: python -m jev_offcycle examples/listings.json"""

from __future__ import annotations

import argparse
import json
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

    results, total_cost, total_ms = [], 0.0, 0.0
    for item in raw:
        listing = Listing(**item)
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

    n = len(results) or 1
    print(
        f"\n{len(results)} listings · "
        f"{sum(r.verdict == 'shortlist' for r in results)} shortlist · "
        f"{sum(r.verdict == 'review' for r in results)} review · "
        f"{sum(r.verdict == 'reject' for r in results)} reject",
        file=sys.stderr,
    )
    print(
        f"${total_cost:.6f} total · {total_ms / n:.0f}ms median-ish per listing",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
