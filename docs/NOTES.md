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

## 2026-11-14 · suggest_alternatives: what "≤ 2 alternatives, ±7 d" means in code (D60)
- **Symptom:** the spec's one line ("other type free → ≤ 2 cheapest first · all booked → nearest same-length ranges ±7 d, else decline") left open: cheapest by what, ties, mixing types with dates, shifted stays that hit closed days or short notice, and a call made while the original plan is still free.
- **Cause:** written as a policy sentence, not an algorithm.
- **Evidence:** 15 tests in `test_suggest_alternatives.py`; flipping the date tie-break or letting the Accessible room compete freely makes 5 of them fail.
- **Fix:** D60: free original plan → `ORIGINAL_AVAILABLE` (store nothing); other types first (fit, free, cheapest by accommodation subtotal, Accessible last resort); dates only if no type is free (same plan, nearest, earlier on tie, unchanged length, no short notice); else `NONE_FOUND`. Trade-off: a guest who would rather move dates than change room type is not offered that when another type is free (Later).

## 2026-11-14 · Superseding needs a per-sender lock, not just a transaction (D48)
- **Symptom:** "a newer email supersedes the waiting draft" and "a draft created after a newer email is born superseded" can each miss the other: under READ COMMITTED, neither transaction sees the other's uncommitted row, so a draft could stay pending although a newer email exists.
- **Cause:** the rule spans two tables written by two different code paths (storing an email, creating a draft), and no single row is shared that a `FOR UPDATE` could lock.
- **Evidence:** `test_storing_waits_for_the_senders_lock` (committed_db): with the sender's lock held, `store_email` provably waits; another sender goes straight through.
- **Fix:** `lock_sender` = `pg_advisory_xact_lock(hashtextextended(address, 0))`, taken first by `store_email` and (step 10) by draft creation; released at commit. Trade-off: two addresses can share a hash (64-bit, so rarely); they then only wait briefly for each other's transaction, never deadlock or mix data. A code path that forgets the lock reopens the race, so the step 10 tests must cover it. Recorded as D61.

## 2026-11-14 · What the stub drafter does not write, and why it escalates instead
- **Symptom:** some tool results had no reply template: CLOSED, MIN_STAY_CLOSED, NONE_FOUND (a decline), and ORIGINAL_AVAILABLE appearing between two tool calls.
- **Cause:** decline wording and its guardrails belong to the agent (week 5) and the guardrail work (week 6); the quote templates cover quotes only.
- **Evidence:** `test_min_stay_closed_still_escalates`: email `escalated` with `MODEL`, no draft row; `NO_RATE` escalates `SYSTEM_ERROR` (a data error).
- **Fix:** CLOSED and NONE_FOUND got a `decline` route with EN/EL templates (D62, migration 0009); the rest escalate `MODEL` (the stub stands in for the model, which may escalate, D11). Every change in a quote gets its own note line, in a fixed order (`test_open_part_and_min_stay_give_both_notes`). Trade-off: the NONE_FOUND decline says the same stay a week either side is not available (booked, closed or too short), which is checked for the plan's room type only (other types on other dates are not searched: Later); the 30 test emails' labels must expect the agent's behaviour, not the stub's.

## 2026-11-14 · Guardrails on owner edits see rendered text, not placeholders (D63)
- **Symptom:** a stored draft version is the rendered text (prices visible); `{{QUOTE_BLOCK}}` and `{{REQUEST_SUMMARY}}` exist only before rendering, and only `{{HOLD_UNTIL}}` survives until approval. Guardrail 1 counts placeholders, so it cannot judge an owner's edit.
- **Cause:** the owner edits what the guest will read; the model writes text with placeholders (D8).
- **Evidence:** `test_exact_request_gives_a_held_quote_waiting_for_approval`: the body has "Our offer: ..." and no `{{QUOTE_BLOCK}}`.
- **Fix (brought forward from week 6, D63):** each offer stores its rendered block (`draft_offers.rendered_block`, migration 0010); an edit passes the price check only if every block appears verbatim, otherwise the version records the edited-price warning and approval needs `confirm_warnings` (D10). Chosen over re-rendering from the stored quote, which would have to match byte for byte and would misfire after any template change. Evidence: `test_edit_changing_a_price_warns`, `test_edit_deleting_the_block_warns`, `test_price_check_against_a_real_stub_draft`. Trade-off: harmless wording changes inside a block also warn; the owner confirms once. The other guardrails on edits (2–6) remain week 6.

