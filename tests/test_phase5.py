"""Isolated analytics pipeline execution with independent oracle/contract checks."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import unquote

import yaml
from bson import BSON
from pymongo.errors import OperationFailure

from test_phase3 import MemoryCollection, ROOT, main, request
from aggregation_memory import expression, pipeline
from final_phase3_verification import validate_schema
from phase5_fixtures import fixture_data, monthly_expected, summary_expected, leaderboard_expected, trend_expected


class AggregateCollection(MemoryCollection):
    def __init__(self, docs, collections):
        super().__init__(docs)
        self.collections = collections
        self.calls = []

    def aggregate(self, stages):
        self.calls.append(copy.deepcopy(stages))
        self.check_error()
        return iter(pipeline(self.docs, stages, self.collections))

    def insert_many(self, docs):
        for doc in docs:
            self.insert_one(doc)


class Phase5Tests(unittest.TestCase):
    def setUp(self):
        employees, records = fixture_data()
        self.collections = {"employees": employees, "attendance_logs": records}
        self.db = SimpleNamespace(employees=AggregateCollection(employees, self.collections),
                                  attendance_logs=AggregateCollection(records, self.collections))
        self.collections["employees"] = self.db.employees.docs
        self.collections["attendance_logs"] = self.db.attendance_logs.docs
        self.patch_db = patch.object(main, "db", self.db)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        self.contract = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))

    def http(self, path, query, contract_path=None):
        status, body = asyncio.run(request("GET", path, query=query))
        if contract_path is None:
            if path.startswith("/analytics/employees/"):
                contract_path = "/analytics/employees/{emp_code}/monthly"
            elif path.endswith("/trend"):
                contract_path = "/analytics/departments/{department}/trend"
            else:
                contract_path = path
        response = self.contract["paths"][contract_path]["get"]["responses"][str(status)]
        if "$ref" in response:
            response = self.contract["components"]["responses"][response["$ref"].split("/")[-1]]
        validate_schema(self.contract, response["content"]["application/json"]["schema"], body)
        return status, body

    def monthly(self, code="EMP9101", month="2026-07"):
        return self.http(f"/analytics/employees/{code}/monthly", {"month": month})

    def summary(self, month="2026-07", department=None):
        return self.http("/analytics/departments/summary", {"month": month, **({"department": department} if department is not None else {})})

    def leaderboard(self, month="2026-07", **query):
        return self.http("/analytics/leaderboard/late", {"month": month, **query})

    def trend(self, department="Analytics QA", first="2026-07-01", last="2026-07-10"):
        return self.http(f"/analytics/departments/{department}/trend", {"from": first, "to": last})

    def expected_monthly(self, code, month="2026-07"):
        return monthly_expected(self.collections["employees"], self.collections["attendance_logs"], code, month)

    def test_monthly_normal_stored_values_presence_weekends_leave_absence(self):
        status, result = self.monthly()
        self.assertEqual(status, 200)
        self.assertEqual(result, self.expected_monthly("EMP9101"))
        self.assertEqual((result["present_days"], result["leave_days"], result["total_late_minutes"], result["total_overtime_minutes"]), (2, 1, 50, 70))
        self.assertNotIn("absent_days", result)

    def test_monthly_mid_join_excludes_pre_join_presence_only(self):
        status, result = self.monthly("EMP9102")
        self.assertEqual((status, result), (200, self.expected_monthly("EMP9102")))
        self.assertEqual((result["present_days"], result["total_late_minutes"]), (1.5, 50))

    def test_monthly_zero_logs_and_join_after_month(self):
        for code in ("EMP9104", "EMP9105", "EMP9106"):
            with self.subTest(code=code):
                self.assertEqual(self.monthly(code), (200, self.expected_monthly(code)))
        self.assertIsNone(self.monthly("EMP9105")[1]["attendance_pct"])

    def test_monthly_unknown_employee(self):
        self.assertEqual(self.monthly("unknown")[0], 404)

    def test_all_month_validations_and_missing_required_parameter(self):
        for month in ("2026-00", "2026-13", "2026-1", "0000-01", "bad", "202607", "10000-01"):
            with self.subTest(month=month):
                for path in ("/analytics/employees/EMP9101/monthly", "/analytics/departments/summary", "/analytics/leaderboard/late"):
                    self.assertEqual(self.http(path, {"month": month})[0], 422)
        self.assertEqual(self.http("/analytics/employees/EMP9101/monthly", {})[0], 422)

    def test_monthly_leap_year_month_boundaries_and_last_calendar_year(self):
        self.collections["employees"][0]["joined_on"] = "2024-02-15"
        for month in ("2024-02", "2024-03", "2026-06", "2026-08", "9999-12"):
            with self.subTest(month=month):
                self.assertEqual(self.monthly(month=month), (200, self.expected_monthly("EMP9101", month)))
        self.assertEqual(self.monthly(month="2024-02")[1]["working_days"], 11)

    def test_monthly_percentage_half_up(self):
        self.collections["employees"][0]["joined_on"] = "2026-07-21"
        self.assertEqual(self.monthly(), (200, self.expected_monthly("EMP9101")))
        self.assertEqual(self.monthly()[1]["attendance_pct"], 11.11)

    def test_summary_all_departments_headcount_zero_logs_and_future_joiners(self):
        result = self.summary()
        self.assertEqual(result, (200, summary_expected(*fixture_data(), "2026-07")))
        self.assertEqual([item["department"] for item in result[1]["items"]], ["Analytics QA", "Analytics Quiet"])
        quiet = result[1]["items"][1]
        self.assertEqual((quiet["headcount"], quiet["present_days"], quiet["avg_work_hours"]), (2, 0, None))

    def test_summary_exact_case_sensitive_department_filter_and_no_match(self):
        for department in ("Analytics QA", "Analytics Quiet", "analytics qa", "unknown", "Analytics Future"):
            with self.subTest(department=department):
                self.assertEqual(self.summary(department=department), (200, summary_expected(*fixture_data(), "2026-07", department)))

    def test_summary_record_weighted_average_not_employee_averages(self):
        result = self.summary(department="Analytics QA")[1]["items"][0]
        self.assertEqual(result["avg_work_hours"], 4.14)
        self.assertEqual(result["headcount"], 4)

    def test_summary_decimal_half_up_boundary_and_null_hours_exclusion(self):
        self.collections["attendance_logs"][:] = [self.collections["attendance_logs"][1]]
        self.assertEqual(self.summary(department="Analytics QA")[1]["items"][0]["avg_work_hours"], 2.35)
        self.collections["attendance_logs"][0]["work_hours"] = None
        self.assertIsNone(self.summary(department="Analytics QA")[1]["items"][0]["avg_work_hours"])

    def test_leaderboard_competition_ties_gap_cutoff_and_stable_order(self):
        self.assertEqual(self.leaderboard(), (200, leaderboard_expected(*fixture_data(), "2026-07")))
        for limit in (1, 2, 3, 50):
            self.assertEqual(self.leaderboard(limit=limit), (200, leaderboard_expected(*fixture_data(), "2026-07", limit)))
        self.assertEqual([item["rank"] for item in self.leaderboard()[1]["items"]], [1, 1, 3, 4])
        self.assertEqual(len(self.leaderboard(limit=1)[1]["items"]), 2)

    def test_leaderboard_department_filter_before_ranking(self):
        for department in ("Analytics QA", "Analytics Future", "unknown"):
            self.assertEqual(self.leaderboard(department=department), (200, leaderboard_expected(*fixture_data(), "2026-07", department=department)))

    def test_leaderboard_no_late_records_single_employee_and_month_boundary(self):
        self.assertEqual(self.leaderboard(month="2026-08"), (200, {"month": "2026-08", "items": []}))
        self.assertEqual(self.leaderboard(month="2026-06"), (200, leaderboard_expected(*fixture_data(), "2026-06")))
        self.assertEqual(len(self.leaderboard(month="2026-06")[1]["items"]), 1)

    def test_leaderboard_invalid_limit(self):
        for limit in (0, 51, "bad", "1.5"):
            self.assertEqual(self.leaderboard(limit=limit)[0], 422)

    def test_trend_full_calendar_gaps_weekends_rates_and_window(self):
        result = self.trend()
        self.assertEqual(result, (200, trend_expected(*fixture_data(), "Analytics QA", "2026-07-01", "2026-07-10")))
        self.assertEqual(len(result[1]["items"]), 10)
        self.assertIsNone(result[1]["items"][3]["attendance_rate"])
        self.assertEqual(result[1]["items"][3]["present_count"], 1)
        self.assertEqual(result[1]["items"][2]["attendance_rate"], 0)

    def test_trend_zero_log_department_and_changing_headcount(self):
        result = self.trend("Analytics Quiet")
        self.assertEqual(result, (200, trend_expected(*fixture_data(), "Analytics Quiet", "2026-07-01", "2026-07-10")))
        self.assertEqual((result[1]["items"][0]["headcount"], result[1]["items"][7]["headcount"]), (1, 2))

    def test_trend_future_join_and_all_null_window(self):
        result = self.trend("Analytics Future", first="2026-07-01", last="2026-07-03")
        self.assertEqual(result[0], 200)
        self.assertTrue(all(item["attendance_rate"] is None and item["moving_avg_7d"] is None for item in result[1]["items"]))
        weekend = self.trend(first="2026-07-04", last="2026-07-05")[1]["items"]
        self.assertTrue(all(item["moving_avg_7d"] is None for item in weekend))

    def test_trend_requested_window_does_not_include_pre_range_rows(self):
        result = self.trend(first="2026-07-02", last="2026-07-09")
        self.assertEqual(result, (200, trend_expected(*fixture_data(), "Analytics QA", "2026-07-02", "2026-07-09")))
        self.assertEqual(result[1]["items"][0]["moving_avg_7d"], 0)

    def test_trend_inclusive_92_day_limit_and_invalid_ranges_dates(self):
        self.assertEqual(len(self.trend(first="2026-07-01", last="2026-09-30")[1]["items"]), 92)
        for first, last in (("2026-07-01", "2026-10-01"), ("2026-07-03", "2026-07-01"),
                            ("2026-02-30", "2026-03-01"), ("bad", "2026-07-01")):
            self.assertEqual(self.trend(first=first, last=last)[0], 422)
        self.assertEqual(self.trend(first="2026-07-01", last="2026-07-01")[0], 200)

    def test_trend_unknown_department_and_missing_parameters(self):
        self.assertEqual(self.trend("unknown")[0], 404)
        self.assertEqual(self.http("/analytics/departments/Analytics QA/trend", {"from": "2026-07-01"})[0], 422)

    def test_aggregation_rounding_positive_negative_null_and_four_places(self):
        for value, places, expected in (("2.345", 2, 2.35), ("-2.345", 2, -2.35), ("0.12345", 4, .1235), (None, 4, None)):
            self.assertEqual(expression(main.mongo_half_up(value, places), {}, {}), expected)

    def test_analytics_never_mutates_documents_or_audit_history(self):
        before = [BSON.encode(doc) for collection in self.collections.values() for doc in collection]
        self.monthly(); self.summary(); self.leaderboard(); self.trend()
        self.assertEqual([BSON.encode(doc) for collection in self.collections.values() for doc in collection], before)

    def test_all_analytics_are_aggregate_calls_without_python_collection_fetches(self):
        with patch.object(self.db.employees, "find", side_effect=AssertionError("raw employee fetch")), \
             patch.object(self.db.attendance_logs, "find", side_effect=AssertionError("raw log fetch")):
            self.monthly(); self.summary(); self.leaderboard(); self.trend()
        self.assertEqual(len(self.db.employees.calls), 3)
        self.assertEqual(len(self.db.attendance_logs.calls), 1)
        for stages in self.db.employees.calls + self.db.attendance_logs.calls:
            self.assertIn("$match", stages[0])
            self.assertNotIn("$out", json.dumps(stages))
            self.assertNotIn("$merge", json.dumps(stages))

    def test_schema_operation_ids_parameters_and_contract_admin_endpoint(self):
        actual = main.app.openapi()
        for path, expected in self.contract["paths"].items():
            if not path.startswith("/analytics"): continue
            operation = actual["paths"][path]["get"]
            self.assertEqual(operation["operationId"], expected["get"]["operationId"])
            self.assertEqual(set(operation["responses"]), set(expected["get"]["responses"]))
            parameters = [self.contract["components"]["parameters"][p["$ref"].split("/")[-1]] if "$ref" in p else p for p in expected["get"]["parameters"]]
            self.assertEqual({(p["name"], p["in"]) for p in operation["parameters"]}, {(p["name"], p["in"]) for p in parameters})
        self.assertEqual({path for path in actual["paths"] if path.startswith("/admin")}, {"/admin/explain/{endpoint}"})

    def test_database_outage_stays_sanitized(self):
        self.db.employees.error = OperationFailure("private connection details", code=13)
        status, response = asyncio.run(request("GET", "/analytics/employees/EMP9101/monthly", query={"month": "2026-07"}))
        self.assertEqual((status, response), (503, {"detail": "MongoDB unavailable"}))

    def test_phase5_live_workflow_isolated_and_oracle_checked(self):
        from final_phase5_verification import phase5_checks
        class OwnedDatabase(SimpleNamespace):
            def __getitem__(self, name):
                return getattr(self, name)
        self.collections["employees"].clear()
        self.collections["attendance_logs"].clear()
        name = "hrone_p5v_20261010_0123456789abcdef"
        owned = OwnedDatabase(name=name, employees=self.db.employees, attendance_logs=self.db.attendance_logs,
            _verification_owner=MemoryCollection([{"_id": "owner", "database": name, "run_token": "a" * 32}]))
        labels = []
        def http(method, path, expected, body=None, query=None):
            self.assertEqual(method, "GET")
            result = self.http(unquote(path), query or {})
            self.assertIn(result[0], expected)
            return result
        phase5_checks(http, owned, [], None, lambda label, **details: labels.append(label))
        self.assertEqual(len(labels), 6)
        self.assertEqual(labels[-1], "phase5_analytics_read_only")

    def test_phase5_fixture_seeding_refuses_unowned_or_wrong_database(self):
        from final_phase5_verification import seed_owned_fixtures
        from unittest.mock import MagicMock
        for name, owner in (("attendance_db", None), ("hrone_p4v_20261010_0123456789abcdef", None),
                            ("hrone_p5v_20261010_0123456789abcdef", None),
                            ("hrone_p5v_20261010_0123456789abcdef", {"database": "attendance_db", "run_token": "a" * 32})):
            database = MagicMock()
            database.name = name
            database.__getitem__.return_value.find_one.return_value = owner
            with self.subTest(name=name), self.assertRaises((AssertionError, RuntimeError)):
                seed_owned_fixtures(database)
            database.employees.insert_many.assert_not_called()
            database.attendance_logs.insert_many.assert_not_called()

    def test_phase5_database_name_length_collision_and_phase_guard(self):
        import final_phase3_verification as harness
        name = harness.generate_database_name("a" * 32, phase=5)
        self.assertEqual(len(name.encode("utf-8")), 35)
        self.assertTrue(name.startswith("hrone_p5v_"))
        harness.guard_database(name, name)
        with self.assertRaises(RuntimeError):
            harness.guard_database(name, name[:-1] + "b")
        with self.assertRaises(RuntimeError):
            harness.generate_database_name("a" * 32, phase=8)

    def test_trend_four_decimal_half_up_tie(self):
        # 1 / 32 = .03125: half-up must give .0313 rather than .0312.
        first = self.collections["employees"][0]
        self.collections["employees"][:] = [{**first, "emp_code": "EMP" + str(9200 + index)} for index in range(32)]
        self.collections["attendance_logs"][:] = [{"emp_code": "EMP9200", "date": "2026-07-01", "status": "PRESENT"}]
        result = self.trend(last="2026-07-01")
        self.assertEqual(result[1]["items"][0]["attendance_rate"], .0313)
        self.assertEqual(result[1]["items"][0]["moving_avg_7d"], .0313)

    def test_summary_empty_database_and_department_literal_with_dollar(self):
        self.collections["employees"].clear()
        self.assertEqual(self.summary(), (200, {"month": "2026-07", "items": []}))
        employee = fixture_data()[0][0]
        self.collections["employees"].append({**employee, "department": "$literal"})
        self.assertEqual(self.summary(department="$literal")[1]["items"][0]["department"], "$literal")


if __name__ == "__main__":
    unittest.main()
