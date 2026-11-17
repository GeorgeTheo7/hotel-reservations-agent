"""Hotel Anemos reservations inbox agent."""

import os

# LangSmith tracing off, whatever the environment says (D75): langchain-core, which
# langgraph brings, would otherwise send every traced run (transcripts with guest
# emails) to an outside service when one of these variables is "true". Set here, at the
# first import of the package, before any langgraph or langsmith import of ours.
for _name in (
    "LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2",
):  # fmt: skip
    os.environ[_name] = "false"