## 2026-11-14 · An action on an expired draft returns, it does not raise (D54)
- **Symptom:** "a draft over 72 h is flipped to expired and the action is refused" needs two things at once: a refusal for the owner and a committed write (the flip).
- **Cause:** if the refusal were an exception, the caller's transaction would roll back and take the flip with it; the next action would flip it again, forever.
- **Evidence:** `test_expiry_flip_survives_the_refused_action` (committed_db): `reject_draft` past 72 h returns `DraftExpired`, the caller commits, a second connection sees draft and email `expired`.
- **Fix:** refusals that change nothing raise `TransitionRefused` (409); the expiry refusal is a return value `DraftExpired` (409 "expired" in the API) after the flip. Status is checked before age, so a decided draft is refused, not "expired". The audit log gets both facts: `draft.expired` (actor `system`, time did it) and `draft.action_refused` (actor `owner`, with the action). A `TransitionRefused` is rolled back, so it leaves no audit row. **Planned for step 16:** after the rollback, the API writes a `draft.action_refused` / `email.action_refused` row in its own short transaction, with a test. Exactly 72 h is not yet expired (states.md: "more than"). Trade-off: callers must handle a return value, not only exceptions; mypy forces it through the union type.

## 2026-11-14 · The approval transaction: three details D10 does not spell out
- **Symptom:** D10 lists the steps (lock, flip stale holds, re-validate, holds, outbox, audit, approved) but not how the sweep locks rows, what "re-validate" compares, or what a 2-room hold costs per booking.
- **Cause:** a design-level list, written before the schema existed.
- **Evidence:** `test_approval.py` (20 tests); removing the stale-hold flip, the price re-check or the savepoint each fails exactly one named test; `test_write_boundaries.py` proves only `approval.py` inserts bookings or outbox rows.
- **Fix:** (1) the stale-hold sweep locks rows `ORDER BY id` (`UPDATE … WHERE id IN (SELECT … FOR UPDATE)`), so two approvals sweeping the same holds lock them in the same order and cannot deadlock; (2) re-validation prices each offer again from a fresh `load_hotel_data` and compares every stored amount and nightly line, and checks availability with `rooms_for_plan` (D59), whose room numbers are then held; (3) a 2-room hold splits the accommodation total in whole cents, remainder on the lowest room. Trade-off: **the sweep touches every stale hold in the hotel**, not only the rooms in question (cheap, and the tidy-up job would do it anyway); on invalidation the flips stay, since those holds had expired regardless. Added after review (D64): an offer whose check-in moment (15:00 Athens) is before the approval time is invalidated (`NO_LONGER_AVAILABLE`); held rooms are inserted in ascending room order regardless of the caller, so concurrent multi-room approvals lock rooms in one order.

## 2026-11-14 · Two simultaneous holds on the same room deadlock under an exclusion constraint (D52)
- **Symptom:** the 2-room race (both approvals released together by a barrier) failed 4 times in 20 runs with `DeadlockDetected … while checking exclusion constraint on tuple … in relation "bookings"`, although both approvals insert rooms in ascending order.
- **Cause:** an exclusion constraint checks for conflicts *after* inserting the new row. Two transactions inserting a hold on room 101 at the same moment each find the other's uncommitted row and wait for it: a deadlock. Ordering prevents deadlocks across different rooms, not two inserts racing for the same first room. The deterministic version of the test passed because B started only after A had inserted.
- **Evidence:** without the fix, 5 of 40 repeated runs deadlock; with it, 100 of 100 pass (`--count=100`), and the 7 race tests pass 20 times in a row.
- **Fix:** before inserting holds, `pg_advisory_xact_lock(1, room_id)` per room in ascending order; the second approval waits before inserting, then its insert fails cleanly with an exclusion violation, which the savepoint turns into an invalidation (D52 updated). The two-key form is its own key space, so it cannot collide with the sender lock (D61). Lesson: a race test that passes once proves little; repeat it.

