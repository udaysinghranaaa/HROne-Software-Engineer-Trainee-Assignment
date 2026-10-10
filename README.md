# Employee Attendance & Analytics API

A FastAPI and MongoDB backend for the HROne Software Engineer Trainee assignment. Employees can punch in and out, managers can correct attendance with an audit trail, and MongoDB aggregation reports provide monthly summaries, department totals, late rankings and daily trends. The Admin Explain API exposes real query execution plans for the five contract-supported read operations.

**Status:** Phases 1–6 complete; all 12 required API operations implemented. The Phase 7 isolated suite passed **174 tests**, and Phase 6 live integration passed **14 checks / 62 HTTP requests**. Phase 7 QA implementation is complete with live verification pending; Phases 8–10 remain pending. **100,000-record performance and grader-scale query-plan acceptance are not verified.**

## Specification and review documents

Read the original candidate-kit documents in this order:

1. [PROBLEM_STATEMENT.docx](PROBLEM_STATEMENT.docx): assignment, evaluation and submission requirements.
2. [openapi.yaml](openapi.yaml): authoritative HTTP contract and rules R1–R10.
3. [DATA_MODEL.md](DATA_MODEL.md): MongoDB collections, BSON types and sample document shapes.
4. [app/main.py](app/main.py): all application code, including startup and index initialization.
5. [REVIEW.md](REVIEW.md) and [DECISIONS.md](DECISIONS.md): defect analysis, fixes, test evidence and implementation choices. Earlier preparation-only statements in those documents describe historical sessions; the saved live reports below establish subsequent results.

## Technology stack

| Component | Technology |
|---|---|
| Runtime | Python 3.11+ |
| API / server | FastAPI, Uvicorn |
| Database / driver | MongoDB 6.0+ / PyMongo; assignment grader uses MongoDB 7.0 |
| Validation / configuration | Pydantic, python-dotenv |
| Tests | Standard-library unittest, isolated storage/aggregation doubles, HTTP/MongoDB verification harnesses |

Application dependencies are declared in [requirements.txt](requirements.txt). Verification scripts additionally use PyYAML to read the original OpenAPI contract.

## Project structure

```text
candidate_kit/
|-- app/
|   |-- __init__.py
|   `-- main.py                     # All application code
|-- sample_data/
|   |-- employees.json             # Original Extended JSON fixtures
|   `-- attendance_logs.json
|-- tests/
|   |-- test_phase3.py / test_phase4.py / test_phase5.py / test_phase6.py / test_phase7.py
|   |-- test_final_phase3_verification.py
|   |-- aggregation_memory.py      # Isolated test interpreter, not MongoDB
|   |-- phase5_fixtures.py
|   |-- live_phase3.py
|   |-- final_phase3_verification.py ... final_phase7_verification.py
|   `-- phase3_final_results.json ... phase6_final_results.json
|-- requirements.txt
|-- sample_seed.py
|-- PROBLEM_STATEMENT.docx
|-- openapi.yaml
|-- DATA_MODEL.md
|-- REVIEW.md
|-- DECISIONS.md
`-- README.md
```

A local `.env` and `.venv/` are private development files, not submission artifacts.

## Installation and configuration

