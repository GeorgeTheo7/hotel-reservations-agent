"""Which rooms are free for a stay: the only part of the core that reads bookings.

D22: a hold whose expires_at is before "now" no longer blocks. "Now" is the injected
clock (D20), passed into the query as a parameter: the SQL never calls now(), so tests
can fix the time. The query only reads; flipping stale holds to 'expired' happens in
the approval transaction (D10).
"""

from collections.abc import Mapping
from types import MappingProxyType

import psycopg
from psycopg.rows import TupleRow

from anemos.clock import Clock
from anemos.reservations.model import Stay

# A room is free if no active booking overlaps the stay. `&&` = "ranges overlap";
# both ranges are half-open [check-in, check-out), so a booking that checks out on
# the day this stay checks in does not overlap.
FREE_ROOMS_SQL = """
SELECT r.room_type_id,
       r.id,
       NOT EXISTS (
           SELECT 1
           FROM bookings b
           WHERE b.room_id = r.id
             AND b.stay && daterange(%(check_in)s, %(check_out)s, '[)')
             AND (b.status = 'confirmed'
                  OR (b.status = 'hold' AND b.expires_at >= %(now)s))
       ) AS free
FROM rooms r
ORDER BY r.room_type_id, r.id
"""


def free_rooms(
    conn: psycopg.Connection[TupleRow], stay: Stay, clock: Clock
) -> Mapping[str, tuple[int, ...]]:
    """Room type id -> free room numbers for every night of the stay.

    Every room type appears, with an empty tuple when nothing is free. A room counts
    only if it is free for the whole stay: no moving guests between rooms mid-stay.

    The result is a snapshot at query time, not a reservation: another booking can
    take a room a moment later. That is why approval re-validates availability inside
    its own transaction before creating holds (D10), and the database's exclusion
    constraint is the final guard against double booking.
    """
    rows = conn.execute(
        FREE_ROOMS_SQL,
        {"check_in": stay.check_in, "check_out": stay.check_out, "now": clock.now()},
    ).fetchall()
    result: dict[str, list[int]] = {}
    for room_type_id, room_id, free in rows:
        rooms = result.setdefault(str(room_type_id), [])
        if free:
            rooms.append(int(room_id))
    return MappingProxyType({type_id: tuple(ids) for type_id, ids in result.items()})
