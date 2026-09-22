"""Tests. Run offline by default; the live arm needs a key and costs ~$0.0003.

    python3 -m pytest tests/ -q              # offline only
    JEV_LIVE=1 python3 -m pytest tests/ -q   # includes the live fixture run

The offline tests matter more than they look. Every bug found in review was in
the *banding logic*, not the model: uncertainty being read as falsity. That is
exactly what can be tested without spending anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jev_offcycle.classify import Listing, Result, _read, _score_level, classify
from jev_offcycle.jev import Decision, JevError
from jev_offcycle.questions import (
    GATE_REJECT,
    GATE_REVIEW,
    CandidateProfile,
    build_questions,
)

PROFILE = CandidateProfile(
    available_from="2027-01-22",
    availability_note="Final examination is on 21 January 2027.",
    right_to_work=["United Kingdom", "European Union"],
    target_sectors=["venture capital", "early-stage technology investing"],
)


def _decision(**answers) -> Decision:
    return Decision(answers=answers, model="test", cost_usd=0.0, latency_ms=0.0)


class _FakeClient:
    """Returns a canned Decision so banding logic can be tested for free."""

    def __init__(self, decision: Decision):
        self.decision = decision

    def decide(self, state, questions):  # noqa: ARG002
        return self.decision


def _full(**over):
    base = {
        "is_offcycle": {"type": "noul", "noul": 0.95},
        "is_target_sector": {"type": "noul", "noul": 0.95},
        "is_early_stage": {"type": "noul", "noul": 0.9},
        "starts_too_early": {"type": "noul", "noul": 0.05},
        "has_visa_barrier": {"type": "noul", "noul": 0.05},
        "start_date_stated": {"type": "noul", "noul": 0.95},
        "role_kind": {"type": "choice", "choice": "offcycle_internship", "confidence": 0.95},
        "technical_depth": {
            "type": "score",
            "score": 2.0,
            "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
            "probabilities": {"0": 0, "1": 0, "2": 1, "3": 0},
            "confidence": 1.0,
        },
    }
    base.update(over)
    return _decision(**base)


LISTING = Listing(title="Off-Cycle Investment Intern", organisation="Test Fund")


def _run(decision) -> Result:
    return classify(LISTING, PROFILE, _FakeClient(decision))


# --- the regression that motivated the whole redesign -------------------


def test_uncertain_visa_does_not_reject():
    """A London posting with no visa language scored 0.42 and was REJECTED.

    That is the bug this repo exists to document: an unstated condition
    scoring the same as a false one. 0.42 sits in the review band now.
    """
    r = _run(_full(has_visa_barrier={"type": "noul", "noul": 0.42}))
    assert r.verdict != "reject"
    assert any("visa" in x or "authorisation" in x for x in r.reasons)


def test_established_visa_barrier_does_reject():
    r = _run(_full(has_visa_barrier={"type": "noul", "noul": 0.96}))
    assert r.verdict == "reject"
    assert r.score == 0.0


def test_missing_start_date_routes_to_review_not_reject():
    r = _run(
        _full(
            start_date_stated={"type": "noul", "noul": 0.05},
            starts_too_early={"type": "noul", "noul": 0.20},
        )
    )
    assert r.verdict == "review"
    assert any("no start date" in x for x in r.reasons)


def test_gate_bands_are_ordered():
    assert 0.0 < GATE_REVIEW < GATE_REJECT < 1.0


# --- scoring ------------------------------------------------------------


def test_summer_internship_is_penalised():
    r = _run(
        _full(
            is_offcycle={"type": "noul", "noul": 0.02},
            role_kind={"type": "choice", "choice": "summer_internship", "confidence": 1.0},
        )
    )
    assert r.verdict == "reject"


def test_technical_depth_raises_score():
    low = _run(_full(technical_depth={
        "type": "score", "score": 0.0,
        "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
        "probabilities": {}, "confidence": 1.0}))
    high = _run(_full(technical_depth={
        "type": "score", "score": 3.0,
        "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
        "probabilities": {}, "confidence": 1.0}))
    assert high.score > low.score, "technical_depth must affect the score, not just signals"


def test_score_level_is_zero_indexed():
    """N levels span 0..N-1. Getting this wrong misreports every score answer."""
    d = _full(technical_depth={
        "type": "score", "score": 2.8,
        "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
        "probabilities": {"2": 0.2, "3": 0.8}, "confidence": 0.8})
    level, n = _score_level(d, "technical_depth")
    assert (level, n) == (2.8, 4)
    assert level <= n - 1, "2.8 must sit within 0..3, not be read as 2.8/4"


def test_read_preserves_score_probabilities():
    sig = _read(_full())
    assert "probabilities" in sig["technical_depth"]
    assert "confidence" in sig["technical_depth"]


# --- contract with the model -------------------------------------------


def test_questions_use_instructions_not_prompt():
    """`prompt` is silently wrong -- the API rejects it. Easy regression."""
    for qid, q in build_questions(PROFILE).items():
        assert "instructions" in q, f"{qid} missing `instructions`"
        assert "prompt" not in q, f"{qid} uses `prompt`; the API wants `instructions`"


def test_choice_questions_have_criteria_and_a_no_match_option():
    qs = build_questions(PROFILE)
    for qid, q in qs.items():
        if q["type"] == "choice":
            assert "criteria" in q, f"{qid} is a choice without `criteria`"
            assert "unclear" in q["criteria"], f"{qid} needs a no-match option"


def test_score_criteria_is_an_ordered_list():
    qs = build_questions(PROFILE)
    for qid, q in qs.items():
        if q["type"] == "score":
            assert isinstance(q["criteria"], list), f"{qid} score criteria must be ordered"
            assert len(q["criteria"]) >= 2


def test_profile_is_interpolated_into_instructions():
    text = json.dumps(build_questions(PROFILE))
    assert "2027-01-22" in text
    assert "United Kingdom" in text


def test_decision_noul_rejects_wrong_type():
    d = _full()
    with pytest.raises(JevError):
        d.noul("role_kind")


# --- live -----------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("JEV_LIVE"), reason="set JEV_LIVE=1 (costs ~$0.0003)")
def test_live_fixture_verdicts():
    """The six-listing fixture, against the real model.

    Expected verdicts are asserted loosely -- shortlist vs not, reject vs not --
    because exact probabilities will drift as the model updates. Drift in the
    VERDICT is a real regression; drift in the third decimal is not.
    """
    from jev_offcycle.jev import JevClient

    client = JevClient()
    listings = json.loads((Path(__file__).parent.parent / "examples/listings.json").read_text())
    known = set(Listing.__dataclass_fields__)
    got = {}
    for item in listings:
        lst = Listing(**{k: v for k, v in item.items() if k in known})
        got[lst.organisation] = classify(lst, PROFILE, client).verdict

    assert got["Northgate Ventures"] == "shortlist"
    assert got["Cobalt Ventures"] == "review"
    for org in ("Meridian Capital Partners", "Halberd Asset Management",
                "Lattice Fund", "Aperture Seed"):
        assert got[org] == "reject", f"{org} should be rejected, got {got[org]}"
