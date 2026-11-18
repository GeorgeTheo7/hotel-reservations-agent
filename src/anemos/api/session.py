"""Log in, check, log out (D82). POST /api/session is the one API route without
require_owner: it is how a browser gets a session in the first place."""

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from anemos.api.auth import (
    COOKIE_NAME,
    COOKIE_PATH,
    SESSION_LIFETIME,
    OwnerAuth,
    create_session,
    delete_session,
    login_locked_until,
    password_matches,
    retry_after_seconds,
)
from anemos.api.deps import ClockDep, ConnDep, require_owner
from anemos.audit import AuditEvent, write_audit

router = APIRouter()


class LoginRequest(BaseModel):
    password: str = Field(max_length=1024)


class SessionInfo(BaseModel):
    owner: Literal[True] = True


@router.post("/session", status_code=status.HTTP_204_NO_CONTENT, responses={401: {}, 429: {}})
def login(http: Request, conn: ConnDep, clock: ClockDep, body: LoginRequest) -> Response:
    """A right password: a new session and its cookie. A wrong one: 401, audited with
    actor `system` (nobody is known to be the owner yet). The audit never holds the
    password, the token or its hash. While locked (5 failures in 15 minutes): 429
    with Retry-After, the password not even checked, nothing written."""
    auth: OwnerAuth = http.app.state.auth
    now = clock.now()
    until = login_locked_until(conn, now)
    if until is not None:
        conn.rollback()
        headers = {"Retry-After": str(retry_after_seconds(until, now))}
        return JSONResponse({"error": "login_locked"}, status_code=429, headers=headers)
    if not password_matches(body.password, auth.password):
        write_audit(conn, now, "system", AuditEvent.OWNER_LOGIN_FAILED)
        conn.commit()
        return JSONResponse({"error": "wrong_password"}, status_code=401)
    token, expires_at = create_session(conn, now)
    write_audit(conn, now, "owner", AuditEvent.OWNER_LOGIN,
                output={"expires_at": expires_at.isoformat()})  # fmt: skip
    conn.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path=COOKIE_PATH,
        secure=auth.cookie_secure,
        httponly=True,  # page scripts cannot read it
        samesite="strict",  # never sent on a request another site starts
    )
    return response


@router.get("/session", dependencies=[Depends(require_owner)], responses={401: {}})
def check() -> SessionInfo:
    """200 while the cookie is a live session; require_owner answers 401 otherwise."""
    return SessionInfo()


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
    responses={401: {}},
)
def logout(http: Request, conn: ConnDep, clock: ClockDep) -> Response:
    """Deletes the session row, so the token is dead even if the cookie was copied."""
    auth: OwnerAuth = http.app.state.auth
    delete_session(conn, http.cookies[COOKIE_NAME])  # require_owner saw a live one
    write_audit(conn, clock.now(), "owner", AuditEvent.OWNER_LOGOUT)
    conn.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        COOKIE_NAME, path=COOKIE_PATH, secure=auth.cookie_secure, httponly=True,
        samesite="strict",
    )  # fmt: skip
    return response
