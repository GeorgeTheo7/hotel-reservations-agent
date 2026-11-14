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

## 2026-11-13 · An unseeded year said "closed" (D36, NO_CALENDAR)
- **Symptom:** a June 2028 stay got `CLOSED`, which would tell the guest the hotel is shut in June.
- **Cause:** "a night in no season period = closed" cannot tell "outside the season" from "season not set up yet"; only 2027 is seeded.
- **Evidence:** `test_year_without_calendar_escalates_not_closed` (June 2028) and a 30 Dec 2027 – 2 Jan 2028 stay both escalate; 1 Nov 2027 and Western Easter 2027 stay `CLOSED`.
- **Fix:** `HotelData.has_calendar_for(year)`; `check_stay` returns `NO_CALENDAR` (escalation) before `CLOSED`. Trade-off: a far-future request reaches the owner instead of getting an automatic answer.

## 2026-11-13 · Guest wording lives in template files, not in Python strings
- **Symptom:** ruff's look-alike check (RUF001) flagged Greek words in `quote_template.py` ("για", "να" look like Latin letters); wording was also scattered across code.
- **Cause:** sentences the guest reads were built in Python f-strings, mixed with logic.
- **Evidence:** 19 RUF001 findings in one module; after the move, `src/` has no Greek sentences and passes ruff unchanged.
- **Fix:** layout in `reservations/templates/quote_*.txt` and `summary_*.txt`, sentences in `phrases_*.json` (`string.Template`); a test checks both languages have the same keys; golden files pin the output. Trade-off: a typo in a phrase key only fails at render time (covered by the golden tests), and `tests/` is exempt from RUF001 because tests must assert bilingual text.

## 2026-11-13 · "Newer email" needs a total order (D48)
- **Symptom:** D48 ("a newer email from the same guest supersedes") did not say which column defines "newer" or what breaks a tie.
- **Cause:** two candidate orders: `received_at` (when the guest wrote) and storage order (`id`); an overnight batch can be fetched out of order, so they can disagree.
- **Evidence:** with storage order, an email written Monday but stored after Tuesday's would supersede Tuesday's draft: the older request would win.
- **Fix:** newer = later `received_at`, tie → higher `id`; index `(from_address, received_at, id)`. Trade-off: an email stored late supersedes nothing and its draft is born superseded, so the owner never sees a draft for it.

## 2026-11-13 · Constraints that can never fire alone (migration 0005)
- **Symptom:** tests for `bookings_email_id_fkey`, `bookings_request_id_fkey` and `requests_children_non_negative` could only assert "some constraint refused the row", never this one by name.
- **Cause:** each is implied by another: the two-column key `(request_id, email_id)` `MATCH FULL` already needs the pair in `requests`, whose `email_id` points at `emails`; and `children = -1` also fails "ages ≤ children" (0 ≤ −1).
- **Evidence:** `children = -1` reported `requests_child_ages_not_more_than_children`, not the non-negative check; no row exists that only a one-column key refuses.
- **Fix:** dropped the two one-column keys (spec Data model says the two-column key replaces them); kept `requests_children_non_negative` for its clearer error name, commented as unable to fire alone. Rule: a constraint whose test cannot name it is a sign it is redundant.

## 2026-11-13 · `btrim(x) <> ''` does not catch a blank of newlines
- **Symptom:** `test_blank_body_rejected` failed: a draft body of `"  \n "` passed `draft_versions_body_not_blank`.
- **Cause:** `btrim` with one argument trims **spaces only**; tabs and newlines stay, so the trimmed text is not empty.
- **Evidence:** the same pattern was already in `room_types_name_el_not_blank` (0004) and `emails_message_id_not_blank` / `emails_from_address_not_blank` (0005); their tests used spaces only, so they passed.
- **Fix:** `x ~ '\S'` ("has a non-whitespace character") in 0006; the three older checks are replaced in their own migration (0007), not edited in place, because databases may already be at 0004/0005. Rule: test "blank" with a tab and a newline, not only spaces.

## 2026-11-13 · Why an offer differs from the request lives on `quotes` (`changes`, D9)
- **Symptom:** nothing stored why an offer was not what the guest asked (min-stay version, open part, other type, 2 rooms, ±7 d, window option), and `hold_allowed` was a free boolean that could disagree with it.
- **Cause:** the reason is a fact about a quote against its request; `hold_allowed(request, offers)` mixes that with a per-draft fact ("the only offer").
- **Evidence:** changes combine (open part + min stay, D34), so one enum value would lose information; on `draft_offers` the same quote could be labelled differently in two drafts.
- **Fix:** `quotes.changes text[]` (CHECKed set), `hold_allowed` **generated** as `cardinality(changes) = 0`, so it cannot be written or disagree. Trade-off: the per-draft half of D9 ("a held offer is the only offer") needs a count across rows, which a CHECK cannot do; guardrail 7 checks it in code.

