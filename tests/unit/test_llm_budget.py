"""The spend cap (D2): reserve before a call, settle after, under a file lock."""

import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anemos.clock import FixedClock
from anemos.llm.budget import (
    Budget,
    BudgetExceeded,
    LedgerCorrupt,
    cap_from_env,
    ledger_path,
    main,
    repo_root,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def budget(tmp_path: Path, cap: int = 1_000) -> Budget:
    return Budget(FixedClock(NOW), cap, tmp_path / "llm_spend.jsonl")


def test_an_empty_ledger_has_spent_nothing(tmp_path: Path) -> None:
    assert budget(tmp_path).spent() == 0


def test_a_settled_call_counts_its_actual_cost(tmp_path: Path) -> None:
    b = budget(tmp_path)
    reservation = b.reserve("gpt-6-luna", 600)
    assert b.spent() == 600  # reserved: counted at its maximum
    b.settle(reservation, 75)
    assert b.spent() == 75


def test_an_unsettled_reservation_keeps_counting_at_its_maximum(tmp_path: Path) -> None:
    """A crash mid-call leaves a reservation open: it still blocks its maximum."""
    budget(tmp_path).reserve("gpt-6-luna", 400)
    assert budget(tmp_path).spent() == 400  # a new process reading the same file


def test_a_call_that_would_pass_the_cap_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    b = budget(tmp_path)
    b.settle(b.reserve("gpt-6-luna", 700), 700)
    before = b.ledger.read_text(encoding="utf-8")
    with pytest.raises(BudgetExceeded, match="300 µ\\$ left"):
        b.reserve("gpt-6-luna", 301)
    assert b.ledger.read_text(encoding="utf-8") == before


def test_a_call_that_ends_exactly_at_the_cap_is_allowed(tmp_path: Path) -> None:
    b = budget(tmp_path)
    b.settle(b.reserve("gpt-6-luna", 700), 700)
    b.reserve("gpt-6-luna", 300)
    assert b.spent() == 1_000


def test_a_cost_above_the_reservation_is_recorded_as_it_is(tmp_path: Path) -> None:
    b = budget(tmp_path)
    b.settle(b.reserve("gpt-6-luna", 100), 150)
    assert b.spent() == 150


def test_settling_twice_or_an_unknown_reservation_is_refused(tmp_path: Path) -> None:
    b = budget(tmp_path)
    reservation = b.reserve("gpt-6-luna", 100)
    b.settle(reservation, 10)
    with pytest.raises(ValueError, match="already closed"):
        b.settle(reservation, 10)
    other = budget(tmp_path / "other").reserve("gpt-6-luna", 1)
    with pytest.raises(ValueError, match="not in the ledger"):
        b.settle(other, 1)


def test_ledger_lines_carry_the_clock_time(tmp_path: Path) -> None:
    b = budget(tmp_path)
    b.reserve("gpt-6-luna", 5)
    assert b.ledger.read_text(encoding="utf-8").startswith('{"at": "2026-11-16T09:00:00+00:00"')


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"type": "reserve", "id": "a"}',
        '{"type": "refund", "id": "a", "micro_usd": 5}',
        '{"type": "settle", "id": "a", "micro_usd": -5}',
        '{"type": "settle", "id": "a", "micro_usd": 1.5}',
        '{"type": "reserve", "id": "a", "micro_usd": 5',
    ],
    ids=["garbage", "no-amount", "unknown-type", "negative", "float", "cut-off-write"],
)
def test_an_unreadable_line_stops_spending(tmp_path: Path, line: str) -> None:
    """Guessing zero for a line it cannot read could let spending through."""
    b = budget(tmp_path)
    b.ledger.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(LedgerCorrupt, match=":1: unreadable line"):
        b.reserve("gpt-6-luna", 1)
    with pytest.raises(LedgerCorrupt):
        b.spent()


@pytest.mark.parametrize(
    ("raw", "micro"), [(None, 2_000_000), ("0.5", 500_000), ("0", 0), (" 3 ", 3_000_000)]
)
def test_cap_from_env(monkeypatch: pytest.MonkeyPatch, raw: str | None, micro: int) -> None:
    if raw is None:
        monkeypatch.delenv("ANEMOS_LLM_BUDGET_USD", raising=False)
    else:
        monkeypatch.setenv("ANEMOS_LLM_BUDGET_USD", raw)
    assert cap_from_env() == micro


@pytest.mark.parametrize("raw", ["two", "-1", "NaN", "Infinity"])
def test_a_bad_cap_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("ANEMOS_LLM_BUDGET_USD", raw)
    with pytest.raises(ValueError, match="ANEMOS_LLM_BUDGET_USD"):
        cap_from_env()


# --- two processes racing for the last of the budget ---------------------------------

RACER = textwrap.dedent(
    """
    import sys, time
    from datetime import UTC, datetime
    from pathlib import Path
    from anemos.clock import FixedClock
    from anemos.llm.budget import Budget, BudgetExceeded

    ledger, go, ready = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    b = Budget(FixedClock(datetime(2026, 11, 16, tzinfo=UTC)), 1_000, ledger,
               _pause_after_check_s=0.2)
    ready.touch()  # imported and set up: only the reserve call is left
    while not go.exists():
        time.sleep(0.002)
    try:
        b.reserve("gpt-6-luna", 600)
        print("reserved")
    except BudgetExceeded:
        print("refused")
    """
)


