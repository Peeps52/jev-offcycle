"""Turn a listing into a verdict: shortlist, review, or reject -- with reasons.

Design choice worth defending: uncertainty routes to REVIEW rather than
collapsing into accept or reject. A noul of 0.5 means Jev could not establish
the condition, which is a different thing from establishing it is false. For a
job search, silently discarding an ambiguous listing is the expensive error;
glancing at one that turns out to be irrelevant costs seconds.
"""

from __future__ import annotations

import re
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
    candidacy: float | None = None   # separate axis; None when no CV supplied

    def line(self) -> str:
        mark = {"shortlist": "+", "review": "?", "reject": "-"}[self.verdict]
        cand = f" cand {self.candidacy:.2f}" if self.candidacy is not None else ""
        head = f"{mark} [{self.score:.2f}{cand}] {self.listing.title} — {self.listing.organisation}"
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
            # span 0..N-1. A 2.8 over 4 levels sits between levels 2 and 3 --
            # it is NOT 2.8 out of 4. Keep `probabilities` and `confidence`:
            # a 2.8 split 0.2/0.8 across two adjacent levels means something
            # different from one spread thinly over four.
            out[qid] = {
                "level": round(float(a.get("score", 0)), 2),
                "legend": a.get("legend"),
                "probabilities": a.get("probabilities"),
                "confidence": round(float(a.get("confidence", 0)), 3),
            }
    return out


def _score_level(d: Decision, qid: str) -> tuple[float, int]:
    """(level, n_levels) for a `score` answer. Level is 0-indexed."""
    a = d.answers.get(qid)
    if not a or a.get("type") != "score":
        raise JevError(f"question {qid!r} is not a score answer: {a!r}")
    return float(a.get("score", 0.0)), len(a.get("legend") or {}) or 1


def _title_too_early(listing: Listing, profile: CandidateProfile) -> str | None:
    """A year in the title is the hardest date evidence a posting carries, and
    it is free to read. "French Intern | January 2026" had an empty description
    and the model put it at 0.32 -- a lapsed internship shortlisted at 1.00.
    Every year named in the title predating the availability year is a
    certain conflict; no API call needed."""
    years = [int(y) for y in re.findall(r"\b(20[2-3]\d)\b", listing.title)]
    avail = int(profile.available_from[:4])
    if years and max(years) < avail:
        return f"title dates the role to {max(years)}, before {profile.available_from}"
    return None


