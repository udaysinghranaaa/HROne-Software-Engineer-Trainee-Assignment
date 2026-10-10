# REVIEW.md

## Current status: Phase 4 implementation

The earlier BLOCKED results below are historical. The saved `tests/phase3_final_results.json` now records Phase 3 PASS: 11 live checks, 46 HTTP requests, 2.485-second startup, employee and punch-in races passed, six employees/nine attendance logs unchanged, child stopped and owned database cleanup PASS. That file was supplied by the user's successful manual run and was not regenerated during Phase 4.

Phase 4 adds `POST /attendance/punch-out` and `PATCH /attendance/{emp_code}/{date}`. Punch-out finds the latest punch-in at or before the truncated instant, including overnight records; returns 404 when none exists, 409 for a closed/conflicting record and 422 for equal times or durations over 24 hours. It preserves manual history, status and existing lateness.

PATCH validates the exact natural-key date, required reason/actor, permitted status and strict bounded millisecond timestamps. Presence requires a punch-in on the original R1 attendance day; absence clears both times. Corrections recalculate R2-R5 and append one `{at, by, reason, changes}` entry containing only actual changes, including derived values. No-op corrections are 422. Missing history/half_day retain their legacy defaults.

Two source ambiguities were raised before completing the APIs. The user's explicit decisions are: PATCH rejects derived/unsupported fields with 422; existing Phase 3 field-ignore behavior remains. Punch-out before every known punch-in returns 404 under the documented candidate selector. PATCH codes follow the plain-string path schema, not the employee-creation regex.

Both persistence operations use natural-key/snapshot conditional `find_one_and_update`. A competing change yields 409. PATCH's `$set` and `$push` are one atomic document operation, so corrected fields cannot be saved without their required history entry. No automatic retries, process locks, extra revision fields or transactions are introduced. Previous history and unrelated fields are preserved.

The original four indexes remain; the fifth, `attendance_latest_punch`, supports employee-scoped latest-punch selection. Startup stays idempotent. Whole-second UTC BSON storage, IST dates/overnight shift bounds, 10-minute strict grace, floor minutes, 30-minute overtime, decimal half-up hours and rounded 4.50-hour half-day logic reuse Phase 3 helpers.

Executed isolated verification: `.venv/Scripts/python.exe -B -m unittest discover -s tests -v` ran **86 tests: 86 passed, 0 failed, 0 skipped** (35 original application, 23 original harness, 28 Phase 4). Tests cover real parallel ASGI calls with atomic fake MongoDB writes, competing punch-out/PATCH, exact audits, database failures, legacy records, time boundaries and OpenAPI request/response checks. The old exact route/index assertions now require seven implemented operations and five indexes; their existing contract checks were retained. AST syntax checks passed for application and every test script.

`tests/final_phase4_verification.py` is prepared for manual execution only. It shares the existing harness safety workflow, uses a fresh 35-byte `hrone_p4v_` database, checks collisions and acknowledged ownership, verifies child PID/database/token and its bound localhost port, checks real HTTP races and BSON/audit values, compares development snapshots, then stops its server and cleans up only after exact ownership verification. Phase 4 output uses `tests/phase4_final_results.json`; the Phase 3 evidence file is preserved.

**Phase 4 live verification: NOT EXECUTED.** No Atlas connection, record/index writes, deletes, permission changes or seed execution occurred during this implementation. Original API contract, problem statement and sample/seed files are unchanged. Live MongoDB concurrency, query plans, startup timing with the fifth index and 100k-record performance remain unmeasured. No Phase 5 analytics or Phase 6 admin API was implemented.

Manual command from `candidate_kit`: `.venv/Scripts/python.exe -B tests/final_phase4_verification.py`.

