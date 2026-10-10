"""Real HTTP/MongoDB Phase 3 verification in a fresh, owned test database.

Run: .venv/Scripts/python.exe -B tests/final_phase3_verification.py
Only the generated test database is written/dropped. attendance_db is read-only.
No credentials, connection strings or raw database errors are emitted.
"""
import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bson import BSON
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
IST = timezone(timedelta(hours=5, minutes=30))
RESULT_FILE = ROOT / "tests/phase3_final_results.json"


def load_configuration():
    """Resolve the kit's .env independently of cwd; explicit environment wins."""
    load_dotenv(dotenv_path=ROOT / ".env", override=False)


def guard_database(name, expected):
    if len(name.encode("utf-8")) > 38:
        raise RuntimeError("Test database name exceeds the 38-byte safety limit")
    if name != expected or not re.fullmatch(r"hrone_p[345678]v_[0-9]{8}_[0-9a-f]{16}", name):
        raise RuntimeError("Unsafe test database identity; refusing writes or cleanup")
    if name == "attendance_db":
        raise RuntimeError("Development database must never be a write target")


def generate_database_name(token, phase=3):
    if phase not in (3, 4, 5, 6, 7):
        raise RuntimeError("Unsupported verification phase")
    name = f"hrone_p{phase}v_" + datetime.now(IST).strftime("%Y%m%d") + "_" + token[:16]
    guard_database(name, name)
    return name


def mongo_failure(error, operation):
    """Emit fixed categories only; never persist raw server messages or details."""
    message = str(error).lower()
    code = getattr(error, "code", None)
    if "database name" in message and any(word in message for word in ("too long", "length", "bytes")):
        category = "database_name_too_long"
    elif code == 13 or "not authorized" in message or "unauthorized" in message:
        category = "authorization"
    elif code == 18 or "authentication failed" in message:
        category = "authentication"
    elif any(word in message for word in ("tls", "ssl handshake")):
        category = "tls"
    elif any(word in message for word in ("dns", "resolution lifetime", "getaddrinfo")):
        category = "dns"
    elif "timed out" in message or "timeout" in message:
        category = "timeout"
    else:
        category = "mongodb_error"
    return {"operation": operation, "type": type(error).__name__,
            "code": code if type(code) is int else None, "category": category}


def development_snapshot(client):
    result = {}
    for name in ("employees", "attendance_logs"):
        collection = client["attendance_db"][name]
        count = collection.count_documents({})
        if count > 2000:
            raise RuntimeError("Read-only snapshot safety limit exceeded")
        digest = hashlib.sha256()
        for doc in collection.find({}).sort("_id", 1):
            digest.update(BSON.encode(doc))
        result[name] = {"count": count, "sha256": digest.hexdigest()}
    return result


def child_failure(error, operation):
    """Inspect suppressed exception context without emitting exception messages."""
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PyMongoError):
            return mongo_failure(current, operation)
        current = current.__cause__ or current.__context__
    category = "child_startup_error"
    if isinstance(error, ImportError):
        category = "import_error"
    elif isinstance(error, OSError):
        category = "os_error"
    elif "Duplicate natural keys" in str(error):
        category = "duplicate_natural_keys"
    elif operation in ("database_environment", "database_identity"):
        category = "database_identity_mismatch"
    return {"operation": operation, "type": type(error).__name__, "code": None, "category": category}


def write_child_failure(args, failure):
    destination = pathlib.Path(args.ready_file).with_suffix(".error.json")
    staging = destination.with_suffix(".tmp")
    staging.write_text(json.dumps(failure), encoding="utf-8")
    staging.replace(destination)


async def serve(args):
    diagnostics = {"operation": "database_environment"}
    try:
        await serve_child(args, diagnostics)
    except (Exception, SystemExit) as error:
        failure = diagnostics.get("failure") or child_failure(error, diagnostics["operation"])
        write_child_failure(args, failure)
        raise


