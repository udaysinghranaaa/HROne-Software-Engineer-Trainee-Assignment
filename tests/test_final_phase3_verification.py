"""Isolated harness safety tests: no application connection or live MongoDB use."""
import contextlib
import asyncio
import io
import json
import pathlib
import os
import subprocess
import sys
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pymongo.errors import ConfigurationError, OperationFailure

import final_phase3_verification as harness


class HarnessSafetyTests(unittest.TestCase):
    def test_generated_name_preserves_date_and_random_identifier(self):
        token = "0123456789abcdef" * 2
        name = harness.generate_database_name(token)
        self.assertRegex(name, r"^hrone_p3v_[0-9]{8}_0123456789abcdef$")
        self.assertEqual(len(name.encode("utf-8")), 35)
        self.assertIn(harness.datetime.now(harness.IST).strftime("%Y%m%d"), name)
        harness.guard_database(name, name)
        self.assertNotEqual(name, harness.generate_database_name("f" * 32))

    def test_utf8_byte_boundary(self):
        # Names within the byte limit must still satisfy the strict identity regex.
        for name in ("a" * 38, "é" * 19):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "Unsafe test database identity"):
                harness.guard_database(name, name)
        for name in ("a" * 39, "é" * 19 + "a", "é" * 20):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "38-byte"):
                harness.guard_database(name, name)

    def test_regex_and_identity_reject_unsafe_names(self):
        valid = "hrone_p3v_20261009_0123456789abcdef"
        invalid = ("attendance_db", "other_db", valid + "\n", valid.upper(),
                   valid[:-1], valid + "f", valid.replace("20261009", "２０２６１００９"),
                   valid.replace("p3v", "phase3_verify"), valid.replace("abcdef", "abcdeg"))
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                harness.guard_database(name, name)
        with self.assertRaises(RuntimeError):
            harness.guard_database(valid, valid[:-1] + "0")

    def test_invalid_generated_name_rejected_before_connection(self):
        main = SimpleNamespace(connect_database=MagicMock())
        with patch.dict("sys.modules", {"app": SimpleNamespace(main=main)}), \
             patch.object(harness, "generate_database_name", return_value="a" * 39):
            with self.assertRaisesRegex(RuntimeError, "38-byte"):
                harness.run_verification()
        main.connect_database.assert_not_called()

    def run_fake(self, insert_error=None, acknowledged=True, mismatch=False,
                 unsafe_identity=False, collision=False, cleanup_error=None, startup_failure=None):
        client = MagicMock()
        database = MagicMock()
        owner = MagicMock()
        client.__getitem__.return_value = database
        database.__getitem__.return_value = owner
        name = "hrone_p3v_20261009_0123456789abcdef"
        database.name = name
        client.list_database_names.return_value = [name] if collision else []

        def insert(document):
            if insert_error:
                raise insert_error
            owner.find_one.return_value = {**document, "run_token": "wrong"} if mismatch else dict(document)
            if unsafe_identity:
                database.name = "attendance_db"
            return SimpleNamespace(acknowledged=acknowledged)

        owner.insert_one.side_effect = insert
        if cleanup_error:
            owner.find_one.side_effect = cleanup_error
        main = SimpleNamespace(client=client, connect_database=MagicMock())
        snapshot = {"employees": {"count": 6}, "attendance_logs": {"count": 9}}
        def failed_child(command, **kwargs):
            ready = pathlib.Path(command[command.index("--ready-file") + 1])
            ready.with_suffix(".error.json").write_text(json.dumps(startup_failure), encoding="utf-8")
            return SimpleNamespace(pid=12345, poll=lambda: 1)
        with tempfile.TemporaryDirectory() as directory:
            result_file = pathlib.Path(directory) / "results.json"
            with patch.dict("sys.modules", {"app": SimpleNamespace(main=main)}), \
                 patch.object(harness, "generate_database_name", return_value=name), \
                 patch.object(harness, "development_snapshot", return_value=snapshot), \
                 patch.object(harness, "RESULT_FILE", result_file), \
                 patch.object(harness.subprocess, "Popen", side_effect=failed_child if startup_failure else OSError("fake process stop")) as process, \
                 contextlib.redirect_stdout(io.StringIO()):
                harness.run_verification()
            result = json.loads(result_file.read_text(encoding="utf-8"))
        return result, client, owner, process

    def test_acknowledged_marker_allows_verified_cleanup(self):
        result, client, owner, process = self.run_fake()
        owner.insert_one.assert_called_once()
        owner.find_one.assert_called_once_with({"_id": "owner"})
        client.drop_database.assert_called_once_with("hrone_p3v_20261009_0123456789abcdef")
        process.assert_called_once()
        self.assertEqual(result["cleanup"], "PASS")

    def test_failed_insertion_never_reads_marker_or_deletes(self):
        error = OperationFailure("database name too long: secret mongodb://hidden", code=8000)
        result, client, owner, process = self.run_fake(insert_error=error)
        owner.find_one.assert_not_called()
        client.drop_database.assert_not_called()
        process.assert_not_called()
        self.assertEqual(result["cleanup"], "NOT_NEEDED")
        self.assertEqual(result["failure"], {"operation": "_verification_owner.insert_one",
                         "type": "OperationFailure", "code": 8000, "category": "database_name_too_long"})
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("mongodb://", json.dumps(result))

    def test_unacknowledged_insertion_never_deletes(self):
        result, client, owner, process = self.run_fake(acknowledged=False)
        owner.find_one.assert_not_called()
        client.drop_database.assert_not_called()
        process.assert_not_called()
        self.assertEqual(result["cleanup"], "NOT_NEEDED")

    def test_wrong_ownership_token_prevents_cleanup(self):
        result, client, _, _ = self.run_fake(mismatch=True)
        client.drop_database.assert_not_called()
        self.assertEqual(result["cleanup"], "FAIL")

    def test_development_database_identity_prevents_cleanup(self):
        result, client, owner, _ = self.run_fake(unsafe_identity=True)
        owner.find_one.assert_not_called()
        client.drop_database.assert_not_called()
        self.assertEqual(result["cleanup"], "FAIL")

    def test_collision_prevents_insert_and_cleanup(self):
        result, client, owner, process = self.run_fake(collision=True)
        owner.insert_one.assert_not_called()
        client.drop_database.assert_not_called()
        process.assert_not_called()
        self.assertEqual(result["cleanup"], "NOT_NEEDED")

    def test_cleanup_error_reports_operation_and_code(self):
        result, client, _, _ = self.run_fake(cleanup_error=OperationFailure("not authorized secret", code=13))
        client.drop_database.assert_not_called()
        self.assertEqual(result["cleanup_failure"], {"operation": "_verification_owner.find_one.cleanup",
                         "type": "OperationFailure", "code": 13, "category": "authorization"})
        self.assertNotIn("secret", json.dumps(result))

    def test_error_categories_are_fixed_and_secret_free(self):
        for message, code, category in (("authentication failed", 18, "authentication"),
                                        ("TLS handshake failed", None, "tls"),
                                        ("DNS resolution lifetime expired", None, "dns"),
                                        ("timed out", None, "timeout"),
                                        ("private server details", 8000, "mongodb_error")):
            with self.subTest(category=category):
                report = harness.mongo_failure(OperationFailure(message + " password=hidden", code=code), "test.operation")
                self.assertEqual(report["category"], category)
                self.assertNotIn("hidden", json.dumps(report))

    def test_child_environment_explicitly_preserves_uri_and_overrides_database(self):
        name = "hrone_p3v_20261009_0123456789abcdef"
        with patch.dict(os.environ, {"MONGO_URI": "mongodb://fake.invalid", "MONGO_DB": "attendance_db"}):
            _, env = harness.child_process_configuration(name)
            self.assertEqual(env["MONGO_DB"], name)
            self.assertEqual(env["MONGO_URI"], "mongodb://fake.invalid")
            self.assertEqual(os.environ["MONGO_DB"], "attendance_db")
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_windows_redirector_bypass_preserves_virtualenv(self):
        with patch.object(sys, "platform", "win32"), \
             patch.object(sys, "executable", "venv-python.exe"), \
             patch.object(sys, "_base_executable", "base-python.exe"):
            executable, env = harness.child_process_configuration("hrone_p3v_20261009_0123456789abcdef")
        self.assertEqual(executable, "base-python.exe")
        self.assertEqual(env["__PYVENV_LAUNCHER__"], "venv-python.exe")

    def test_non_windows_keeps_current_interpreter(self):
        with patch.object(sys, "platform", "linux"):
            executable, _ = harness.child_process_configuration("hrone_p3v_20261009_0123456789abcdef")
        self.assertEqual(executable, sys.executable)

    def test_real_local_subprocess_pid_and_virtualenv_propagation(self):
        # Pure Python child: no application import, HTTP requests or MongoDB access.
        name = "hrone_p3v_20261009_0123456789abcdef"
        executable, env = harness.child_process_configuration(name)
        code = ('import json,os,sys; print(json.dumps({"pid":os.getpid(),'
                '"prefix":sys.prefix,"database":os.getenv("MONGO_DB")}))')
        process = subprocess.Popen([executable, "-B", "-c", code], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        identity = json.loads(output)
        self.assertEqual(identity["pid"], process.pid)
        self.assertEqual(identity["prefix"], sys.prefix)
        self.assertEqual(identity["database"], name)

    def test_strict_identity_accepts_expected_server(self):
        name = "hrone_p3v_20261009_0123456789abcdef"
        process = SimpleNamespace(pid=1234, poll=lambda: None)
        self.assertEqual(harness.validate_child_identity(
            {"pid": 1234, "database": name, "token": "owned", "port": 54321}, process, name, "owned"), 54321)

    def test_strict_identity_rejects_wrong_database_process_token_or_port(self):
        name = "hrone_p3v_20261009_0123456789abcdef"
        process = SimpleNamespace(pid=1234, poll=lambda: None)
        valid = {"pid": 1234, "database": name, "token": "owned", "port": 54321}
        for field, value in (("pid", 9999), ("database", "attendance_db"), ("token", "other"),
                             ("port", 0), ("port", 65536), ("port", True)):
            with self.subTest(field=field, value=value), self.assertRaises(AssertionError):
                harness.validate_child_identity({**valid, field: value}, process, name, "owned")
        with self.assertRaises(AssertionError):
            harness.validate_child_identity(valid, SimpleNamespace(pid=1234, poll=lambda: 0), name, "owned")

    def test_dotenv_does_not_override_explicit_child_database(self):
        from dotenv import load_dotenv
        name = "hrone_p3v_20261009_0123456789abcdef"
        with patch.dict(os.environ, {"MONGO_DB": name}):
            load_dotenv(stream=io.StringIO("MONGO_DB=attendance_db\n"))
            self.assertEqual(os.environ["MONGO_DB"], name)

    def run_fake_server(self, database_name=None, environment_name=None):
        name = "hrone_p3v_20261009_0123456789abcdef"
        server = SimpleNamespace(started=False, servers=[SimpleNamespace(sockets=[
            SimpleNamespace(getsockname=lambda: ("127.0.0.1", 54321))])])
        async def start():
            server.started = True
        server.serve = start
        uvicorn = SimpleNamespace(Config=MagicMock(), Server=MagicMock(return_value=server))
        main = SimpleNamespace(app=object(), db=SimpleNamespace(name=database_name or name))
        with tempfile.TemporaryDirectory() as directory:
            ready = pathlib.Path(directory) / "ready.json"
            args = SimpleNamespace(database=name, token="owned", ready_file=str(ready))
            with patch.dict(os.environ, {"MONGO_DB": environment_name or name}), \
                 patch.dict("sys.modules", {"app": SimpleNamespace(main=main), "uvicorn": uvicorn}):
                asyncio.run(harness.serve(args))
            return json.loads(ready.read_text(encoding="utf-8")), uvicorn

    def test_child_binds_port_zero_and_reports_its_owned_socket(self):
        identity, uvicorn = self.run_fake_server()
        self.assertEqual(uvicorn.Config.call_args.kwargs["port"], 0)
        self.assertEqual(uvicorn.Config.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(identity["port"], 54321)
        self.assertEqual(identity["pid"], os.getpid())

    def test_real_local_server_avoids_an_occupied_port(self):
        # Real loopback sockets and Uvicorn, but an empty app with no database code.
        import uvicorn
        from fastapi import FastAPI
        name = "hrone_p3v_20261009_0123456789abcdef"
        main = SimpleNamespace(app=FastAPI(), db=SimpleNamespace(name=name))
        servers = []
        real_server = uvicorn.Server
        def make_server(config):
            server = real_server(config)
            servers.append(server)
            return server
        with socket.socket() as occupied, tempfile.TemporaryDirectory() as directory:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            occupied_port = occupied.getsockname()[1]
            ready = pathlib.Path(directory) / "ready.json"
            args = SimpleNamespace(database=name, token="owned", ready_file=str(ready))
            async def verify():
                task = asyncio.create_task(harness.serve(args))
                try:
                    async def wait_ready():
                        while not ready.exists():
                            if task.done():
                                await task
                                self.fail("Local test server exited before readiness")
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(wait_ready(), timeout=5)
                    identity = json.loads(ready.read_text(encoding="utf-8"))
                    self.assertNotEqual(identity["port"], occupied_port)
                    self.assertEqual(identity["port"], servers[0].servers[0].sockets[0].getsockname()[1])
                    self.assertEqual(identity["database"], name)
                finally:
                    for server in servers:
                        server.should_exit = True
                    await asyncio.wait_for(task, timeout=5)
            with patch.dict(os.environ, {"MONGO_DB": name}), \
                 patch.dict("sys.modules", {"app": SimpleNamespace(main=main)}), \
                 patch.object(uvicorn, "Server", side_effect=make_server):
                asyncio.run(verify())

    def test_child_rejects_wrong_database_before_readiness(self):
        with self.assertRaisesRegex(RuntimeError, "Unsafe test database identity"):
            self.run_fake_server(database_name="attendance_db")

    def test_child_rejects_wrong_environment_before_server_start(self):
        with self.assertRaisesRegex(RuntimeError, "Child database environment"):
            self.run_fake_server(environment_name="attendance_db")

    def test_suppressed_mongo_startup_context_is_sanitized(self):
        for message, category in (("DNS failed mongodb://user:password@private", "dns"),
                                  ("TLS handshake failed mongodb://user:password@private", "tls")):
            with self.subTest(category=category):
                try:
                    try:
                        raise ConfigurationError(message)
                    except ConfigurationError:
                        raise RuntimeError("MongoDB startup/index setup failed") from None
                except RuntimeError as error:
                    report = harness.child_failure(error, "application_lifespan")
                self.assertEqual(report["type"], "ConfigurationError")
                self.assertEqual(report["category"], category)
                self.assertNotIn("password", json.dumps(report))
                self.assertNotIn("private", json.dumps(report))

    def test_failed_lifespan_writes_sanitized_diagnostics_before_readiness(self):
        async def failing_app(scope, receive, send):
            try:
                raise OperationFailure("not authorized mongodb://user:password@private", code=13)
            except OperationFailure:
                raise RuntimeError("MongoDB startup/index setup failed") from None
        server = SimpleNamespace(started=False)
        def make_server(application):
            async def run():
                try:
                    await application({"type": "lifespan"}, None, None)
                except RuntimeError:
                    pass  # Uvicorn reports failed startup and returns without readiness.
            server.serve = run
            return server
        uvicorn = SimpleNamespace(Config=lambda application, **kwargs: application, Server=make_server)
        name = "hrone_p4v_20261009_0123456789abcdef"
        main = SimpleNamespace(app=failing_app)
        with tempfile.TemporaryDirectory() as directory:
            ready = pathlib.Path(directory) / "ready.json"
            args = SimpleNamespace(database=name, ready_file=str(ready), token="owned")
            with patch.dict(os.environ, {"MONGO_DB": name}), \
                 patch.dict("sys.modules", {"app": SimpleNamespace(main=main), "uvicorn": uvicorn}):
                with self.assertRaisesRegex(RuntimeError, "before startup completed"):
                    asyncio.run(harness.serve(args))
            self.assertFalse(ready.exists())
            report = json.loads(ready.with_suffix(".error.json").read_text(encoding="utf-8"))
            self.assertEqual(report, {"operation": "application_lifespan", "type": "OperationFailure",
                                      "code": 13, "category": "authorization"})
            self.assertNotIn("password", json.dumps(report))

    def test_parent_records_child_exit_diagnostics_and_preserves_cleanup(self):
        failure = {"operation": "application_lifespan", "type": "ConfigurationError", "code": None, "category": "dns"}
        result, client, _, _ = self.run_fake(startup_failure=failure)
        self.assertEqual(result["child_exit_code"], 1)
        self.assertEqual(result["child_diagnostics"], failure)
        self.assertEqual(result["http_requests"], 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["cleanup"], "PASS")
        client.drop_database.assert_called_once_with("hrone_p3v_20261009_0123456789abcdef")

    def test_non_mongo_import_error_never_emits_sensitive_message(self):
        report = harness.child_failure(ImportError("private path and password=hidden"), "application_import")
        self.assertEqual(report["category"], "import_error")
        self.assertNotIn("hidden", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