## 2026-11-13 · Frozen rows vs. the right to erasure (D51)
- **Symptom:** after D51, a final email, draft, booking or outbox row and every audit entry can never be updated or deleted, yet they hold personal data (addresses, email text).
- **Cause:** the triggers protect the audit trail against bugs and manual edits; erasing a guest's data is also a change to those rows.
- **Evidence:** a `DELETE FROM emails` on a `closed` email is refused by `emails_final_status`; `TRUNCATE` is not, because it skips row triggers (the `committed_db` fixture relies on that).
- **Fix:** none in v1 (all data is fictional). Spec "Later": a controlled, audited erasure/anonymisation procedure that bypasses the triggers on purpose, and an application DB role without `TRUNCATE` (week 9, roles). Trade-off: integrity now, a documented gap for real data.

## 2026-11-14 · A second test harness for real commits (`committed_db`)
- **Symptom:** the `db` fixture rolls every test back and refuses `commit()`, so it cannot test the approval transaction: a second connection never sees the first one's work, and no lock is ever held across connections.
- **Cause:** isolation by rollback is exactly what makes a race untestable.
- **Evidence:** `test_a_commit_is_visible_on_a_second_connection` (invisible before commit, visible after); `test_wait_until_blocked_sees_a_lock_wait` (B provably waits on A's `FOR UPDATE`, then reads A's committed change).
- **Fix:** own database `<dev>_commit_test`, operational tables truncated before and after each test (`TRUNCATE` skips the D51 triggers; a test checks every table is either reset or seed); lock waits detected by polling `pg_stat_activity.wait_event_type = 'Lock'`, never by sleeping. Trade-off: one more migrate + seed per session (~1 s), and races use threads, so a hung test fails by timeout (5 s) rather than instantly.

## 2026-11-14 · Three gaps in the request-to-offer path (D55–D57)
- **Symptom:** writing `build_offer` as one function hit three cases the spec did not decide: no room type named, an accessibility need with 2 rooms, and a min-stay version that runs into closed nights.
- **Cause:** D27/D33/D34 describe substitutions for a named type and a single stay; the combinations were open.
- **Evidence:** 3 adults, no type → Sea View (1 room, may be held); need + 2 rooms → `ACCESSIBILITY`; with a 3-night minimum, 31 Oct for 1 night → `MIN_STAY_CLOSED(3, [1 Nov, 2 Nov])` (tests in `tests/unit/test_offer.py`, using a copy of the hotel with a changed minimum).
- **Fix:** D55 smallest type that fits the rooms asked; D56 escalate; D57 a typed `MinStayIntoClosed` that carries both facts, not `Closed`. Trade-off: one more error type the reply templates must render (week 6); plain "closed" would have been simpler but untrue for the nights asked.

## 2026-11-14 · quote_price prices the stored request, and stores every call (D58)
- **Symptom:** a tool that took dates and a party as arguments would let the model price a party the guest never described; calling it twice for one request stored two quotes, and it was unclear if that was a bug.
- **Cause:** the tool's inputs and its write behaviour were not decided by the spec.
- **Evidence:** `test_missing_facts_need_info_and_store_nothing` (missing dates/adults/ages → NEEDS_INFO, no row); a second call returns a new `quote_id` with its own `created_at`.
- **Fix:** `quote_price(request_id)` reads dates, party and type from `requests` (rooms not stated = 1; accessibility from the extraction flags); every call stores a new write-once quote, never reused (D58). Trade-off: unlinked quotes accumulate (bounded by the 8-call loop); reuse would risk handing back a price from before a rate change.

## 2026-11-14 · quote_price refuses a full room type (D59)
- **Symptom:** `quote_price` priced and stored a quote for a fully booked type; in week 5 the model could skip `check_availability`, put that quote in a draft, and approval would invalidate it (D44).
- **Cause:** availability lived in a separate tool, so the rule "only offer what is free" depended on the model calling tools in the right order (against D11).
- **Evidence:** 5 of 6 Standard rooms booked, 2 asked → `FULLY_BOOKED("standard", 2, 1)`, no row; Ex 8 with all Suites booked → `FULLY_BOOKED("family_suite", 1, 0)` even though the Sea View asked for is free.
- **Fix:** shared `rooms_for_plan` (lowest free numbers or `FULLY_BOOKED`), called after `build_offer`, before storing; the same function serves `suggest_alternatives` and approval. Trade-off: two tools ask the database the same question; still a snapshot, so approval re-checks.
