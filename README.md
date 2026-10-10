# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

## Phase 6 status and verification

Employee APIs, attendance listing/punch-in/punch-out/correction, four MongoDB analytics APIs and the contract-restricted admin explain API are implemented (12 operations). Run isolated tests from this directory:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

For **manual, opt-in** Atlas integration using an owned temporary test database:

```powershell
.\.venv\Scripts\python.exe -B tests/final_phase6_verification.py
```

The verifier reads `attendance_db` for before/after snapshots; fixture writes and verified cleanup target only its unique owned `hrone_p6v_` database. It does not run the seed script. The saved Phase 5 run passed 16 checks and 85 HTTP requests, measured 2.55-second startup, verified seven indexes and preserved development data. Phase 6 live verification has **not been executed**; its manual run reports real plans and execution statistics but does not establish 100k-record performance. Current isolated verification: **144 passed, 0 failed**. See `REVIEW.md` and `DECISIONS.md` for evidence and limitations.

`GET /admin/explain/{endpoint}` supports only `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`. Pass the target's query parameters (monthly: `emp_code`/`month`; trend: `department`/`from`/`to`). It returns real MongoDB `executionStats` output with deployment metadata redacted; arbitrary commands, pipelines and collections are rejected.
