"""Standard-library tests: no network, .env access, or live database writes.

Run from candidate_kit: .venv/Scripts/python.exe -B -m unittest discover -s tests -v
"""
import asyncio
import copy
import importlib
import json
import pathlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from bson import BSON, ObjectId, json_util
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError
from pydantic import ValidationError

ROOT = pathlib.Path(__file__).resolve().parents[1]


class MemoryCursor:
    def __init__(self, collection, docs):
        self.collection = collection
        self.docs = docs
        self.offset = 0
        self.size = None

    def sort(self, key, direction=None):
        keys = [(key, direction)] if isinstance(key, str) else key
        self.collection.last_sort = keys
        for field, order in reversed(keys):
            self.docs.sort(key=lambda doc: doc[field], reverse=order < 0)
        return self

    def skip(self, offset):
        self.offset = offset
        self.collection.last_skip = offset
        return self

    def limit(self, size):
        self.size = size
        self.collection.last_limit = size
        return self

    def __iter__(self):
        # Catch accidental materialization of the entire matching collection.
        if self.size is None:
            raise AssertionError("Listing must apply a database limit before iteration")
        selected = self.docs[self.offset:self.offset + self.size]
        self.collection.last_yielded = len(selected)
        return iter(copy.deepcopy(selected))


class MemoryCollection:
    def __init__(self, documents=()):
        self.docs = copy.deepcopy(list(documents))
        self.indexes = {}
        self.lock = threading.Lock()
        self.barrier = None
        self.error = None
        self.last_sort = self.last_skip = self.last_limit = self.last_yielded = None

    @staticmethod
    def matches(doc, query):
        for field, value in query.items():
            actual = doc.get(field)
            if isinstance(value, dict):
                if "$gte" in value and actual < value["$gte"]:
                    return False
                if "$lte" in value and actual > value["$lte"]:
                    return False
            elif actual != value:
                return False
        return True

    def check_error(self):
        if self.error:
            raise self.error

    def find_one(self, query):
        self.check_error()
        return next((copy.deepcopy(doc) for doc in self.docs if self.matches(doc, query)), None)

    def count_documents(self, query):
        self.check_error()
        self.count_filter = copy.deepcopy(query)
        return sum(self.matches(doc, query) for doc in self.docs)

    def find(self, query, projection=None):
        self.check_error()
        self.find_filter = copy.deepcopy(query)
        docs = [copy.deepcopy(doc) for doc in self.docs if self.matches(doc, query)]
        if projection and projection.get("_id") == 0:
            for doc in docs:
                doc.pop("_id", None)
        return MemoryCursor(self, docs)

    def insert_one(self, doc):
        self.check_error()
        if self.barrier:
            self.barrier.wait(timeout=5)
        with self.lock:
            for fields, unique in self.indexes.values():
                if unique and any(all(old.get(field) == doc.get(field) for field, _ in fields)
                                  for old in self.docs):
                    raise DuplicateKeyError("simulated database unique constraint")
            doc["_id"] = ObjectId()
            self.docs.append(copy.deepcopy(doc))
        return SimpleNamespace(inserted_id=doc["_id"])

    def aggregate(self, pipeline):
        self.check_error()
        keys = pipeline[0]["$group"]["_id"]
        counts = {}
        for doc in self.docs:
            key = tuple(doc.get(field) for field in keys)
            counts[key] = counts.get(key, 0) + 1
        return iter([{"count": count} for count in counts.values() if count > 1][:1])

    def create_index(self, fields, name, unique=False):
        self.check_error()
        spec = (fields, unique)
        if name in self.indexes and self.indexes[name] != spec:
            raise AssertionError("index changed between setups")
        self.indexes[name] = spec
        return name


class MemoryDatabase:
    def __init__(self):
        options = json_util.JSONOptions(tz_aware=True)
        self.employees = MemoryCollection(json_util.loads(
            (ROOT / "sample_data/employees.json").read_text(), json_options=options))
        self.attendance_logs = MemoryCollection(json_util.loads(
            (ROOT / "sample_data/attendance_logs.json").read_text(), json_options=options))
        for doc in self.employees.docs + self.attendance_logs.docs:
            doc["_id"] = ObjectId()

    def __getitem__(self, name):
        return getattr(self, name)


class MemoryClient:
    def __init__(self, *args, **kwargs):
        self.database = MemoryDatabase()
        self.error = None
        self.closed = False
        self.admin = self

    def __getitem__(self, name):
        return self.database

    def command(self, name):
        if self.error:
            raise self.error
        assert name == "ping"
        return {"ok": 1}

    def close(self):
        self.closed = True


