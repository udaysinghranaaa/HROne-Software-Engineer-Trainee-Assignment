"""Opt-in HTTP/MongoDB analytics verification. No live operations occur on import.

Run: .venv/Scripts/python.exe -B tests/final_phase5_verification.py
Only its fresh, owned hrone_p5v_ database receives deterministic fixtures or cleanup.
"""
import sys
import re
from urllib.parse import quote

from bson import BSON
import final_phase3_verification as harness
from phase5_fixtures import fixture_data, monthly_expected, summary_expected, leaderboard_expected, trend_expected


def seed_owned_fixtures(database, phase=5):
    assert phase in (5, 6) and database.name.startswith(f"hrone_p{phase}v_"), "Fixture phase/database mismatch"
    harness.guard_database(database.name, database.name)
    owner = database["_verification_owner"].find_one({"_id": "owner"})
    assert owner and owner.get("database") == database.name and isinstance(owner.get("run_token"), str) and re.fullmatch(r"[0-9a-f]{32}", owner["run_token"]), "Fixture ownership missing"
    employees, logs = fixture_data()
    for employee in employees:
        assert database.employees.find_one({"emp_code": employee["emp_code"]}) is None, "Fixture code collision"
    # The parent additionally verifies the marker against its exact run token before this callback.
    database.employees.insert_many(employees)
    harness.guard_database(database.name, database.name)
    database.attendance_logs.insert_many(logs)


def phase5_checks(http, database, codes, epoch, passed):
    seed_owned_fixtures(database)
    assert database.employees.count_documents({}) <= 1000 and database.attendance_logs.count_documents({}) <= 1000
    employees = list(database.employees.find({}).limit(1000))
    logs = list(database.attendance_logs.find({}).limit(1000))
    before = ([BSON.encode(doc) for doc in employees], [BSON.encode(doc) for doc in logs])
    month = "2026-07"
    for code in ("EMP9101", "EMP9102", "EMP9104", "EMP9105", "EMP9106"):
        actual = http("GET", f"/analytics/employees/{code}/monthly", [200], query={"month": month})[1]
        assert actual == monthly_expected(employees, logs, code, month), "Monthly oracle mismatch"
    for leap_month in ("2024-02", "2026-06", "2026-08"):
        actual = http("GET", "/analytics/employees/EMP9101/monthly", [200], query={"month": leap_month})[1]
        assert actual == monthly_expected(employees, logs, "EMP9101", leap_month)
    passed("phase5_monthly_oracles")

    for department in (None, "Analytics QA", "Analytics Quiet", "Analytics Future"):
        query = {"month": month, **({"department": department} if department is not None else {})}
        actual = http("GET", "/analytics/departments/summary", [200], query=query)[1]
        assert actual == summary_expected(employees, logs, month, department), "Summary oracle mismatch"
    passed("phase5_summary_oracles")

    for department in (None, "Analytics QA", "Analytics Quiet"):
        for limit in (1, 2, 3, 50):
            query = {"month": month, "limit": limit, **({"department": department} if department is not None else {})}
            actual = http("GET", "/analytics/leaderboard/late", [200], query=query)[1]
            assert actual == leaderboard_expected(employees, logs, month, limit, department), "Leaderboard oracle mismatch"
    ties = http("GET", "/analytics/leaderboard/late", [200], query={"month": month, "department": "Analytics QA", "limit": 1})[1]
    assert len(ties["items"]) == 2 and all(item["rank"] == 1 for item in ties["items"])
    passed("phase5_leaderboard_oracles_and_ties")

    for department, first, last in (("Analytics QA", "2026-07-01", "2026-07-10"),
                                     ("Analytics Quiet", "2026-07-01", "2026-07-10"),
                                     ("Analytics Future", "2026-07-01", "2026-07-03"),
                                     ("Analytics QA", "2026-07-04", "2026-07-05"),
                                     ("Analytics QA", "2026-07-02", "2026-07-09")):
        actual = http("GET", "/analytics/departments/" + quote(department, safe="") + "/trend", [200], query={"from": first, "to": last})[1]
        assert actual == trend_expected(employees, logs, department, first, last), "Trend oracle mismatch"
    passed("phase5_trend_oracles")

    http("GET", "/analytics/employees/unknown/monthly", [404], query={"month": month})
    http("GET", "/analytics/departments/unknown/trend", [404], query={"from": "2026-07-01", "to": "2026-07-10"})
    for path in ("/analytics/employees/EMP9101/monthly", "/analytics/departments/summary", "/analytics/leaderboard/late"):
        http("GET", path, [422], query={"month": "2026-13"})
    for limit in (0, 51):
        http("GET", "/analytics/leaderboard/late", [422], query={"month": month, "limit": limit})
    for end in ("2026-06-30", "2026-10-01"):
        http("GET", "/analytics/departments/Analytics%20QA/trend", [422], query={"from": "2026-07-01", "to": end})
    passed("phase5_validation_contract")
    after = ([BSON.encode(doc) for doc in database.employees.find({}).limit(1000)],
             [BSON.encode(doc) for doc in database.attendance_logs.find({}).limit(1000)])
    assert after == before, "Analytics modified fixture documents"
    passed("phase5_analytics_read_only")


if __name__ == "__main__":
    harness.RESULT_FILE = harness.ROOT / "tests/phase5_final_results.json"
    sys.exit(harness.run_verification(extra_checks=phase5_checks, phase=5))
