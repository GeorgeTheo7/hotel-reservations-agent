"""Guardrails: code checks on every draft before the owner sees it (spec Guardrails).

Guardrails 1 and 7 since week 3 (D53), 6 since week 5 (D74), 2-5 since week 6. Each
check returns True or a short reason, never raises, so all results can be stored with
the draft version (draft_versions.guardrail_results) and a failure is visible, not
hidden. Guardrails 2, 4 and 5 read the **free text**: the draft without the
placeholders, i.e. only what the model wrote (`free_text`).
"""

import json
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from functools import cache
from importlib.resources import files

QUOTE_BLOCK = "{{QUOTE_BLOCK}}"
REQUEST_SUMMARY = "{{REQUEST_SUMMARY}}"
OPEN_SEASON = "{{OPEN_SEASON}}"
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
PRICE_CHECK = "price_check"  # guardrail result key of an owner edit (D63)


@dataclass(frozen=True)
class ProposedOffer:
    quote_id: int
    hold: bool


def quote_block_once(text: str, route: str) -> bool | str:
    """Guardrail 1: on the quote route, {{QUOTE_BLOCK}} exactly once (prices come only
    from the code-rendered block, D8); on other routes, not at all."""
    found = text.count(QUOTE_BLOCK)
    expected = 1 if route == "quote" else 0
    if found == expected:
        return True
    return f"{QUOTE_BLOCK} appears {found} time(s) on the {route} route, expected {expected}"


def holds_valid(
    offers: Sequence[ProposedOffer], run_quote_ids: Collection[int], hold_allowed: dict[int, bool]
) -> bool | str:
    """Guardrail 7: proposed holds come from this run's quotes and satisfy D9: the held
    quote matches the request exactly (`hold_allowed`) and is the draft's only offer.

    The database enforces the first two again (draft_offers foreign keys and CHECK);
    "only offer" needs a count across rows, so this check is its only guard.
    """
    unknown = [o.quote_id for o in offers if o.quote_id not in run_quote_ids]
    if unknown:
        return f"offers quotes not priced in this run: {unknown}"
    held = [o for o in offers if o.hold]
    if not held:
        return True
    if len(offers) != 1:
        return f"a hold with {len(offers)} offers; D9 allows a hold only on a single offer"
    if not hold_allowed.get(held[0].quote_id, False):
        return f"quote {held[0].quote_id} differs from the request; it cannot be held (D9)"
    return True


def rendered_blocks_unchanged(text: str, blocks: Sequence[str]) -> bool | str:
    """Price check on an owner edit (D63, D10): every quote block of the agent's
    version must still appear verbatim. A changed amount, a reworded line or a deleted
    block is the edited-price warning, which approval refuses unless confirm_warnings.
    """
    missing = missing_blocks(text, blocks)
    if not missing:
        return True
    which = ", ".join(str(i + 1) for i in missing)
    return f"edited-price warning: quote block {which} of {len(blocks)} changed or removed"


def missing_blocks(text: str, blocks: Sequence[str]) -> list[int]:
    """Positions (0-based) of the blocks that no longer appear verbatim in `text`. The
    one definition of "changed or removed" for the price check (D63) and approval."""
    return [i for i, block in enumerate(blocks) if block not in text]


def free_text(text: str) -> str:
    """The draft without its placeholders: only what the model wrote."""
    return PLACEHOLDER.sub(" ", text)


# --- guardrail 2: numbers (D78) ---------------------------------------------------------------

# A time (08:00, 8.30 is not a time: only a colon), or a number with optional "." or ","
# groups (25, 25,00, 1.500, €25.00), with an optional % after it.
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def number_values(text: str) -> set[str]:
    """Every number in `text` as a normalised value: a time as HH:MM ("8:00" ->
    "08:00"); any other number as its decimal value, so "25", "25,00", "25.00" and
    "€25.00" are all "25", and "1.500" / "1,500" (thousands) are "1500". A separator
    followed by exactly three digits groups thousands; by one or two, it is the
    decimal point. Currency signs and % are not part of the value."""
    values = {f"{int(h):02d}:{m}" for h, m in _TIME.findall(text)}
    for token in _NUMBER.findall(_TIME.sub(" ", text)):
        values.add(_value(token))
    return values


def _value(token: str) -> str:
    parts = re.split(r"[.,]", token)
    if len(parts) > 1 and len(parts[-1]) in (1, 2):
        whole, fraction = "".join(parts[:-1]), parts[-1]
    else:
        whole, fraction = "".join(parts), "0"
    value = Decimal(f"{whole}.{fraction}").normalize()
    return format(value, "f")


def numbers_from_policies(text: str, policy_texts: Iterable[str]) -> bool | str:
    """Guardrail 2 (narrow, D78): every number in the free text has the same value as a
    number in a policy chunk returned by search_policies in this run. Number words are
    not checked (measured in evaluation)."""
    allowed: set[str] = set()
    for chunk in policy_texts:
        allowed |= number_values(chunk)
    stray = sorted(number_values(free_text(text)) - allowed)
    if not stray:
        return True
    return f"numbers outside the code-rendered blocks and the returned policies: {stray}"


# --- guardrail 3: the request summary (D80) ---------------------------------------------------


