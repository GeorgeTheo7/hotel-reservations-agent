"""The UI's status lists (web/src/types.ts) are the API's (anemos.states): a status
added on one side only would show as an unknown value or never be offered as a filter.
The fixtures check the shapes (D84); this checks the full lists, including statuses no
fixture happens to contain."""

import re
from enum import StrEnum
from pathlib import Path

import pytest

from anemos.states import DraftStatus, EmailStatus

pytestmark = pytest.mark.unit

TYPES = Path(__file__).resolve().parents[2] / "web" / "src" / "types.ts"


def ts_list(name: str) -> list[str]:
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", TYPES.read_text("utf-8"), re.S)
    assert match is not None, f"{name} not found in types.ts"
    return re.findall(r'"([a-z_]+)"', match.group(1))


@pytest.mark.parametrize(
    ("name", "enum"), [("EMAIL_STATUSES", EmailStatus), ("DRAFT_STATUSES", DraftStatus)]
)
def test_the_ui_knows_exactly_the_api_s_statuses(name: str, enum: type[StrEnum]) -> None:
    assert ts_list(name) == [status.value for status in enum]
