"""The escalation summary's structured output (D81), apart from the call (anemos.summary)
so the prompt registry (anemos.prompts) can hash it without importing the call."""

from typing import Literal

from pydantic import BaseModel, Field

# The codes a summary may add: the ones only a model can judge, and that code-raised
# escalations never reach the agent to get (D81, D11). Never removes a code.
SecondaryCode = Literal["COMPLAINT", "DISCOUNT"]  # a plain alias: inline in the schema


class EscalationSummary(BaseModel):
    """A note for the hotel owner about an escalated email."""

    summary: str = Field(description="2-3 short sentences in English for the hotel owner.")
    add_codes: list[SecondaryCode] = Field(
        max_length=2, description="COMPLAINT and/or DISCOUNT if they apply; else empty."
    )