## 2026-11-15 · The six cross-rules as checkers, and what the coverage guard caught
- **Symptom:** on its first run the coverage guard reported statuses no row was left in: `processing` and `set_aside` (emails), `hold` (bookings).
- **Cause:** `processing` only lives inside the stub drafter's transaction and the one set-aside email was reopened at once; the "live" hold arrived 20 Aug, inside the 10–20 Aug 5-night rule, so its offer was the min-stay version and was never held (D9).
- **Evidence:** `emails: no row in ['processing', 'set_aside']` and `bookings: no row in ['hold']` from `test_the_scenario_reaches_every_status_the_database_allows`.
- **Fix:** the scenario keeps one email `set_aside` and one `processing` (raw SQL, standing in for the router and a run in flight) and the live hold moved to 24–27 Aug; the guard reads allowed statuses from the CHECK constraints in pg_catalog, so a new status fails it.
- **Trade-off:** rules 3 and 6 can only break if the code *and* the database fail (deferred foreign key, D51 trigger), so their checkers are shown failing with planted rows and a trigger switched off in a rolled-back transaction, not with a code mutation.

## 2026-11-15 · "The plan uses the index" is not proof the index expression is used (step 15)
- **Symptom:** the mutation that dropped `coalesce(next_attempt_at, created_at)` from the sender's claim query passed `test_the_claim_query_uses_the_due_index`.
- **Cause:** `outbox_pending_due_idx` is partial (`WHERE status = 'pending'`); with sequential scans off, the planner can use it just to find pending rows, so its name appears in the plan without its key being used.
- **Evidence:** `EXPLAIN` of the real query shows `Index Cond: (COALESCE(next_attempt_at, created_at) <= …)` under the index scan; the mutated query only filters. The tightened test fails on the mutation, passes on the real query.
- **Fix:** the test requires the index condition on the coalesce expression, not the index name; the claim query repeats the index expression exactly in WHERE and ORDER BY. Trade-off: the test reads plan text, so a Postgres version that formats plans differently would need the string updated.

## 2026-11-15 · LookupError means two different things, so the API maps only NotFound to 404
- **Symptom:** the planned mapping "LookupError → 404" would answer 404 for bugs.
- **Cause:** `owner_actions` raises LookupError for an unknown id, but `approval.py` also raises it for rows that must exist and do not ("draft has no version", "offers quotes but has no request").
- **Evidence:** `test_a_plain_lookup_error_is_a_bug_not_404`; the mutation that maps LookupError to 404 fails it.
- **Fix:** `states.NotFound(LookupError)` for "no such id" only (a subclass, so existing `pytest.raises(LookupError)` still pass); the API maps NotFound to 404 and everything unhandled to a logged 500.

## 2026-11-15 · The owner-hook test calls every route instead of walking `app.routes`
- **Symptom:** the planned test "every route in `app.routes` depends on `require_owner`" found no routes at all.
- **Cause:** FastAPI 0.163 keeps an included router as one private `_IncludedRouter` entry instead of copying its routes into `app.routes`.
- **Evidence:** `app.routes` lists the four documentation routes and one `_IncludedRouter`; walking it would rely on a private class.
- **Fix:** the test replaces `require_owner` with a dependency that answers 401 and calls every path in the app's OpenAPI description: all must answer 401, before any connection is opened. It is also the week-7 401 test in miniature. Trade-off: a route left out of the OpenAPI description (`include_in_schema=False`) would escape it.

## 2026-11-15 · The default inbox index is partial, so its query must repeat the predicate
- **Decision:** `emails_visible_received_at_id_idx` covers only `status <> 'set_aside'`; set-aside rows are never read for the default view.
- **Cost:** the planner uses a partial index only if it can prove the query's WHERE implies the index's, so `inbox_query` writes `e.status <> 'set_aside'` literally; dropping or rewording it silently falls back to another plan.
- **Evidence:** `test_a_second_inbox_page_seeks_in_its_index` requires the index *and* the `ROW(received_at, id) < ROW(...)` index condition on a page with a cursor; replacing the predicate with TRUE, or the row comparison with an OR of two comparisons, fails it.

## 2026-11-15 · "Commit instead of rollback on a refusal" cannot be caught by a refusal test
- **Symptom:** the planned mutation (commit instead of rollback when an action raises) passed every refusal test.
- **Cause:** every refusal (TransitionRefused, StaleVersion) is raised before the action writes anything, so committing an empty transaction changes nothing.
- **Fix:** the rollback is tested where it matters, an exception *after* a write: `approve_draft` flips stale holds (step 3) and then fails building a text with `{{HOLD_UNTIL}}` and nothing held (step 6). `test_a_bug_after_a_write_rolls_everything_back` requires 500 and the stale hold still `hold`; the mutation fails it.

