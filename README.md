# Employee Attendance & Analytics API

HROne trainee assignment backend using Python, FastAPI, Pydantic and PyMongo. All application code is in [app/main.py](app/main.py). The service manages employees, attendance, atomic corrections with audit history, four MongoDB analytics reports and five explain targets.

**Current verification:** 12 API operations implemented; 218 automated tests and 170 additional subtests passed. The saved 100,000-attendance-record benchmark completed and index plans were verified. The repository is publicly available and `main` is synchronized, as confirmed by the candidate. These are local/saved results and candidate-confirmed repository status, not independent production guarantees. **Ready for candidate submission, subject to completing official form requirements.**

See [REVIEW.md](REVIEW.md) for the compliance matrix, security review and limitations, and [DECISIONS.md](DECISIONS.md) for the five required concise answers.

## Tech Stack

Python 3.11+, FastAPI, Pydantic, PyMongo and MongoDB 6.0+; python-dotenv for optional environment configuration, unittest and PyYAML for offline verification.

## Key Features

- Employee management and attendance workflows with IST/overnight handling.
- Atomic punch-out and corrections, concurrency protection and append-only audit history.
- Four MongoDB aggregation analytics APIs and five execution-plan explain targets.
- Seven indexes, bounded pagination and saved 100,000-record benchmark evidence.

## Quick Start

### 1. Install dependencies

Prerequisites: Python 3.11+ (verified locally with 3.13.16), MongoDB 6.0+; the original candidate-kit local setup uses MongoDB 7. Run from the directory containing requirements.txt. Runtime dependencies are in [requirements.txt](requirements.txt); PyYAML is needed only for tests/verification. The established test runner is unittest; pytest is not required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install PyYAML
```

On Linux/macOS, create the environment with `python3 -m venv .venv`, activate with `source .venv/bin/activate`, then run the same pip commands using `python`.

### 2. Configure MongoDB

Set MONGO_URI and MONGO_DB in the environment or a private project-root `.env`. Explicit environment variables take priority. Local defaults are:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For Atlas, use your private Python driver URI, an existing permitted client IP and a database user authorized for reads/writes and startup index creation. Never publish the URI, password or .env contents. Verification also needs access to its dedicated temporary databases, ownership markers, indexes and cleanup; it performs no permission or network-setting changes. The shared verification harness explicitly loads project-root .env before initializing its parent client and inheriting the same URI into its child.

### 3. Start the API

On Windows, activate the environment and use the required assignment command:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --port 8000
```

If PowerShell activation is unavailable, the equivalent interpreter-specific command is `.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`; the official command above remains supported.

On Linux/macOS, use the activated environment from step 1 and run `uvicorn app.main:app --port 8000`.

### 4. Open Swagger UI

Startup pings MongoDB, audits duplicate natural keys and creates all seven indexes idempotently. It never repairs or reseeds records. Health readiness must complete within 20 seconds. Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs); generated schema: `/openapi.json`. Try read requests first; Swagger write requests affect the selected MONGO_DB.

The original kit optionally supports `docker run -p 27017:27017 mongo:7`; no Dockerfile is included. Original [sample data](sample_data/employees.json) and [sample_seed.py](sample_seed.py) are retained. The seed command `python sample_seed.py` deletes existing employee/attendance records before loading samples: use only an intentionally disposable database, never the existing attendance_db. Startup and verification do not require it.

## API overview

The authoritative contract is [openapi.yaml](openapi.yaml); BSON shapes and legacy defaults are in [DATA_MODEL.md](DATA_MODEL.md). There are 12 operations across 11 paths.

| Group | Method / path | Behavior |
|---|---|---|
| System | GET `/health` | 200 only after MongoDB ping; otherwise 503 |
| Employees | POST `/employees` | Customer-supplied immutable code; 201, duplicate 409 |
| Employees | GET `/employees` | Exact department filter; code-ascending pagination |
| Attendance | POST `/attendance/punch-in` | Presence status, IST attendance date, lateness; 201/409 |
| Attendance | POST `/attendance/punch-out` | Close latest eligible punch-in; hours/overtime/half-day |
| Attendance | PATCH `/attendance/{emp_code}/{date}` | Correct supplied fields, recompute values, append audit |
| Attendance | GET `/attendance` | Employee/date/status filters; date DESC/code ASC pagination |
| Analytics | GET `/analytics/employees/{emp_code}/monthly` | Weekday/join-date summary and attendance percentage |
| Analytics | GET `/analytics/departments/summary` | Eligible headcount, including zero-log employees; record-weighted hours |
| Analytics | GET `/analytics/leaderboard/late` | Competition ranks; department filter before rank; retain cutoff ties |
| Analytics | GET `/analytics/departments/{department}/trend` | Inclusive 1-92-day calendar and seven-row moving average |
| Admin | GET `/admin/explain/{endpoint}` | Real executionStats for the same production query |

