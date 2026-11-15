"""The spend cap (D2): no model call starts unless the budget can pay its maximum cost.

The ledger is an append-only file of JSON lines. It is a file, not a database table,
because the test, dev and eval databases come and go, while the cap must count every
call made from this machine. Where it lives never depends on the current directory
(a run from another folder must not start an empty ledger): `ANEMOS_LLM_LEDGER` if set
(an absolute path), else `<repo root>/var/llm_spend.jsonl` (git-ignored).

Protocol, per call:
1. `reserve(model, max_cost)`: under an exclusive file lock, sum what is spent, refuse
   if `spent + max_cost` would pass the cap, else append a `reserve` line. The check
   and the append happen under one lock, so two processes cannot both take the last
   of the budget.
2. The call runs (no lock held).
3. `settle(reservation, actual)`: append a `settle` line with the real cost from the
   response's token usage (0 for a call that failed before using tokens).

Spent = every settled or released amount + the full amount of every reservation still
open. A reservation never settled (a crash mid-call) keeps counting at its maximum, so
the cap errs on the safe side, until someone releases it by hand:
`python -m anemos.llm.budget list`, then `... release ID [--cost-usd X]`, which appends a
`release` line (the cost the dashboard shows for that call, default 0). Nothing is ever
deleted. A line that cannot be read stops everything: guessing "zero" could let
spending through.

Set a hard usage limit in the OpenAI dashboard as well: this cap guards this machine,
the dashboard guards the account.
"""

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO

from anemos.clock import Clock, SystemClock

DEFAULT_CAP_USD = "2.00"
LEDGER_IN_REPO = Path("var") / "llm_spend.jsonl"
MICRO_PER_USD = 1_000_000
CLOSING = ("settle", "release")  # line types that close a reservation


class BudgetExceeded(Exception):
    """The call could cost more than what is left; it was not started."""


class LedgerCorrupt(Exception):
    """A ledger line cannot be read; spending stops until someone looks at the file."""


@dataclass(frozen=True)
class Reservation:
    id: str
    model: str
    micro_usd: int  # the maximum this call may cost
    at: str = ""  # when it was reserved (ISO time, from the ledger)


def cap_from_env() -> int:
    """ANEMOS_LLM_BUDGET_USD (default 2.00), in micro-dollars."""
    raw = os.environ.get("ANEMOS_LLM_BUDGET_USD", DEFAULT_CAP_USD).strip()
    usd = _usd(raw, "ANEMOS_LLM_BUDGET_USD")
    return int(usd * MICRO_PER_USD)


def _usd(raw: str, name: str) -> Decimal:
    try:
        usd = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not usd.is_finite() or usd < 0:
        raise ValueError(f"{name} must be 0 or more, got {raw!r}")
    return usd


def repo_root(start: Path | None = None) -> Path | None:
    """The folder holding this project's pyproject.toml, found from the package's own
    location (not the current directory); None for an installed copy."""
    here = (start or Path(__file__)).resolve()
    for folder in here.parents:
        pyproject = folder / "pyproject.toml"
        if pyproject.is_file() and 'name = "anemos"' in pyproject.read_text(encoding="utf-8"):
            return folder
    return None


def ledger_path() -> Path:
    """ANEMOS_LLM_LEDGER (absolute), else <repo root>/var/llm_spend.jsonl."""
    configured = os.environ.get("ANEMOS_LLM_LEDGER", "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError(
                f"ANEMOS_LLM_LEDGER must be an absolute path, got {configured!r}: a relative "
                "one would depend on the current directory"
            )
        return path
    root = repo_root()
    if root is None:
        raise RuntimeError("no repository found for the spend ledger; set ANEMOS_LLM_LEDGER")
    return root / LEDGER_IN_REPO