## 2026-11-15 · The audit coverage guard and events only the API writes
- **Symptom:** after adding `email.action_refused`, `booking.action_refused` and `draft.price_warning_shown`, `test_the_scenario_performs_every_audited_transition` failed.
- **Cause:** the scenario calls the domain functions directly; these three are written only by the API after it rolled an action back.
- **Fix:** `audit.API_ONLY_EVENTS` names them; the guard excludes exactly that set, and `test_api_actions.py` covers them (the booking one with the Holds actions). A new domain event is still caught by the guard.

## 2026-11-15 · The API audit coverage guard skipped itself in exactly the run CI makes
- **Symptom:** `test_every_api_only_event_is_written_by_an_api_test` passed with all API files, but was *skipped* under `pytest -m integration`, the CI command.
- **Cause:** it skips unless every `test_api_*` test ran; `tests/unit/test_api_app.py` has the same prefix, and `-m integration` deselects it, so the run looked partial.
- **Evidence:** `SKIPPED … not every API test ran` in the full integration run; after the fix it runs and passes there, and both guard mutations (no test writes `booking.action_refused`; an untested event added to `API_ONLY_EVENTS`) fail under `-m integration`.
- **Fix:** only API test files in `tests/integration/` count, and with `CI=true` an incomplete run fails instead of skipping. Lesson: a guard that can skip must be shown running in the exact CI command.

## 2026-11-15 · Week 3 done: a mutation run is only valid if every mutation was undone
- **Symptom:** in step 16a, the first run of seven mutations reported plausible-looking failures that were wrong: they piled up from run to run.
- **Cause:** the mutations were undone with `git checkout`, which cannot restore files git does not track yet; the new API files were untracked, so each mutation stayed in place.
- **Evidence:** the failure counts only grew (9, 11, 12, … 15) and every later mutation also failed the status-filter tests; checking the source showed all seven mutations still in it. Re-run from a clean source, each mutation failed only the test meant for it.
- **Fix:** 16a's re-run restored from backup copies; from 16b on, new files are `git add`-ed first, each mutation is undone with `git checkout`, `git diff` must show exactly the one changed line, and the tree must be clean at the end.

## 2026-11-16 · A claim held across the model call deadlocks the run against itself
- **Symptom:** the mutation that kept the claim's transaction open across the model call did not fail the extraction tests; it hung until the 300 s timeout.
- **Cause:** the model call's audit row is written on its own connection with `email_id` as a foreign key; checking that key needs a share lock on the email row, which the leaked claim still holds `FOR UPDATE`. The run waits on itself. (In production it would also block every other run for that sender, D61.)
- **Evidence:** with a 5 s `lock_timeout` on the run's test connections, the mutation fails `test_no_lock_is_held_while_the_model_works` and the gold-case test in 12 s.
- **Fix:** the code already commits the claim before the call (D65); every connection the app opens (`db.connection.connect`: API, runs, migrations, the test harness) now sets `lock_timeout` 5 s and `statement_timeout` 30 s, so a leaked lock fails the next run fast instead of hanging it, in production as in CI.

