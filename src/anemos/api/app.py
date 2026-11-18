"""The app factory and the error handlers shared by every route.

Endpoints are plain `def` functions: FastAPI runs them in a worker thread, so a
blocking psycopg call does not hold up other requests, and the domain code stays
synchronous.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from anemos.api import actions, reads, session
from anemos.api.auth import OwnerAuth, auth_from_env
from anemos.api.deps import Connect, Unauthenticated
from anemos.api.security import SECURITY_HEADERS, guard_and_headers
from anemos.api.transactions import record_after_rollback
from anemos.audit import AuditEvent
from anemos.clock import Clock, SystemClock
from anemos.db.connection import connect as db_connect
from anemos.states import ActionRefused, Entity, NotFound

log = logging.getLogger("anemos.api")

# D83: every API route lives under /api, so the built UI can be served from the same
# origin at / and a path under /api never falls back to the UI.
API_PREFIX = "/api"


def create_app(connect: Connect, clock: Clock, auth: OwnerAuth) -> FastAPI:
    # D82: no /docs, /redoc or /openapi.json; tests read the schema with app.openapi().
    app = FastAPI(title="Hotel Anemos owner API", docs_url=None, redoc_url=None,
                  openapi_url=None)  # fmt: skip
    app.state.connect = connect
    app.state.clock = clock
    app.state.auth = auth
    app.middleware("http")(guard_and_headers)  # D82 CSRF guard, D83 headers
    app.include_router(session.router, prefix=API_PREFIX)
    app.include_router(reads.router, prefix=API_PREFIX)
    app.include_router(actions.router, prefix=API_PREFIX)
    app.add_exception_handler(Unauthenticated, _unauthenticated)
    app.add_exception_handler(ActionRefused, _refused)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(Exception, _internal_error)
    return app


def production_app() -> FastAPI:
    """DATABASE_URL, the system clock and the owner password from the environment;
    for `python -m anemos.api`. Without ANEMOS_OWNER_PASSWORD it refuses to start."""
    return create_app(lambda: db_connect(), SystemClock(), auth_from_env())


REFUSED_EVENT: dict[Entity, AuditEvent] = {
    "draft": AuditEvent.DRAFT_ACTION_REFUSED,
    "email": AuditEvent.EMAIL_ACTION_REFUSED,
    "booking": AuditEvent.BOOKING_ACTION_REFUSED,
}


def _refused(request: Request, exc: Exception) -> JSONResponse:
    """Every refused owner action (TransitionRefused, StaleVersion): 409, and one
    `<entity>.action_refused` audit row. run_action has already rolled the request's
    transaction back; the row is written on a new connection in its own transaction,
    so it does not depend on when FastAPI closes the request's connection."""
    assert isinstance(exc, ActionRefused)
    record_after_rollback(
        request.app.state.connect, request.app.state.clock.now(), REFUSED_EVENT[exc.entity],
        email_id=exc.email_id, draft_id=exc.draft_id, output=exc.audit_output(),
    )  # fmt: skip
    return JSONResponse({"error": exc.reason} | exc.response_details(), status_code=409)


def _unauthenticated(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "not_found"}, status_code=404)


def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Any exception no other handler took is a bug: the full traceback goes to the
    server log, the client only learns that it failed. Starlette answers this one
    outside every middleware, so the security headers are added here too (D83)."""
    log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"error": "internal"}, status_code=500, headers=SECURITY_HEADERS)
