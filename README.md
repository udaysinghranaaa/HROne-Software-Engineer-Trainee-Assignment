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

## Phase 4 status and verification

Employee APIs and attendance listing, punch-in, punch-out and manual correction are implemented. Analytics and admin explain remain for later phases. Run isolated tests from this directory:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

For **manual, opt-in** Atlas integration using an owned temporary test database:

```powershell
.\.venv\Scripts\python.exe -B tests/final_phase4_verification.py
```

The verifier reads `attendance_db` for before/after snapshots; writes and verified cleanup target only its unique temporary database. It does not run the seed script. Phase 4 live integration has not yet been executed; see `REVIEW.md` and `DECISIONS.md` for evidence, atomicity and limitations.
