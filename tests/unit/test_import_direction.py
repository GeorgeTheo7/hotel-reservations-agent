"""`anemos.reservations` is the core (spec D16: the tools become an MCP server in week 9):
it must not depend on the rest of the app. Every `anemos.*` import in it must be from
`anemos.reservations` itself, or one of the pairs listed here, which were already in
use when this test was written. A new pair fails the test: add it only on purpose.

Checked with `ast`, so imports inside functions and `if TYPE_CHECKING` count too.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "anemos" / "reservations"
INSIDE = "anemos.reservations"

# (module file, relative to reservations/; imported module)
ALLOWED = frozenset({
    ("availability.py", "anemos.clock"),
    ("tools.py", "anemos.clock"),
})  # fmt: skip


def outside_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: always inside the package
                continue
            names = [node.module or ""]
        else:
            continue
        found |= {
            name for name in names
            if (name == "anemos" or name.startswith("anemos."))
            and not (name == INSIDE or name.startswith(INSIDE + "."))
        }  # fmt: skip
    return found


def test_reservations_imports_nothing_else_from_the_app() -> None:
    used = {
        (path.relative_to(PACKAGE).as_posix(), name)
        for path in sorted(PACKAGE.rglob("*.py"))
        for name in outside_imports(path)
    }
    assert used - ALLOWED == set(), "new imports from outside anemos.reservations"


def test_the_allowed_imports_are_still_used() -> None:
    """A pair that is no longer used is removed from ALLOWED, so it cannot come back
    unnoticed."""
    used = {
        (path.relative_to(PACKAGE).as_posix(), name)
        for path in sorted(PACKAGE.rglob("*.py"))
        for name in outside_imports(path)
    }
    assert ALLOWED - used == set()


def test_the_check_sees_an_outside_import(tmp_path: Path) -> None:
    module = tmp_path / "m.py"
    module.write_text(
        "from anemos.reservations.model import Stay\n"
        "def f():\n    from anemos.extraction import OutDateSpec\n"
        "import anemos.db.seed\nfrom . import sibling\n",
        encoding="utf-8",
    )
    assert outside_imports(module) == {"anemos.extraction", "anemos.db.seed"}
