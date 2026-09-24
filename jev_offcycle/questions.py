"""The question set. This is the actual domain knowledge in this repo.

Generic CV-vs-JD matchers miss off-cycle roles because they do not model
"off-cycle" as a concept at all. You cannot filter for something your taxonomy
does not contain.

Three conventions worth stating, because they are why this exists:

1. OFF-CYCLE is a UK/European convention: a 3-6 month internship running
   outside the summer cycle, typically starting in January, February or
   September. US-shaped tools have no equivalent and classify these as
   "internship" or miss them entirely.

2. OFF-CYCLE ROLES ARE MISFILED. On Trackr they live under `uk-finance`, not
   `uk-tech` -- even when the fund is a technical/deep-tech investor. A crawler
   that reasons from category alone will never see them.

3. AVAILABILITY WINDOW IS A HARD GATE, not a ranking signal. A perfect role
   starting before you are free is noise, and should be scored 0 rather than
   surfaced as a near-miss.

Writing rules for Jev instructions (from the model's own usage contract):
  * Question ids are NOT sent to the model. Every instruction must carry its
    full meaning standalone.
  * Name the condition to test, never the conclusion you expect. "This role is
    an off-cycle internship" -- not "Confirm this excellent off-cycle match".
  * `choice` questions get a no-match option, because often nothing fits.
  * A noul near 0.5 means UNCERTAIN, not "medium intensity".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CandidateProfile:
    """Everything the filter needs about the person searching.

    Defaults are deliberately empty. Fill these in rather than editing the
    question text, so the repo stays useful to anyone running the same search.
    """

    available_from: str  # ISO date, e.g. "2027-01-22"
    availability_note: str  # why -- gives Jev something concrete to reason over
    right_to_work: list[str]  # e.g. ["United Kingdom", "European Union"]
    target_sectors: list[str]
    # Which programme types you want. Defaults to off-cycle because that is the
    # search nothing else serves, but the engine is indifferent -- pass
    # ["summer_internship"] or ["offcycle_internship", "placement_year"] and
    # the scoring follows. This is the generalisation: off-cycle is a choice
    # the profile makes, not a rule baked into the questions.
    target_programmes: tuple[str, ...] = ("offcycle_internship",)
    min_months: int = 3
    max_months: int = 12
    # Condensed professional summary used for candidacy scoring. Empty string
    # disables it and the CV questions are not asked at all.
    #
    # PRIVACY: whatever goes here is sent to the model on EVERY listing. Put
    # in what an employer needs to judge you -- education, employers, what you
    # did -- and leave out name, email, phone and address. They add nothing to
    # the judgement and everything to the exposure.
    cv_summary: str = ""


# Every programme type the classifier knows, with the description Jev reasons
# over. Order is roughly by duration. "unclear" is mandatory -- a choice
# question without a no-match option forces a wrong answer on a vague posting.
PROGRAMME_TYPES: dict[str, str] = {
    "offcycle_internship": (
        "A fixed-term internship running OUTSIDE the summer cycle, typically "
        "three to six months and usually starting in January, February, or "
        "September. Common in UK and European finance. Not a summer internship."
    ),
    "summer_internship": (
        "An internship explicitly scheduled for the summer period, typically "
        "eight to twelve weeks between June and August."
    ),
    "placement_year": (
        "A year-long industrial placement or sandwich year, typically twelve "
        "months, taken between years of a degree."
    ),
    "spring_week": (
        "A short insight programme of a few days to two weeks, aimed at first-"
        "year students, usually around the spring vacation."
    ),
    "graduate_scheme": (
        "A structured entry programme for recent graduates, usually starting "
        "at a fixed annual intake and lasting one to three years."
    ),
    "permanent_role": "An open-ended full-time position with no fixed end date.",
    "unclear": "The posting does not give enough information to tell these apart.",
}


def build_questions(profile: CandidateProfile) -> dict[str, dict]:
    """One batch of independent judgements about a single listing."""
    rtw = ", ".join(profile.right_to_work)
    sectors = ", ".join(profile.target_sectors)

    # Candidacy questions are added only when a CV summary is supplied, so the
    # default build stays free of personal data and costs nothing extra.
    cv: dict[str, dict] = {}
    if profile.cv_summary.strip():
        cv = {
            "meets_stated_requirements": {
                "type": "noul",
                "instructions": (
                    "The candidate described under `candidate` in the state "
                    "satisfies the hard eligibility requirements this posting "
                    "states -- year of study, degree status, graduation window, "
                    "and any required prior experience. Judge only against "
                    "requirements the posting actually states. Where it states "
                    "none, this condition is satisfied."
                ),
            },
            "experience_fit": {
                "type": "score",
                "instructions": (
                    "How well does the candidate's prior experience, described "
                    "under `candidate`, match the work this role actually "
                    "involves day to day?"
                ),
                "criteria": [
                    "No relevant experience: nothing in the background touches "
                    "this kind of work",
                    "Adjacent only: related field or transferable analytical "
                    "skills, but not this work",
                    "Directly relevant: has done substantially this work before, "
                    "in a comparable setting",
                    "Unusually strong: directly relevant experience plus "
                    "something distinctive this employer would find hard to "
                    "find elsewhere",
                ],
            },
            "employer_scale_fit": {
                "type": "choice",
                "instructions": (
                    "What size of organisation is hiring, judged from the "
                    "posting? Smaller employers often weigh unusual backgrounds "
                    "more heavily than structured graduate pipelines do."
                ),
                "criteria": {
                    "startup": "An operating company under roughly 50 people",
                    "small_firm": "A boutique fund or firm, roughly 10-50 people",
                    "mid_size": "An established firm of roughly 50-500 people",
                    "large_institution": (
                        "A bank, asset manager or corporation of 500+ people, "
                        "typically with a structured recruitment programme"
                    ),
                    "unclear": "The posting does not indicate the size",
                },
            },
        }

    return {
        **cv,
        "is_target_sector": {
            "type": "noul",
            "instructions": (
                f"The hiring organisation works in one of these sectors: {sectors}. "
                "Judge by what the organisation actually does, not by the job "
                "title. A corporate venture arm or an accelerator counts; a bank's "
                "general graduate programme does not."
            ),
        },
        "is_early_stage": {
            "type": "noul",
            "instructions": (
                "The hiring organisation invests primarily at pre-seed, seed, or "
                "Series A stage. Growth-equity, buyout, late-stage, and "
                "public-markets investors do not satisfy this."
            ),
        },
        # --- hard gates ----------------------------------------------------
        # Also framed as a barrier. "Starts after X" returns a low probability
        # both when the role starts too early AND when no date is stated --
        # two very different situations that a single threshold cannot tell
        # apart. Testing for a CONFLICT means silence scores low, which is
        # correct: an unstated date is not a conflict, it is a question.
        "starts_too_early": {
            "type": "noul",
            "instructions": (
                "This posting states a start date that falls BEFORE "
                f"{profile.available_from}. Context on the candidate's "
                f"availability: {profile.availability_note}. Answer only on "
                "the basis of a start date the posting actually states. If no "
                "start date is given, the posting does not state a conflicting "
                "date and this condition does not hold."
            ),
        },
        "start_date_stated": {
            "type": "noul",
            "instructions": (
                "This posting states a specific start date or start period for "
                "the role, such as a named month, quarter, or date."
            ),
        },
        # Framed as "is a BARRIER present", not "is it permitted". Absence of
        # visa language is the common case and is weak evidence FOR eligibility,
        # not evidence against it. Asking the positive form made a plain London
        # posting score 0.42 -- uncertain -- and get rejected. Asking for the
        # barrier lets silence mean what it actually means.
        "has_visa_barrier": {
            "type": "noul",
            "instructions": (
                "This posting presents a work-authorisation barrier for a "
                f"candidate who already has the right to work in: {rtw}. "
                "A barrier means the role is located outside those places, or "
                "the posting explicitly requires work authorisation the "
                "candidate would not hold, or states that sponsorship is "
                "unavailable and is needed. A role located inside those places "
                "with no stated authorisation requirement is NOT a barrier."
            ),
        },
        # --- shape of the role ---------------------------------------------
        # This replaced a separate `is_offcycle` noul question. The two were
        # asking the same thing, so one API call per listing was being paid
        # for twice, and the pair could disagree. A choice over all programme
        # types is strictly more informative: it says what the role IS, not
        # merely whether it is one particular thing.
        "role_kind": {
            "type": "choice",
            "instructions": (
                "Classify what kind of position this posting is for, based on "
                "its stated duration, timing, seniority, and terms."
            ),
            "criteria": dict(PROGRAMME_TYPES),
        },
        "technical_depth": {
            "type": "score",
            "instructions": (
                "How much engineering or technical work does this role involve "
                "day to day, as described in the posting?"
            ),
            "criteria": [
                "Purely commercial: sourcing, screening, market mapping, no "
                "technical evaluation expected",
                "Some technical literacy expected: reading product docs, "
                "understanding a technical pitch, but no building",
                "Technical diligence is a named responsibility: assessing "
                "architecture, evaluating technical founders' claims",
                "Hands-on building: writing code, running data analysis, or "
                "creating internal tooling as a core part of the job",
            ],
        },
    }


# Disqualifying conditions, phrased so that HIGH probability means "this
# listing is out". Three bands, not one threshold:
#
#   p >= GATE_REJECT   the barrier is established        -> reject
#   p >= GATE_REVIEW   the barrier might be there        -> human looks
#   p <  GATE_REVIEW   no barrier found                  -> continue scoring
#
# The two-band split exists because a single 0.5 cut treats "uncertain" as
# "disqualified". That rejected a London pre-seed role starting February 2027
# -- an exact match -- on a visa probability of 0.42. Uncertainty must cost a
# glance, never a missed role.
DISQUALIFIERS = ("starts_too_early", "has_visa_barrier")
GATE_REJECT = 0.70
GATE_REVIEW = 0.35

# Below this, a listing is not worth a human's attention.
SHORTLIST_THRESHOLD = 0.6

# Between GATE_THRESHOLD and this, route for manual review rather than
# auto-accepting or auto-rejecting. Uncertainty should cost a glance, not a
# missed role.
REVIEW_BAND = (0.4, 0.6)