## 2026-11-16 · The SDK's `responses.parse` hides why a reply was cut off
- **Symptom:** a reply stopped at `max_output_tokens` (status `incomplete`) was recorded as "unreadable reply" and billed at the reserved maximum.
- **Cause:** `responses.parse` parses the partial JSON before returning, so the error is a JSON validation error and the response, with its `status`, `incomplete_details` and `usage`, never reaches us.
- **Evidence:** `test_a_reply_cut_off_at_the_output_limit_escalates_and_stores_nothing`, through `process_email` against a simulated endpoint.
- **Fix:** the client calls `responses.create` with the same strict schema (the SDK's own converter) and parses the reply itself: usage first, then status, then refusal, then the JSON. A cut-off reply is now `reply incomplete: max_output_tokens`, billed at its real usage; the reserved maximum stays only for a reply with no readable usage.

## 2026-11-16 · OpenAI's strict mode refused the extraction schema (`oneOf`)
- **Symptom:** the first real smoke run (gpt-6-luna) failed all 15 calls with HTTP 400 "Invalid schema for response_format 'ExtractionOutput': 'oneOf' is not permitted"; nothing was spent.
- **Cause:** `date_spec` was a pydantic discriminated union (`Field(discriminator="kind")`), which pydantic writes as `oneOf`; strict structured outputs accept `anyOf` only. The tests used a simulated endpoint that accepts any schema.
- **Fix:** a plain union (`anyOf`; each date kind has its own fixed `kind`, so one variant matches). A unit test walks the schema as sent and checks OpenAI's strict rules: no `oneOf`, `additionalProperties: false` on every object, every property required. `prompt_version` now hashes the prompt file *and* the schema as sent (`extraction_v1@21acca18`), so a schema change is a new version too.

## 2026-11-16 · The router and `build_offer` ordered two room escalations differently
- **Finding:** with an accessibility need and fewer adults than rooms, both `UNACCOMPANIED_CHILDREN` and `ACCESSIBILITY` apply. D70 puts `UNACCOMPANIED_CHILDREN` first; `build_offer` checks the need first and returns `ACCESSIBILITY`.
- **Evidence:** `test_room_rules_agree_with_build_offer` compares the two over 27 parties × rooms 1–3 × three room types × need yes/no; they agreed everywhere except this corner.
- **Fix:** `build_offer` now follows D70: with an accessibility need it checks an adult in every room asked for before the Accessible plan, so both return `UNACCOMPANIED_CHILDREN`. The agreement test has no skip any more; `test_accessibility_need_checks_adults_per_room_first` covers it in `test_offer.py`. Removing the new check fails both (2 and 13 tests). The pricing-example tests are unchanged and pass.

## 2026-11-16 · Window requests (Easter, part of a month) could only ever be asked about
- **Symptom:** en-easter-plain and el-approximate-late-june, labelled `quote`, drafted as `ask_info` ("the dates of your stay"); en-western-easter-closed, labelled `decline`, the same.
- **Cause:** extraction stores no stay for a window (`requests.stay` NULL, the DateSpec in `extracted`), and `load_request` read a NULL stay as "no dates". `options.window_options` existed and was unit-tested, but no tool called it.
- **Fix:** `load_request` resolves the stored DateSpec again at the email's `received_at` (the same code as at storing, so the same window; no migration; parsed by `datespec.date_spec_from_json`, so `reservations/` imports nothing from `anemos.extraction`, checked by `test_import_direction.py`) and returns a `LoadedWindow`. `quote_price` hands it to `quote_window`: NO_CALENDAR, then CLOSED if every candidate night is closed, then up to 3 options (calendar, not SHORT_NOTICE, priced, plan free), stored with `window_option` (and `min_stay` for the D7 fallback), never held. No option: a party escalation, else NONE_FOUND if a stay was taken, else SHORT_NOTICE if every stay was too soon, else CLOSED. The stub drafter writes the options as a quote and approval re-validates them like any offer. A stored DateSpec that does not parse raises (a bug in what we stored); the integration fixture's placeholder `{"kind": "exact"}` became `null`.
- **Evidence:** `tests/integration/test_window_requests.py` (17 tests: each outcome above, the three gold cases through `store_extraction` and the stub drafter, and approving a window draft: no booking, one outbox row). Mutations caught: window ignored (12 tests), `min_stay` not recorded (1), short-notice check-ins not skipped (1).
- **Trade-offs:** a window whose plan's room type is taken at every check-in is declined (NONE_FOUND, now carrying that type); other room types are not offered (D60's search is for one stay; spec Later). The general decline text claimed "the same stay up to a week earlier or later is not available either", untrue for a window, which is never shifted: a window decline has its own text naming only that type (`test_window_fully_taken_declines_naming_only_that_room_type`). SHORT_NOTICE inside a window uses the clock, as `_other_dates` does, not `received_at`.

## 2026-11-16 · A window request's summary always said "1 room" and dropped the room type
- **Symptom:** a window request for 2 Sea View Double rooms was summarised as "Your request: 1 room, check-in between …", although the quote below it offered 2 Sea View rooms. `{{REQUEST_SUMMARY}}` is how the guest checks what we understood (guardrail 3), so it must restate the rooms and type too.
- **Cause:** `render_request_summary` took a bare `Window` for window requests, which has no rooms or type, and hard-coded one room.
- **Fix:** it takes a `WindowRequest` (window, rooms, room type) and shares the "what" part with `RoomRequest`; a bare `Window` no longer type-checks there. Tests: `test_summary_for_a_two_room_window_names_the_rooms_and_type` (EN and EL) and `test_two_room_window_summary_names_the_rooms_and_type` (through the stub drafter); dropping the rooms and type in the stub, or the type in the summary, fails one each.

