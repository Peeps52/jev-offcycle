# jev-offcycle

Finds **off-cycle** early-stage VC internships that generic CV-vs-JD matchers miss, using [TypeSafe's Jev](https://typesafe.ai) for typed, calibrated decisions.

```
+ [0.95] Off-Cycle Investment Intern — Northgate Ventures
      off-cycle 0.96 · sector 0.97 · early-stage 0.91
? [0.65] Investment Team Intern — Cobalt Ventures
      no start date given — confirm timing before applying; off-cycle status uncertain (p=0.40)
- [0.00] Venture Capital Intern (Off-Cycle) — Lattice Fund
      work-authorisation barrier (p=0.96)
```

Six listings, `$0.000265`, ~450 ms each. Roughly **22,600 listings per dollar**.

## Why this exists

There are already several good Jev job tools. Every one of them does generic
CV-versus-job-description matching, and all of them are US-shaped. This one
encodes three things they structurally cannot:

**1. "Off-cycle" is a concept, not a keyword.** It is a UK/European convention
— a 3–6 month placement running outside the summer cycle, usually starting in
January, February, or September. A matcher whose taxonomy has no such category
cannot filter for it. Here it is the highest-weighted signal in the score.

**2. Off-cycle roles are misfiled.** On Trackr they sit under `uk-finance`, not
`uk-tech`, even when the fund is a deep-tech investor. Anything reasoning from
source category alone will never see them. The source category is passed to the
model as observed context, never used as a filter.

**3. Availability is a hard gate, not a ranking signal.** A perfect role
starting before you are free is noise. It should score zero, not surface as a
near-miss.

## Install

Pure standard library. No dependencies.

```bash
git clone https://github.com/YOUR_USER/jev-offcycle && cd jev-offcycle
export OPENROUTER_API_KEY=...        # openrouter.ai/keys
# or: export TYPESAFE_API_KEY=...    # console.typesafe.ai — one less proxy hop
python3 -m jev_offcycle examples/listings.json
```

Edit `DEFAULT_PROFILE` in `jev_offcycle/cli.py` — availability date, right to
work, target sectors. Don't edit the question text; the profile is
interpolated into it.

## The lesson worth stealing: uncertainty is not falsity

The first working version rejected the *ideal* listing — London, February 2027,
pre-seed, exactly the target — on a visa probability of **0.42**.

Two bugs, one root cause. Both questions were phrased so that a thing being
**unstated** produced the same low number as it being **false**:

```python
# BROKEN — silence scores the same as a genuine conflict
"no_visa_barrier":        "A candidate with RTW in {places} could take this role."
"starts_after_available": "The role's start date is on or after {date}."
```

A London posting with no visa language is the *common* case. Asking Jev to
positively establish eligibility from an absence of evidence returns ~0.4 —
uncertain — and a single `p < 0.5` gate read that as disqualifying.

```python
# FIXED — test for the barrier, so silence means what it should
"has_visa_barrier":  "This posting presents a work-authorisation barrier ...
                      A role located inside those places with no stated
                      requirement is NOT a barrier."
"starts_too_early":  "This posting states a start date BEFORE {date}. Answer
                      only on a date the posting actually states."
```

Plus three bands instead of one threshold:

| Probability | Action |
|---|---|
| `p ≥ 0.70` | reject — barrier established |
| `0.35 ≤ p < 0.70` | **review** — a human glances |
| `p < 0.35` | continue scoring |

And a separate `start_date_stated` question, so "no date given" routes to review
with *confirm timing before applying* rather than being silently discarded.

Missing a real opportunity is the expensive error. Glancing at an irrelevant
listing costs seconds. The thresholds encode that asymmetry.

## Jev API notes

Established empirically on 2026-09-22, because they are not documented
anywhere obvious. Four failed calls to find these:

- OpenRouter endpoint is `https://openrouter.ai/api/alpha/decisions`
- Model slug is **`~typesafe/jev-latest`** — **the leading tilde is required**.
  Without it: `{"error": "Model typesafe/jev-latest does not exist"}`
- Jev is **not** in OpenRouter's public `/api/v1/models` list, and
  `openrouter.ai/~typesafe/jev-latest` returns 404. It lives only on the
  Decisions endpoint. Expected, not a fault.
- `questions` is an **object** keyed by question id, not an array
- Each question needs **`instructions`**, not `prompt`
- `choice` questions additionally require a **`criteria`** record
- Question types are `noul` | `choice` | `score`
- Phrase `instructions` as a **statement to assess**, not a question:
  "This is an off-cycle internship", not "Is this off-cycle?"

`/api/alpha/` is alpha and upstream warns it may move. That is why every call
goes through one function in `jev_offcycle/jev.py`.

## Measured latency

Via OpenRouter, persistent connection, single question:

```
548 392 296 403 324 324 395 305 ms     median 324 · min 296
DNS 2ms · TLS 55ms · TTFB ≈ total
```

Only ~57 ms is connection overhead. The rest is inference plus OpenRouter's
proxy hop, so **a local transport saves less than you would expect** — Jev has
no public weights and cannot be self-hosted. A direct `TYPESAFE_API_KEY` removes
the proxy hop; for a fixed question set like this one, distilling to a local
classifier from Jev-labelled examples is the only route to sub-50 ms.

## Layout

```
jev_offcycle/
  jev.py         transport — the only file that knows how decisions reach a model
  questions.py   the domain knowledge: off-cycle conventions, gates, thresholds
  classify.py    listing -> shortlist | review | reject, with reasons
  cli.py         edit DEFAULT_PROFILE here
```

Errors are raised, never swallowed. A filter that silently drops a listing on
an API error is a filter you cannot trust.

## Tests

```bash
python3 -m pytest tests/ -q              # 13 offline, free
JEV_LIVE=1 python3 -m pytest tests/ -q   # + live fixture, ~$0.0003
```

The offline tests carry most of the weight. Every bug found in review was in
the **banding logic**, not the model — uncertainty being read as falsity — and
that needs no API call to catch. They also pin the API contract: `instructions`
not `prompt`, `criteria` on every `choice`, a no-match option, ordered `score`
criteria, and 0-indexed score levels.

The live arm asserts *verdicts*, not probabilities. Drift in the third decimal
as the model updates is expected; a listing changing verdict is a regression.

## Use as a Claude Code skill

`skills/jev-offcycle/SKILL.md` ships alongside the library, so Claude Code can
drive it conversationally. The skill carries the interpretation rules that are
easy to get wrong: a `noul` of 0.5 is *uncertain* not *medium*, `confidence`
measures distribution concentration rather than correctness, and `score`
answers are 0-indexed over their criteria.

## Status

Early. Classification is tested against the six-listing fixture in `examples/`,
which includes deliberate traps: a summer programme labelled as an analyst role,
an off-cycle role that needs US authorisation, a role starting too early, and a
posting with no date at all. **There is no crawler yet — feed it JSON.** That is
the obvious next piece.

MIT.
