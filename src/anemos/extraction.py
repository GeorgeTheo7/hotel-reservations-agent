"""Extraction vocabulary (spec Escalation rules, D5): the flags and scripts an
extraction can report. One definition, used by the test-email labels now and by the
extraction step (week 4) later."""

from enum import StrEnum


class ExtractionFlag(StrEnum):
    LEGAL_THREAT = "legal_threat"
    BILLING_REQUEST = "billing_request"
    MEDICAL_NEED = "medical_need"
    ACCESSIBILITY_NEED = "accessibility_need"
    INJECTION_SUSPECTED = "injection_suspected"


class Script(StrEnum):
    """The letters the guest wrote in. `latin` with language `el` is Greeklish (D5)."""

    GREEK = "greek"
    LATIN = "latin"
