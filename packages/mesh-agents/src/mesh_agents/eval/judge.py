"""The accuracy oracle: grade a belief statement against the live web.

``BeliefJudge`` is the seam the accuracy eval depends on; tests inject a stub.
``AnthropicWebSearchJudge`` is the real one — it hands the claim to Claude with
the server-side ``web_search`` tool, lets the model research it, and parses a
structured verdict from the final text. Grounding comes from Anthropic's own
web search, so it needs only ``ANTHROPIC_API_KEY`` (no Brave key).

The judge never raises into the caller: any provider/parse failure grades the
belief ``unverifiable`` with the error as rationale, so one bad grade can't
abort a sweep.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Protocol

from mesh_agents.eval.models import Verdict

logger = logging.getLogger(__name__)

_DEFAULT_JUDGE_MODEL = "claude-haiku-4-5"
_DEFAULT_MAX_SEARCHES = 4

_SYSTEM = """\
You are a fact-checker for a knowledge base about: {field}.

You are given ONE belief the knowledge base currently holds. Use web search to
determine whether it is TRUE and CURRENT as of today. Search for the specific
entities, numbers, and events in the belief; prefer primary/official and recent
sources.

Then return your verdict as a JSON object — and nothing after it — of exactly
this shape:

{{"verdict": "<supported|partially_supported|contradicted|unverifiable>",
  "confidence": <0.0-1.0>,
  "rationale": "<one or two sentences citing what the evidence showed>",
  "evidence_urls": ["<url>", ...]}}

Verdict meanings:
- "supported": the evidence confirms the belief.
- "partially_supported": the core is right but some detail is wrong, stale, or
  overstated.
- "contradicted": the evidence shows the belief is false or out of date.
- "unverifiable": you could not find evidence either way (opinion, too obscure,
  or malformed belief text such as a raw UUID or an unresolved entity id).

Judge the belief as written. If the belief's subject is an unresolved id/UUID
rather than a real name, that is "unverifiable"."""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class GradeResult(dict[str, Any]):
    """Loose grade payload: verdict/judge_confidence/rationale/evidence_urls/tokens."""


class BeliefJudge(Protocol):
    def grade(self, statement: str, field_label: str) -> GradeResult:
        """Grade one belief statement against external evidence."""
        ...


def _parse_verdict(text: str) -> tuple[Verdict, float, str, list[str]]:
    """Pull the trailing JSON verdict out of the model's answer, leniently."""
    matches = _JSON_RE.findall(text)
    for candidate in reversed(matches):  # the verdict is the last JSON object
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or "verdict" not in data:
            continue
        try:
            verdict = Verdict(str(data["verdict"]).strip().lower())
        except ValueError:
            verdict = Verdict.unverifiable
        conf = data.get("confidence", 0.5)
        try:
            conf = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            conf = 0.5
        rationale = str(data.get("rationale", "")).strip()
        urls_raw = data.get("evidence_urls") or []
        urls = [str(u) for u in urls_raw if isinstance(u, str)][:8]
        return verdict, conf, rationale, urls
    return Verdict.unverifiable, 0.0, "judge returned no parsable verdict", []


class AnthropicWebSearchJudge:
    """Grade beliefs with Claude + the server-side web_search tool."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_searches: int = _DEFAULT_MAX_SEARCHES,
    ) -> None:
        self.model = model or os.environ.get("MESH_EVAL_JUDGE_MODEL", _DEFAULT_JUDGE_MODEL)
        self._max_searches = max_searches
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set — the accuracy judge needs it for "
                "web-grounded fact-checking. Set it, or run with --no-accuracy."
            )
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=key)

    def grade(self, statement: str, field_label: str) -> GradeResult:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM.format(field=field_label),
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": self._max_searches,
                    }
                ],
                messages=[{"role": "user", "content": f"Belief: {statement}"}],
            )
        except Exception as exc:  # provider error — never abort the sweep
            logger.warning("judge_error", extra={"error": str(exc)})
            return GradeResult(
                verdict=Verdict.unverifiable,
                judge_confidence=0.0,
                rationale=f"judge error: {exc}",
                evidence_urls=[],
                tokens=0,
            )

        text = "".join(
            getattr(block, "text", "")
            for block in message.content
            if getattr(block, "type", None) == "text"
        )
        verdict, conf, rationale, urls = _parse_verdict(text)
        usage = getattr(message, "usage", None)
        tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        return GradeResult(
            verdict=verdict,
            judge_confidence=conf,
            rationale=rationale,
            evidence_urls=urls,
            tokens=tokens,
        )
