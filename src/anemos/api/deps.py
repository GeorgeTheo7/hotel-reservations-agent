"""Per-request dependencies: a connection, the clock, the owner.

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


def require_owner() -> Literal["owner"]:
    """The D19 hook: every route depends on it (a unit test checks). Until the week-7
    password exists it lets every request through, so the API must not be reachable
    from anywhere but this machine (`python -m anemos.api` binds 127.0.0.1 only)."""
    return "owner"


ConnDep = Annotated[Conn, Depends(get_conn)]
ClockDep = Annotated[Clock, Depends(get_clock)]