# Patch before importing the application: tests cannot discover or access Atlas.
with patch("pymongo.MongoClient", MemoryClient), patch("dotenv.load_dotenv", lambda: False):
    main = importlib.import_module("app.main")


async def request(method, path, query=None, body=None):
    messages = []
    payload = b"" if body is None else json.dumps(body).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": urlencode(query or {}).encode(), "root_path": "",
             "headers": [(b"content-type", b"application/json")],
             "client": ("127.0.0.1", 1), "server": ("test", 80)}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        messages.append(message)

    await main.app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(raw)


class Phase3Tests(unittest.TestCase):
    def setUp(self):
        main.client = MemoryClient()
        main.db = main.client.database
        main.ensure_indexes(main.db)

    def http(self, method, path, query=None, body=None):
        return asyncio.run(request(method, path, query, body))

    def employee_body(self, **overrides):
        body = {"emp_code": "EMP9999", "name": "Test", "email": "test@example.com",
                "department": "QA", "joined_on": "2026-07-01"}
        return {**body, **overrides}

    @staticmethod
    def instant(hour=9, minute=30, second=0, day=6, microsecond=0):
        return datetime(2026, 7, day, hour, minute, second, microsecond, tzinfo=main.IST)

    def epoch(self, **kwargs):
        dt = self.instant(**kwargs)
        return int(dt.timestamp() * 1000)

    def assert_validation(self, status, body):
        self.assertEqual(status, 422, body)
        self.assertIsInstance(body["detail"], list)

    def test_health_ping_and_outage_D01(self):
        self.assertEqual(self.http("GET", "/health"), (200, {"status": "ok"}))
        main.client.error = ServerSelectionTimeoutError("secret connection details")
        self.assertEqual(self.http("GET", "/health"), (503, {"detail": "MongoDB unavailable"}))

    def test_employee_invalid_fields_D02(self):
        cases = [("emp_code", "EMP123"), ("emp_code", "EMP1234567"), ("emp_code", "X0001"),
                 ("name", ""), ("name", "x" * 101), ("email", "invalid"),
                 ("email", "x" * 121), ("email", "a b@example.com"),
                 ("department", ""), ("department", "x" * 51),
                 ("joined_on", "2026-02-29"), ("joined_on", "20260701"),
                 ("joined_on", "2026-7-01"), ("joined_on", "2026-13-01"),
                 ("shift_start", "24:00"), ("shift_end", "18:60"), ("shift_start", "9:30")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assert_validation(*self.http("POST", "/employees", body=self.employee_body(**{field: value})))
        self.assertEqual(len(main.db.employees.docs), 6)

    def test_employee_required_and_valid_boundaries_D02(self):
        for field in ["emp_code", "name", "email", "department", "joined_on"]:
            with self.subTest(missing=field):
                body = self.employee_body()
                del body[field]
                self.assert_validation(*self.http("POST", "/employees", body=body))
        status, body = self.http("POST", "/employees", body=self.employee_body(
            emp_code="EMP123456", name="x" * 100, department="x" * 50,
            email="x" * 114 + "@x.com", joined_on="2024-02-29"))
        self.assertEqual(status, 201)
        self.assertEqual((body["shift_start"], body["shift_end"]), ("09:30", "18:30"))

    def test_equal_shifts_D03(self):
        self.assert_validation(*self.http("POST", "/employees", body=self.employee_body(shift_end="09:30")))

    def test_employee_create_serialization_utc_D05_D06(self):
        status, body = self.http("POST", "/employees", body=self.employee_body(created_at=1, _id="client-id"))
        self.assertEqual(status, 201)
        self.assertEqual(set(body), set(main.Employee.model_fields))
        self.assertIs(type(body["created_at"]), int)
        stored = main.db.employees.docs[-1]
        self.assertEqual(stored["created_at"].utcoffset(), timedelta(0))
        self.assertEqual(stored["created_at"].microsecond, 0)
        self.assertEqual(body["created_at"], main.to_epoch_ms(stored["created_at"]))
        self.assertIsInstance(stored["_id"], ObjectId)

    def test_employee_duplicate_D04(self):
        self.assertEqual(self.http("POST", "/employees", body=self.employee_body())[0], 201)
        self.assertEqual(self.http("POST", "/employees", body=self.employee_body())[0], 409)
        self.assertEqual(sum(d["emp_code"] == "EMP9999" for d in main.db.employees.docs), 1)

    def test_employee_concurrency_D04(self):
        main.db.employees.barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.http("POST", "/employees", body=self.employee_body()), range(2)))
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(sum(d["emp_code"] == "EMP9999" for d in main.db.employees.docs), 1)

    def test_employee_first_page_sort_D07_D09(self):
        main.db.employees.docs.reverse()
        status, body = self.http("GET", "/employees")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 6)
        self.assertEqual(body["total"], 6)
        self.assertEqual([d["emp_code"] for d in body["items"]], [f"EMP000{i}" for i in range(1, 7)])
        self.assertEqual((body["page"], body["page_size"]), (1, 20))
        self.assertEqual(main.db.employees.last_skip, 0)
        status, body = self.http("GET", "/employees", {"page": 2, "page_size": 2})
        self.assertEqual([d["emp_code"] for d in body["items"]], ["EMP0003", "EMP0004"])
        self.assertEqual(main.db.employees.last_skip, 2)

    def test_department_filtered_count_D08(self):
        status, body = self.http("GET", "/employees", {"department": "Sales", "page_size": 1})
        self.assertEqual((status, body["total"], len(body["items"])), (200, 2, 1))
        self.assertEqual(main.db.employees.count_filter, main.db.employees.find_filter)
        for department in ["sales", "", "unknown"]:
            self.assertEqual(self.http("GET", "/employees", {"department": department})[1]["total"], 0)

    def test_pagination_validation_D10(self):
        for path in ["/employees", "/attendance"]:
            for field, value in [("page", 0), ("page", -1), ("page", "x"), ("page_size", 0),
                                 ("page_size", -1), ("page_size", 101), ("page_size", "1.5")]:
                with self.subTest(path=path, field=field, value=value):
                    self.assert_validation(*self.http("GET", path, {field: value}))
            self.assertEqual(self.http("GET", path, {"page_size": 100})[0], 200)
            self.assertEqual(self.http("GET", path, {"page": 1000})[1]["items"], [])

    def test_unknown_employee_D11(self):
        for code in ["EMP9999", "unknown"]:
            self.assertEqual(self.http("POST", "/attendance/punch-in", body={"emp_code": code})[0], 404)

    def test_timestamp_strict_bounds_D12(self):
        for value in [1783312500, 99999999999, 4102444800001, 1783312500000.0,
                      1783312500000.5, "1783312500000", True, False, None]:
            with self.subTest(value=value):
                self.assert_validation(*self.http("POST", "/attendance/punch-in",
                                                 body={"emp_code": "EMP0001", "punched_at": value}))
        for value in [100000000000, 4102444800000]:
            with self.subTest(bound=value):
                self.assertEqual(main.PunchInIn(emp_code="EMP0001", punched_at=value).punched_at, value)
        before = main.current_epoch_ms()
        model = main.PunchInIn(emp_code="EMP0001")
        self.assertLessEqual(before, model.punched_at)
        self.assertLessEqual(model.punched_at, main.current_epoch_ms())
        self.assertNotIn("punched_at", model.model_fields_set)

    def test_timestamp_omitted_uses_utc_D05_D12(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 20, 4, 0, 0, 900000, tzinfo=timezone.utc).astimezone(tz)
        with patch.object(main, "datetime", Clock):
            status, body = self.http("POST", "/attendance/punch-in", body={"emp_code": "EMP0001"})
        self.assertEqual(status, 201)
        self.assertEqual(body["date"], "2026-07-20")
        self.assertEqual(body["punch_in"], int(datetime(2026, 7, 20, 4, tzinfo=timezone.utc).timestamp() * 1000))

    def test_presence_statuses_D13(self):
        for status in ["ABSENT", "LEAVE", "INVALID", "present", None]:
            self.assert_validation(*self.http("POST", "/attendance/punch-in",
                                             body={"emp_code": "EMP0001", "status": status}))
        for index, status in enumerate(["PRESENT", "WFH", "ON_DUTY"]):
            code, body = self.http("POST", "/attendance/punch-in", body={
                "emp_code": "EMP0001", "status": status, "punched_at": self.epoch(day=20 + index)})
            self.assertEqual((code, body["status"]), (201, status))

    def test_ist_date_and_bson_instant_D05_D14(self):
        instant = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)
        epoch = int(instant.timestamp() * 1000)
        status, body = self.http("POST", "/attendance/punch-in", body={"emp_code": "EMP0001", "punched_at": epoch})
        self.assertEqual((status, body["date"], body["punch_in"]), (201, "2026-07-20", epoch))
        stored = main.db.attendance_logs.docs[-1]
        self.assertEqual(stored["punch_in"], instant)
        decoded = BSON(BSON.encode({"t": stored["punch_in"]})).decode()["t"]
        self.assertEqual(decoded.replace(tzinfo=timezone.utc), instant)
        self.assertEqual(main.to_epoch_ms(decoded), epoch)

    def test_fractional_truncation_D15(self):
        status, body = self.http("POST", "/attendance/punch-in", body={
            "emp_code": "EMP0001", "punched_at": self.epoch(day=20, minute=40, microsecond=900000)})
        self.assertEqual(status, 201)
        self.assertEqual(body["punch_in"] % 1000, 0)
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(main.db.attendance_logs.docs[-1]["punch_in"].microsecond, 0)

    def test_overnight_date_and_late_D16(self):
        status, body = self.http("POST", "/attendance/punch-in", body={
            "emp_code": "EMP0005", "punched_at": self.epoch(day=20, hour=0, minute=30)})
        self.assertEqual((status, body["date"], body["late_minutes"]), (201, "2026-07-19", 150))
        self.assertEqual(main.attendance_date(self.instant(day=20, hour=5, minute=59, second=59), "22:00", "06:00"), "2026-07-19")
        self.assertEqual(main.attendance_date(self.instant(day=20, hour=6, minute=0), "22:00", "06:00"), "2026-07-20")
        self.assertEqual(main.attendance_date(self.instant(day=20, hour=0, minute=30), "06:00", "06:00"), "2026-07-19")

    def test_grace_boundary_D17(self):
        for minute, second, expected in [(40, 0, 0), (40, 1, 10), (40, 59, 10), (41, 0, 11), (15, 59, 0)]:
            self.assertEqual(main.compute_late_minutes(self.instant(minute=minute, second=second), "09:30"), expected)
        self.assertEqual(main.compute_late_minutes(self.instant(hour=10, minute=15, second=59), "09:30"), 45)

    def test_punch_defaults_and_duplicate_D18_D19(self):
        body = {"emp_code": "EMP0001", "punched_at": self.epoch(day=20),
                "work_hours": 999, "half_day": True, "history": [{"bad": True}], "id": "bad"}
        status, result = self.http("POST", "/attendance/punch-in", body=body)
        self.assertEqual(status, 201)
        self.assertEqual(set(result), set(main.AttendanceRecord.model_fields))
        self.assertEqual(result["status"], "PRESENT")
        self.assertIsNone(result["punch_out"])
        self.assertIsNone(result["work_hours"])
        self.assertEqual((result["overtime_minutes"], result["half_day"], result["history"]), (0, False, []))
        self.assertEqual(self.http("POST", "/attendance/punch-in", body=body)[0], 409)

    def test_punch_concurrency_D18(self):
        main.db.attendance_logs.barrier = threading.Barrier(2)
        body = {"emp_code": "EMP0005", "punched_at": self.epoch(day=20, hour=0, minute=30)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.http("POST", "/attendance/punch-in", body=body), range(2)))
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(sum(d["emp_code"] == "EMP0005" and d["date"] == "2026-07-19"
                             for d in main.db.attendance_logs.docs), 1)

    def test_attendance_query_validation_D20(self):
        for query in [{"date_from": "bad"}, {"date_to": "2026-02-30"}, {"date_from": "20260701"},
                      {"status": "INVALID"}, {"date_from": "2026-07-10", "date_to": "2026-07-01"}]:
            self.assert_validation(*self.http("GET", "/attendance", query))

    def test_attendance_filters_pagination_D21(self):
        query = {"date_from": "2026-07-06", "date_to": "2026-07-07", "emp_code": "EMP0001", "page_size": 1}
        status, result = self.http("GET", "/attendance", query)
        self.assertEqual((status, result["total"], len(result["items"])), (200, 2, 1))
        self.assertEqual(result["items"][0]["date"], "2026-07-07")
        collection = main.db.attendance_logs
        self.assertEqual(collection.count_filter, collection.find_filter)
        self.assertEqual((collection.last_skip, collection.last_limit, collection.last_yielded), (0, 1, 1))
        query["page"] = 2
        self.assertEqual(self.http("GET", "/attendance", query)[1]["items"][0]["date"], "2026-07-06")
        for status in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            result = self.http("GET", "/attendance", {"status": status})[1]
            self.assertTrue(all(doc["status"] == status for doc in result["items"]))
        self.assertEqual(self.http("GET", "/attendance", {"emp_code": "unknown"})[1]["total"], 0)

    def test_attendance_sort_D22(self):
        main.db.attendance_logs.docs.reverse()
        status, body = self.http("GET", "/attendance")
        keys = [(d["date"], d["emp_code"]) for d in body["items"]]
        expected = sorted(keys, key=lambda pair: pair[1])
        expected.sort(key=lambda pair: pair[0], reverse=True)
        self.assertEqual(keys, expected)
        self.assertEqual(main.db.attendance_logs.last_sort, [("date", -1), ("emp_code", 1)])

    def test_history_legacy_no_mutation_D06_D19_D23(self):
        before = copy.deepcopy(main.db.attendance_logs.docs)
        status, body = self.http("GET", "/attendance")
        self.assertEqual(status, 200)
        for doc in body["items"]:
            self.assertEqual(set(doc), set(main.AttendanceRecord.model_fields))
            for field in ["punch_in", "punch_out"]:
                self.assertTrue(doc[field] is None or type(doc[field]) is int)
        legacy = next(doc for doc in body["items"] if doc["emp_code"] == "EMP0002")
        self.assertEqual((legacy["history"], legacy["half_day"]), ([], False))
        corrected = next(doc for doc in body["items"] if doc["history"])
        entry = corrected["history"][0]
        self.assertIs(type(entry["at"]), int)
        self.assertEqual(entry["changes"]["punch_in"], {"from": 1783398900000, "to": 1783396680000})
        self.assertEqual(entry["changes"]["work_hours"], {"from": 8.42, "to": 9.03})
        self.assertEqual(main.db.attendance_logs.docs, before)
        absent = next(doc for doc in body["items"] if doc["status"] == "ABSENT")
        self.assertIsNone(absent["punch_in"])

    def test_half_up_hours_and_half_day_D24(self):
        start = self.instant()
        for seconds, hours, half in [(18, 0.01, True), (54, 0.02, True), (16181, 4.49, True),
                                     (16182, 4.50, False), (16200, 4.50, False)]:
            with self.subTest(seconds=seconds):
                actual = main.compute_work_hours(start, start + timedelta(seconds=seconds))
                self.assertEqual(actual, hours)
                self.assertEqual(main.compute_half_day(actual), half)
        self.assertIsNone(main.compute_work_hours(start, None))
        self.assertFalse(main.compute_half_day(None))
        self.assertEqual(main.compute_work_hours(start, start + timedelta(seconds=54, microseconds=900000)), 0.02)

    def test_overtime_threshold_D25(self):
        end = self.instant(hour=18, minute=30)
        for seconds, expected in [(-60, 0), (300, 0), (1799, 0), (1800, 30), (1859, 30)]:
            self.assertEqual(main.compute_overtime(end + timedelta(seconds=seconds), "18:30", "2026-07-06", "09:30"), expected)

    def test_overnight_overtime_and_naive_utc_D26(self):
        end = self.instant(day=7, hour=6, minute=40)
        self.assertEqual(main.compute_overtime(end, "06:00", "2026-07-06", "22:00"), 40)
        naive = end.astimezone(timezone.utc).replace(tzinfo=None)
        self.assertEqual(main.compute_overtime(naive, "06:00", "2026-07-06", "22:00"), 40)
        start, finish = main.shift_bounds("2026-07-06", "06:00", "06:00")
        self.assertEqual(finish - start, timedelta(days=1))

    def test_indexes_idempotent_D27(self):
        before = copy.deepcopy((main.db.employees.docs, main.db.attendance_logs.docs))
        main.ensure_indexes(main.db)
        for collection, fields, name, unique in main.INDEXES:
            self.assertEqual(main.db[collection].indexes[name], (fields, unique))
        self.assertEqual(sum(len(main.db[name].indexes) for name in ["employees", "attendance_logs"]), 7)
        self.assertEqual((main.db.employees.docs, main.db.attendance_logs.docs), before)

    def test_duplicate_audit_stops_before_index_changes_D27(self):
        for collection in ["employees", "attendance_logs"]:
            with self.subTest(collection=collection):
                database = MemoryDatabase()
                database[collection].docs.append(copy.deepcopy(database[collection].docs[0]))
                before = copy.deepcopy(database[collection].docs)
                with self.assertRaisesRegex(RuntimeError, "Duplicate natural keys"):
                    main.ensure_indexes(database)
                self.assertEqual(database.employees.indexes, {})
                self.assertEqual(database.attendance_logs.indexes, {})
                self.assertEqual(database[collection].docs, before)

    def test_lifespan_index_setup_and_cleanup_D27(self):
        main.db = MemoryDatabase()
        async def run():
            async with main.lifespan(main.app):
                self.assertIn("employee_code_unique", main.db.employees.indexes)
                self.assertIn("attendance_employee_date_unique", main.db.attendance_logs.indexes)
            self.assertTrue(main.client.closed)
        asyncio.run(run())

    def test_lifespan_connects_only_at_startup(self):
        main.client = None
        main.db = None
        self.assertEqual(self.http("GET", "/health"), (503, {"detail": "MongoDB unavailable"}))
        async def run():
            with patch.object(main, "MongoClient", MemoryClient):
                async with main.lifespan(main.app):
                    self.assertEqual(main.health(), {"status": "ok"})
                    self.assertIn("employee_code_unique", main.db.employees.indexes)
            self.assertTrue(main.client.closed)
        asyncio.run(run())

    def test_lifespan_outage_sanitized_and_closed(self):
        main.client.error = ServerSelectionTimeoutError("private URI must not escape")
        async def run():
            with self.assertRaisesRegex(RuntimeError, "MongoDB startup/index setup failed") as context:
                async with main.lifespan(main.app):
                    self.fail("Startup cannot succeed without MongoDB")
            self.assertNotIn("private URI", str(context.exception))
            self.assertTrue(main.client.closed)
        asyncio.run(run())

    def test_lifespan_duplicate_failure_closes_client_D27(self):
        main.db = MemoryDatabase()
        main.db.attendance_logs.docs.append(copy.deepcopy(main.db.attendance_logs.docs[0]))
        async def run():
            with self.assertRaisesRegex(RuntimeError, "Duplicate natural keys"):
                async with main.lifespan(main.app):
                    self.fail("Duplicate data must stop startup")
            self.assertTrue(main.client.closed)
            self.assertEqual(main.db.employees.indexes, {})
        asyncio.run(run())

    def test_database_outage_safe_response(self):
        for path, collection in [("/employees", main.db.employees), ("/attendance", main.db.attendance_logs)]:
            collection.error = ServerSelectionTimeoutError("private connection string")
            self.assertEqual(self.http("GET", path), (503, {"detail": "MongoDB unavailable"}))

    def test_only_implemented_routes_and_contract_schemas(self):
        schema = main.app.openapi()
        operations = {(method.upper(), path) for path, item in schema["paths"].items() for method in item}
        self.assertEqual(operations, {("GET", "/health"), ("POST", "/employees"), ("GET", "/employees"),
                                      ("POST", "/attendance/punch-in"), ("GET", "/attendance"),
                                      ("POST", "/attendance/punch-out"), ("PATCH", "/attendance/{emp_code}/{date}"),
                                      ("GET", "/analytics/employees/{emp_code}/monthly"), ("GET", "/analytics/departments/summary"),
                                      ("GET", "/analytics/leaderboard/late"), ("GET", "/analytics/departments/{department}/trend"), ("GET", "/admin/explain/{endpoint}")})
        self.assertEqual(set(main.EmployeeIn.model_fields), {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on"})
        self.assertEqual(set(main.PunchInIn.model_fields), {"emp_code", "punched_at", "status"})
        employee = schema["paths"]["/employees"]
        self.assertEqual(employee["post"]["operationId"], "createEmployee")
        self.assertEqual(set(employee["post"]["responses"]), {"201", "409", "422"})
        self.assertEqual(set(schema["paths"]["/attendance/punch-in"]["post"]["responses"]), {"201", "404", "409", "422"})
        punch_schema = main.PunchInIn.model_json_schema()["properties"]["punched_at"]
        self.assertEqual((punch_schema["type"], punch_schema["minimum"], punch_schema["maximum"]),
                         ("integer", 100000000000, 4102444800000))
        self.assertNotIn("anyOf", punch_schema)
        for path, names in [("/employees", {"department", "page", "page_size"}),
                            ("/attendance", {"emp_code", "date_from", "date_to", "status", "page", "page_size"})]:
            params = schema["paths"][path]["get"]["parameters"]
            self.assertEqual({p["name"] for p in params}, names)
        status, body = self.http("GET", "/employees")
        self.assertEqual(set(body), {"items", "total", "page", "page_size"})
        self.assertIs(type(body["items"][0]["created_at"]), int)


if __name__ == "__main__":
    unittest.main()