## Historical Phase 2/3 review and verification

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| D01 | `health` (original lines 72-74) | No database ping: an outage falsely reports readiness. | `test_health_ping_and_outage_D01`: healthy mock gives 200; simulated outage gives 503 with sanitized detail. | Added bounded real ping and controlled 503. Isolated tests passed. A subsequent read-only Atlas ping passed; the latest dedicated HTTP verification is BLOCKED at DNS preflight. |
| D02 | `EmployeeIn` (53-60) | Missing patterns, lengths and real-date validation permit invalid stored employees. | `test_employee_invalid_fields_D02`, `test_employee_required_and_valid_boundaries_D02`: invalid cases 422; valid bounds/defaults 201. | Added exact contract constraints and calendar-date validator. |
| D03 | `EmployeeIn` (58-59) | No cross-field check permits equal shift times on creation. | `test_equal_shifts_D03`: equal shifts return 422. | Added Pydantic model validator requiring different shifts. |
| D04 | `create_employee` (79-83) | Check-then-insert has a race; no unique index/duplicate-key handling. Duplicate identities can be created. | `test_employee_duplicate_D04`, `test_employee_concurrency_D04`: unique-index fake yields one 201, remaining request 409, one stored document. | Removed check-then-insert; catch `DuplicateKeyError`; startup declares unique employee index. **PARTIALLY_FIXED operationally:** isolated tests passed; live index creation and live concurrency are BLOCKED. |
| D05 | `create_employee`, `punch_in` (82,102) | Host-local naive times are interpreted as UTC by PyMongo, corrupting instants. | `test_employee_create_serialization_utc_D05_D06`, `test_ist_date_and_bson_instant_D05_D14`, `test_timestamp_omitted_uses_utc_D05_D12`: exact UTC values, BSON round-trip and frozen UTC clock pass. | Use aware UTC now/conversion; configure timezone-aware PyMongo; interpret legacy naive reads as UTC. |
| D06 | Employee/attendance responses (85,96,121,150) | Default datetime JSON serialization returns strings instead of integer milliseconds, including history. | `test_employee_create_serialization_utc_D05_D06`, `test_history_legacy_no_mutation_D06_D19_D23`: exact fields/types and nested before/after milliseconds pass. | Added explicit boundary serializers and typed response models. |
| D07 | `list_employees` (93) | One extra page is skipped, so the six-employee default first page is empty. | `test_employee_first_page_sort_D07_D09`: six first-page employees; page 2/size 2 returns EMP0003/EMP0004. | Offset is now `(page - 1) * page_size`. |
| D08 | `list_employees` (94) | Unfiltered count makes department pagination totals incorrect. | `test_department_filtered_count_D08`: Sales total 2 with one returned item; identical count/find filters asserted. | Count with the same filter as the listing. |
| D09 | `list_employees` (95) | Missing sort makes order depend on storage order. | `test_employee_first_page_sort_D07_D09`: reversed fixture still returns employee codes ascending. | MongoDB sorts by `emp_code` before skip/limit. |
| D10 | List query parameters (89,130-131) | No bounds permit zero/negative pages and oversized pages. | `test_pagination_validation_D10`: invalid values 422; size 100 accepted; distant page empty. | Added query bounds: page >= 1 and page size 1-100. |
| D11 | `punch_in` (101,113) | Missing employee is dereferenced, producing 500. | `test_unknown_employee_D11`: unknown formatted and unformatted string codes return 404. | Guard employee lookup before shift access. |
| D12 | `PunchInIn`, `punch_in` (65,102) | Coercion and missing bounds accept seconds, floats, strings, booleans or explicit null. | `test_timestamp_strict_bounds_D12`, `test_timestamp_omitted_uses_utc_D05_D12`: invalid inputs 422; both inclusive bounds accepted; omitted value uses UTC clock. | Strict bounded integer type; default factory supplies omitted timestamp while explicit null remains invalid. |
| D13 | `PunchInIn` (66) | Free-form status allows absent/leave records with punch times. | `test_presence_statuses_D13`: only PRESENT/WFH/ON_DUTY accepted; invalid variants 422. | Presence-status Literal with documented default. |
| D14 | `punch_in` (102-103) | Host-local calendar date can differ from the required IST day. | `test_ist_date_and_bson_instant_D05_D14`: UTC July 19 20:00 maps to IST July 20 without changing stored instant. | Attendance date explicitly derived in IST. |
| D15 | `punch_in` (102,110) | Milliseconds retained, violating whole-second calculation/storage convention. | `test_fractional_truncation_D15`: 09:40:00.900 becomes 09:40:00, lateness 0, stored microseconds 0. | Truncate incoming/current instants before calculation and storage. |
| D16 | Attendance date/late anchor (35,103,113) | Overnight punch uses current day/start, giving wrong natural key and lateness. | `test_overnight_date_and_late_D16`: 00:30 belongs to previous day and is 150 minutes late; strict 06:00 boundary and seeded equal shifts tested. | Added `attendance_date` and explicit attendance-day anchoring in late calculation. |
| D17 | `compute_late_minutes` (36-37) | Flooring before comparison incorrectly grants nearly another minute of grace. | `test_grace_boundary_D17`: 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45. | Compare elapsed seconds strictly > 600, then floor minutes from shift start. |
| D18 | `punch_in` (104-118) | Check-then-insert race can create two records for the same employee/day. | `test_punch_defaults_and_duplicate_D18_D19`, `test_punch_concurrency_D18`: duplicate 409; simultaneous overnight punches give [201,409] and one fake record. | Unique natural-key index declared; insert directly and map duplicate key to 409. **PARTIALLY_FIXED operationally:** live unique-index creation and race verification BLOCKED. |
| D19 | Attendance responses (119,149) | Internal ObjectId is exposed as undocumented public `id`. | `test_punch_defaults_and_duplicate_D18_D19`, `test_history_legacy_no_mutation_D06_D19_D23`: exact ten-field response, no id/_id. | Build an explicit response copy; never generate a public attendance ID. |
| D20 | `list_attendance` (127-143) | Invalid date/status and reversed ranges silently produce misleading results. | `test_attendance_query_validation_D20`: malformed/impossible dates, invalid status and reversed range return default-shaped 422 detail array. | Calendar/status validation plus cross-query range validation. |
| D21 | `list_attendance` (144-147) | Materializing the full matching result makes memory/time grow with collection size. | `test_attendance_filters_pagination_D21`: inclusive range, filtered total, cursor skip/limit and one yielded item asserted; fake rejects iteration without a limit. | Count separately; MongoDB sorts/skips/limits before page iteration. 100k benchmark not executed. |
| D22 | `list_attendance` (145) | Date-only sort misses employee-code tie-break. | `test_attendance_sort_D22`: shuffled records sorted date descending, then code ascending; cursor sort asserted. | Compound MongoDB sort. |
| D23 | `list_attendance` (148-150) | Legacy missing history/half-day causes required response fields to be absent. | `test_history_legacy_no_mutation_D06_D19_D23`: legacy defaults present; source documents byte-equivalent in memory after serialization. | Normalize response copies to history [], half_day false, and other documented missing/null defaults. |
| D24 | `compute_work_hours` (41) | Built-in float rounding violates half-up and can affect half-day classification. | `test_half_up_hours_and_half_day_D24`: 54 seconds -> 0.02; 16181 seconds -> 4.49/true; 16182 -> 4.50/false; open record null. | Exact whole-second duration, Decimal ROUND_HALF_UP, half-day helper based on rounded hours. |
| D25 | `compute_overtime` (46-47) | Under-30-minute overtime counted, overstating overtime. | `test_overtime_threshold_D25`: 5 minutes and 29:59 -> 0; 30:00 -> 30. | Floor elapsed minutes, then apply minimum 30. |
| D26 | `compute_overtime` (46) | Overnight end anchored to same date, overstating overtime by a day. | `test_overnight_overtime_and_naive_utc_D26`: next-day 06:40 -> 40; aware and naive UTC inputs agree. | Shared `shift_bounds` rolls overnight end forward. Helper now also receives shift start, which is required to distinguish overnight shifts. |
| D27 | Application startup (original 22-27) | No indexes created: uniqueness and query support absent. | `test_indexes_idempotent_D27`, `test_duplicate_audit_stops_before_index_changes_D27`, lifespan tests: idempotence, both duplicate audits before writes, startup initialization/cleanup, fail-safe outage passed. | Lifespan declares four query/uniqueness indexes, audits natural keys, applies bounded setup and closes client. Four application indexes were subsequently confirmed by a read-only check. Fresh dedicated-database setup/cold timing is currently BLOCKED by DNS. |

