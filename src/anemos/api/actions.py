"""Draft and email actions: edit, approve, reject, close, reopen.

Each endpoint runs one domain function through `run_action` (transactions.py), then
maps the result to HTTP:

| Domain result                    | HTTP | Transaction                                |
|----------------------------------|------|--------------------------------------------|
| success                          | 2xx  | committed                                  |
| DraftExpired (return)            | 409  | committed: the flip and its audit (D54)    |
| Invalidated (return)             | 409  | committed: invalidated + escalated (D44)   |
| PriceWarning (return)            | 422  | rolled back, then an audit row (own txn)   |
| ActionRefused (raise)            | 409  | rolled back, then an audit row (app.py)    |
| NotFound (raise)                 | 404  | rolled back                                |
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from anemos.api.deps import ClockDep, ConnDep, require_owner
from anemos.api.transactions import record_after_rollback, run_action
from anemos.approval import Approved, Invalidated, PriceWarning, approve_draft
from anemos.audit import AuditEvent
from anemos.owner_actions import (
    DraftExpired,
    Edited,
    close_email,
    edit_draft,
    reject_draft,
    reopen_email,
)
from anemos.states import DraftStatus, EmailStatus

router = APIRouter(dependencies=[Depends(require_owner)])


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
    return EditResult(version=result.version, price_warning=result.price_warning)


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
        keep=lambda r: not isinstance(r, PriceWarning),  # it wrote nothing
    )
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
            record_after_rollback(
                http.app.state.connect, clock.now(), AuditEvent.DRAFT_PRICE_WARNING_SHOWN,
                email_id=None, draft_id=draft_id,
                output={"action": "approve", "version": result.version,
                        "quote_ids": list(result.quote_ids)},
            )  # fmt: skip
            return JSONResponse(
                {
                    "error": "price_warning",
                    "message": result.message,
                    "hint": "repeat with confirm_warnings=true to send it anyway",
                },
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )


@router.post("/drafts/{draft_id}/reject", responses={409: {}, 404: {}}, response_model=DraftDecided)
def reject(conn: ConnDep, clock: ClockDep, draft_id: int) -> DraftDecided | JSONResponse:
    result = run_action(conn, lambda c: reject_draft(c, clock, draft_id))
    if isinstance(result, DraftExpired):
        return expired(result)
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
