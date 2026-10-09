"""Opt-in live verification: reads records and creates only the app's indexes.

Run from candidate_kit: .venv/Scripts/python.exe -B tests/live_phase3.py --create-indexes
No insert/update/delete/seed operations are performed. Connection details are never printed.
"""
import argparse
import asyncio
import hashlib
import pathlib
import sys
import time

from bson import BSON
from pymongo.errors import PyMongoError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app import main


def snapshot():
    result = {}
    for name in ("employees", "attendance_logs"):
        count = main.db[name].count_documents({})
        if count > 2000:
            raise RuntimeError("Live safety check limited to 2000 existing records per collection")
        digest = hashlib.sha256()
        for doc in main.db[name].find({}).sort("_id", 1):
            digest.update(BSON.encode(doc))
        result[name] = (count, digest.hexdigest())
    return result


async def run(create_indexes):
    if main.db.name != "attendance_db":
        raise RuntimeError("This verification script is restricted to attendance_db")
    main.client.admin.command("ping")
    before = snapshot()
    print("LIVE_COUNTS_BEFORE", {name: value[0] for name, value in before.items()})
    for name in before:
        print("INDEXES_BEFORE", name, [(dict(i["key"]), i.get("unique", i["name"] == "_id_"))
                                      for i in main.db[name].list_indexes()])
    if not create_indexes:
        print("READ_ONLY_COMPLETE: pass --create-indexes to test actual startup index setup")
        return
    # lifespan calls ensure_indexes, which audits BOTH collections before the first index write.
    started = time.monotonic()
    async with main.lifespan(main.app):
        print("STARTUP_SECONDS", round(time.monotonic() - started, 3))
        assert time.monotonic() - started < 20, "Startup exceeded 20 seconds"
        assert main.health() == {"status": "ok"}
        print("LIVE_HEALTH", "PASS")
        main.ensure_indexes(main.db)
        print("REPEATED_INDEX_SETUP", "PASS")
        for collection, fields, name, unique in main.INDEXES:
            index = next(i for i in main.db[collection].list_indexes() if i["name"] == name)
            assert list(index["key"].items()) == fields
            assert bool(index.get("unique", False)) == unique
            print("LIVE_INDEX", collection, name, "PASS", "unique=" + str(unique))
        employees = main.list_employees(page=1, page_size=20)
        main.EmployeePage.model_validate(employees)
        assert employees["total"] == before["employees"][0]
        assert len(employees["items"]) == min(20, employees["total"])
        assert [e["emp_code"] for e in employees["items"]] == sorted(e["emp_code"] for e in employees["items"])
        sales = main.list_employees(department="Sales", page=1, page_size=20)
        assert sales["total"] == main.db.employees.count_documents({"department": "Sales"})
        attendance = main.list_attendance(page=1, page_size=20)
        main.AttendancePage.model_validate(attendance)
        assert attendance["total"] == before["attendance_logs"][0]
        print("LIVE_LISTS_AND_SCHEMAS", "PASS", "sales_total=" + str(sales["total"]))
        after = snapshot()
        assert after == before, "Existing database record contents changed"
        print("LIVE_RECORD_CONTENTS_UNCHANGED", "PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-indexes", action="store_true")
    args = parser.parse_args()
    try:
        main.connect_database()
        asyncio.run(run(args.create_indexes))
    except PyMongoError as exc:
        print("LIVE_CHECK_FAILED", type(exc).__name__, "connection details suppressed")
        sys.exit(1)
    except (RuntimeError, AssertionError) as exc:
        print("LIVE_CHECK_FAILED", str(exc))
        sys.exit(1)
    finally:
        if main.client is not None:
            main.client.close()