## 2026-11-16 · D68: a code-side list of holiday spellings read New Year's Eve a year early
- **Symptom:** to catch the model's spellings, code first normalised holiday names and mapped variants ("Xmas", "New Year's Eve") to `christmas` / `new_year`. Then "New Year's Eve 2027" became `new_year` 2027, which D68 reads as 31 Dec 2026 – 1 Jan 2027: a year early, and already past.
- **Cause:** the year means different things for the two names (the guest's "New Year's Eve 2027" is in 2027; `new_year` 2027 is the one ending on 1 Jan 2027), and a name map in code cannot fix the year.
- **Evidence:** `resolve(HolidayDates("New Year's Eve", year=2027), …)` returned `FixedHoliday('new_year', 2026-12-31, 2027-01-01)` before the fix.
- **Fix:** the alias list is gone. Prompt `extraction_v2` makes the model write canonical names (`easter`, `christmas`, `new_year`, with New Year's year the year of 1 January) and code matches them exactly; anything else, "Easter" with a capital too, is NEEDS_INFO (asks for dates, never a guess). Tests: `test_new_years_eve_2027_as_extracted_by_v2` (new_year 2028 → 31 Dec 2027 – 1 Jan 2028), `test_only_canonical_names_are_known`, and checks that the prompt states the names and every gold holiday label uses them. "New Year 2028" = 31 Dec 2027 – 1 Jan 2028 is recorded in D68.
- **Mutations caught:** CLOSED checked before NO_CALENDAR for holidays (2 tests); names matched case-insensitively (`test_only_canonical_names_are_known[Christmas]`).

## 2026-11-16 · The extraction scorer counted `rooms` null vs 1 as a miss
- **Symptom:** the v2 smoke run scored `rooms` 14/15 and el-approximate-late-june inconsistent: one repeat returned `rooms` null, two returned 1 (gold 1).
- **Cause:** a definition gap, not a model weakness. The email names "a standard double" without a number; null means "not stated", which the code reads as one room (D5), so the draft is the same. The scorer compared raw values.
- **Fix:** `rooms` is compared after the D5 default, in the score and in the repeat check, as `stay` is compared after resolving. The default now has one definition, `model.rooms_or_default`, used by the tools, the router and the scorer. `eval/rescore.py` scores a results file again from its recorded outputs (no calls), writing `<file>.rescored.json` and never touching the original. The v2 run re-scored: every field 100%, 5/5 consistent (D2 records both).
- **Evidence:** `test_rooms_are_scored_after_the_d5_default` (null and 1 equal, 2 not), `test_null_and_one_room_are_the_same_answer_across_repeats`, `test_rescore_uses_todays_scorer_without_a_model_call`. Mutations caught: raw comparison in the score (2 tests), in the repeat check (1).

## 2026-11-16 · Routing wired into the run: the gold run isolates cases, superseding is tested on purpose
- **Gold run isolation:** each eval case is one email on its own (docs/email-format.md), so each runs on an emptied database (committed_db truncates every operational table, bookings included, before each test; this was already so), with only its own `world` bookings, then the run. It is now checked, not assumed: the run asserts no emails, bookings, drafts or quotes are left before a case starts, so one case's world (en-fully-booked-decline blocks every Standard room 25 Aug - 20 Sep) cannot change another's result. The comparison now includes holds as well as route and codes: 36 exact (en-quote-sea-view-june: quote, 1 hold), 7 week 5.
- **Superseding (D48) on purpose:** two explicit tests with two emails from one guest: storing a newer email supersedes the older pending draft; with both stored before either runs, the older draft is born superseded. An earlier draft of the `process_inbox` test used en-quote-sea-view-june and en-thread-reply, which are from the same guest, so one superseded the other as a side effect; that test now uses three different guests and says so.
- **A test that only counted runs passed on errors:** the first `process_inbox` test gave the scripted client a step with the wrong signature; every call raised inside the model call (escalated `SYSTEM_ERROR`) and the test still passed, because it only checked each email had a run result. mypy flagged the step; the test now checks every email's final outcome.
- **What changed:** `process_email` escalates thread replies in the claim transaction (no model call); its final transaction calls `run.route_and_draft` (hotel data read in that transaction, router decision audited as `email.routed`, then set aside / escalate with every code / stub drafter via `draft_claimed`). `process_inbox` runs every received email oldest first. The demo inbox goes through the same run, with a scripted client answering the gold labels (audited as `gold-labels`, cost 0, throwaway ledger); its plain-SQL stand-ins are gone.
- **Mutations caught:** no thread screen (3 tests), only the first code stored (1), routing skipped (10+), stub never holding (10), never born superseded (1), storing a newer email superseding nothing (1), demo skipping the run (1).