Also note anything you looked at and decided was **not** a defect, and why.

## Non-defects and contract interpretation

- `load_dotenv()` preserves real environment-variable priority by default; retained.
- Synchronous PyMongo with synchronous FastAPI routes is appropriate; retained.
- Employee shift defaults and punch-in open-record defaults were correct; retained and tested.
- Inclusive `$gte`/`$lte` date filters were correct; retained and tested.
- Derived request fields must be ignored under the global contract; default Pydantic extra-field ignoring is retained and tested, rather than globally forbidding extras.
- PunchInRequest defines `emp_code` as a plain string. The employee-creation pattern is not imposed on punch-in; unknown codes return 404.
- Attendance has no public ID. BSON ObjectId remains correct internally.
- No employee-email uniqueness, joining-date punch restriction, authentication or holiday rule is added.
- The OpenAPI version emitted by FastAPI differs from the supplied YAML version; the YAML is unchanged, and required route/field behavior is preserved.

## Historical Phase 3 verification and limitations

On 2026-10-09, `.venv/Scripts/python.exe -B -W error -m unittest discover -s tests -v` ran **35 tests: 35 passed, 0 failed, 0 skipped**. These are isolated helper/model/ASGI/lifecycle tests using unique-index-enforcing, thread-safe test doubles. They never connect to Atlas, load `.env`, or write live records. Race tests assert actual HTTP statuses and stored fake document counts. They are not live MongoDB race tests.