def summary_present(text: str, route: str, *, relative_or_holiday: bool) -> bool | str:
    """Guardrail 3: {{REQUEST_SUMMARY}} on quote and decline, and on ask_info when the
    request used a relative or holiday date (the guest must see the resolved dates)."""
    required = route in ("quote", "decline") or (route == "ask_info" and relative_or_holiday)
    if not required or REQUEST_SUMMARY in text:
        return True
    why = "a relative or holiday date" if route == "ask_info" else f"the {route} route"
    return f"{REQUEST_SUMMARY} missing: required for {why}"


# --- guardrail 4: the reply's script (D79) ----------------------------------------------------


def _is_greek(char: str) -> bool:
    """Greek and Coptic (U+0370-U+03FF) or Greek Extended (U+1F00-U+1FFF)."""
    return "Ͱ" <= char <= "Ͽ" or "ἀ" <= char <= "῿"


def reply_script(text: str, language: str) -> bool | str:
    """Guardrail 4: an EN email gets no Greek letter; an EL email (Greek or Greeklish)
    gets a reply whose letters are at least 2/3 Greek (names such as Anemos and Wi-Fi
    stay Latin). Free text with no letters passes."""
    letters = [c for c in free_text(text) if c.isalpha()]
    greek = sum(1 for c in letters if _is_greek(c))
    if language == "el":
        if not letters or 3 * greek >= 2 * len(letters):
            return True
        return f"Greek reply with {greek} of {len(letters)} letters Greek (needs 2/3)"
    if greek == 0:
        return True
    return f"English reply with {greek} Greek letter(s)"


# --- guardrail 5: forbidden phrases -----------------------------------------------------------


def normalised(text: str) -> str:
    """Casefolded, accents removed (Greek tonos and dialytika), spaces collapsed."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(bare.split())


@cache
def forbidden_phrases() -> tuple[str, ...]:
    """Both languages' phrases (templates/forbidden_phrases.json), normalised."""
    raw = files("anemos").joinpath("templates", "forbidden_phrases.json").read_text("utf-8")
    data = json.loads(raw)
    return tuple(normalised(p) for lang in ("en", "el") for p in data[lang])


def no_forbidden_phrases(text: str, policy_texts: Iterable[str]) -> bool | str:
    """Guardrail 5: none of the forbidden phrases in the free text, unless the phrase is
    policy wording (it appears in a chunk returned in this run). Honest negatives are
    blocked too ("not guaranteed until the deposit"): measured as false alarms."""
    body = normalised(free_text(text))
    policy = [normalised(t) for t in policy_texts]
    found = [p for p in forbidden_phrases() if p in body and not any(p in t for t in policy)]
    if not found:
        return True
    return f"forbidden phrases: {found}"


# --- guardrails 2-5 on an owner edit (D77) ------------------------------------------------------

_WORD = re.compile(r"\S+")
RUN_SEPARATOR = " | "  # between runs of changed words: no phrase spans two edits


@dataclass(frozen=True)
class EditChanges:
    """The words an owner inserted or replaced, against version 1, in runs."""

    free: tuple[str, ...]  # outside the agent's rendered quote blocks
    in_blocks: tuple[str, ...]  # inside one: a changed price is the price check's (D63)

    def all(self) -> tuple[str, ...]:
        return self.free + self.in_blocks


def edit_changes(original: str, edited: str, blocks: Sequence[str] = ()) -> EditChanges:
    """A word diff (whitespace-separated tokens) of `edited` against `original`: each
    inserted or replaced run, marked by whether it falls inside one of the original's
    rendered quote blocks. Deleted words are not text the owner wrote."""
    spans = [(m.start(), m.end()) for m in _WORD.finditer(original)]
    old = [original[a:b] for a, b in spans]
    new = _WORD.findall(edited)
    protected: set[int] = set()
    for block in blocks:
        start = original.find(block)
        if start < 0:
            continue
        end = start + len(block)
        protected |= {i for i, (a, b) in enumerate(spans) if a >= start and b <= end}
    free: list[str] = []
    in_blocks: list[str] = []
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("replace", "insert") or j1 == j2:
            continue
        run = " ".join(new[j1:j2])
        touched = set(range(i1, i2)) if tag == "replace" else {i1 - 1, i1}
        inside = bool(touched) and touched <= protected
        (in_blocks if inside else free).append(run)
    return EditChanges(tuple(free), tuple(in_blocks))


def summary_kept(original_line: str | None, edited: str) -> bool | str:
    """Guardrail 3 on an edit: version 1's summary line, if it had one, is still there
    word for word (a changed-words check cannot see a deletion)."""
    if original_line is None or original_line in edited:
        return True
    return f"the request summary line was changed or removed: {original_line!r}"


def edit_checks(
    original: str, edited: str, *, blocks: Sequence[str], summary: str | None,
    policy_texts: Sequence[str], language: str,
) -> dict[str, bool | str]:  # fmt: skip
    """Guardrails 2-5 on an owner edit (D77), each a warning the owner confirms at
    approval, never a block. G2: a changed number outside the quote blocks must be a
    number of a returned policy or of version 1. G4 and G5 read every changed run."""
    changes = edit_changes(original, edited, blocks)
    free = RUN_SEPARATOR.join(changes.free)
    every = RUN_SEPARATOR.join(changes.all())
    return {
        "g2_numbers_from_policies": numbers_from_policies(free, [*policy_texts, original]),
        "g3_summary_present": summary_kept(summary, edited),
        "g4_reply_script": reply_script(every, language),
        "g5_no_forbidden_phrases": no_forbidden_phrases(every, policy_texts),
    }


def all_passed(results: dict[str, bool | str]) -> bool:
    return all(result is True for result in results.values())
