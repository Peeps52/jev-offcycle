---
name: jev-offcycle
description: >-
  Filter job listings for OFF-CYCLE early-stage VC and technical roles — the
  UK/European 3–6 month placements that run outside the summer cycle and that
  generic CV-vs-JD matchers miss entirely. Scores each listing with calibrated
  probabilities via TypeSafe's Jev and returns shortlist / review / reject with
  named reasons. Use when asked to sift job listings, find off-cycle
  internships, check whether a role fits an availability window or right-to-work
  constraint, or triage a careers feed. Triggers: "off-cycle", "find me VC
  internships", "filter these listings", "is this role worth applying to",
  "shortlist these jobs". NOT for writing CVs or cover letters, and NOT for
  scraping job boards — it takes listings as JSON.
---

# jev-offcycle

Classifies job listings into **shortlist / review / reject** using Jev, a
System One model returning typed answers with calibrated probabilities rather
than prose.

## Before running

Needs a key in the environment — `OPENROUTER_API_KEY` or `TYPESAFE_API_KEY`.
If neither is set, say so and stop; do not fabricate verdicts.

## Running it

```bash
python3 -m jev_offcycle listings.json          # hides rejects
python3 -m jev_offcycle listings.json --all    # shows everything
python3 -m jev_offcycle listings.json --json   # machine-readable, includes raw signals
```

Input is a JSON array. Only `title` and `organisation` are required; unknown
keys are ignored with a warning.

```json
[{"title": "...", "organisation": "...", "location": "", "start_date": "",
  "duration": "", "description": "", "source": "", "url": ""}]
```

## Setting the candidate profile

Edit `DEFAULT_PROFILE` in `jev_offcycle/cli.py` — availability date, right to
work, target sectors. **Do not edit the question text in `questions.py`**; the
profile is interpolated into it, and rewriting instructions by hand is how the
calibration gets broken.

## Reading the output

```
+ [0.96] Off-Cycle Investment Intern — Northgate Ventures      shortlist
? [0.66] Investment Team Intern — Cobalt Ventures              review
- [0.00] Venture Capital Intern — Lattice Fund                 reject
```

**`review` is a real verdict, not a weak reject.** It means a condition could
not be established — usually a missing start date. Report these to the user as
things to confirm, never silently drop them.

## Interpreting probabilities — read this before reporting anything

- A `noul` near **0.5 means UNCERTAIN**, not "medium". Never report 0.5 as a
  half-match.
- `confidence` measures how **concentrated** the distribution is, not whether
  the answer is right. High confidence on a wrong premise is still wrong.
- `score` answers are **0-indexed** over their criteria: N levels span 0 to
  N-1. A 2.8 over 4 levels sits between levels 2 and 3 — it is *not* 2.8/4.
  Report it using the labels in the response `legend`.

## When a listing is rejected

Always say *why*, using the reason strings. "Rejected" alone is useless; "states
a start date before 2027-01-22 (p=0.81)" lets the user overrule it.

## Errors

`JevError` is raised, never swallowed. If a listing fails to classify, surface
it. A filter that silently discards listings on API errors is worse than no
filter — it hides the outage it should have reported.
