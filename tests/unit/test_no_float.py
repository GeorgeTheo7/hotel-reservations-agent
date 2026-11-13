"""No-float guard for `anemos.reservations` (D14: money is integer cents).

Parses every module of the package into an AST (Python's own parse tree) and fails on
code that produces or handles floats:

- float literals (`0.3`, `1e3`) and complex literals
- true division `/` and `/=` (always returns a float, even for two ints). This includes
  `/` for file paths: there is no exception. Code in this package joins paths with
  `Path.joinpath(...)` or `importlib.resources`, so the rule stays simple and
  cannot be fooled by an operand that only looks like a path.
- a negative power (`10 ** -2` is a float)
- the names `float` and `round`
- imports of `math`, `decimal`, `fractions`, `statistics`
- float format specs in f-strings (`f"{x:.2f}"` converts x to float first)

Not caught here: `%` / `str.format` strings, floats returned by other libraries.
Those are covered by tests asserting `type(amount) is int` on results.
Floor division `//` and `divmod` are allowed: they stay integers.
"""

import ast
from pathlib import Path

import pytest

import anemos.reservations

pytestmark = pytest.mark.unit

PACKAGE_DIR = Path(anemos.reservations.__file__).parent
BANNED_NAMES = {"float", "round"}
BANNED_MODULES = {"math", "decimal", "fractions", "statistics"}
FLOAT_FORMAT_TYPES = set("eEfFgG%")


def float_risks(source: str) -> list[str]:
    """Return 'line N: reason' for each float risk in `source`."""
    found: list[str] = []

    def flag(node: ast.AST, reason: str) -> None:
        found.append(f"line {getattr(node, 'lineno', '?')}: {reason}")

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and type(node.value) in (float, complex):
            flag(node, f"float literal {node.value!r}")
        elif isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ast.Div):
            flag(node, "true division '/' (use '//', or joinpath() for paths)")
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Pow)
            and isinstance(node.right, ast.UnaryOp)
            and isinstance(node.right.op, ast.USub)
        ):
            flag(node, "negative power")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            flag(node, f"use of {node.id!r}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_MODULES:
                    flag(node, f"import of {alias.name!r}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in BANNED_MODULES:
                flag(node, f"import from {node.module!r}")
        elif isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            spec = "".join(
                part.value
                for part in ast.walk(node.format_spec)
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if spec and spec[-1] in FLOAT_FORMAT_TYPES:
                flag(node, f"float format spec {spec!r}")
    return found


def package_modules() -> list[Path]:
    return sorted(PACKAGE_DIR.rglob("*.py"))


def test_guard_scans_the_money_module() -> None:
    # Protects against a guard that passes because it scans nothing.
    assert PACKAGE_DIR / "money.py" in package_modules()


@pytest.mark.parametrize("module", package_modules(), ids=lambda p: p.name)
def test_reservations_module_has_no_float_risks(module: Path) -> None:
    assert float_risks(module.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "x = 0.3",
        "x = 1e3",
        "x = a / b",
        "x /= 2",
        "path = here / 'templates'",
        "x = 10 ** -2",
        "x = float(a)",
        "x = round(a)",
        "def f() -> float: ...",
        "import math",
        "from decimal import Decimal",
        "s = f'{x:.2f}'",
        "s = f'{x:{width}.2f}'",
        "s = f'{x:%}'",
    ],
)
def test_guard_flags(snippet: str) -> None:
    assert float_risks(snippet), f"guard missed: {snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "x = a // b",
        "x = -(-a * 30 // 10_000)",
        "q, r = divmod(a, 100)",
        "x = 10 ** 2",
        "path = here.joinpath('templates', 'quote_en.txt')",
        "s = f'{x:,}'",
        "s = f'{x:02d}'",
        "s = f'{x}'",
        "x = int('3')",
    ],
)
def test_guard_allows(snippet: str) -> None:
    assert float_risks(snippet) == []
