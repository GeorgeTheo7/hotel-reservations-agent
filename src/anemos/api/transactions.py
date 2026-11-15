"""How an action's transaction ends, and the audit rows written after a rollback.

`run_action` is the only place an endpoint's transaction is finished:
- an exception (a refusal, NotFound, a bug) -> roll back *before* re-raising, so the
  request's connection holds nothing by the time any handler runs;
- a result -> commit, unless `keep(result)` says the result wrote nothing worth
  keeping (PriceWarning), then roll back.

`record_after_rollback` writes one audit row in its own short transaction on a new
connection, never the request's. It is called from exactly two places: the
ActionRefused handler (every refused owner action) and the approve endpoint for a
PriceWarning. So a refused or unconfirmed attempt is on record even though the action
itself was rolled back.
"""

from collections.abc import Callable, Mapping
from datetime import datetime

from anemos.api.deps import Conn, Connect
from anemos.audit import AuditEvent, write_audit


def run_action[T](
    conn: Conn, action: Callable[[Conn], T], *, keep: Callable[[T], bool] = lambda _: True
) -> T:
    try:
        result = action(conn)
    except BaseException:
        conn.rollback()
        raise
    if keep(result):
        conn.commit()
    else:
        conn.rollback()
    return result


def record_after_rollback(
    connect: Connect,
    at: datetime,
    event: AuditEvent,
    *,
    email_id: int | None,
    draft_id: int | None,
    output: Mapping[str, object],
) -> None:
    conn = connect()
    try:
        if email_id is None and draft_id is not None:
            row = conn.execute("SELECT email_id FROM drafts WHERE id = %s", (draft_id,)).fetchone()
            email_id = None if row is None else int(row[0])
        write_audit(conn, at, "owner", event, email_id=email_id, draft_id=draft_id, output=output)
        conn.commit()
    finally:
        conn.close()
