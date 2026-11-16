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


# --- the retrieval eval's ranking code stays out of production (D15) ---------------------

SRC = PACKAGE.parent  # src/anemos
RANKING = "anemos.policies.ranking"
# Eval-side modules: they may reach the ranking code. Everything else under src/anemos is
# production (the tools, the run, the agent) and must not, directly or through another
# module.
EVAL_ONLY = frozenset({RANKING, "anemos.retrieval_scoring", "anemos.retrieval_eval"})


def module_name(path: Path) -> str:
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def anemos_imports(path: Path) -> set[str]:
    """Every anemos module the file imports; `from anemos.x import y` also counts y when
    y is a module (anemos.x.y)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] == "anemos"}
        elif (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and node.module
            and node.module.split(".")[0] == "anemos"
        ):
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def import_graph() -> dict[str, set[str]]:
    modules = {module_name(p): p for p in SRC.rglob("*.py")}
    return {name: {m for m in anemos_imports(path) if m in modules}
            for name, path in modules.items()}  # fmt: skip


def reaches(graph: dict[str, set[str]], start: str, target: str) -> bool:
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name == target:
            return True
        if name not in seen:
            seen.add(name)
            todo.extend(graph.get(name, ()))
            # importing anemos.a.b runs anemos.a's __init__ first
            todo.extend(".".join(name.split(".")[:i]) for i in range(2, name.count(".") + 1))
    return False


def test_production_code_never_reaches_the_ranking_code() -> None:
    graph = import_graph()
    production = sorted(set(graph) - EVAL_ONLY)
    assert "anemos.policies.search" in production and "anemos.prompts" in production
    leaking = [name for name in production if reaches(graph, name, RANKING)]
    assert leaking == [], f"production modules that import {RANKING}: {leaking}"


def test_the_ranking_check_sees_an_indirect_import() -> None:
    graph = {"anemos.a": {"anemos.b"}, "anemos.b": {RANKING}, RANKING: set()}
    assert reaches(graph, "anemos.a", RANKING)
    assert not reaches({"anemos.a": set()}, "anemos.a", RANKING)