Python AST syntax checks passed for application and test scripts. `pip check` reported no broken requirements. The protected contract/data/seed/decisions/requirements files remain unchanged.

Initial Phase 3 live checks were **BLOCKED** by DNS/TLS failures, including `TLSV1_ALERT_INTERNAL_ERROR`. A later read-only retry outside the sandbox succeeded: Atlas ping, six employees, nine attendance logs and all four application indexes were verified. This read-only evidence did not establish real HTTP concurrency or cold startup timing. The latest dedicated verification outcome is recorded below.

`tests/live_phase3.py` is an opt-in read-only/metadata verification script. Its default mode only reads; `--create-indexes` invokes the real startup audit/index routine, repeats setup, checks response schemas, and compares BSON record-content snapshots. It contains no record insert/update/delete operations. It must stop if duplicate keys or a safety concern appears; it must never repair records automatically.

R1-R5 and R10 are covered for the existing routes/shared helpers; R6 presence-status validation is covered. R8 half-up work-hour rounding is covered. Analytics-specific R7/R9 and rate/ranking/window behavior are deferred to Phase 4. The seven pending APIs are still unimplemented, and DECISIONS.md remains unchanged. Actual MongoDB 7.0 integration and 100k-record performance are not executed.

Phase 3 implementation and isolated tests are complete. Final dedicated-database verification is **BLOCKED** as detailed below. Review this draft in your own words before final submission.

## Historical Phase 3 dedicated-database verification - 2026-10-09 (IST)

The command `.venv/Scripts/python.exe -B tests/final_phase3_verification.py` was attempted once inside the sandbox and twice outside it. All three attempts failed at the initial MongoDB ping with `ConfigurationError`, categorized safely as DNS / resolution lifetime expired / timed out. No raw exception text or credentials were printed.

**Actual execution:** 0 HTTP requests; no dedicated database created; no server process launched; no MongoDB record or index writes/deletes; cleanup NOT_NEEDED. Cold startup duration is **NOT MEASURED**, not a passing result. Current live concurrency, five-API integration and fresh-test-database indexes are **BLOCKED**. Existing attendance_db snapshots were not obtained during these failed attempts, so a current before/after content comparison is not claimed.

The new verification harness is prepared, not live-validated. It generates a unique `hrone_phase3_verify_YYYYMMDD_<16-hex>` database; refuses existing/generated-name collisions; writes an ownership marker; injects that exact name through MONGO_DB into a fresh child process; verifies the ready marker's PID/database/token before HTTP requests; synchronizes two client threads for each race; validates JSON responses against the original OpenAPI; checks stored BSON data, legacy/history serialization, pagination and indexes; then stops only its process and drops only its database after rechecking the exact name and ownership marker. No production/development seed script is used.

