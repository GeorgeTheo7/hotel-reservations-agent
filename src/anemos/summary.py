"""The escalation summary (D81): one structured call, with no transaction open (D71).

Every escalation after extraction gets one, except INJECTION (the email is not sent to
a model again) and THREAD_REPLY (no extraction); an extraction that failed gets none
either (owner, week 6). The model writes 2-3 English sentences for the owner and may
**add** COMPLAINT or DISCOUNT; it never removes a code (D11). A failed or refused call
returns None: the email is escalated without a summary, never SYSTEM_ERROR because of
it (the adapter audits the failure).
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from anemos.extraction_run import delimited_email
from anemos.llm.adapter import LLM, Prompt
from anemos.prompts import CURRENT_SUMMARY, PromptSpec
from anemos.reservations.errors import ReasonCode
from anemos.summary_schema import EscalationSummary

log = logging.getLogger("anemos.summary")

STEP = "summary"
NO_SUMMARY = frozenset({ReasonCode.INJECTION, ReasonCode.THREAD_REPLY})


@dataclass(frozen=True)
class Summary:
    text: str
    added: tuple[ReasonCode, ...]  # codes the model added, none already present

    def json(self) -> dict[str, Any]:
        return {"text": self.text, "added": [c.value for c in self.added]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Summary":
        return cls(data["text"], tuple(ReasonCode(c) for c in data["added"]))


def wanted(codes: Sequence[ReasonCode]) -> bool:
    return not NO_SUMMARY & set(codes)


def summary_input(
    codes: Sequence[ReasonCode], detail: str, extraction: dict[str, Any] | None,
    tool_results: Sequence[str], subject: str, body: str,
) -> str:  # fmt: skip
    """What the model reads: code's facts first, then the guest's email, escaped and
    between the markers (untrusted)."""
    lines = [f"Reason codes: {', '.join(c.value for c in codes)}", f"Why: {detail}",
             f"Extracted: {json.dumps(extraction, ensure_ascii=False)}"]  # fmt: skip
    lines += [f"Tool result: {r}" for r in tool_results] or ["Tool results: none"]
    return "\n".join(lines) + "\n\n" + delimited_email(subject, body)


def summarise(
    llm: LLM, *, email_id: int, codes: Sequence[ReasonCode], detail: str,
    extraction: dict[str, Any] | None, tool_results: Sequence[str], subject: str, body: str,
    spec: PromptSpec = CURRENT_SUMMARY,
) -> Summary | None:  # fmt: skip
    prompt = Prompt(spec.text(), summary_input(codes, detail, extraction, tool_results,
                                               subject, body))  # fmt: skip
    settings = replace(llm.settings, max_output_tokens=spec.max_output_tokens,
                       reasoning_effort=spec.reasoning_effort)  # fmt: skip
    try:
        result = llm.structured(prompt, EscalationSummary, step=STEP,
                                prompt_version=spec.version, email_id=email_id,
                                settings=settings)  # fmt: skip
    except Exception:  # a failed or refused call: escalated without a summary
        log.exception("escalation summary for email %s failed", email_id)
        return None
    present = set(codes)
    added = tuple(ReasonCode(c) for c in dict.fromkeys(result.output.add_codes)
                  if ReasonCode(c) not in present)  # fmt: skip
    return Summary(result.output.summary.strip(), added)
