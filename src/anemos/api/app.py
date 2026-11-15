"""The app factory and the error handlers shared by every route.

Endpoints are plain `def` functions: FastAPI runs them in a worker thread, so a
blocking psycopg call does not hold up other requests, and the domain code stays
synchronous.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from anemos.api import reads
from anemos.api.deps import Connect
from anemos.clock import Clock, SystemClock
from anemos.db.connection import connect as db_connect
from anemos.states import NotFound

log = logging.getLogger("anemos.api")


def create_app(connect: Connect, clock: Clock) -> FastAPI:
    app = FastAPI(title="Hotel Anemos owner API")
    app.state.connect = connect
    app.state.clock = clock
    app.include_router(reads.router)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(Exception, _internal_error)
    return app


def production_app() -> FastAPI:
    """DATABASE_URL and the system clock; for `python -m anemos.api`."""
    return create_app(lambda: db_connect(), SystemClock())


def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "not_found"}, status_code=404)


def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Any exception no other handler took is a bug: the full traceback goes to the
    server log, the client only learns that it failed."""
    log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"error": "internal"}, status_code=500)