Unknown resources return 404 where specified, conflicting writes 409, validation 422 with a detail array, and driver failures sanitized 503. Pagination defaults to page 1 / size 20, maximum 100, with filtered totals. Internal ObjectIds never identify public resources.

R1 uses IST dates/overnight shifts and truncated whole-second UTC punches. R2 is strictly >10 minutes late, flooring time since shift start. R3 requires >=30 floored overtime minutes. R4 uses half-up work hours; R5 classifies rounded hours <4.50 as half-day. R6 counts PRESENT/WFH/ON_DUTY. R7 uses weekdays/join-date working days; R8 half-up numbers (two decimals, rates four); R9 includes eligible zero-log employees; R10 bounds pagination. Stored derived fields are authoritative for analytics. Monthly/summary presence excludes pre-join logs under the agreed interpretation; trend's weekend records still count as presence while weekend rates are null.

Punch-out and correction use atomic snapshot-conditional writes; stale snapshots return 409. PATCH sets changed fields and pushes one audit entry in the same MongoDB operation. History is append-only and records before/after values; punch-in/out create no manual history. PATCH rejects unsupported/derived fields with 422, while other models ignore extras under the global convention. No eligible punch-in at/before punch-out time is 404.

All analytics run in MongoDB, including calendar generation, grouping, ranking and windows. Department work-hour averages use presence-status records with non-null hours, including weekends. Trend gaps, daily eligibility and moving averages are generated inside the requested range.

Explain targets: `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`. Supply the corresponding endpoint's parameters (trend uses department/from/to; monthly uses emp_code/month). Arbitrary collections, commands and pipelines are unavailable. Planner/execution data is retained; deployment metadata is redacted as approved. See the literal-contract caveats in REVIEW.

## Indexes and project layout

| Collection | Index | Keys |
|---|---|---|
| employees | employee_code_unique | emp_code ASC, unique |
| employees | employee_department_code | department ASC, emp_code ASC |
| employees | employee_joined_department | joined_on ASC, department ASC |
| employees | employee_department_joined | department ASC, joined_on ASC |
| attendance_logs | attendance_employee_date_unique | emp_code ASC, date ASC, unique |
| attendance_logs | attendance_date_code | date DESC, emp_code ASC |
| attendance_logs | attendance_latest_punch | emp_code ASC, punch_in DESC, date DESC |

`app/main.py` holds the runtime. `tests/test_phase3.py` through `test_phase8.py` and `test_final_phase3_verification.py` hold offline regressions; `aggregation_memory.py` is a test double, not MongoDB. Phase-specific live harnesses and sanitized saved results are in `tests/`. Original assignment/model/contract/sample files remain at their supplied paths. There are no additional application source modules.

## Testing and saved evidence

The original test runner is `unittest`. Run the offline suite without live database operations:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Optional alternative: with the project virtual environment activated, install and run `pytest`:

```sh
python -m pip install pytest
python -m pytest -q
```

Candidate-verified pytest result: **218 tests passed and 170 subtests passed**. Pytest is optional and is not included in `requirements.txt`; the original `unittest` command above remains supported. This README update did not rerun either test runner.

Phase 10 audit: **218 passed, 0 failed, 0 skipped**, 17.841 seconds; pip check passed. The candidate additionally reports **170 passing subtests**, separate from the 218 test-method total. No new tests were run for this README update. Isolated doubles do not prove server behavior; saved live reports supply separate evidence.

| Phase | Saved PASS checks | HTTP requests | Startup | Report |
|---|---:|---:|---:|---|
| 3 | 11 | 46 | 2.485s | [JSON](tests/phase3_final_results.json) |
| 4 | 16 | 68 | 3.627s | [JSON](tests/phase4_final_results.json) |
| 5 | 17 | 85 | 2.550s | [JSON](tests/phase5_final_results.json) |
| 6 | 14 | 62 | 2.579s | [JSON](tests/phase6_final_results.json) |
| 7 | 20 | 120 | 3.920s | [JSON](tests/phase7_final_results.json) |
| 8 | 20 | 111 | 2.363s | [JSON](tests/phase8_final_results.json) |

Phase 5's saved JSON contains 17 PASS entries; the earlier supplied summary said 16. This table uses actual saved entries. Historical automated totals were 58/92/123/144/174 through Phases 3-7; Phase 8 preparation plus environment regression tests brought the current suite to 218.

Phase 8 added exactly 100,000 attendance fixtures and 1,050 employee fixtures to the parent's small probes. Full-data fresh startup/index creation took **3.260s**, then idempotent startup **2.134s**. All five tested explain targets reported indexed access and no collection scans; the trend root used EXPRESS_IXSCAN. Analytics oracles, pagination, two-client races, original attendance_db preservation (6 employees / 9 logs), stopped server and owned temporary cleanup passed.