35 isolated tests were re-executed: **35 passed, 0 failed, 0 skipped**. Additional harness checks passed: valid database identity plus four unsafe-identity refusal cases; valid response schema plus two invalid-response refusal cases; all application/test AST syntax checks. These local checks do not substitute for the blocked HTTP/MongoDB run.

Results are saved in `tests/phase3_final_results.json`. PASS below means the applicable implementation/helper evidence passed; PARTIAL means isolated evidence passed but the requested current HTTP/MongoDB verification is blocked. No failure in application behavior was established by the connection preflight failure.

| Defect | Fix implementation | Isolated result | Latest live integration | Final status |
|---|---|---|---|---|
| D01 | Bounded ping and controlled 503 | PASS | Prior read-only ping PASS; dedicated HTTP check BLOCKED | PARTIAL |
| D02 | Employee field/calendar constraints | PASS | Invalid/valid employee HTTP checks BLOCKED | PARTIAL |
| D03 | Different-shift validator | PASS | Equal-shift HTTP check BLOCKED | PARTIAL |
| D04 | Unique employee index and duplicate-key 409 | PASS | Prior index existence PASS; real simultaneous HTTP creates BLOCKED | PARTIAL |
| D05 | UTC creation/punch storage, aware reads | PASS | Dedicated stored-value checks BLOCKED | PARTIAL |
| D06 | Top-level/nested epoch serializers | PASS | Dedicated HTTP/BSON comparison BLOCKED | PARTIAL |
| D07 | Correct employee offset | PASS | Dedicated employee pagination BLOCKED | PARTIAL |
| D08 | Filtered employee count | PASS | Dedicated department totals BLOCKED | PARTIAL |
| D09 | Employee-code DB sort | PASS | Dedicated sorted listing BLOCKED | PARTIAL |
| D10 | List pagination bounds | PASS | Dedicated invalid-query HTTP checks BLOCKED | PARTIAL |
| D11 | Missing-employee 404 guard | PASS | Dedicated unknown-employee HTTP check BLOCKED | PARTIAL |
| D12 | Strict bounded timestamps and omitted default | PASS | Dedicated timestamp HTTP checks BLOCKED | PARTIAL |
| D13 | Presence-status enum | PASS | Dedicated status HTTP checks BLOCKED | PARTIAL |
| D14 | Explicit IST attendance date | PASS | Dedicated instant/date checks BLOCKED | PARTIAL |
| D15 | Whole-second truncation | PASS | Dedicated stored/returned truncation BLOCKED | PARTIAL |
| D16 | Overnight attendance-date/start anchor | PASS | Dedicated overnight HTTP check BLOCKED | PARTIAL |
| D17 | Strict seconds threshold before minute floor | PASS | Dedicated grace-boundary HTTP check BLOCKED | PARTIAL |
| D18 | Unique attendance key and duplicate-key 409 | PASS | Prior index existence PASS; real simultaneous punches BLOCKED | PARTIAL |
| D19 | Exclude public id/_id | PASS | Dedicated exact-field checks BLOCKED | PARTIAL |
| D20 | Date/status/range validation | PASS | Dedicated invalid-filter HTTP checks BLOCKED | PARTIAL |
| D21 | DB count/sort/skip/limit | PASS | Dedicated pagination BLOCKED; 100k benchmark NOT EXECUTED | PARTIAL |
| D22 | Date-descending/code-ascending DB sort | PASS | Dedicated tie-order checks BLOCKED | PARTIAL |
| D23 | Response-only legacy defaults | PASS | Dedicated legacy/BSON preservation BLOCKED | PARTIAL |
| D24 | Decimal half-up and rounded half-day helper | PASS | N/A: standalone helper; no punch-out API in this phase | PASS |
| D25 | Minimum 30 whole overtime minutes | PASS | N/A: standalone helper; no punch-out API in this phase | PASS |
| D26 | Next-day overnight shift end | PASS | N/A: standalone helper; no punch-out API in this phase | PASS |
| D27 | Startup audit and idempotent indexes | PASS | Prior four-index existence PASS; fresh startup/timing BLOCKED | PARTIAL |

Phase 4 was not started. Complete the prepared dedicated verification where Atlas DNS/network access works before final Phase 3 verification sign-off. No application code, credentials, Atlas settings, original contract, problem statement or existing development records were changed by this final verification task.

