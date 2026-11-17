"""Owner actions: edit, approve, reject (drafts); close, reopen (emails); confirm,
release, cancel (bookings, the Holds page).

Each endpoint runs one domain function through `run_action` (transactions.py), then
maps the result to HTTP:

| Domain result                    | HTTP | Transaction                                |
|----------------------------------|------|--------------------------------------------|
| success                          | 2xx  | committed                                  |
| DraftExpired (return)            | 409  | committed: the flip and its audit (D54)    |
| Invalidated (return)             | 409  | committed: invalidated + escalated (D44)   |
| PriceWarning (return)            | 422  | rolled back, then an audit row (own txn)   |
| GuardrailWarning (return)        | 422  | rolled back, one audit row per warning     |
| ActionRefused (raise)            | 409  | rolled back, then an audit row (app.py)    |
| NotFound (raise)                 | 404  | rolled back                                |

After an approve (approved or invalidated) or a reject has committed, the run's graph
thread is resumed to record the run finished (D75, `resume_thread`). A failed resume
never fails the owner's request: the error is logged and audited (`run.resume_failed`)
and the thread stays interrupted for the week-10 tidy-up. Resuming is idempotent.
"""

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from anemos.api.deps import ClockDep, ConnDep, require_owner
from anemos.api.transactions import record_after_rollback, run_action
from anemos.approval import (
    Approved,
    GuardrailWarning,
    Invalidated,
    PriceWarning,
    approve_draft,
)
from anemos.audit import AuditEvent, write_audit
from anemos.clock import Clock
from anemos.graph import resume_after_owner
from anemos.guardrails import PRICE_CHECK
from anemos.owner_actions import (
    DraftExpired,
    Edited,
    cancel_booking,
    close_email,
    confirm_hold,
    edit_draft,
    reject_draft,
    release_hold,
    reopen_email,
)
from anemos.states import BookingStatus, DraftStatus, EmailStatus

router = APIRouter(dependencies=[Depends(require_owner)])
log = logging.getLogger("anemos.api")


# --- bodies -----------------------------------------------------------------------------


class EditRequest(BaseModel):
    body: str
    base_version: int = Field(ge=1)  # D67: the version the owner edited from

    @field_validator("body")
    @classmethod
    def not_blank(cls, body: str) -> str:
        if not body.strip():
            raise ValueError("an edited draft cannot be blank")
        return body


class EditResult(BaseModel):
    version: int
    price_warning: str | None  # set: approval will need confirm_warnings (D63)
    warnings: list[str] = []  # guardrails 2-5 on the edit (D77): confirm_warnings too


class ApproveRequest(BaseModel):
    version: int = Field(ge=1)  # D67: the version on the owner's screen
    confirm_warnings: bool = False


class ApproveResult(BaseModel):
    outbox_id: int
    booking_ids: list[int]
    hold_until: datetime | None


class DraftDecided(BaseModel):
    draft_id: int
    status: Literal[DraftStatus.REJECTED]


class EmailMoved(BaseModel):
    email_id: int
    status: EmailStatus


class BookingMoved(BaseModel):
    booking_id: int
    status: BookingStatus


def resume_thread(http: Request, clock: Clock, draft_id: int) -> None:
    """D75: after the owner's action committed, resume the run's thread to record it
    finished. Any failure is logged and audited, never raised: the action stands."""
    connect = http.app.state.connect
    email_id: int | None = None
    try:
        conn = connect()
        try:
            row = conn.execute("SELECT email_id FROM drafts WHERE id = %s", (draft_id,)).fetchone()
            conn.commit()
        finally:
            conn.close()
        email_id = None if row is None else int(row[0])
        if email_id is not None:
            resume_after_owner(connect, clock, email_id)
    except Exception as exc:
        log.exception("resuming the run of draft %s failed", draft_id)
        try:
            conn = connect()
            try:
                write_audit(conn, clock.now(), "system", AuditEvent.RUN_RESUME_FAILED,
                            email_id=email_id, draft_id=draft_id,
                            output={"error": f"{type(exc).__name__}: {exc}"})  # fmt: skip
                conn.commit()
            finally:
                conn.close()
        except Exception:
            log.exception("auditing the failed resume of draft %s failed", draft_id)


def expired(result: DraftExpired) -> JSONResponse:
    return JSONResponse(
        {"error": "expired", "draft_id": result.draft_id}, status_code=status.HTTP_409_CONFLICT
    )


# --- drafts -----------------------------------------------------------------------------


@router.post(
    "/drafts/{draft_id}/versions",
    status_code=status.HTTP_201_CREATED,
    responses={409: {}, 404: {}},
    response_model=EditResult,
)
def edit(
    conn: ConnDep, clock: ClockDep, draft_id: int, request: EditRequest
) -> EditResult | JSONResponse:
    result = run_action(
        conn,
        lambda c: edit_draft(c, clock, draft_id, request.body, base_version=request.base_version),
    )
    if isinstance(result, DraftExpired):
        return expired(result)
    assert isinstance(result, Edited)
    return EditResult(version=result.version, price_warning=result.price_warning,
                      warnings=list(result.warnings))  # fmt: skip


