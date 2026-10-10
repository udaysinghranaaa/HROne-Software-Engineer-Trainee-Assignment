"""Isolated Phase 4 ASGI, BSON and atomic-update tests; no live database access."""
import asyncio
import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
import yaml
from bson import BSON
from pymongo.errors import OperationFailure
from final_phase3_verification import validate_schema

from test_phase3 import MemoryCollection, MemoryDatabase, main, request


class AtomicCollection(MemoryCollection):
    @staticmethod
    def matches(doc, query):
        for field, value in query.items():
            if field == "$and":
                if not all(AtomicCollection.matches(doc, part) for part in value):
                    return False
                continue
            actual = doc.get(field)
            if isinstance(value, dict):
                for op, operand in value.items():
                    if op == "$exists" and (field in doc) != operand:
                        return False
                    if op == "$eq" and actual != operand:
                        return False
                    if op == "$type" and not isinstance(actual, datetime):
                        return False
                    if op == "$lte" and (actual is None or actual > operand):
                        return False
                    if op == "$gte" and (actual is None or actual < operand):
                        return False
            elif actual != value:
                return False
        return True

    def find_one(self, query, sort=None):
        with self.lock:
            docs = [copy.deepcopy(doc) for doc in self.docs if self.matches(doc, query)]
        for field, direction in reversed(sort or []):
            docs.sort(key=lambda doc: doc[field], reverse=direction < 0)
        return docs[0] if docs else None

    def find_one_and_update(self, query, update, return_document=None):
        if self.barrier:
            self.barrier.wait(timeout=5)
        with self.lock:
            doc = next((doc for doc in self.docs if self.matches(doc, query)), None)
            if doc is None:
                return None
            doc.update(copy.deepcopy(update.get("$set", {})))
            for field, value in update.get("$push", {}).items():
                doc.setdefault(field, []).append(copy.deepcopy(value))
            return copy.deepcopy(doc)


