"""Per-request dependencies: a connection, the clock, the owner (D82).

FastAPI calls these for every request that names them (`Depends`). They read the
connection factory and the clock from `app.state`, set once by `create_app`, so a
test builds its own app with the test database and a fixed clock: no global override
to forget.
"""

from collections.abc import Callable, Iterator
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, Request
from psycopg.rows import TupleRow

from anemos.api.auth import COOKIE_NAME, session_is_live
from anemos.clock import Clock

type Conn = psycopg.Connection[TupleRow]
type Connect = Callable[[], Conn]


def get_conn(request: Request) -> Iterator[Conn]:
    """A new connection for this request, always closed afterwards. Closing without a
    commit rolls back: an endpoint that writes must commit explicitly."""
    connect: Connect = request.app.state.connect
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


ConnDep = Annotated[Conn, Depends(get_conn)]
ClockDep = Annotated[Clock, Depends(get_clock)]


class Unauthenticated(Exception):
    """No live owner session: 401 (handler in app.py)."""


def require_owner(request: Request, conn: ConnDep, clock: ClockDep) -> Literal["owner"]:
    """The D19 hook, D82's check: every route but login depends on it (a unit test
    checks). The cookie's token must match a live row in owner_sessions.

    It reads on the request's own connection (FastAPI gives the endpoint the same one)
    and then rolls back, so the endpoint starts a fresh transaction exactly as it did
    before this check existed."""
    try:
        live = session_is_live(conn, request.cookies.get(COOKIE_NAME), clock.now())
    finally:
        conn.rollback()
    if not live:
        raise Unauthenticated
    return "owner"
