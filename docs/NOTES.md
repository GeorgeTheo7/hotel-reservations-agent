# Engineering log

Real problems found and fixed, and design choices with a trade-off.
Each entry: symptom, cause, evidence, fix, in at most five lines. Newest last.

## 2026-11-12 · DB connections took minutes on Windows (`localhost` vs `127.0.0.1`)
- **Symptom:** two `SELECT 1` integration tests took 4 min 21 s.
- **Cause:** on Windows `localhost` tries IPv6 `::1` first; compose publishes Postgres on IPv4 `127.0.0.1` only, so every connect waited for the IPv6 attempt to time out.
- **Evidence:** timed connects: `127.0.0.1` 0.03 s, `localhost` 200 s.
- **Fix:** `DATABASE_URL` uses `127.0.0.1` (commented in `.env.example`); `connect()` sets `connect_timeout=10` so a wrong host fails fast. Tests: 0.65 s.

## 2026-11-12 · Expired holds still block in the database (D10, D22)
- **Symptom:** a hold past `expires_at` still rejects an overlapping booking via `bookings_no_double_booking`.
- **Cause:** the constraint's `WHERE status IN ('hold','confirmed')` cannot use "now": Postgres requires an immutable predicate, so the database cannot know a hold has expired.
- **Evidence:** `test_hold_past_expiry_still_blocks_until_flipped_to_expired`: blocked while `hold`, free after the flip to `expired`.
- **Fix:** reads treat a hold with `expires_at < now` (injected clock) as free, filter only, no write (D22); approval flips stale holds to `expired` in its transaction before inserting new ones (D10); the periodic job only tidies.

## 2026-11-12 · Rates have no dates (`seasons` + `season_periods` + `room_rates`)
- **Symptom:** dated `rate_periods` next to a season calendar = two sets of date ranges that can disagree (14 Jul High in rates but Mid in seasons; a rate on a closed night).
- **Cause:** city tax and the High-season cancellation rule need the season, so the calendar would have existed twice.
- **Evidence:** (a) dated rates + seed tests only checks the seed, not later edits; (b) rates keyed by room type + season make disagreement impossible.
- **Fix:** (b): `seasons` (city tax once), `season_periods` (the only calendar; no period = closed), `room_rates` (PK type + season). Trade-off: no per-year or special rates (spec "Later"); `min_stay_rules` stays dated, checked against `season_periods` by seed tests.

## 2026-11-13 · Over capacity: one bigger room before two smaller ones (D27)
- **Symptom:** spec said "over capacity → smallest fitting type (1–2 rooms)"; `pricing-examples.md` Q4 said one room first. Example 8 (Sea View, 4 adults) differed.
- **Cause:** the spec wording ranked type size first and room count second; Q4 ranks room count first.
- **Evidence:** Example 8: 2 × Standard = 570 / deposit 171 vs 1 × Family Suite = 750 / deposit 225.
- **Fix:** D27, one room first; 2 rooms only if no single type fits. Trade-off: the guest is offered the dearer option first; the owner can still propose the 2-room split.

## 2026-11-13 · Proving "no float" in money code (AST guard, no exception for `/`)
- **Symptom:** "money is integer cents" (D14) was a convention; one `* 0.3` or `/` would silently bring floats back (`7 / 1` is `7.0`).
- **Cause:** Python has no type-level ban on floats; mypy's `NewType("Cents", int)` rejects a float argument but not a float computed inside a function.
- **Evidence:** a planted `return s * 0.3` in `reservations/` fails `test_no_float.py` with `line 2: float literal 0.3`.
- **Fix:** AST guard over `reservations/` (float literals, `/`, negative powers, `float`/`round`, `math`/`decimal`, float f-string specs) + runtime `type(x) is int` checks. Trade-off: `/` is banned even for paths (use `joinpath`), because the AST can't tell a `Path` from a number; `%`/`.format` strings aren't scanned.
