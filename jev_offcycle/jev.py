"""Jev transport. The ONLY module that knows how decisions reach a model.

Swap backends here and nothing else changes:
  - openrouter (default) -- OPENROUTER_API_KEY, via OpenRouter's Decisions endpoint
  - typesafe             -- TYPESAFE_API_KEY, direct to TypeSafe (one less proxy hop)

Pure stdlib. No dependencies.

Endpoint notes, established empirically on 2026-09-22 because they are not
documented anywhere obvious:
  * OpenRouter model slug is "~typesafe/jev-latest" -- the LEADING TILDE IS
    REQUIRED. Without it you get {"error": "Model ... does not exist"}.
  * Jev is NOT in OpenRouter's public /api/v1/models list, and
    openrouter.ai/~typesafe/jev-latest returns 404. It lives only on the
    Decisions endpoint. This is expected, not a fault.
  * `questions` is an OBJECT keyed by question id, not an array.
  * Each question needs `instructions` (NOT `prompt`).
  * `choice` questions additionally require a `criteria` record.
  * The /api/alpha/ path is alpha and upstream warns it may move. That is the
    entire reason this module exists as a seam.

Measured latency via OpenRouter with a persistent connection: median 324 ms,
min 296 ms, of which only ~57 ms is DNS+TLS. The rest is inference plus the
proxy hop, so a local transport saves less than you would hope. See README.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "~typesafe/jev-latest"  # leading tilde is load-bearing

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"


class JevError(RuntimeError):
    """Raised when a decision could not be obtained. Never swallowed silently.

    A guardrail that fails open is worse than no guardrail: it hides the
    outage it was meant to catch. Callers decide what to do; this layer
    refuses to invent an answer.
    """


@dataclass
class Decision:
    """One Jev response. `answers` maps question id -> typed answer."""

    answers: dict[str, Any]
    model: str
    cost_usd: float
    latency_ms: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def noul(self, qid: str) -> float:
        """Probability for a `noul` question. 0.5 means UNCERTAIN, not medium."""
        a = self.answers.get(qid)
        if not a or a.get("type") != "noul":
            raise JevError(f"question {qid!r} is not a noul answer: {a!r}")
        return float(a["noul"])

    def choice(self, qid: str) -> tuple[str, float]:
        """(selected option, confidence). Confidence measures how concentrated
        the distribution is -- not whether the answer is correct."""
        a = self.answers.get(qid)
        if not a or a.get("type") != "choice":
            raise JevError(f"question {qid!r} is not a choice answer: {a!r}")
        return a["choice"], float(a.get("confidence", 0.0))

    def probabilities(self, qid: str) -> dict[str, float]:
        """Full distribution over a `choice` question's options.

        Using only the top pick throws away most of the answer. A listing at
        0.7 off-cycle / 0.3 summer and one at 1.0 off-cycle both return
        "offcycle_internship", and they are not the same listing. Summing the
        mass on the options you actually want gives a graded match for free --
        no extra question, no threshold to tune.

        Returns {} if the backend omitted the distribution, so callers must
        degrade rather than assume.
        """
        a = self.answers.get(qid)
        if not a or a.get("type") != "choice":
            raise JevError(f"question {qid!r} is not a choice answer: {a!r}")
        return {k: float(v) for k, v in (a.get("probabilities") or {}).items()}


class JevClient:
    def __init__(self, backend: str | None = None, timeout: float = 20.0):
        self.timeout = timeout
        self.backend = backend or os.getenv("JEV_BACKEND") or self._autodetect()
        if self.backend == "typesafe":
            self.url, self.model = TYPESAFE_URL, TYPESAFE_MODEL
            self.key = os.environ["TYPESAFE_API_KEY"]
        elif self.backend == "openrouter":
            self.url, self.model = OPENROUTER_URL, OPENROUTER_MODEL
            self.key = os.environ["OPENROUTER_API_KEY"]
        else:
            raise JevError(f"unknown backend {self.backend!r}")

    @staticmethod
    def _autodetect() -> str:
        # TypeSafe first: it is the direct route, one fewer hop than OpenRouter.
        if os.getenv("TYPESAFE_API_KEY"):
            return "typesafe"
        if os.getenv("OPENROUTER_API_KEY"):
            return "openrouter"
        raise JevError(
            "No key found. Set OPENROUTER_API_KEY (openrouter.ai/keys) or "
            "TYPESAFE_API_KEY (console.typesafe.ai)."
        )

    def decide(self, state: Any, questions: dict[str, dict]) -> Decision:
        """Ask several independent questions about one state, in ONE call.

        Batched questions run in parallel and cannot see each other's answers,
        which is what we want: three independent reads, not a chain where an
        early guess contaminates the rest.

        `state` should be what was OBSERVED -- the raw listing text and fields.
        Do not put your own verdict in it. Jev reads an asserted conclusion as
        evidence, and the confidence you get back is then agreement with
        yourself rather than independent corroboration.
        """
        if not questions:
            raise JevError("no questions given")
        if not isinstance(state, str):
            state = json.dumps(state, ensure_ascii=False, default=str)

        payload = json.dumps(
            {"model": self.model, "state": state, "questions": questions}
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise JevError(f"Jev HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise JevError(f"Jev unreachable: {e.reason}") from e
        latency_ms = (time.perf_counter() - t0) * 1000

        if "answers" not in body:
            raise JevError(f"malformed Jev response: {str(body)[:400]}")

        return Decision(
            answers=body["answers"],
            model=body.get("model", self.model),
            cost_usd=float((body.get("usage") or {}).get("cost", 0.0)),
            latency_ms=latency_ms,
            raw=body,
        )
