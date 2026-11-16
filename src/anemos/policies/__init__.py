"""The hotel's policies as chunks (D15): one chunk per `## ` heading of policies.md.

Each heading is followed by a line `<!-- policy_id: some-id -->`. The id is written
out, not derived from the heading, so a reworded title keeps its id: drafts cite
policies by id (guardrail 6) and eval labels name them. Pure: reads the packaged file,
no database. `embeddings` holds the vectors (and the build step that makes them),
`store` loads chunks and vectors into `policy_chunks`.
"""

import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

POLICY_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ID_LINE = re.compile(r"<!--\s*policy_id:\s*(\S+)\s*-->")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class PolicyChunk:
    policy_id: str
    title: str
    text: str  # the body, without the heading or the id line


class PolicyFormatError(ValueError):
    """policies.md does not follow the chunk format."""


def parse_policies(markdown: str) -> tuple[PolicyChunk, ...]:
    """Split on `## ` headings. Text before the first heading may only be a comment."""
    sections = re.split(r"^## ", markdown, flags=re.MULTILINE)
    preamble, bodies = sections[0], sections[1:]
    if _COMMENT.sub("", preamble).strip():
        raise PolicyFormatError("text before the first '## ' heading")
    chunks = [_chunk(body) for body in bodies]
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.policy_id in seen:
            raise PolicyFormatError(f"duplicate policy_id {chunk.policy_id!r}")
        seen.add(chunk.policy_id)
    return tuple(chunks)


def _chunk(body: str) -> PolicyChunk:
    title, _, rest = body.partition("\n")
    title = title.strip()
    lines = rest.strip().splitlines()
    match = _ID_LINE.fullmatch(lines[0].strip()) if lines else None
    if match is None:
        raise PolicyFormatError(f"{title!r}: the line after the heading must be the policy_id")
    policy_id = match.group(1)
    if not POLICY_ID.fullmatch(policy_id):
        raise PolicyFormatError(f"{title!r}: policy_id {policy_id!r} is not lowercase-hyphen")
    text = "\n".join(lines[1:]).strip()
    if not title or not text:
        raise PolicyFormatError(f"{policy_id!r}: empty title or text")
    if "<!--" in text:
        raise PolicyFormatError(f"{policy_id!r}: a comment inside the text")
    return PolicyChunk(policy_id, title, text)


@cache
def load_policies() -> tuple[PolicyChunk, ...]:
    """The packaged policies.md, parsed."""
    markdown = files("anemos.policies").joinpath("policies.md").read_text(encoding="utf-8")
    return parse_policies(markdown)


def policy(policy_id: str) -> PolicyChunk:
    for chunk in load_policies():
        if chunk.policy_id == policy_id:
            return chunk
    raise KeyError(policy_id)