class Budget:
    def __init__(
        self,
        clock: Clock,
        cap_micro_usd: int,
        ledger: Path | None = None,
        *,
        _pause_after_check_s: float = 0.0,  # tests only: widen the check-to-append gap
    ) -> None:
        self.clock = clock
        self.cap = cap_micro_usd
        self.ledger = ledger if ledger is not None else ledger_path()
        self._lock_path = self.ledger.with_name(self.ledger.name + ".lock")
        self._pause = _pause_after_check_s

    def spent(self) -> int:
        with self._locked():
            return self._spent()

    def open_reservations(self) -> list[Reservation]:
        """Reservations with no settle or release line: calls still running, or crashed."""
        with self._locked():
            return self._open()

    def reserve(self, model: str, max_micro_usd: int) -> Reservation:
        if max_micro_usd < 0:
            raise ValueError("a maximum cost cannot be negative")
        with self._locked():
            spent = self._spent()
            if spent + max_micro_usd > self.cap:
                raise BudgetExceeded(
                    f"{model}: may cost {max_micro_usd} µ$, {self.cap - spent} µ$ left of "
                    f"{self.cap} µ$ (ANEMOS_LLM_BUDGET_USD)"
                )
            if self._pause:
                time.sleep(self._pause)
            reservation = Reservation(uuid.uuid4().hex, model, max_micro_usd)
            self._append({"type": "reserve", "id": reservation.id, "model": model,
                          "micro_usd": max_micro_usd})  # fmt: skip
            return reservation

    def settle(self, reservation: Reservation, actual_micro_usd: int) -> None:
        """Record what the call really cost. More than reserved is recorded as it is
        (the money is spent either way); the token bound should prevent it."""
        self._close("settle", reservation.id, actual_micro_usd)

    def release(self, reservation_id: str, cost_micro_usd: int = 0) -> None:
        """Close a stuck reservation by hand (a crashed call), at the cost the OpenAI
        dashboard shows for it (default 0). Appends a line; deletes nothing."""
        self._close("release", reservation_id, cost_micro_usd)

    # --- ledger -----------------------------------------------------------------------

    def _close(self, kind: str, reservation_id: str, micro_usd: int) -> None:
        if micro_usd < 0:
            raise ValueError("a cost cannot be negative")
        with self._locked():
            reserved, closed = self._ids()
            if reservation_id in closed:
                raise ValueError(f"reservation {reservation_id} is already closed")
            if reservation_id not in reserved:
                raise ValueError(f"reservation {reservation_id} is not in the ledger")
            model = next(r.model for r in self._reservations() if r.id == reservation_id)
            self._append({"type": kind, "id": reservation_id, "model": model,
                          "micro_usd": micro_usd})  # fmt: skip

    def _lines(self) -> Iterator[dict[str, object]]:
        if not self.ledger.exists():
            return
        with self.ledger.open(encoding="utf-8") as f:
            for n, raw in enumerate(f, 1):
                try:
                    line = json.loads(raw)
                    if (
                        line["type"] not in ("reserve", *CLOSING)
                        or not isinstance(line["id"], str)
                        or type(line["micro_usd"]) is not int
                        or line["micro_usd"] < 0
                    ):
                        raise ValueError(line)
                except (ValueError, KeyError, TypeError) as exc:
                    raise LedgerCorrupt(f"{self.ledger}:{n}: unreadable line {raw!r}") from exc
                yield line

    def _reservations(self) -> Iterator[Reservation]:
        for line in self._lines():
            if line["type"] == "reserve":
                amount = line["micro_usd"]
                assert isinstance(amount, int)
                yield Reservation(str(line["id"]), str(line.get("model", "")), amount,
                                  str(line.get("at", "")))  # fmt: skip

    def _ids(self) -> tuple[set[str], set[str]]:
        reserved: set[str] = set()
        closed: set[str] = set()
        for line in self._lines():
            (reserved if line["type"] == "reserve" else closed).add(str(line["id"]))
        return reserved, closed

    def _open(self) -> list[Reservation]:
        _, closed = self._ids()
        return [r for r in self._reservations() if r.id not in closed]

    def _spent(self) -> int:
        reserved: dict[str, int] = {}
        closed: dict[str, int] = {}
        for line in self._lines():
            amount = line["micro_usd"]
            assert isinstance(amount, int)
            (reserved if line["type"] == "reserve" else closed)[str(line["id"])] = amount
        open_reservations = sum(v for k, v in reserved.items() if k not in closed)
        return sum(closed.values()) + open_reservations

    def _append(self, line: dict[str, object]) -> None:
        line = {"at": self.clock.now().isoformat()} | line
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """An exclusive lock on a separate lock file, held for the whole block; other
        processes wait. (fcntl on Linux/macOS, msvcrt on Windows: no dependency.)"""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as f:
            _lock(f)
            try:
                yield
            finally:
                _unlock(f)


if sys.platform == "win32":
    import msvcrt

    def _lock(f: IO[bytes]) -> None:
        f.seek(0)
        while True:  # LK_LOCK gives up after ~10 s; keep waiting
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue

    def _unlock(f: IO[bytes]) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(f: IO[bytes]) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock(f: IO[bytes]) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --- command line: list and release stuck reservations ---------------------------------


def main(argv: list[str] | None = None, *, budget: Budget | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m anemos.llm.budget",
        description="Show the model spend, list open reservations, release a stuck one.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="spend so far and open reservations")
    release = commands.add_parser("release", help="close a stuck reservation (appends a line)")
    release.add_argument("id")
    release.add_argument("--cost-usd", default="0", help="the call's cost per the dashboard")
    args = parser.parse_args(argv)
    b = budget or Budget(SystemClock(), cap_from_env())
    if args.command == "release":
        cost = int(_usd(args.cost_usd, "--cost-usd") * MICRO_PER_USD)
        b.release(args.id, cost)
        print(f"released {args.id} at {cost} µ$")
    print(f"ledger: {b.ledger}")
    print(f"spent: {b.spent()} µ$ of {b.cap} µ$")
    for r in b.open_reservations():
        print(f"open: {r.id}  {r.at}  {r.model}  up to {r.micro_usd} µ$")


if __name__ == "__main__":
    main()
