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

## 2026-11-13 · Extra-bed prices in the database, age bands in code (migration 0003)
- **Symptom:** the spec gives extra-bed prices (€20 child 2–11, €35 12+) but no table held them; pricing would have needed constants in code, against "prices from the database".
- **Cause:** week 1's schema covered room rates and city tax only.
- **Evidence:** `seed.py` had no extra-bed values; `HotelData` had no field for them.
- **Fix:** `extra_bed_rates` (band → `rate_cents`, CHECK on band names and rate > 0), loaded into `HotelData`, which refuses a snapshot missing a band. Trade-off: the age bounds (2–11, 12+) stay in code next to the band names, not as DB columns: simpler, but changing a bound is a code change.

## 2026-11-13 · Escalate instead of substituting (D32, D33)
- **Symptom:** `smallest_fitting_plan` offered a Sea View to a party of 3 with an accessibility need, and could plan a room with only children in it (Ex 12's party split 2 adults / 3 children).
- **Cause:** the plan looked at head count only.
- **Evidence:** tests: need + 3 people → `ACCESSIBILITY`; 1 adult for a 2-room plan, or a children-only party → `UNACCOMPANIED_CHILDREN`; 9 children → `GROUP` (checked first).
- **Fix:** typed `NeedsEscalation(reason, detail)` result with two new reason codes. Trade-off: some parties a human would happily serve now reach the owner; a wrong substitute would reach the guest.

## 2026-11-13 · DST: subtracting two Athens times gives wall-clock hours, not real hours
- **Symptom:** arrival 31 Oct 2027 15:00 minus received 29 Oct 16:00 (both `Europe/Athens`) = 47 h, so `SHORT_NOTICE` would fire for an email received exactly 48 real hours ahead.
- **Cause:** Python subtracts two aware datetimes that share the *same* tzinfo by wall clock, ignoring the DST change in between (clocks go back on 31 Oct).
- **Evidence:** `test_naive_wall_clock_subtraction_would_be_wrong`: 47 h in Athens time, 48 h after converting to UTC; DST tests on 28 Mar and 31 Oct pass with `received_at` given in Athens time and via `FixedClock` (UTC).
- **Fix:** `short_notice` converts both instants to UTC before subtracting (`hotel_time.check_in_moment` returns UTC). Rule: compute durations in UTC, use Athens only for calendar dates and display.
