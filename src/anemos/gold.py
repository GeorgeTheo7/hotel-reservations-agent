"""The gold client: a `ModelClient` that answers every model call of a run from the
case's labels, so the real run (tools, router, loop, final write) can be tested and
demonstrated without a model. It tests the pipeline, not a model: the agent eval
measures the model (eval/).

Only the demo inbox and tests may import this module (tests/unit/test_import_direction).

- Extraction (`send`, ExtractionOutput): the case's gold extraction.
- The policy selector (`send`, PolicySelection): the case's labelled policy_ids (empty
  for a NO_POLICY case, so code escalates it).
- Agent turns (`send_turn`): decided from the transcript, as a careful agent would:
  1. route `other`: escalate with the labelled code;
  2. each labelled required tool not called yet, in label order;
  3. a labelled code only the model chooses (COMPLAINT, DISCOUNT, CHANGE, CANCELLATION,
     MODEL): escalate it; a labelled NO_POLICY: search_policies (code ends the run);
  4. a stay: quote_price; after FULLY_BOOKED, suggest_alternatives; after
     ORIGINAL_AVAILABLE, quote_price again; a policy question: search_policies;
  5. write_draft: offer every quote of the latest answer that has quotes; else decline
     (CLOSED, with {{OPEN_SEASON}}; NONE_FOUND), ask for what is missing, or answer the
     question. Texts are fixed per route and language (the stub drafter's templates,
     with {{OPEN_SEASON}} for the season dates;
     gold_policy_*.txt for an answer), citing every policy id returned.

Every answer reports zero usage and the response model `gold-labels`, so the audit
says who answered; callers give it a throwaway spend ledger.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from string import Template
from typing import Any, get_args

from pydantic import BaseModel

from anemos.agent.tools import (
    ESCALATE,
    QUOTE_PRICE,
    SEARCH_POLICIES,
    SUGGEST_ALTERNATIVES,
    WRITE_DRAFT,
    ModelEscalationCode,
)
from anemos.drafting import OPEN_SEASON, phrases, template
from anemos.email_cases import EmailCase
from anemos.extraction import ExtractionOutput
from anemos.extraction_run import LEFT, RIGHT
from anemos.llm.adapter import (
    Prompt,
    Reply,
    Settings,
    Tool,
    ToolCall,
    ToolOutput,
    Transcript,
    TurnReply,
    UserText,
)
from anemos.llm.pricing import Usage
from anemos.policies import PolicySelection
from anemos.reservations.formatting import Lang
from anemos.summary_schema import EscalationSummary

GOLD_RESPONSE_MODEL = "gold-labels"
NO_USAGE = Usage(0, 0, 0)
GOLD_QUERY = "What does the hotel's policy say about the question in this email?"
MODEL_CODES = frozenset({"COMPLAINT", "DISCOUNT", "CHANGE", "CANCELLATION", "MODEL"})
SECONDARY = frozenset({"COMPLAINT", "DISCOUNT"})  # what a summary may add (D81)


@dataclass
class GoldClient:
    cases: Sequence[EmailCase]
    current: EmailCase | None = None  # the email being run (the selector's call has none)
    turns: int = 0
    calls: list[str] = field(default_factory=list)

    def send[T: BaseModel](self, prompt: Prompt, schema: type[T], settings: Settings) -> Reply[T]:
        if schema is PolicySelection:
            case = self._require_current()
            ids = case.labels.policy_ids
            output: Any = schema.model_validate({"policy_ids": ids})
        elif schema is EscalationSummary:  # D81: the labelled codes only a model judges
            case = self._find(prompt.input)
            labelled = {c.value for c in case.labels.reason_codes} & SECONDARY
            output = schema.model_validate({"summary": f"Gold summary of {case.id}.",
                                            "add_codes": sorted(labelled)})  # fmt: skip
        elif schema is ExtractionOutput:
            case = self._find(prompt.input)
            gold = case.gold_output()
            if gold is None:
                raise LookupError(f"{case.id} has no gold extraction")
            output = schema.model_validate(gold.model_dump())
        else:
            raise LookupError(f"the gold client does not answer {schema.__name__}")
        self.calls.append(schema.__name__)
        return Reply(output, NO_USAGE, GOLD_RESPONSE_MODEL)

    def send_turn(
        self, transcript: Transcript, tools: Sequence[Tool], settings: Settings
    ) -> TurnReply:
        first = transcript.items[0]
        assert isinstance(first, UserText)
        case = self._find(first.text)
        self.turns += 1
        name, arguments = _decide(case, transcript)
        self.calls.append(name)
        call = ToolCall(f"gold_{self.turns}", name, json.dumps(arguments, ensure_ascii=False))
        return TurnReply(call, NO_USAGE, GOLD_RESPONSE_MODEL)

    def _find(self, text: str) -> EmailCase:
        for case in self.cases:
            body = case.email.body.strip().replace("<", LEFT).replace(">", RIGHT)
            if body in text:
                self.current = case
                return case
        raise LookupError("this email is not one of the gold cases")

    def _require_current(self) -> EmailCase:
        if self.current is None:
            raise LookupError("a selector call before any email")
        return self.current


# --- the gold agent's decisions -----------------------------------------------------------


def _decide(case: EmailCase, transcript: Transcript) -> tuple[str, dict[str, Any]]:
    labels = case.labels
    route = _route(transcript)
    called = [i.name for i in transcript.items if isinstance(i, ToolCall)]
    answers = _answers(transcript)
    codes = [c.value for c in labels.reason_codes]
    model_code = next((c for c in codes if c in MODEL_CODES), None)

    if route == "other":
        return ESCALATE, _escalation(model_code or "MODEL")
    for tool in labels.required_tools:
        if tool not in called:
            return tool, _arguments(tool)
    if model_code is not None:
        return ESCALATE, _escalation(model_code)
    if "NO_POLICY" in codes and SEARCH_POLICIES not in called:
        return SEARCH_POLICIES, _arguments(SEARCH_POLICIES)
    if route == "policy":
        if SEARCH_POLICIES not in called:
            return SEARCH_POLICIES, _arguments(SEARCH_POLICIES)
    else:
        priced = [(n, a) for n, a in answers if n in (QUOTE_PRICE, SUGGEST_ALTERNATIVES)]
        last = priced[-1][1].get("result") if priced else None
        if not priced:
            return QUOTE_PRICE, {}
        if last == "FULLY_BOOKED" and SUGGEST_ALTERNATIVES not in called:
            return SUGGEST_ALTERNATIVES, {}
        if last == "ORIGINAL_AVAILABLE":
            return QUOTE_PRICE, {}
    return WRITE_DRAFT, _draft(case, transcript, answers)


def _draft(case: EmailCase, transcript: Transcript,
           answers: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:  # fmt: skip
    lang: Lang = "el" if case.labels.language == "el" else "en"
    results = {a.get("result") for _, a in answers}
    with_quotes = [a for _, a in answers if a.get("quotes")]
    cited = [s["policy_id"] for _, a in answers for s in a.get("sections", [])]
    offers: list[int] = []
    if with_quotes:
        latest = with_quotes[-1]
        offers = [q["quote_id"] for q in latest["quotes"]]
        intro = {"ALTERNATIVES": "alternatives_intro",
                 "WINDOW_OPTIONS": "window_intro"}.get(latest["result"])  # fmt: skip
        words = str(phrases(lang)[intro]) if intro else ""
        text = Template(template(f"stub_quote_{lang}.txt")).substitute(intro=words)
    elif "CLOSED" in results:
        closed = template(f"stub_decline_closed_{lang}.txt")
        text = closed.replace(str(phrases(lang)["open_season"]), OPEN_SEASON)
    elif "NONE_FOUND" in results:
        text = template(f"stub_decline_full_{lang}.txt")
    elif _route(transcript) == "policy":
        text = template(f"gold_policy_{lang}.txt")
    else:
        text = Template(template(f"stub_ask_info_{lang}.txt")).substitute(
            missing=_missing(lang, answers, transcript)
        )
        # No {{REQUEST_SUMMARY}}: code inserts it where guardrail 3 requires it (D80).
    return {"text": text, "offer_quote_ids": offers, "policy_ids": list(dict.fromkeys(cited))}


def _missing(lang: Lang, answers: list[tuple[str, dict[str, Any]]],
             transcript: Transcript) -> str:  # fmt: skip
    asked = next((a["missing"] for _, a in reversed(answers) if "missing" in a), None)
    if asked is None:
        first = transcript.items[0]
        assert isinstance(first, UserText)
        lines = [ln for ln in first.text.splitlines() if ln.startswith("Missing: ")]
        asked = lines[0].removeprefix("Missing: ") if lines else ""
    words = phrases(lang)
    missing = words["missing"]
    assert isinstance(missing, dict)
    return str(missing.get(asked, words["missing_fallback"]))


def _route(transcript: Transcript) -> str:
    first = transcript.items[0]
    assert isinstance(first, UserText)
    return first.text.split("\n", 1)[0].removeprefix("Route: ").split(" ", 1)[0]


def _answers(transcript: Transcript) -> list[tuple[str, dict[str, Any]]]:
    """Each tool output with the name of the call it answers."""
    names = {i.call_id: i.name for i in transcript.items if isinstance(i, ToolCall)}
    return [(names[i.call_id], json.loads(i.output))
            for i in transcript.items if isinstance(i, ToolOutput)]  # fmt: skip


def _arguments(tool: str) -> dict[str, Any]:
    return {"query": GOLD_QUERY} if tool == SEARCH_POLICIES else {}


def _escalation(code: str) -> dict[str, Any]:
    assert code in get_args(ModelEscalationCode)
    return {"reason_code": code, "note": "Gold label: the owner handles this email."}
