"""The `search_policies` query check (D15, spec Tools): pure, no model, no database.

The agent writes the query in English, at most 300 characters, with no `<` or `>` (the
selector's prompt puts it inside `<question>`, so an injected email must not be able to
make the agent close that block early). Anything else is an invalid call: it counts
toward the loop's 8 (D11), and its message tells the model how to fix the query.
"""

import re
from dataclasses import dataclass
from typing import Any

QUERY_MAX_CHARS = 300
FIX_HINT = f"write the query in English, up to {QUERY_MAX_CHARS} characters, without < or >"
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")  # Greek and Coptic, Greek Extended
_ANGLE = re.compile(r"[<>]")


@dataclass(frozen=True)
class InvalidQuery:
    query: str
    message: str

    def record(self) -> dict[str, Any]:
        return {"query": self.query, "invalid_query": self.message}


def check_query(query: str) -> str | InvalidQuery:
    """The query, stripped, or why the model must write it again."""
    text = query.strip()
    if not text:
        problem = "the query is blank"
    elif len(text) > QUERY_MAX_CHARS:
        problem = f"the query has {len(text)} characters"
    elif _GREEK.search(text):
        problem = "the query contains Greek letters"
    elif _ANGLE.search(text):
        problem = "the query contains < or >"
    else:
        return text
    return InvalidQuery(query, f"{problem}: {FIX_HINT}")