| Read API | Samples | Median ms | Descriptive p95 / max ms |
|---|---:|---:|---:|
| health | 5 | 56.94 | 63.99 / 63.99 |
| employees | 5 | 139.58 | 143.54 / 143.54 |
| attendance | 5 | 158.54 | 166.50 / 166.50 |
| monthly | 5 | 64.45 | 68.59 / 68.59 |
| summary | 5 | 589.14 | 649.54 / 649.54 |
| leaderboard | 5 | 338.27 | 353.48 / 353.48 |
| trend | 5 | 3889.79 | 3915.70 / 3915.70 |

Five-sample p95 equals the maximum and is not statistically representative or a production guarantee. Client/network latency is separate from server explain timing. Trend explain took 4,166ms, with expensive work localized to the attendance/department lookup; a precise nested cause is not established. No speculative optimization was applied. Full plan, write latency and deep-offset evidence is summarized in [REVIEW.md](REVIEW.md#phase-8-saved-performance-evidence).

RSS snapshots were 75,833,344 and 77,885,440 bytes; peak memory remains NOT MEASURED. Logical data: 20,245,327 bytes; allocated storage: 5,816,320; indexes: 8,339,456. These measurements describe one benchmark on the user's reported Atlas M0 environment, not every deployment or future available capacity. No numeric API latency or RSS threshold is specified by the assignment.

### Optional verification tools

No live rerun was performed in Phase 9 or Phase 10. A Phase 3-7 harness can be manually selected using `python -B tests/final_phaseN_verification.py`; it starts an owned server on a dynamic localhost port, verifies PID/database/token, writes only its collision-checked temporary database and checks development snapshots before exact-owner cleanup. Live execution needs separate approval and database permissions.

Phase 8's default command only prints an offline plan:

```powershell
.\.venv\Scripts\python.exe -B tests/final_phase8_verification.py
```

**Do not rerun the completed 100k benchmark without separate approval.** An approved future run additionally requires `--approve-100k --capacity-file tests/phase8_capacity.local.json`. The ignored local JSON must contain exactly tier (M0 or FLEX), available_bytes, verified_at (UTC ISO timestamp within 24h), same_deployment=true and source="Atlas UI". Inspect actual deployment-wide headroom first. The 94,281,167-byte budget is advisory, not measured free capacity. The single writer averages 50 documents/sec in <=100-document batches, so loading takes roughly 33 minutes; the advisory execution budget is one hour. Only verified owned data/indexes are modified; exact owner mismatch refuses cleanup. Never remediate a failed cleanup by blindly dropping a database.

## Limitations and submission

There is no authentication, holiday calendar, global payload cap or ingress rate limiting in the assignment. regularized_by is free text. Audit history and leaderboard tie responses can grow; listing limits bound document count, not response bytes. Peak memory, clean-clone MongoDB 7 execution and hidden grader results are unverified. A literal IXSCAN-only checker or strict unredacted-explain checker may differ from the accepted modern-stage/redaction interpretation; these caveats are documented rather than hidden. Changes to pipelines/indexes or production security controls need separate review and approval.

Submit the original required runtime and documentation files in a public Git repository: app/main.py, requirements.txt, REVIEW.md, DECISIONS.md and README.md. Preserve the official contract and model, single-file runtime and required launch command. Exclude .env, credentials, virtual environments, private dumps, capacity files, caches and Dockerfiles. Neither final audit staged, committed, pushed, modified the remote or submitted anything. Phase 10 is complete. The candidate confirms that the repository is publicly available and `main` is synchronized; this updates the historical audit's unverified publication status without changing its findings. Complete the official submission form and follow the deadline/method in the invitation email; the exact deadline is not documented here.

### Final compatibility verdict

The five explain response envelopes satisfy the original required fields (`endpoint`, `collection`, `explain`); metadata removal does not delete any mandatory schema field. The prose request for raw output still has the documented user-approved redaction exception. MongoDB documents EXPRESS_IXSCAN as a real optimized index scan introduced in 8.0; stages are preserved exactly, and no fake IXSCAN or hint was added. A hidden literal-stage or complete-document-equality checker remains unknown. See [Phase 10 findings](REVIEW.md#phase-10-final-verification) for primary sources and final qualifications.

**Submission verdict: Ready for candidate submission, subject to completing official form requirements.** Hidden-grader behavior and clean-environment compatibility are not independently verified. The historical Phase 10 audit retains its qualified findings in REVIEW.md. No critical runtime defect was established. A fresh MongoDB 6/7 or clean-clone dependency installation was not performed; installed Python 3.13 execution and Python 3.11 syntax were verified separately. No MongoDB installation, live connection or benchmark repeat was attempted.
