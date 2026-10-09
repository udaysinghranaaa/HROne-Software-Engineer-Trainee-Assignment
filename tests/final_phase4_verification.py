"""Manual-only Phase 4 HTTP/MongoDB verification in an owned temporary database.

Run: .venv/Scripts/python.exe -B tests/final_phase4_verification.py
Reuses Phase 3's guarded ownership, PID, environment, startup and cleanup workflow.
Never seeds or writes attendance_db. No live operations occur on import.
"""
import copy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import final_phase3_verification as harness


def phase4_checks(http, database, codes, epoch, passed):
    from app import main
    assert database.name.startswith("hrone_p4v_"), "Phase 4 requires its own test database"
    harness.guard_database(database.name, database.name)

    def race(method, path, bodies, expected):
        gate = threading.Barrier(2)
        def send(body):
            gate.wait(timeout=5)
            return http(method, path, expected, body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(send, bodies))

    body = {"emp_code": codes[0], "punched_at": epoch(6, 19, 0, milliseconds=900)}
    results = race("POST", "/attendance/punch-out", [body, body], [200, 409])
    assert sorted(status for status, _ in results) == [200, 409]
    doc = next(doc for status, doc in results if status == 200)
    assert doc["punch_out"] == epoch(6, 19, 0) and doc["overtime_minutes"] == 30
    assert doc["history"] == [] and not doc["half_day"]
    stored = database.attendance_logs.find_one({"emp_code": codes[0], "date": "2026-07-06"})
    assert stored["punch_out"].utcoffset().total_seconds() == 0
    assert harness.BSON.encode(stored)
    passed("phase4_punch_out_concurrency", statuses=[200, 409], history_entries=0)

    http("POST", "/attendance/punch-out", [409], body)
    http("POST", "/attendance/punch-out", [404], {"emp_code": "unknown", "punched_at": epoch(6, 19, 0)})
    http("POST", "/attendance/punch-out", [404], {"emp_code": codes[0], "punched_at": epoch(5, 9, 0)})
    for invalid in (None, True, 1783312500, "1783312500000", 1783312500000.0):
        http("POST", "/attendance/punch-out", [422], {"emp_code": codes[1], "punched_at": invalid})
    # The overnight employee's punch-in belongs to July 6, despite being on July 7.
    night = http("POST", "/attendance/punch-out", [200], {
        "emp_code": codes[2], "punched_at": epoch(7, 6, 30)})[1]
    assert (night["date"], night["work_hours"], night["overtime_minutes"]) == ("2026-07-06", 6.0, 30)
    passed("phase4_punch_out_validation_and_overnight")

    path = f"/attendance/{codes[0]}/2026-07-06"
    common = {"reason": "Phase 4 approved correction", "regularized_by": "phase4.qa"}
    corrected = http("PATCH", path, [200], {**common, "punch_in": epoch(6, 9, 25), "status": "WFH"})[1]
    assert corrected["late_minutes"] == 0 and corrected["work_hours"] == 9.58
    assert len(corrected["history"]) == 1
    assert corrected["history"][0]["changes"]["punch_in"] == {
        "from": epoch(6, 9, 40, 1), "to": epoch(6, 9, 25)}
    old_history = copy.deepcopy(corrected["history"])
    stored = database.attendance_logs.find_one({"emp_code": codes[0], "date": "2026-07-06"})
    assert stored["history"][0]["changes"]["punch_in"]["to"].utcoffset().total_seconds() == 0
    passed("phase4_correction_values_and_bson_audit")

    results = race("PATCH", path, [{**common, "status": "ON_DUTY"},
                                   {**common, "punch_out": epoch(6, 19, 30)}], [200, 409])
    successes = sum(status == 200 for status, _ in results)
    assert successes in (1, 2)
    stored = database.attendance_logs.find_one({"emp_code": codes[0], "date": "2026-07-06"})
    serialized = main.serialize_attendance(stored)
    assert serialized["history"][:1] == old_history
    assert len(serialized["history"]) == 1 + successes
    for field in ("status", "punch_out"):
        changes = [entry["changes"][field] for entry in serialized["history"] if field in entry["changes"]]
        if changes:
            assert serialized[field] == changes[-1]["to"]
    passed("phase4_correction_concurrency", statuses=[status for status, _ in results],
           successful_corrections=successes, history_entries=len(serialized["history"]))

    snapshot = harness.BSON.encode(stored)
    http("PATCH", path, [422], common)
    for extra in ({"work_hours": 999}, {"history": []}, {"punch_out": None},
                  {"punch_out": epoch(6, 9, 0)}, {"punch_in": epoch(7, 9, 0)},
                  {"status": "ABSENT", "punch_in": epoch(6, 9, 0)}):
        http("PATCH", path, [422], {**common, **extra})
    assert harness.BSON.encode(database.attendance_logs.find_one({"emp_code": codes[0], "date": "2026-07-06"})) == snapshot
    absent = http("PATCH", path, [200], {**common, "status": "ABSENT"})[1]
    assert absent["punch_in"] is None and absent["punch_out"] is None and absent["work_hours"] is None
    assert (absent["late_minutes"], absent["overtime_minutes"], absent["half_day"]) == (0, 0, False)
    assert len(absent["history"]) == 2 + successes
    passed("phase4_correction_rejections_and_absence")


if __name__ == "__main__":
    harness.RESULT_FILE = harness.ROOT / "tests/phase4_final_results.json"
    sys.exit(harness.run_verification(extra_checks=phase4_checks, phase=4))
