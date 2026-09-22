"""Turn a listing into a verdict: shortlist, review, or reject -- with reasons.

Design choice worth defending: uncertainty routes to REVIEW rather than
collapsing into accept or reject. A noul of 0.5 means Jev could not establish
the condition, which is a different thing from establishing it is false. For a
job search, silently discarding an ambiguous listing is the expensive error;
glancing at one that turns out to be irrelevant costs seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .jev import Decision, JevClient
from .questions import (
    DISQUALIFIERS,
    GATE_REJECT,
    GATE_REVIEW,
    REVIEW_BAND,
    SHORTLIST_THRESHOLD,
    CandidateProfile,
    build_questions,
)

Verdict = Literal["shortlist", "review", "reject"]


@dataclass
class Listing:
    """What was observed. Keep raw text raw -- do not pre-summarise into a
    judgement, or Jev ends up scoring your opinion instead of the posting."""

    title: str
    organisation: str
    location: str = ""
    description: str = ""
    start_date: str = ""
    duration: str = ""
    url: str = ""
    source: str = ""

    def as_state(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "organisation": self.organisation,
            "location": self.location,
            "stated_start_date": self.start_date or "not stated",
            "stated_duration": self.duration or "not stated",
            "description": self.description,
            "listed_under_source_category": self.source or "unknown",
        }


@dataclass
class Result:
    listing: Listing
    verdict: Verdict
    score: float
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    def line(self) -> str:
        mark = {"shortlist": "+", "review": "?", "reject": "-"}[self.verdict]
        head = f"{mark} [{self.score:.2f}] {self.listing.title} — {self.listing.organisation}"
        if self.reasons:
            head += f"\n      {'; '.join(self.reasons)}"
        return head


def _read(d: Decision) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, a in d.answers.items():
        t = a.get("type")
        if t == "noul":
            out[qid] = round(float(a["noul"]), 3)
        elif t == "choice":
            out[qid] = {
                "choice": a["choice"],
                "confidence": round(float(a.get("confidence", 0)), 3),
            }
        elif t == "score":
            # Score answers are 0-indexed over the ordered criteria: N levels
            # span 0..N-1. A 2.4 over 4 levels sits between levels 2 and 3 --
            # it is NOT 2.4 out of 4.
            out[qid] = {
                "level": round(float(a.get("score", 0)), 2),
                "legend": a.get("legend"),
            }
    return out


def classify(listing: Listing, profile: CandidateProfile, client: JevClient | None = None) -> Result:
    client = client or JevClient()
    decision = client.decide(listing.as_state(), build_questions(profile))
    sig = _read(decision)

    reasons: list[str] = []
    forced_review = False

    # --- disqualifiers: three bands, never one threshold -----------------
    labels = {
        "starts_too_early": f"states a start date before {profile.available_from}",
        "has_visa_barrier": "work-authorisation barrier",
    }
    for gate in DISQUALIFIERS:
        p = decision.noul(gate)
        if p >= GATE_REJECT:
            return Result(
                listing=listing,
                verdict="reject",
                score=0.0,
                reasons=[f"{labels[gate]} (p={p:.2f})"],
                signals=sig,
                cost_usd=decision.cost_usd,
                latency_ms=decision.latency_ms,
            )
        if p >= GATE_REVIEW:
            forced_review = True
            reasons.append(f"possible {labels[gate]} (p={p:.2f})")

    # A listing with no stated start date is a question, not a rejection --
    # most early-stage funds simply do not publish one.
    if decision.noul("start_date_stated") < 0.5:
        forced_review = True
        reasons.append("no start date given — confirm timing before applying")

    # --- fit score ------------------------------------------------------
    offcycle = decision.noul("is_offcycle")
    sector = decision.noul("is_target_sector")
    early = decision.noul("is_early_stage")

    # Off-cycle is weighted hardest: it is the whole premise of the search and
    # the thing no other tool checks.
    score = 0.5 * offcycle + 0.3 * sector + 0.2 * early

    kind, kind_conf = decision.choice("role_kind")
    if kind in ("summer_internship", "graduate_scheme", "permanent_role") and kind_conf > 0.7:
        score *= 0.3
        reasons.append(f"classified {kind} (conf {kind_conf:.2f})")
    elif kind == "unclear":
        reasons.append("role type unclear from posting")

    lo, hi = REVIEW_BAND
    if lo <= offcycle <= hi:
        reasons.append(f"off-cycle status uncertain (p={offcycle:.2f})")
        verdict: Verdict = "review"
    elif forced_review and score >= lo:
        verdict = "review"
    elif score >= SHORTLIST_THRESHOLD:
        verdict = "shortlist"
    elif score >= lo:
        verdict = "review"
    else:
        verdict = "reject"
        reasons.append(f"weak fit (off-cycle {offcycle:.2f}, sector {sector:.2f})")

    if verdict == "shortlist":
        reasons.insert(0, f"off-cycle {offcycle:.2f} · sector {sector:.2f} · early-stage {early:.2f}")

    return Result(
        listing=listing,
        verdict=verdict,
        score=round(score, 3),
        reasons=reasons,
        signals=sig,
        cost_usd=decision.cost_usd,
        latency_ms=decision.latency_ms,
    )
