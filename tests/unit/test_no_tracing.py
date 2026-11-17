"""LangSmith tracing stays off whatever the environment says (D75): a variable set to
"true" must not send guest emails to an outside service. Each check runs in a fresh
interpreter, because langsmith caches what it read from the environment."""

import os
import subprocess
import sys

TRACING_ON = {
    "LANGSMITH_TRACING": "true", "LANGSMITH_TRACING_V2": "true",
    "LANGCHAIN_TRACING": "true", "LANGCHAIN_TRACING_V2": "true",
}  # fmt: skip
ENABLED = "import langsmith.utils as u; print(u.tracing_is_enabled())"


def tracing_enabled(setup: str) -> str:
    done = subprocess.run(
        [sys.executable, "-c", f"{setup}; {ENABLED}"],
        env={**os.environ, **TRACING_ON}, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return done.stdout.strip()


def test_the_environment_alone_would_turn_tracing_on() -> None:
    """The control: without anemos, these variables do enable tracing."""
    assert tracing_enabled("pass") == "True"


def test_importing_anemos_turns_tracing_off() -> None:
    assert tracing_enabled("import anemos.graph") == "False"