@pytest.mark.slow  # ~3 s on Windows (process start-up); CI runs it
def test_processes_racing_for_the_last_of_the_budget_get_it_once(tmp_path: Path) -> None:
    """Four processes, each wanting 600 µ$ of a 1,000 µ$ cap, released together once
    all are ready. The check-to-append gap is widened to 0.2 s, so without the lock
    several would read "0 spent" and all reserve; with it, exactly one gets the money."""
    ledger, go = tmp_path / "llm_spend.jsonl", tmp_path / "go"
    ready = [tmp_path / f"ready-{i}" for i in range(4)]
    racers = [
        subprocess.Popen(
            [sys.executable, "-c", RACER, str(ledger), str(go), str(flag)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for flag in ready
    ]
    deadline = time.monotonic() + 30
    while not all(flag.exists() for flag in ready):
        assert time.monotonic() < deadline, "a racer never got ready"
        time.sleep(0.005)
    go.touch()
    outcomes = sorted(racer.communicate(timeout=60)[0].strip() for racer in racers)
    assert outcomes == ["refused", "refused", "refused", "reserved"]
    assert Budget(FixedClock(NOW), 1_000, ledger).spent() == 600


# --- where the ledger lives ----------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]


def test_the_ledger_does_not_depend_on_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run from another folder must use the same ledger, not start an empty one."""
    monkeypatch.delenv("ANEMOS_LLM_LEDGER", raising=False)
    paths = []
    for folder in (tmp_path / "a", tmp_path / "b" / "deeper"):
        folder.mkdir(parents=True)
        monkeypatch.chdir(folder)
        paths.append(ledger_path())
    assert paths == [REPO / "var" / "llm_spend.jsonl"] * 2


def test_another_process_in_another_folder_finds_the_same_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANEMOS_LLM_LEDGER", raising=False)
    out = subprocess.run(
        [sys.executable, "-c", "from anemos.llm.budget import ledger_path; print(ledger_path())"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert Path(out.stdout.strip()) == REPO / "var" / "llm_spend.jsonl"


def test_an_explicit_ledger_must_be_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANEMOS_LLM_LEDGER", str(tmp_path / "spend.jsonl"))
    assert ledger_path() == tmp_path / "spend.jsonl"
    monkeypatch.setenv("ANEMOS_LLM_LEDGER", "var/spend.jsonl")
    with pytest.raises(ValueError, match="absolute"):
        ledger_path()


def test_an_installed_copy_has_no_repo_root(tmp_path: Path) -> None:
    """Outside the repository there is nothing to anchor to: ledger_path() then asks
    for ANEMOS_LLM_LEDGER instead of guessing."""
    assert repo_root(tmp_path / "site-packages" / "anemos" / "llm" / "budget.py") is None
    assert repo_root() == REPO


# --- stuck reservations: list and release by hand -----------------------------------


def test_a_stuck_reservation_is_listed_and_released_by_a_new_line(tmp_path: Path) -> None:
    b = budget(tmp_path)
    stuck = b.reserve("gpt-6-luna", 400)  # the process crashed here
    done = b.reserve("gpt-6-luna", 100)
    b.settle(done, 20)
    assert [r.id for r in b.open_reservations()] == [stuck.id]
    assert b.spent() == 420
    before = b.ledger.read_text(encoding="utf-8")

    b.release(stuck.id)
    after = b.ledger.read_text(encoding="utf-8")
    assert after.startswith(before)  # append-only: nothing removed or rewritten
    assert after[len(before) :].count("\n") == 1
    assert '"type": "release"' in after[len(before) :]
    assert (b.open_reservations(), b.spent()) == ([], 20)


def test_a_release_can_record_the_dashboard_cost(tmp_path: Path) -> None:
    b = budget(tmp_path)
    b.release(b.reserve("gpt-6-luna", 400).id, 35)
    assert b.spent() == 35


def test_closing_a_reservation_twice_is_refused(tmp_path: Path) -> None:
    b = budget(tmp_path)
    settled = b.reserve("gpt-6-luna", 10)
    b.settle(settled, 5)
    with pytest.raises(ValueError, match="already closed"):
        b.release(settled.id)
    released = b.reserve("gpt-6-luna", 10)
    b.release(released.id)
    with pytest.raises(ValueError, match="already closed"):
        b.settle(released, 1)
    with pytest.raises(ValueError, match="not in the ledger"):
        b.release("no-such-id")


def test_the_command_lists_and_releases(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b = budget(tmp_path)
    stuck = b.reserve("gpt-6-luna", 400)
    main(["list"], budget=b)
    listed = capsys.readouterr().out
    assert f"open: {stuck.id}" in listed
    assert "spent: 400 µ$ of 1000 µ$" in listed
    main(["release", stuck.id, "--cost-usd", "0.000035"], budget=b)
    released = capsys.readouterr().out
    assert f"released {stuck.id} at 35 µ$" in released
    assert "open:" not in released
    assert "spent: 35 µ$" in released