@router.post(
    "/drafts/{draft_id}/approve",
    responses={409: {}, 422: {}, 404: {}},
    response_model=ApproveResult,
)
def approve(
    http: Request, conn: ConnDep, clock: ClockDep, draft_id: int, request: ApproveRequest
) -> ApproveResult | JSONResponse:
    result = run_action(
        conn,
        lambda c: approve_draft(
            c, clock, draft_id, version=request.version, confirm_warnings=request.confirm_warnings
        ),
        keep=lambda r: not isinstance(r, PriceWarning | GuardrailWarning),  # wrote nothing
    )
    if isinstance(result, (Approved, Invalidated)):
        resume_thread(http, clock, draft_id)
    match result:
        case Approved():
            return ApproveResult(
                outbox_id=result.outbox_id,
                booking_ids=list(result.booking_ids),
                hold_until=result.hold_until,
            )
        case DraftExpired():
            return expired(result)
        case Invalidated():
            return JSONResponse(
                {"error": "invalidated", "reasons": list(result.reasons)},
                status_code=status.HTTP_409_CONFLICT,
            )
        case PriceWarning():
            _shown_price(http, clock, draft_id, result.version, result.quote_ids)
            return JSONResponse(
                {
                    "error": "price_warning",
                    "message": result.message,
                    "warnings": [{"check": PRICE_CHECK, "message": result.message}],
                    "hint": CONFIRM_HINT,
                },
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        case GuardrailWarning():
            # D77: every warning of the version in one response, each one audited, so
            # confirm_warnings never confirms a warning the owner was not shown.
            for check, message in result.warnings:
                if check == PRICE_CHECK:
                    _shown_price(http, clock, draft_id, result.version, result.quote_ids)
                else:
                    record_after_rollback(
                        http.app.state.connect, clock.now(),
                        AuditEvent.DRAFT_GUARDRAIL_WARNING_SHOWN, email_id=None,
                        draft_id=draft_id,
                        output={"action": "approve", "version": result.version,
                                "check": check, "message": message},
                    )  # fmt: skip
            return JSONResponse(
                {
                    "error": "guardrail_warning",
                    "warnings": [{"check": c, "message": m} for c, m in result.warnings],
                    "hint": CONFIRM_HINT,
                },
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )


CONFIRM_HINT = "repeat with confirm_warnings=true to send it anyway"


def _shown_price(
    http: Request, clock: Clock, draft_id: int, version: int, quote_ids: tuple[int, ...]
) -> None:
    record_after_rollback(
        http.app.state.connect, clock.now(), AuditEvent.DRAFT_PRICE_WARNING_SHOWN,
        email_id=None, draft_id=draft_id,
        output={"action": "approve", "version": version, "quote_ids": list(quote_ids)},
    )  # fmt: skip


@router.post("/drafts/{draft_id}/reject", responses={409: {}, 404: {}}, response_model=DraftDecided)
def reject(
    http: Request, conn: ConnDep, clock: ClockDep, draft_id: int
) -> DraftDecided | JSONResponse:
    result = run_action(conn, lambda c: reject_draft(c, clock, draft_id))
    if isinstance(result, DraftExpired):
        return expired(result)
    resume_thread(http, clock, draft_id)
    return DraftDecided(draft_id=draft_id, status=DraftStatus.REJECTED)


# --- emails -----------------------------------------------------------------------------


@router.post("/emails/{email_id}/close", responses={409: {}, 404: {}})
def close(conn: ConnDep, clock: ClockDep, email_id: int) -> EmailMoved:
    run_action(conn, lambda c: close_email(c, clock, email_id))
    return EmailMoved(email_id=email_id, status=EmailStatus.CLOSED)


@router.post("/emails/{email_id}/reopen", responses={409: {}, 404: {}})
def reopen(conn: ConnDep, clock: ClockDep, email_id: int) -> EmailMoved:
    run_action(conn, lambda c: reopen_email(c, clock, email_id))
    return EmailMoved(email_id=email_id, status=EmailStatus.ESCALATED)


# --- bookings (Holds page) ---------------------------------------------------------------


@router.post("/bookings/{booking_id}/confirm", responses={409: {}, 404: {}})
def confirm(conn: ConnDep, clock: ClockDep, booking_id: int) -> BookingMoved:
    """hold -> confirmed: deposit received (D43); allowed past expires_at while the
    booking is still a hold."""
    run_action(conn, lambda c: confirm_hold(c, clock, booking_id))
    return BookingMoved(booking_id=booking_id, status=BookingStatus.CONFIRMED)


@router.post("/bookings/{booking_id}/release", responses={409: {}, 404: {}})
def release(conn: ConnDep, clock: ClockDep, booking_id: int) -> BookingMoved:
    """hold -> cancelled (D46)."""
    run_action(conn, lambda c: release_hold(c, clock, booking_id))
    return BookingMoved(booking_id=booking_id, status=BookingStatus.CANCELLED)


@router.post("/bookings/{booking_id}/cancel", responses={409: {}, 404: {}})
def cancel(conn: ConnDep, clock: ClockDep, booking_id: int) -> BookingMoved:
    """confirmed -> cancelled (D46)."""
    run_action(conn, lambda c: cancel_booking(c, clock, booking_id))
    return BookingMoved(booking_id=booking_id, status=BookingStatus.CANCELLED)