async def serve_child(args, diagnostics):
    guard_database(args.database, args.database)
    if os.getenv("MONGO_DB") != args.database:
        raise RuntimeError("Child database environment does not match test identity")
    diagnostics["operation"] = "application_import"
    from app import main
    diagnostics["operation"] = "database_environment"
    if os.getenv("MONGO_DB") != args.database:
        raise RuntimeError("Child database environment changed during application import")
    import uvicorn
    diagnostics["operation"] = "uvicorn_configuration"

    async def diagnostic_app(scope, receive, send):
        try:
            await main.app(scope, receive, send)
        except Exception as error:
            if scope["type"] == "lifespan":
                diagnostics["failure"] = child_failure(error, "application_lifespan")
                write_child_failure(args, diagnostics["failure"])
            raise

    # Bind port 0 in the child itself: no released-port race with another server.
    server = uvicorn.Server(uvicorn.Config(diagnostic_app, host="127.0.0.1", port=0,
                                          log_level="critical", access_log=False))
    diagnostics["operation"] = "server_startup_or_port_bind"
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("Child server stopped before startup completed")
        await asyncio.sleep(0.01)
    diagnostics["operation"] = "database_identity"
    guard_database(main.db.name, args.database)
    port = server.servers[0].sockets[0].getsockname()[1]
    diagnostics["operation"] = "readiness_file"
    ready = pathlib.Path(args.ready_file)
    staging = ready.with_suffix(".tmp")
    staging.write_text(json.dumps({
        "pid": os.getpid(), "database": main.db.name, "token": args.token,
        "port": port,
    }), encoding="utf-8")
    staging.replace(ready)
    diagnostics["operation"] = "server_running"
    await task


def child_process_configuration(name):
    guard_database(name, name)
    load_configuration()
    env = os.environ.copy()
    env["MONGO_DB"] = name
    env["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    executable = sys.executable
    base = getattr(sys, "_base_executable", executable)
    if sys.platform == "win32" and os.path.normcase(executable) != os.path.normcase(base):
        # Same redirector bypass as CPython multiprocessing/popen_spawn_win32.py.
        # Popen must own the actual server PID, while retaining virtualenv paths.
        env["__PYVENV_LAUNCHER__"] = executable
        executable = base
    return executable, env


def validate_child_identity(identity, process, name, token):
    port = identity.get("port") if isinstance(identity, dict) else None
    assert type(port) is int and 0 < port <= 65535, "Invalid child server port"
    assert process.poll() is None, "Owned server process is no longer running"
    assert identity == {"pid": process.pid, "database": name, "token": token, "port": port}, "Wrong child process identity"
    guard_database(identity["database"], name)
    return port


def validate_schema(contract, schema, value):
    if "$ref" in schema:
        node = contract
        for part in schema["$ref"][2:].split("/"):
            node = node[part]
        return validate_schema(contract, node, value)
    for part in schema.get("allOf", []):
        validate_schema(contract, part, value)
    if value is None:
        assert schema.get("nullable", False), "Unexpected null response field"
        return
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict), "Response must be an object"
        assert set(schema.get("required", [])) <= set(value), "Required response fields missing"
        props = schema.get("properties", {})
        if props:
            assert set(value) <= set(props), "Unexpected response fields"
        for field, item in value.items():
            if field in props:
                validate_schema(contract, props[field], item)
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_schema(contract, schema["additionalProperties"], item)
    elif kind == "array":
        assert isinstance(value, list), "Response field must be an array"
        for item in value:
            validate_schema(contract, schema.get("items", {}), item)
    elif kind == "integer":
        assert type(value) is int, "Response field must be an integer"
    elif kind == "number":
        assert type(value) in (int, float), "Response field must be numeric"
    elif kind == "string":
        assert isinstance(value, str), "Response field must be a string"
    elif kind == "boolean":
        assert type(value) is bool, "Response field must be boolean"
    if "enum" in schema:
        assert value in schema["enum"], "Response enum value invalid"
    if "minimum" in schema:
        assert value >= schema["minimum"], "Response numeric minimum violated"
    if "maximum" in schema:
        assert value <= schema["maximum"], "Response numeric maximum violated"