class Phase4Tests(unittest.TestCase):
    def setUp(self):
        database = MemoryDatabase()
        database.attendance_logs = AtomicCollection()
        self.database = database
        self.db_patch = patch.object(main, "db", database)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def http(self, method, path, body=None):
        return asyncio.run(request(method, path, body=body))

    def instant(self, hour=9, minute=30, second=0, day=6):
        return int(datetime(2026, 7, day, hour, minute, second, tzinfo=main.IST).timestamp()) * 1000

    def open_record(self, code="EMP0001", at=None):
        status, doc = self.http("POST", "/attendance/punch-in", {
            "emp_code": code, "punched_at": at or self.instant()})
        self.assertEqual(status, 201)
        return doc

    def close(self, at=None, code="EMP0001"):
        return self.http("POST", "/attendance/punch-out", {"emp_code": code, "punched_at": at or self.instant(hour=19, minute=0)})

    def correct(self, **updates):
        return self.http("PATCH", "/attendance/EMP0001/2026-07-06", {
            "reason": "Manager approved correction", "regularized_by": "QA", **updates})

    def test_punch_out_success_utc_serialization_and_history(self):
        self.open_record()
        old = copy.deepcopy(self.database.attendance_logs.docs[0])
        status, doc = self.close(self.instant(hour=19, minute=0) + 999)
        self.assertEqual(status, 200)
        self.assertEqual((doc["work_hours"], doc["overtime_minutes"], doc["half_day"], doc["history"]), (9.5, 30, False, []))
        self.assertEqual(doc["punch_out"], self.instant(hour=19, minute=0))
        stored = self.database.attendance_logs.docs[0]
        self.assertEqual(stored["punch_out"].utcoffset(), timedelta(0))
        for field in ("punch_in", "late_minutes", "status", "history"):
            self.assertEqual(stored[field], old[field])
        self.assertNotIn("_id", doc)

    def test_punch_out_unknown_missing_and_before_first_punch(self):
        self.assertEqual(self.close(code="unknown")[0], 404)
        self.assertEqual(self.close()[0], 404)
        self.open_record()
        self.assertEqual(self.close(self.instant(hour=9, minute=0))[0], 404)

    def test_punch_out_duplicate_and_order_duration_boundaries(self):
        self.open_record()
        self.assertEqual(self.close(self.instant())[0], 422)
        self.assertEqual(self.close(self.instant(day=7) + 1000)[0], 422)
        self.assertEqual(self.close(self.instant(day=7))[0], 200)
        self.assertEqual(self.close(self.instant(day=7))[0], 409)

    def test_punch_out_overtime_whole_minutes(self):
        for hour, minute, second, expected in ((18, 59, 59, 0), (19, 0, 0, 30), (19, 1, 59, 31)):
            with self.subTest(expected=expected):
                self.database.attendance_logs.docs.clear()
                self.open_record()
                status, doc = self.close(self.instant(hour=hour, minute=minute, second=second))
                self.assertEqual(status, 200)
                self.assertEqual(doc["overtime_minutes"], expected)

    def test_punch_out_half_up_and_rounded_half_day_boundary(self):
        for seconds, expected_hours, half in ((16181, 4.49, True), (16182, 4.50, False), (16218, 4.51, False)):
            with self.subTest(seconds=seconds):
                self.database.attendance_logs.docs.clear()
                self.open_record()
                status, doc = self.close(self.instant() + seconds * 1000)
                self.assertEqual(status, 200)
                self.assertEqual((doc["work_hours"], doc["half_day"]), (expected_hours, half))

    def test_punch_out_overnight_midnight_and_correct_shift_end(self):
        employee = self.database.employees.docs[0]
        employee.update(shift_start="22:00", shift_end="06:00")
        self.open_record(at=self.instant(hour=22, minute=0))
        status, doc = self.close(self.instant(hour=6, minute=30, day=7))
        self.assertEqual(status, 200)
        self.assertEqual((doc["date"], doc["work_hours"], doc["overtime_minutes"]), ("2026-07-06", 8.5, 30))

    def test_punch_out_selects_latest_eligible_even_if_already_closed(self):
        self.open_record(at=self.instant(day=5))
        self.open_record()
        self.assertEqual(self.close()[0], 200)
        self.assertEqual(self.close()[0], 409)
        self.assertIsNone(self.database.attendance_logs.docs[0]["punch_out"])

    def test_punch_out_invalid_request_types_and_ignored_derived_fields(self):
        for body in ({}, {"emp_code": 123}, *({"emp_code": "EMP0001", "punched_at": value}
                    for value in (None, True, 1783312500, "1783312500000", 1783312500000.0, 4102444800001))):
            with self.subTest(body=body):
                self.assertEqual(self.http("POST", "/attendance/punch-out", body)[0], 422)
        self.open_record()
        status, doc = self.http("POST", "/attendance/punch-out", {
            "emp_code": "EMP0001", "punched_at": self.instant(hour=19, minute=0), "work_hours": 999})
        self.assertEqual((status, doc["work_hours"]), (200, 9.5))

    def test_punch_out_default_timestamp(self):
        self.open_record()
        with patch.object(main, "current_epoch_ms", return_value=self.instant(hour=19, minute=0)):
            # Field default factory was bound when the class was created.
            with patch.object(main, "datetime") as clock:
                clock.now.return_value = main.from_epoch_ms(self.instant(hour=19, minute=0))
                self.assertEqual(self.http("POST", "/attendance/punch-out", {"emp_code": "EMP0001"})[0], 200)

    def test_punch_out_real_parallel_asgi_atomicity(self):
        self.open_record()
        self.database.attendance_logs.barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.close()[0], range(2)))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.database.attendance_logs.docs[0]["history"], [])

    def test_correction_single_field_and_exact_audit(self):
        self.open_record()
        status, doc = self.correct(status="WFH")
        self.assertEqual(status, 200)
        self.assertEqual(doc["history"][0]["changes"], {"status": {"from": "PRESENT", "to": "WFH"}})
        self.assertEqual((doc["history"][0]["by"], doc["history"][0]["reason"]), ("QA", "Manager approved correction"))
        self.assertIs(type(doc["history"][0]["at"]), int)

    def test_correction_multiple_fields_derived_and_utc_audit(self):
        self.open_record()
        start, end = self.instant(hour=10, minute=0), self.instant(hour=19, minute=30)
        status, doc = self.correct(punch_in=start + 999, punch_out=end, status="ON_DUTY")
        self.assertEqual(status, 200)
        self.assertEqual((doc["late_minutes"], doc["work_hours"], doc["overtime_minutes"]), (30, 9.5, 60))
        self.assertEqual(doc["history"][0]["changes"]["punch_in"], {"from": self.instant(), "to": start})
        stored = self.database.attendance_logs.docs[0]
        self.assertEqual(stored["history"][0]["changes"]["punch_in"]["to"].utcoffset(), timedelta(0))
        BSON.encode(stored)
        self.assertNotIn("_id", doc)

    def test_correction_absence_clears_times_and_all_derived_fields(self):
        self.open_record()
        self.close()
        status, doc = self.correct(status="LEAVE")
        self.assertEqual(status, 200)
        self.assertEqual((doc["punch_in"], doc["punch_out"], doc["work_hours"], doc["late_minutes"],
                          doc["overtime_minutes"], doc["half_day"]), (None, None, None, 0, 0, False))
        self.assertEqual(len(doc["history"]), 1)

    def test_correction_presence_requires_punch_in_and_date_consistency(self):
        self.open_record()
        self.correct(status="ABSENT")
        self.assertEqual(self.correct(status="PRESENT")[0], 422)
        self.assertEqual(self.correct(status="PRESENT", punch_in=self.instant(day=7))[0], 422)
        self.assertEqual(self.correct(status="PRESENT", punch_in=self.instant())[0], 200)

    def test_correction_invalid_fields_types_reason_and_combinations(self):
        self.open_record()
        for updates in ({"work_hours": 9}, {"half_day": False}, {"history": []}, {"date": "2026-07-07"},
                        {"punch_out": None}, {"status": None}, {"punch_in": True}, {"punch_in": "1783312500000"},
                        {"punch_out": 1783312500000.0}, {"reason": "bad"}, {"regularized_by": ""},
                        {"reason": "x" * 201}, {"regularized_by": "x" * 51},
                        {"status": "ABSENT", "punch_in": self.instant()}):
            with self.subTest(updates=updates):
                self.assertEqual(self.correct(**updates)[0], 422)
        self.assertEqual(self.http("PATCH", "/attendance/EMP0001/2026-07-06", {"status": "WFH"})[0], 422)

    def test_correction_invalid_path_and_missing_resources(self):
        for path, expected in (("/attendance/unknown/2026-07-06", 404),
                               ("/attendance/EMP0001/2026-07-06", 404),
                               ("/attendance/EMP0001/2026-02-30", 422),
                               ("/attendance/EMP0001/20260706", 422)):
            self.assertEqual(self.http("PATCH", path, {"reason": "approved", "regularized_by": "QA", "status": "WFH"})[0], expected)

    def test_correction_noop_ordering_and_24_hour_limit(self):
        self.open_record()
        before = copy.deepcopy(self.database.attendance_logs.docs)
        for updates in ({}, {"status": "PRESENT"}, {"punch_in": self.instant() + 999},
                        {"punch_out": self.instant()}, {"punch_out": self.instant() - 1000},
                        {"punch_out": self.instant(day=7) + 1000}):
            self.assertEqual(self.correct(**updates)[0], 422)
        self.assertEqual(self.database.attendance_logs.docs, before)
        self.assertEqual(self.correct(punch_out=self.instant(day=7))[0], 200)

    def test_correction_preserves_prior_history_and_legacy_fields(self):
        self.open_record()
        stored = self.database.attendance_logs.docs[0]
        stored.pop("history")
        stored.pop("half_day")
        stored["unrelated"] = "preserved"
        self.assertEqual(self.correct(status="WFH")[0], 200)
        old = copy.deepcopy(stored["history"])
        self.assertEqual(self.correct(status="ON_DUTY")[0], 200)
        self.assertEqual(stored["history"][:1], old)
        self.assertEqual(len(stored["history"]), 2)
        self.assertEqual(stored["unrelated"], "preserved")

    def test_correction_overnight_date_and_derived_fields(self):
        self.database.employees.docs[0].update(shift_start="22:00", shift_end="06:00")
        self.open_record(at=self.instant(hour=22, minute=0))
        status, doc = self.correct(punch_in=self.instant(hour=0, minute=30, day=7),
                                   punch_out=self.instant(hour=6, minute=30, day=7))
        self.assertEqual(status, 200)
        self.assertEqual((doc["date"], doc["late_minutes"], doc["work_hours"], doc["overtime_minutes"]),
                         ("2026-07-06", 150, 6.0, 30))

    def test_concurrent_corrections_atomic_history_and_no_lost_updates(self):
        self.open_record()
        self.database.attendance_logs.barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda status: self.correct(status=status), ("WFH", "ON_DUTY")))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        stored = self.database.attendance_logs.docs[0]
        self.assertEqual(len(stored["history"]), 1)
        self.assertEqual(stored["history"][0]["changes"]["status"]["to"], stored["status"])
        self.database.attendance_logs.barrier = None
        loser = next(status for status in ("WFH", "ON_DUTY") if status != stored["status"])
        self.assertEqual(self.correct(status=loser)[0], 200)
        self.assertEqual(len(stored["history"]), 2)
        self.assertEqual(stored["history"][1]["changes"]["status"]["from"], stored["history"][0]["changes"]["status"]["to"])

    def test_punch_out_competing_with_correction_has_one_atomic_winner(self):
        self.open_record()
        self.database.attendance_logs.barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.close)
            second = pool.submit(self.correct, status="WFH")
            results = (first.result(), second.result())
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        stored = self.database.attendance_logs.docs[0]
        self.assertEqual(len(stored["history"]), int(results[1][0] == 200))

    def test_correction_database_failure_cannot_commit_partial_audit(self):
        self.open_record()
        before = copy.deepcopy(self.database.attendance_logs.docs)
        with patch.object(self.database.attendance_logs, "find_one_and_update", side_effect=OperationFailure("simulated", code=13)):
            status, doc = self.correct(status="WFH")
        self.assertEqual((status, doc), (503, {"detail": "MongoDB unavailable"}))
        self.assertEqual(self.database.attendance_logs.docs, before)

    def test_correction_recalculates_grace_half_day_and_overtime_boundaries(self):
        for second, late in ((0, 0), (1, 10)):
            with self.subTest(second=second):
                self.database.attendance_logs.docs.clear()
                self.open_record()
                start = self.instant(hour=9, minute=40, second=second)
                status, doc = self.correct(punch_in=start, punch_out=start + 16182000)
                self.assertEqual(status, 200)
                self.assertEqual((doc["late_minutes"], doc["work_hours"], doc["half_day"]), (late, 4.5, False))
        for minute, second, overtime in ((59, 59, 0), (0, 0, 30)):
            self.database.attendance_logs.docs.clear()
            self.open_record()
            end = self.instant(hour=18 if minute == 59 else 19, minute=minute, second=second)
            status, doc = self.correct(punch_out=end)
            self.assertEqual((status, doc["overtime_minutes"]), (200, overtime))

    def test_endpoint_contract_registration_request_response_fields(self):
        from test_phase3 import ROOT
        contract = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
        generated = main.app.openapi()
        for path, method, model in (("/attendance/punch-out", "post", main.PunchOutIn),
                                    ("/attendance/{emp_code}/{date}", "patch", main.RegularizeIn)):
            expected = contract["paths"][path][method]
            actual = generated["paths"][path][method]
            self.assertEqual(actual["operationId"], expected["operationId"])
            self.assertEqual(set(actual["responses"]), set(expected["responses"]))
            name = expected["requestBody"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
            schema = contract["components"]["schemas"][name]
            self.assertEqual(set(model.model_fields), set(schema["properties"]))
            self.assertEqual(set(model.model_json_schema()["required"]), set(schema["required"]))
        self.open_record()
        for result in (self.close(), self.correct(status="WFH")):
            self.assertEqual(result[0], 200)
            validate_schema(contract, contract["components"]["schemas"]["AttendanceRecord"], result[1])
            self.assertEqual(set(result[1]), set(contract["components"]["schemas"]["AttendanceRecord"]["properties"]))

    def test_phase4_live_workflow_isolated_with_fake_database(self):
        from final_phase4_verification import phase4_checks
        from test_phase3 import ROOT
        contract = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
        codes = ["EMP0001", "EMP0002", "EMP0003"]
        self.database.name = "hrone_p4v_20261009_0123456789abcdef"
        for employee in self.database.employees.docs:
            if employee["emp_code"] in codes:
                employee.update(shift_start="09:30", shift_end="18:30")
                if employee["emp_code"] == codes[2]:
                    employee.update(shift_start="22:00", shift_end="06:00")
        def epoch(day, hour, minute, second=0, milliseconds=0):
            return self.instant(day=day, hour=hour, minute=minute, second=second) + milliseconds
        self.open_record(code=codes[0], at=epoch(6, 9, 40, 1))
        self.open_record(code=codes[1], at=epoch(6, 9, 40))
        self.open_record(code=codes[2], at=epoch(7, 0, 30))
        def http(method, path, expected, body=None):
            status, doc = self.http(method, path, body)
            self.assertIn(status, expected)
            contract_path = "/attendance/{emp_code}/{date}" if method == "PATCH" else path
            response = contract["paths"][contract_path][method.lower()]["responses"][str(status)]
            if "$ref" in response:
                response = contract["components"]["responses"][response["$ref"].split("/")[-1]]
            validate_schema(contract, response["content"]["application/json"]["schema"], doc)
            return status, doc
        checks = []
        phase4_checks(http, self.database, codes, epoch, lambda label, **details: checks.append(label))
        self.assertEqual(len(checks), 5)

    def test_phase4_database_guard_and_phase_limits(self):
        import final_phase3_verification as harness
        name = harness.generate_database_name("a" * 32, phase=4)
        self.assertEqual(len(name.encode("utf-8")), 35)
        harness.guard_database(name, name)
        for phase in (2, 7):
            with self.assertRaises(RuntimeError):
                harness.generate_database_name("a" * 32, phase=phase)
        with self.assertRaises(RuntimeError):
            harness.guard_database(name, name[:-1] + "b")

    def assert_validation_contract(self, result, field):
        from test_phase3 import ROOT
        contract = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
        status, response = result
        self.assertEqual(status, 422)
        validate_schema(contract, contract["components"]["responses"]["ValidationError"]
                        ["content"]["application/json"]["schema"], response)
        self.assertEqual(set(response), {"detail"})
        self.assertEqual(response["detail"][0]["loc"], ["body", field])

    def test_noop_patch_exact_live_failure_validation_response(self):
        self.open_record()
        self.close()
        self.correct(status="WFH")
        before = copy.deepcopy(self.database.attendance_logs.docs)
        self.assert_validation_contract(self.correct(), "body")
        self.assertEqual(self.database.attendance_logs.docs, before)

    def test_all_phase4_domain_validation_responses_follow_openapi(self):
        self.open_record()
        before = copy.deepcopy(self.database.attendance_logs.docs)
        self.assert_validation_contract(self.close(self.instant()), "punched_at")
        self.assert_validation_contract(self.close(self.instant(day=7) + 1000), "punched_at")
        self.assert_validation_contract(self.correct(punch_out=self.instant() - 1000), "punch_out")
        self.assert_validation_contract(self.correct(punch_out=self.instant(day=7) + 1000), "punch_out")
        self.assert_validation_contract(self.correct(punch_in=self.instant(day=7)), "punch_in")
        self.assertEqual(self.database.attendance_logs.docs, before)
        self.correct(status="ABSENT")
        before = copy.deepcopy(self.database.attendance_logs.docs)
        self.assert_validation_contract(self.correct(status="PRESENT"), "punch_in")
        self.assertEqual(self.database.attendance_logs.docs, before)

    def test_snapshot_rejects_changed_or_missing_legacy_fields(self):
        self.open_record()
        doc = self.database.attendance_logs.docs[0]
        query = main.attendance_snapshot_filter(doc)
        self.assertTrue(AtomicCollection.matches(doc, query))
        changed = {**doc, "history": [{"new": True}]}
        self.assertFalse(AtomicCollection.matches(changed, query))
        legacy = {key: value for key, value in doc.items() if key not in ("history", "half_day")}
        legacy_filter = main.attendance_snapshot_filter(legacy)
        self.assertTrue(AtomicCollection.matches(legacy, legacy_filter))
        self.assertFalse(AtomicCollection.matches(doc, legacy_filter))

    def test_calculations_presence_and_absence(self):
        doc = self.open_record()
        employee = self.database.employees.find_one({"emp_code": "EMP0001"})
        stored = self.database.attendance_logs.docs[0]
        updated = {**stored, "punch_out": main.from_epoch_ms(self.instant(hour=19, minute=0))}
        derived = main.attendance_calculations(updated, employee)
        self.assertEqual(derived, {"work_hours": 9.5, "late_minutes": 0,
                                   "overtime_minutes": 30, "half_day": False})
        self.assertEqual(main.attendance_calculations({**updated, "status": "LEAVE"}, employee),
                         {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})


if __name__ == "__main__":
    unittest.main()
