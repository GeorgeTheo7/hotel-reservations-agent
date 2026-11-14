"""states.md: only the approval transaction creates bookings and outbox rows.

A source scan, no database: any `INSERT INTO bookings` / `INSERT INTO outbox` outside
anemos/approval.py fails here. (Test fixtures in tests/ may insert rows directly.)
"""

import re
from importlib.resources import files
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(str(files("anemos")))
INSERT = re.compile(r"INSERT\s+INTO\s+(bookings|outbox)\b", re.IGNORECASE)


def writers() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for match in INSERT.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1).lower(), set()).add(path.relative_to(SRC).as_posix())
    return found


def test_only_approval_inserts_bookings_and_outbox_rows() -> None:
    assert writers() == {"bookings": {"approval.py"}, "outbox": {"approval.py"}}
