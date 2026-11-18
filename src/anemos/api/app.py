"""The app factory and the error handlers shared by every route.

Endpoints are plain `def` functions: FastAPI runs them in a worker thread, so a
blocking psycopg call does not hold up other requests, and the domain code stays
synchronous.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from anemos.api import actions, reads
from anemos.api.deps import Connect
from anemos.api.transactions import record_after_rollback
from anemos.audit import AuditEvent
from anemos.clock import Clock, SystemClock
from anemos.db.connection import connect as db_connect
from anemos.states import ActionRefused, Entity, NotFound

log = logging.getLogger("anemos.api")

# D83: every API route lives under /api, so the built UI can be served from the same
# origin at / and a path under /api never falls back to the UI.
API_PREFIX = "/api"


def create_app(connect: Connect, clock: Clock) -> FastAPI:
    app = FastAPI(title="Hotel Anemos owner API")
    app.state.connect = connect
    app.state.clock = clock
    app.include_router(reads.router, prefix=API_PREFIX)
    app.include_router(actions.router, prefix=API_PREFIX)
    app.add_exception_handler(ActionRefused, _refused)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(Exception, _internal_error)
    return app


def production_app() -> FastAPI:
    """DATABASE_URL and the system clock; for `python -m anemos.api`."""
    return create_app(lambda: db_connect(), SystemClock())


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


def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "not_found"}, status_code=404)


def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Any exception no other handler took is a bug: the full traceback goes to the
    server log, the client only learns that it failed."""
    log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"error": "internal"}, status_code=500)