## Current Phase 5 verification - 2026-10-10 (IST)

This section supersedes the historical phase-status statements above. The saved `tests/phase4_final_results.json` records PASS for 16 checks, 68 HTTP requests, 3.627-second cold startup, five indexes, unchanged development data (6 employees/9 attendance logs), stopped child and successful owned-database cleanup. Those results are from the previous manual run, not a Phase 5 run.

Four GET analytics operations are implemented in `app/main.py` against the unchanged original YAML:

| API | MongoDB design and verified isolated behavior |
|---|---|
| Employee monthly | Employee anchor and bounded grouped attendance lookup; database calendar counts weekday working days from joining; weekday presence excludes pre-join logs; legacy half-day defaults, zero months, leap months and null zero-denominator percentage covered. |
| Department summary | Eligible employees anchor zero-log headcount; grouped monthly lookups feed department totals; record-weighted presence-status/non-null-hours average, weekday presence, joining eligibility and sorted/exact department filters covered. |
| Late leaderboard | Month/positive-late match, employee grouping/join, department filtering before rank, competition ranking and rank cutoff; all ties at cutoff retained, orphan codes excluded, deterministic code ordering covered. |
| Department trend | Database calendar with inclusive 1-92 dates, bounded daily headcount/attendance lookups, null weekend/zero-headcount rates, zero weekday gaps and seven-row null-ignoring window; changing headcount and requested-range-only window covered. |

R7 clarification: exclude pre-join records from monthly and department present_days. Other metrics retain their stated contract filters. Trend present_count follows its separate daily status/half-day rule, including weekend records; weekends still have null attendance_rate. Stored late/overtime/hours are read without recalculation or audit changes. Explicit decimal half-up replaces MongoDB ties-to-even rounding: two decimal places for hours/percentages, four for rates/windows. Legacy missing half_day is false and missing minute totals are zero.

Seven application indexes are declared. The five existing definitions are preserved. Phase 5 adds `employee_joined_department` (joined_on, department) for global month eligibility and `employee_department_joined` (department, joined_on) for daily department headcount. Attendance lookups reuse existing natural-key/date indexes. Index creation remains idempotent; no analytics reports execute at startup. Actual plans, large-dataset performance and seven-index cold startup are not measured yet.

Executed: `.venv/Scripts/python.exe -B -m unittest discover -s tests -v`: **123 passed, 0 failed** (92 prior tests plus 31 Phase 5 tests). The new tests execute production pipelines in a strict test-only aggregation interpreter and compare responses with independently calculated fixture expectations. They cover half-up ties, missing fields, zeros, rank ties/limits, dates, ranges, query validation, exact YAML response schemas, registered operations, ownership rejection, test database naming and the complete Phase 5 callback using fake storage/HTTP. Existing Phase 3/4 regression tests pass; route/index-count assertions were extended for the new operations/indexes while preserving old definitions.

These are isolated tests, not MongoDB server integration. The interpreter is not a replacement for MongoDB and does not establish real server operator compatibility, query plans, Decimal128 extreme-value behavior, HTTP concurrency or startup timing. Live verification remains **NOT EXECUTED**; no Phase 5 PASS artifact is fabricated. The prepared manual harness uses independent expectations and BSON snapshots on its strictly owned temporary database, sharing child PID/database/token/port checks, name length validation, collision checks and exact ownership-token cleanup protection. It does not use the development seed script.

Final quality checks passed: AST syntax validation for 11 Python files; imports and route/schema assertions exercised by the isolated suite; `pip check` (no broken requirements); `git diff --check`; additional whitespace checks for new files; credential-URI exposure checks; six SHA-256 comparisons confirming the original contract, problem statement, data model, seed script and sample datasets are unchanged. No Phase 5 live result file exists. Git emitted only Windows line-ending normalization notices.

No live database operations were performed during Phase 5 implementation or isolated testing. Existing development data, Atlas settings, original assignment/contract/model and sample datasets were not modified. No Phase 6 endpoint, Dockerfile or Git push was added. Run the manual command in README to obtain live evidence before Phase 5 integration sign-off.