def run_verification(extra_checks=None, phase=3, name_factory=None):
    load_configuration()
    from app import main
    token = uuid.uuid4().hex
    name = name_factory(token) if name_factory else generate_database_name(token, phase=phase)
    guard_database(name, name)
    result = {"database": name, "date_ist": datetime.now(IST).isoformat(),
              "checks": {}, "http_requests": 0, "status": "BLOCKED"}
    client = process = database = None
    owned = False
    before = None
    operation = "connect_database"
    temporary = tempfile.TemporaryDirectory(prefix="phase3_verify_")

    def passed(label, **details):
        result["checks"][label] = {"status": "PASS", **details}
        print("PASS", label, json.dumps(details))

    try:
        main.connect_database()
        client = main.client
        operation = "admin.ping"
        client.admin.command("ping")
        operation = "list_database_names.collision_check"
        assert name not in client.list_database_names(), "Generated test database already exists"
        operation = "attendance_db.development_snapshot"
        before = development_snapshot(client)
        result["development_counts_before"] = {key: value["count"] for key, value in before.items()}
        database = client[name]
        guard_database(database.name, name)
        operation = "_verification_owner.insert_one"
        marker = database["_verification_owner"].insert_one({"_id": "owner", "database": name, "run_token": token})
        if not marker.acknowledged:
            raise RuntimeError("Ownership marker insertion was not acknowledged; cleanup refused")
        owned = True

        ready = pathlib.Path(temporary.name) / "ready.json"
        executable, child_env = child_process_configuration(name)
        command = [executable, "-B", str(pathlib.Path(__file__).resolve()), "--serve",
                   "--database", name, "--token", token, "--ready-file", str(ready)]
        started = time.perf_counter()
        process = subprocess.Popen(command, cwd=ROOT, env=child_env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while not ready.exists():
            exit_code = process.poll()
            if exit_code is not None:
                result["child_exit_code"] = exit_code
                diagnostic_file = ready.with_suffix(".error.json")
                if diagnostic_file.exists():
                    result["child_diagnostics"] = json.loads(diagnostic_file.read_text(encoding="utf-8"))
                else:
                    result["child_diagnostics"] = {"category": "unavailable"}
                raise RuntimeError("Test server exited before readiness; no HTTP writes attempted")
            if time.perf_counter() - started > 20:
                result["startup_observed_seconds"] = round(time.perf_counter() - started, 3)
                raise AssertionError("Cold startup exceeded 20 seconds")
            time.sleep(0.025)
        identity = json.loads(ready.read_text(encoding="utf-8"))
        port = validate_child_identity(identity, process, name, token)
        base = "http://127.0.0.1:" + str(port)
        contract = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))

        def http(method, path, expected, body=None, query=None):
            assert process.poll() is None, "Owned server process is no longer running"
            guard_database(identity["database"], name)
            payload = None if body is None else json.dumps(body).encode()
            url = base + path + ("?" + urlencode(query) if query else "")
            request = Request(url, data=payload, method=method, headers={"Content-Type": "application/json"})
            try:
                response = urlopen(request, timeout=8)
            except HTTPError as error:
                response = error
            with response:
                status, data = response.status, json.load(response)
            result["http_requests"] += 1
            assert status in expected, "Unexpected HTTP status for " + method + " " + path + ": " + str(status)
            contract_path = "/attendance/{emp_code}/{date}" if method == "PATCH" else path
            if path.startswith("/analytics/employees/") and path.endswith("/monthly"):
                contract_path = "/analytics/employees/{emp_code}/monthly"
            elif path.startswith("/analytics/departments/") and path.endswith("/trend"):
                contract_path = "/analytics/departments/{department}/trend"
            if path.startswith("/admin/explain/"):
                contract_path = "/admin/explain/{endpoint}"
            response_schema = contract["paths"][contract_path][method.lower()]["responses"][str(status)]
            if "$ref" in response_schema:
                response_schema = contract["components"]["responses"][response_schema["$ref"].split("/")[-1]]
            validate_schema(contract, response_schema["content"]["application/json"]["schema"], data)
            return status, data

        assert http("GET", "/health", [200])[1] == {"status": "ok"}
        startup = time.perf_counter() - started
        assert startup < 20, "Readiness including MongoDB ping exceeded 20 seconds"
        passed("cold_startup", seconds=round(startup, 3), limit_seconds=20,
               includes="process spawn, imports, fresh MongoDB connection, new indexes, bind and health ping")
        passed("health")

        with urlopen(base + "/openapi.json", timeout=8) as response:
            actual = json.load(response)
        expected_ops = {("get", "/health"), ("post", "/employees"), ("get", "/employees"),
                        ("post", "/attendance/punch-in"), ("get", "/attendance"),
                        ("post", "/attendance/punch-out"), ("patch", "/attendance/{emp_code}/{date}"),
                        ("get", "/analytics/employees/{emp_code}/monthly"), ("get", "/analytics/departments/summary"),
                        ("get", "/analytics/leaderboard/late"), ("get", "/analytics/departments/{department}/trend"), ("get", "/admin/explain/{endpoint}")}
        assert {(method, path) for path, item in actual["paths"].items() for method in item} == expected_ops
        for method, path in expected_ops:
            assert actual["paths"][path][method]["operationId"] == contract["paths"][path][method]["operationId"]
            assert set(actual["paths"][path][method]["responses"]) == set(contract["paths"][path][method]["responses"])
        passed("registered_contract")

        codes = sorted({"EMP" + str(secrets.randbelow(900000) + 100000) for _ in range(10)})[:3]
        if phase == 8:
            # Disjoint from deterministic Phase 8 fixture and write-probe codes.
            codes = ["EMP700001", "EMP700002", "EMP700003"]
        assert len(codes) == 3
        def employee(code, department="QA", **extra):
            return {"emp_code": code, "name": "Phase 3 QA " + token[:8], "email": token[:8] + "@example.com",
                    "department": department, "joined_on": "2026-01-01", **extra}

        def concurrent(path, body):
            barrier = threading.Barrier(2)
            sent = []
            def send(_):
                barrier.wait(timeout=5)
                sent.append(time.perf_counter())
                return http("POST", path, [201, 409], body)
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(send, range(2)))
            statuses = sorted(status for status, _ in responses)
            assert statuses == [201, 409], "Concurrent requests must return exactly one 201 and one 409"
            return statuses, next(data for status, data in responses if status == 201), round(abs(sent[0] - sent[1]) * 1000, 3)

        statuses, created, gap = concurrent("/employees", employee(codes[0]))
        operation = "employees.count_documents"
        assert database.employees.count_documents({"emp_code": codes[0]}) == 1
        passed("employee_concurrency", statuses=statuses, stored_documents=1, dispatch_gap_ms=gap)
        http("POST", "/employees", [409], employee(codes[0]))
        http("POST", "/employees", [201], employee(codes[1], "Operations"))
        http("POST", "/employees", [201], employee(codes[2], shift_start="22:00", shift_end="06:00"))
        operation = "employees.find_one"
        stored = database.employees.find_one({"emp_code": codes[0]})
        assert stored["created_at"].utcoffset() == timedelta(0) and stored["created_at"].microsecond == 0
        assert main.to_epoch_ms(stored["created_at"]) == created["created_at"]
        for field, value in [("emp_code", "bad"), ("name", ""), ("email", "bad"), ("department", ""),
                             ("joined_on", "2026-02-30"), ("shift_start", "24:00"), ("shift_end", "09:30")]:
            invalid = employee("EMP999999")
            invalid[field] = value
            http("POST", "/employees", [422], invalid)
        operation = "employees.count_documents"
        assert database.employees.count_documents({}) == 3
        passed("employee_create_validation_and_storage")

        page = http("GET", "/employees", [200])[1]
        assert page["total"] == 3 and [item["emp_code"] for item in page["items"]] == codes
        assert (page["page"], page["page_size"]) == (1, 20)
        page = http("GET", "/employees", [200], query={"page": 2, "page_size": 1})[1]
        assert len(page["items"]) == 1 and page["items"][0]["emp_code"] == codes[1]
        page = http("GET", "/employees", [200], query={"department": "QA", "page_size": 1})[1]
        assert page["total"] == 2 and len(page["items"]) == 1
        assert http("GET", "/employees", [200], query={"department": "qa"})[1]["total"] == 0
        for path in ("/employees", "/attendance"):
            for query in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
                http("GET", path, [422], query=query)
        passed("employee_listing_and_pagination")

        def epoch(day, hour, minute, second=0, milliseconds=0):
            return int(datetime(2026, 7, day, hour, minute, second, tzinfo=IST).timestamp()) * 1000 + milliseconds
        punch = {"emp_code": codes[0], "punched_at": epoch(6, 9, 40, 1, 900)}
        statuses, punched, gap = concurrent("/attendance/punch-in", punch)
        operation = "attendance_logs.count_documents"
        assert database.attendance_logs.count_documents({"emp_code": codes[0], "date": "2026-07-06"}) == 1
        assert punched["late_minutes"] == 10 and punched["punch_in"] == punch["punched_at"] - 900
        assert punched["punch_out"] is None and punched["work_hours"] is None
        assert (punched["half_day"], punched["history"], punched["overtime_minutes"]) == (False, [], 0)
        operation = "attendance_logs.find_one"
        stored = database.attendance_logs.find_one({"emp_code": codes[0]})
        assert stored["punch_in"].utcoffset() == timedelta(0) and stored["punch_in"].microsecond == 0
        assert main.to_epoch_ms(stored["punch_in"]) == punched["punch_in"]
        passed("punch_concurrency", statuses=statuses, stored_documents=1, dispatch_gap_ms=gap)
        http("POST", "/attendance/punch-in", [409], punch)
        http("POST", "/attendance/punch-in", [404], {"emp_code": "unknown"})
        for value in (1783312500, "1783312500000", 1783312500000.0, True, None, 4102444800001):
            http("POST", "/attendance/punch-in", [422], {"emp_code": codes[0], "punched_at": value})
        for status in ("ABSENT", "LEAVE", "INVALID"):
            http("POST", "/attendance/punch-in", [422], {"emp_code": codes[0], "status": status})
        grace = http("POST", "/attendance/punch-in", [201], {
            "emp_code": codes[1], "punched_at": epoch(6, 9, 40, milliseconds=900), "status": "WFH",
            "work_hours": 999, "half_day": True})[1]
        assert grace["late_minutes"] == 0 and grace["work_hours"] is None and grace["half_day"] is False
        night = http("POST", "/attendance/punch-in", [201], {
            "emp_code": codes[2], "punched_at": epoch(7, 0, 30), "status": "ON_DUTY"})[1]
        assert (night["date"], night["late_minutes"]) == ("2026-07-06", 150)
        passed("punch_validation_dates_and_storage")

        guard_database(database.name, name)
        legacy = {"emp_code": codes[1], "date": "2026-07-14", "status": "PRESENT",
                  "punch_in": main.from_epoch_ms(epoch(14, 9, 32)), "punch_out": main.from_epoch_ms(epoch(14, 18, 31)),
                  "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0}
        history_at = epoch(8, 10, 30)
        corrected = {**legacy, "date": "2026-07-07", "punch_in": main.from_epoch_ms(epoch(7, 9, 28)),
                     "punch_out": main.from_epoch_ms(epoch(7, 18, 30)), "work_hours": 9.03,
                     "half_day": False, "history": [{"at": main.from_epoch_ms(history_at), "by": "phase3.qa",
                     "reason": "test-only correction fixture", "changes": {"punch_in": {
                         "from": main.from_epoch_ms(epoch(7, 10, 5)), "to": main.from_epoch_ms(epoch(7, 9, 28))}}}]}
        fixtures = [legacy, corrected]
        for day, status in [(8, "ABSENT"), (9, "LEAVE")]:
            fixtures.append({"emp_code": codes[1], "date": f"2026-07-{day:02d}", "status": status,
                             "punch_in": None, "punch_out": None, "work_hours": None,
                             "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []})
        operation = "attendance_logs.insert_many"
        database.attendance_logs.insert_many(fixtures)
        operation = "attendance_logs.find"
        fixture_digest = {str(doc["_id"]): hashlib.sha256(BSON.encode(doc)).hexdigest()
                          for doc in database.attendance_logs.find({"emp_code": codes[1]})}
        page = http("GET", "/attendance", [200])[1]
        assert page["total"] == 7 and len(page["items"]) == 7
        keys = [(item["date"], item["emp_code"]) for item in page["items"]]
        expected = sorted(keys, key=lambda pair: pair[1])
        expected.sort(key=lambda pair: pair[0], reverse=True)
        assert keys == expected
        old = next(item for item in page["items"] if item["date"] == "2026-07-14")
        assert old["history"] == [] and old["half_day"] is False
        edited = next(item for item in page["items"] if item["history"])
        assert edited["history"][0]["at"] == history_at
        assert edited["history"][0]["changes"]["punch_in"] == {"from": epoch(7, 10, 5), "to": epoch(7, 9, 28)}
        operation = "attendance_logs.find"
        assert fixture_digest == {str(doc["_id"]): hashlib.sha256(BSON.encode(doc)).hexdigest()
                                  for doc in database.attendance_logs.find({"emp_code": codes[1]})}
        page = http("GET", "/attendance", [200], query={"date_from": "2026-07-06", "date_to": "2026-07-06", "page_size": 1, "page": 2})[1]
        assert page["total"] == 3 and len(page["items"]) == 1 and page["items"][0]["emp_code"] == codes[1]
        page = http("GET", "/attendance", [200], query={"emp_code": codes[1], "status": "WFH"})[1]
        assert page["total"] == 1 and page["items"][0]["status"] == "WFH"
        assert http("GET", "/attendance", [200], query={"emp_code": "unknown"})[1]["items"] == []
        for query in ({"status": "INVALID"}, {"date_from": "bad"}, {"date_to": "2026-02-30"},
                      {"date_from": "2026-07-10", "date_to": "2026-07-01"}):
            http("GET", "/attendance", [422], query=query)
        passed("attendance_listing_filters_history_legacy")

        guard_database(database.name, name)
        operation = "ensure_indexes"
        main.ensure_indexes(database)
        indexes = []
        for collection, fields, index_name, unique in main.INDEXES:
            operation = collection + ".list_indexes"
            index = next(item for item in database[collection].list_indexes() if item["name"] == index_name)
            assert list(index["key"].items()) == fields and bool(index.get("unique", False)) == unique
            indexes.append({"collection": collection, "name": index_name, "keys": fields, "unique": unique})
        passed("indexes_and_repeated_setup", indexes=indexes)
        if extra_checks is not None:
            http.server_pid = process.pid
            http.ownership_token = token
            http.database_name = name
            guard_database(database.name, name)
            operation = "_verification_owner.find_one.before_extra_checks"
            assert database["_verification_owner"].find_one({"_id": "owner"}) == {
                "_id": "owner", "database": name, "run_token": token}, "Ownership mismatch before extra checks"
            operation = f"phase{phase}.additional_checks"
            extra_checks(http, database, codes, epoch, passed)
        result["status"] = "PASS"
    except PyMongoError as error:
        result["failure"] = mongo_failure(error, operation)
    except (RuntimeError, AssertionError, OSError, ValueError) as error:
        result["status"] = "FAIL" if isinstance(error, AssertionError) else "BLOCKED"
        result["failure"] = {"type": type(error).__name__, "detail": str(error) if isinstance(error, (AssertionError, RuntimeError)) else "Non-sensitive verification error"}
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            result["server_process_stopped"] = True
        if client is not None:
            try:
                if owned:
                    guard_database(database.name, name)
                    operation = "_verification_owner.find_one.cleanup"
                    owner = database["_verification_owner"].find_one({"_id": "owner"})
                    if owner != {"_id": "owner", "database": name, "run_token": token}:
                        raise RuntimeError("Ownership marker mismatch; cleanup refused")
                    operation = "drop_database.owned_test_database"
                    client.drop_database(name)
                    operation = "list_database_names.cleanup_check"
                    assert name not in client.list_database_names(), "Owned test database cleanup incomplete"
                    result["cleanup"] = "PASS"
                else:
                    result["cleanup"] = "NOT_NEEDED"
                if before is not None:
                    operation = "attendance_db.development_snapshot.cleanup"
                    assert development_snapshot(client) == before, "Development database snapshot changed"
                    passed("attendance_db_unchanged", counts=result["development_counts_before"])
            except (PyMongoError, RuntimeError, AssertionError) as error:
                result["cleanup"] = "BLOCKED" if isinstance(error, PyMongoError) else "FAIL"
                result["cleanup_error_type"] = type(error).__name__
                if isinstance(error, PyMongoError):
                    result["cleanup_failure"] = mongo_failure(error, operation)
                result["status"] = "BLOCKED" if isinstance(error, PyMongoError) else "FAIL"
            client.close()
        temporary.cleanup()
        RESULT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("FINAL_RESULT", json.dumps(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database")
    parser.add_argument("--token")
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve(args))
    else:
        sys.exit(run_verification())