def classify(listing: Listing, profile: CandidateProfile, client: JevClient | None = None) -> Result:
    early = _title_too_early(listing, profile)
    if early:
        return Result(listing=listing, verdict="reject", score=0.0, reasons=[early])
    client = client or JevClient()
    state = {"posting": listing.as_state()}
    if profile.cv_summary.strip():
        state["candidate"] = profile.cv_summary.strip()
    decision = client.decide(state, build_questions(profile))
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
    # A NOTE, not a block. It used to force review, and forced review cannot
    # become shortlist -- while ATS postings almost never state a start date
    # (3 of 56 in the first real run). So ~95% of listings could never
    # shortlist however good they were. That was a large part of why every
    # run came back with zero shortlists.
    if decision.noul("start_date_stated") < 0.5:
        reasons.append("no start date given — confirm timing before applying")

    # --- fit score ------------------------------------------------------
    fn, fn_conf = decision.choice("role_function")
    fn_probs = decision.probabilities("role_function")
    wanted_fn = set(profile.target_functions)
    function = (sum(p for k, p in fn_probs.items() if k in wanted_fn) if fn_probs
                else (1.0 if fn in wanted_fn else 0.0))
    if fn == "unclear":          # unknown is not wrong -- same rule as programme
        function = 0.5
        forced_review = True
    entry = decision.noul("entry_level")
    kind, kind_conf = decision.choice("role_kind")

    # Programme match replaces the old `is_offcycle` noul. The probability
    # mass on the wanted types IS the match strength -- a listing Jev puts at
    # 0.7 offcycle / 0.3 summer scores 0.7 when you want off-cycle, without a
    # second question to ask or a threshold to tune.
    probs = decision.probabilities("role_kind")
    wanted = set(profile.target_programmes)
    programme = sum(p for k, p in probs.items() if k in wanted) if probs else (
        1.0 if kind in wanted else 0.0
    )

    # WEIGHTED GEOMETRIC MEAN, not a weighted sum. These dimensions are
    # conjunctive: you need the right programme AT the right kind of firm.
    # A sum lets one dimension carry the rest, and real data proved it --
    # BlackRock's off-cycle programme scored 0.53 and reached review on
    # programme match alone, with sector 0.05 and early-stage 0.05, because
    # 0.5 x 0.95 already clears the band. It is asset management, not
    # early-stage VC, and should never have surfaced.
    #
    # Geometric weighting means any near-zero dimension sinks the result,
    # which is what "wrong firm" should do. Floored at 0.01 so a single zero
    # does not annihilate the score and destroy the ordering among rejects.
    # One exception, and it is the whole thesis of this repo reappearing in a
    # new place: when Jev returns `unclear`, the probability mass on the
    # wanted types is ~0 -- but that means UNKNOWN, not WRONG. Feeding it to
    # a geometric mean annihilates the score and rejects the listing. The
    # first version of this change did exactly that, dropping a vague
    # early-stage VC posting from review 0.66 to reject 0.10.
    #
    # An unreadable posting is a question for a human, not a no. Substitute a
    # neutral 0.5 and force review.
    if kind == "unclear":
        programme = 0.5
        forced_review = True

    def _g(x: float, w: float) -> float:
        return max(x, 0.01) ** w

    # Right programme, right kind of work, right seniority -- all three
    # needed, so still geometric. Employer type is no longer a factor at all.
    score = _g(programme, 0.4) * _g(function, 0.4) * _g(entry, 0.2)

    # Technical depth was previously computed and then ignored -- an API call
    # paid for on every listing that changed nothing. It is a modest bonus,
    # not a gate: a commercial sourcing role at the right fund is still worth
    # seeing, it just ranks below one with real diligence work.
    level, n_levels = _score_level(decision, "technical_depth")
    if n_levels > 1:
        technical = level / (n_levels - 1)  # 0-indexed levels -> 0..1
        score = min(1.0, score * (1.0 + 0.15 * technical))
        if technical >= 0.66:
            reasons.append(f"technical role (level {level:.1f}/{n_levels - 1})")

    if kind not in wanted and kind != "unclear" and kind_conf > 0.7:
        score *= 0.3
        reasons.append(f"classified {kind} (conf {kind_conf:.2f})")
    elif kind == "unclear":
        reasons.append("programme type unclear from posting")

    lo, hi = REVIEW_BAND
    if lo <= programme <= hi:
        reasons.append(f"programme match uncertain (p={programme:.2f})")
        verdict: Verdict = "review"
    elif forced_review and score >= lo:
        verdict = "review"
    elif score >= SHORTLIST_THRESHOLD:
        verdict = "shortlist"
    elif score >= lo:
        verdict = "review"
    else:
        verdict = "reject"
        reasons.append(f"weak fit ({kind} {programme:.2f}, {fn} {function:.2f}, entry {entry:.2f})")

    # --- candidacy: a SEPARATE axis, deliberately not folded into `score` ----
    # Role fit and competitiveness are different questions. A role can be
    # exactly right and you not be eligible, or you can be perfect for
    # something you do not want. Merging them into one number destroys the
    # distinction and you cannot tell which half failed.
    candidacy = None
    if profile.cv_summary.strip() and "experience_fit" in decision.answers:
        meets = decision.noul("meets_stated_requirements")
        lvl, n = _score_level(decision, "experience_fit")
        exp = lvl / (n - 1) if n > 1 else 0.0
        candidacy = round(0.5 * meets + 0.5 * exp, 3)
        scale, _sc = decision.choice("employer_scale_fit")
        sig["candidacy"] = {"meets_requirements": round(meets, 3),
                            "experience_level": round(lvl, 2),
                            "employer_scale": scale}
        if meets < 0.4:
            reasons.append(f"may not meet stated requirements (p={meets:.2f})")
        # Candidacy stays its own axis and is never folded into `score`. But
        # a role you are clearly ineligible for should not be presented as a
        # shortlist -- that is exactly the BlackRock case (penultimate-year
        # only, you graduate in January). It drops to review, reason visible.
        if meets < 0.3 and verdict == "shortlist":
            verdict = "review"
            reasons.insert(0, "strong role fit, but eligibility looks unmet")
        if exp >= 0.66:
            reasons.append(f"strong experience match ({scale})")

    if verdict == "shortlist":
        reasons.insert(
            0, f"{kind} {programme:.2f} · {fn} {function:.2f} · entry-level {entry:.2f}"
        )

    return Result(
        listing=listing,
        verdict=verdict,
        score=round(score, 3),
        reasons=reasons,
        signals=sig,
        cost_usd=decision.cost_usd,
        latency_ms=decision.latency_ms,
        candidacy=candidacy,
    )