Run commands from `C:\SDT_candidate_kit\candidate_kit` (the directory containing `requirements.txt`).

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Required only for isolated tests and verification scripts:
.\.venv\Scripts\python.exe -m pip install PyYAML
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install PyYAML  # Verification dependency
```

Create `.env` in this directory; no `.env.example` is currently included. For a local MongoDB instance:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For Atlas, set `MONGO_URI` to your private driver connection string instead. Do not publish its value. Real environment variables take priority over `.env`. The application defaults to local MongoDB and `attendance_db` when the variables are absent.

### MongoDB Atlas

Use the existing configured cluster, database user and permitted client IP. For a fresh environment, configure a database user and an IP access-list entry, then obtain the Python driver URI from **Connect ? Drivers**. The user needs access to the intended database and startup index creation; manual integration also requires access to the isolated temporary databases it creates and cleans up. Keep credentials local and configure only the access needed. See the [official Atlas connection instructions](https://www.mongodb.com/docs/atlas/connect-to-database-deployment/).

For local development, the original kit also permits a MongoDB 7 container:

```bash
docker run -p 27017:27017 mongo:7
```

No Dockerfile is required or included.

### Original sample setup

`sample_data/` contains the original sample documents; hidden evaluation data is much larger. The kit's optional initial loader is:

```bash
python sample_seed.py
```

**Use it only against a disposable database selected through `MONGO_DB`: it deletes existing documents in both collections before loading samples. Do not run it against the existing `attendance_db`.** It is unnecessary for startup or verification; live harnesses create their own fixtures.

## Start the backend

The assignment launch command, from the project directory with the virtual environment activated, is:

```bash
uvicorn app.main:app --port 8000
```

Without activation on Windows:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Add `--reload` for local development. Startup pings MongoDB, audits duplicate natural keys and creates indexes idempotently; it does not repair or reseed records. The assignment requires readiness within **20 seconds**. `/health` returns 200 only when MongoDB answers, otherwise 503.

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- Generated OpenAPI: http://127.0.0.1:8000/openapi.json
- Readiness: http://127.0.0.1:8000/health

In Swagger UI, expand an operation and use **Try it out**. Use a dedicated test database for create, punch and correction requests. Query analytics and explain with the parameters below; the original YAML remains authoritative.

## Implemented APIs

| Group | Method | Route | Behavior |
|---|---|---|---|
| System | GET | `/health` | MongoDB readiness |
| Employees | POST | `/employees` | Create employee; 201, duplicate code 409 |
| Employees | GET | `/employees` | Optional department filter, code-sorted pagination |
| Attendance | POST | `/attendance/punch-in` | Create employee/day record; 201, duplicate 409 |
| Attendance | POST | `/attendance/punch-out` | Close selected punch-in and calculate derived fields |
| Attendance | GET | `/attendance` | Employee/date/status filters; date DESC, code ASC pagination |
| Attendance | PATCH | `/attendance/{emp_code}/{date}` | Correct existing record and append audit history |
| Analytics | GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly report |
| Analytics | GET | `/analytics/departments/summary` | Monthly department totals and headcount |
| Analytics | GET | `/analytics/leaderboard/late` | Late-minute competition ranking |
| Analytics | GET | `/analytics/departments/{department}/trend` | Daily calendar series and rolling average |
| Admin | GET | `/admin/explain/{endpoint}` | Actual MongoDB execution plan |

Missing resources return 404 where specified; conflicting writes return 409; invalid input returns 422 with a `detail` array. MongoDB outages produce sanitized 503 responses.

## Attendance rules and workflows

Employees use immutable, client-supplied `emp_code`; attendance is addressed by `(emp_code, date)`. BSON `_id` is not the public resource identity. API instants are integer epoch milliseconds; MongoDB stores UTC BSON datetimes. Calendar dates/months and shift times remain strings. Legacy missing `history` and `half_day` are read as `[]` and `false`.

| Rule | Summary |
|---|---|
| R1 | IST attendance dates/shifts; truncate instants to whole seconds. For overnight shifts (`shift_end <= shift_start`), punches earlier than shift end belong to the previous attendance day; shift end rolls into the next day. |
| R2 | Late only strictly beyond 10 minutes after shift start. Then floor minutes from shift start, not from the end of grace. |
| R3 | Overtime is floored minutes beyond shift end, counted only at 30 minutes or more. |
| R4 | Work hours = elapsed seconds / 3600, rounded half-up to two decimals; null until punch-out. |
| R5 | Rounded work hours below 4.50 mean half-day; presence weight is 0.5. |
| R6 | PRESENT, WFH and ON_DUTY count as present; ABSENT and LEAVE do not. |
| R7 | Working days are Monday–Friday from joining, with no holiday calendar. Monthly/summary presence excludes weekends and pre-join records; weekend logs still contribute to late/overtime totals. |
| R8 | Reported numbers use half-up rounding: two decimals, or four for rates. |
| R9 | Headcount includes employees joined by period end, even with no logs. Trend applies joining eligibility separately each day. |
| R10 | Page starts at 1; page size defaults to 20 and is capped at 100; totals reflect filters. |

**Punch-in:** Supply `emp_code`, optionally `punched_at` and a presence status. The server determines the attendance day and lateness, storing an open record with null work hours and empty history. Unique indexes enforce one employee/day record: simultaneous duplicate requests yield one 201 and one 409.

**Punch-out:** Supply `emp_code` and optionally `punched_at`. The server selects the most recent punch-in at or before that instant. No candidate is 404; an already closed candidate is 409. The end must be later than the start and within 24 hours. An atomic conditional update stores punch-out, hours, overtime and half-day; it preserves punch-in, lateness and history. Simultaneous closures yield one 200 and one 409.

**Correction:** PATCH an existing employee/date with editable `status`, `punch_in` and/or `punch_out`, plus required `reason` and `regularized_by`. Unsupported and derived fields are rejected with 422, as clarified for PATCH; other request models retain their existing extra-field behavior. Corrections cannot move the attendance date; no-op corrections are rejected. ABSENT/LEAVE clear punches and reset calculations.

Punch-out and correction compare the stored snapshot before updating. A stale snapshot returns 409 instead of silently overwriting another request. Correction commits changed fields and exactly one history entry in the same `find_one_and_update`, using `$set` and `$push`. History records `{at, by, reason, changes}` with before/after values for fields that actually changed; prior entries survive. Punch-in/out do not append manual-correction history.

## MongoDB aggregation analytics

All four reports execute MongoDB pipelines and read stored derived values; application Python does not scan attendance collections to calculate reports.

| Report | Parameters | Key behavior |
|---|---|---|
| Employee monthly | Required `emp_code` path and `month=YYYY-MM` query | Joining-date weekday denominator, weighted presence, leave/late/overtime totals; null percentage for zero working days |
| Department summary | Required `month`; optional exact-case `department` | Eligible employee root preserves zero-log headcount; work-hour average is record-weighted over presence-status, non-null hours, including weekends |
| Late leaderboard | Required `month`; optional `department`, `limit` (1–50, default 10) | Department filter before `$rank`; ties share competition ranks with gaps. Rank cutoff retains all ties; output is late-total DESC/code ASC |
| Department trend | Department path; required `from`/`to` | Inclusive 1–92-day calendar, daily headcount, weighted presence, null weekend/zero-headcount rates and seven-row null-ignoring moving average within the requested range |

Monthly and summary `present_days` exclude pre-join logs following the agreed interpretation. Trend `present_count` follows its separate daily status/half-day rules, including weekend records; weekend rates remain null. Weekday gaps with positive headcount produce zero rates. Calendars and windows are generated in MongoDB; explicit decimal arithmetic implements half-up rounding.

## Admin Explain API

The endpoint returns `{endpoint, collection, explain}` and runs MongoDB explain with **`executionStats`** verbosity against the same main query/pipeline as the corresponding API, including filters, sorting and pagination.

| Allowed target | Parameters |
|---|---|
| `attendance_list` | Optional `emp_code`, `date_from`, `date_to`, `status`, `page`, `page_size` |
| `employee_monthly` | Required `emp_code`, `month` |
| `department_summary` | Required `month`; optional `department` |
| `late_leaderboard` | Required `month`; optional `department`, `limit` |
| `department_trend` | Required `department`, `from`, `to` |

Example read request: `/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07`.

Invalid targets, missing required parameters and unsupported query options return 422. Callers cannot select arbitrary collections, commands or pipelines. Planner/execution output and metrics are preserved; deployment metadata is redacted as agreed. BSON-only plan values use Extended JSON. A missing employee can still have a valid empty query plan.

The verifier examines nested winning/executed plans, index names, IXSCAN/COLLSCAN and lookup scan counters; rejected candidate scans are distinguished from executed scans. A fixture-scale indexed plan does not prove grader-scale performance.

## Seven application indexes

Startup declares these indexes idempotently, in addition to MongoDB's default `_id` indexes:

| Collection | Index | Keys | Purpose |
|---|---|---|---|
| employees | `employee_code_unique` | emp_code ASC, unique | Employee identity and joins |
| employees | `employee_department_code` | department ASC, emp_code ASC | Department-filtered employee listing and lookups |
| employees | `employee_joined_department` | joined_on ASC, department ASC | Month-end eligibility across departments |
| employees | `employee_department_joined` | department ASC, joined_on ASC | Department eligibility and daily headcount |
| attendance_logs | `attendance_employee_date_unique` | emp_code ASC, date ASC, unique | Employee/day identity and employee-scoped reads |
| attendance_logs | `attendance_date_code` | date DESC, emp_code ASC | Date-range listing and analytics reads |
| attendance_logs | `attendance_latest_punch` | emp_code ASC, punch_in DESC, date DESC | Latest eligible punch-in selection |

## Automated and live verification

Run isolated tests from the project directory:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

On an activated environment, use `python -B -m unittest discover -s tests -v`. These tests cover validation, serialization, attendance calculations, concurrency, audit history, analytics, explain commands, schemas and harness safety without connecting to Atlas. The isolated aggregation interpreter does not replace real MongoDB verification.

### Recorded results through Phase 6

Automated totals are cumulative historical phase results, not separate counts to add together. The automated counts below are the supplied verified suite results; live figures are recorded in the linked reports.

| Phase | Automated tests passed | Live checks passed | HTTP requests | Cold startup | Live status | Report |
|---|---:|---:|---:|---:|---|---|
| 3 | 58 | 11 | 46 | 2.485 s | PASS | [Phase 3](tests/phase3_final_results.json) |
| 4 | 92 | 16 | 68 | 3.627 s | PASS | [Phase 4](tests/phase4_final_results.json) |
| 5 | 123 | 16* | 85 | 2.550 s | PASS | [Phase 5](tests/phase5_final_results.json) |
| 6 | 144 | 14 | 62 | 2.579 s | PASS | [Phase 6](tests/phase6_final_results.json) |

*Phase 5's supplied summary reports 16 live checks; its saved JSON contains 17 PASS check entries. Both counts are retained here explicitly rather than presenting them as identical evidence.*

Phase 6 verified seven application indexes and all five explain targets. The report records unchanged development data (**6 employees / 9 attendance logs**), stopped test server and temporary database cleanup **PASS**. The four recorded startup measurements are below the assignment's 20-second limit.

### Phase 7 isolated QA

**174 tests passed, zero failures**: all previous 144 tests plus 30 advanced tests covering five-way races, correction retry/history chains, cross-month/year overnight shifts, generated duration/calendar boundaries, all-route error schemas and modern explain stages. The complete Phase 7 verifier callback was tested with fake storage/HTTP; this does not establish real Atlas concurrency or cleanup.

The confirmed fix is in the verification classifier: `EXPRESS_IXSCAN` and explicit documented read-index equivalents now count as indexed access; unknown stage names do not. Genuine collection scans and rejected-plan exclusions remain covered. Application code and the seven indexes are unchanged. See [the Phase 7 coverage matrix](REVIEW.md#requirements-to-tests-coverage-matrix) and contract catalog for requirement-level evidence. Phase 7 live startup/concurrency/cleanup and 100k acceptance remain **NOT TESTED**.

### Optional manual live reruns

Run only when intentionally verifying against the configured MongoDB deployment:

```powershell
.\.venv\Scripts\python.exe -B tests/final_phase3_verification.py
.\.venv\Scripts\python.exe -B tests/final_phase4_verification.py
.\.venv\Scripts\python.exe -B tests/final_phase5_verification.py
.\.venv\Scripts\python.exe -B tests/final_phase6_verification.py
# Prepared Phase 7 harness; has not been executed against Atlas:
.\.venv\Scripts\python.exe -B tests/final_phase7_verification.py
```

Each harness launches an isolated child server on a dynamically bound localhost port and validates its process/database/token identity. Writes target only its fresh, collision-checked, owned temporary database; names are within the Atlas 38-byte limit. The original `attendance_db` is read for before/after snapshots. Cleanup stops the owned server and drops only the temporary database after exact name and ownership-token verification. No harness invokes `sample_seed.py`.

## Development status and limitations

| Phase | Work completed / status |
|---|---|
| 1 | Environment and MongoDB Atlas setup — complete |
| 2 | Original assignment analysis and defect review — complete |
| 3 | Existing API fixes and verification — complete |
| 4 | Punch-out, corrections and atomic audit updates — complete |
| 5 | Four aggregation analytics APIs — complete |
| 6 | Admin Explain API and live fixture verification — complete |
| 7 | Advanced isolated QA and explain-detector fix complete; manual live verification NOT TESTED |
| 8–10 | Pending; not started as part of this work |

The **100,000-record workload remains unverified**: current live fixtures do not establish throughput, latency, query-plan stability or the grader's IXSCAN/no-COLLSCAN acceptance at that scale. Phase 6's saved trend plan reports `EXPRESS_IXSCAN`, supporting indexes and no collection scan; the historical verifier's literal `IXSCAN` flag was false for that target. Phase 7 fixes this detector using an explicit documented read-index-stage allowlist; the original report is preserved. Overall integration PASS must not be read as large-dataset acceptance. Further performance work remains pending.

There is no authentication or holiday calendar in the assignment. `regularized_by` is free text, not an authenticated identity. Earlier DNS/TLS/startup failures are documented historically in REVIEW.md; the saved later reports show successful live runs. No performance guarantee is inferred from isolated test doubles or small live fixtures.

## Submission requirements

Submit a public Git repository containing `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and this README. Keep all application code in `app/main.py` and preserve the required launch command and original contract. Do not include `.env`, credentials, virtual environments, data dumps or a Dockerfile. Retain the original assignment files and sample document shapes; the hidden evaluator uses additional cases and a much larger dataset.
