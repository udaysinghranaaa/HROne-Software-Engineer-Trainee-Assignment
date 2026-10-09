"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pymongo import MongoClient, ReturnDocument, timeout
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Establish connections in lifespan, so importing the module never needs DNS/network.
client = None
db = None

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Indexes required by the implemented employee and attendance endpoints.
INDEXES = (
    ("employees", [("emp_code", 1)], "employee_code_unique", True),
    ("employees", [("department", 1), ("emp_code", 1)], "employee_department_code", False),
    ("attendance_logs", [("emp_code", 1), ("date", 1)], "attendance_employee_date_unique", True),
    ("attendance_logs", [("date", -1), ("emp_code", 1)], "attendance_date_code", False),
    ("attendance_logs", [("emp_code", 1), ("punch_in", -1), ("date", -1)], "attendance_latest_punch", False),
)


def connect_database() -> None:
    global client, db
    client = MongoClient(
        os.getenv("MONGO_URI", "mongodb://localhost:27017"),
        tz_aware=True, connect=False, serverSelectionTimeoutMS=3000,
        connectTimeoutMS=5000, socketTimeoutMS=5000, timeoutMS=5000,
    )
    db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes(database) -> None:
    """Audit both natural keys before changing metadata; never repair data here."""
    for collection, fields, _, unique in INDEXES:
        if not unique:
            continue
        key = {field: f"${field}" for field, _ in fields}
        duplicates = database[collection].aggregate([
            {"$group": {"_id": key, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
            {"$limit": 1},
        ])
        if next(iter(duplicates), None) is not None:
            raise RuntimeError(f"Duplicate natural keys in {collection}; index setup stopped. No records changed.")
    for collection, fields, name, unique in INDEXES:
        database[collection].create_index(fields, name=name, unique=unique)


@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        # One budget for ping, duplicate audit and all startup index operations.
        with timeout(15):
            if client is None:
                connect_database()
            client.admin.command("ping")
            ensure_indexes(db)
    except PyMongoError:
        if client is not None:
            client.close()
        raise RuntimeError("MongoDB startup/index setup failed; check connectivity and index configuration.") from None
    except RuntimeError:
        if client is not None:
            client.close()
        raise
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def as_utc(value: datetime) -> datetime:
    """Naive datetimes read from legacy PyMongo clients represent UTC, not local time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_instant(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def from_epoch_ms(value: int) -> datetime:
    return EPOCH + timedelta(seconds=value // 1000)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    elapsed = truncate_instant(value) - EPOCH
    return (elapsed.days * 86400 + elapsed.seconds) * 1000


def current_epoch_ms() -> int:
    return to_epoch_ms(datetime.now(UTC))


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = truncate_instant(punch_in).astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.time() < time.fromisoformat(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(date_str)
    start = datetime.combine(day, time.fromisoformat(shift_start), IST)
    end = datetime.combine(day, time.fromisoformat(shift_end), IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    local = truncate_instant(punch_in).astimezone(IST)
    day = date.fromisoformat(date_str) if date_str is not None else local.date()
    start = datetime.combine(day, time.fromisoformat(shift_start), IST)
    seconds = int((local - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: Optional[datetime]) -> Optional[float]:
    if punch_out is None:
        return None
    duration = truncate_instant(punch_out) - truncate_instant(punch_in)
    seconds = duration.days * 86400 + duration.seconds
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and work_hours < 4.50


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    _, end = shift_bounds(date_str, shift_start, shift_end)
    minutes = int((truncate_instant(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    fields = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {**{field: doc[field] for field in fields}, "created_at": to_epoch_ms(doc["created_at"])}


def serialize_attendance(doc: dict) -> dict:
    """Build a response copy; do not normalize the database record in place."""
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry["changes"].items():
            changes[field] = {
                side: to_epoch_ms(value) if field in ("punch_in", "punch_out") else value
                for side, value in change.items()
            }
        history.append({"at": to_epoch_ms(entry["at"]), "by": entry["by"],
                        "reason": entry["reason"], "changes": changes})
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False), "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def validate_calendar_date(value: str) -> str:
    date.fromisoformat(value)
    return value


CalendarDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", json_schema_extra={"format": "date"}),
                         AfterValidator(validate_calendar_date)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000,
                                  json_schema_extra={"format": "int64"})]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: CalendarDate

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    # PunchInRequest deliberately has no employee-code pattern in openapi.yaml.
    emp_code: str
    punched_at: EpochMillis = Field(default_factory=current_epoch_ms)
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = Field(default_factory=current_epoch_ms)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Omitted fields are not applied. Explicit nulls are not in the request contract.
    status: Status = Field(default=None)
    punch_in: EpochMillis = Field(default=None)
    punch_out: EpochMillis = Field(default=None)
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def absence_without_punches(self):
        if self.status in ("ABSENT", "LEAVE") and self.model_fields_set & {"punch_in", "punch_out"}:
            raise ValueError("ABSENT or LEAVE cannot be supplied with punch times")
        return self


def attendance_validation_error(field: str, message: str):
    raise RequestValidationError([{"type": "value_error", "loc": ("body", field),
                                   "msg": message, "input": None}])


def chronological_punches(punch_in: datetime, punch_out: Optional[datetime], field: str = "punch_out"):
    if punch_out is not None:
        duration = truncate_instant(punch_out) - truncate_instant(punch_in)
        if not timedelta(0) < duration <= timedelta(hours=24):
            attendance_validation_error(field, "punch_out must be after punch_in and within 24 hours")


def attendance_calculations(doc: dict, employee: dict) -> dict:
    if doc["status"] in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = doc["punch_in"], doc.get("punch_out")
    hours = compute_work_hours(start, end)
    return {"work_hours": hours,
            "late_minutes": compute_late_minutes(start, employee["shift_start"], doc["date"]),
            "overtime_minutes": compute_overtime(end, employee["shift_end"], doc["date"], employee["shift_start"]) if end else 0,
            "half_day": compute_half_day(hours)}


def attendance_snapshot_filter(doc: dict) -> dict:
    """Compare stored values, including legacy missing fields, before an atomic write.

    The natural key identifies the record; the snapshot detects competing punch-out
    or correction updates without adding a revision field to the supplied schema.
    """
    query = {"emp_code": doc["emp_code"], "date": doc["date"]}
    query["$and"] = [{field: {"$eq": doc[field], "$exists": True}} if field in doc
                     else {field: {"$exists": False}}
                     for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
                                   "overtime_minutes", "half_day", "history")]
    return query


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ErrorResponse(BaseModel):
    detail: str


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: CalendarDate
    created_at: EpochMillis


class EmployeePage(BaseModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int


class HistoryEntry(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, dict]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: CalendarDate
    status: Status
    punch_in: Optional[EpochMillis]
    punch_out: Optional[EpochMillis]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


@app.exception_handler(PyMongoError)
async def database_error_handler(request, exception):
    return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse, operation_id="health",
         responses={503: {"model": ErrorResponse}})
def health():
    if client is None:
        raise HTTPException(503, "MongoDB unavailable")
    try:
        with timeout(3):
            client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201, response_model=Employee, operation_id="createEmployee",
          responses={409: {"model": ErrorResponse}})
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_instant(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return serialize_employee(doc)


@app.get("/employees", response_model=EmployeePage, operation_id="listEmployees")
def list_employees(department: Optional[str] = None,
                   page: Annotated[int, Query(ge=1)] = 1,
                   page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, operation_id="punchIn",
          responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_epoch_ms(body.punched_at)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return serialize_attendance(doc)


@app.get("/attendance", response_model=AttendancePage, operation_id="listAttendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[CalendarDate], Query()] = None,
    date_to: Annotated[Optional[CalendarDate], Query()] = None,
    status: Optional[Status] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "date_to"),
                                       "msg": "date_to must be on or after date_from", "input": date_to}])
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = (db.attendance_logs.find(q, {"_id": 0})
              .sort([("date", -1), ("emp_code", 1)])
              .skip((page - 1) * page_size).limit(page_size))
    page_docs = [serialize_attendance(doc) for doc in cursor]
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - later phases of the contract (see openapi.yaml):
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #


@app.post("/attendance/punch-out", response_model=AttendanceRecord, operation_id="punchOut",
          responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    instant = from_epoch_ms(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": instant}},
        sort=[("punch_in", -1), ("date", -1)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    chronological_punches(record["punch_in"], instant, field="punched_at")
    final = {**record, "punch_out": instant}
    # Punch-out does not alter punch-in, status, lateness or manual history.
    calculated = attendance_calculations(final, employee)
    updates = {"punch_out": instant, **{field: calculated[field]
               for field in ("work_hours", "overtime_minutes", "half_day")}}
    updated = db.attendance_logs.find_one_and_update(
        attendance_snapshot_filter(record), {"$set": updates}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance changed; retry with current values")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, operation_id="regularizeAttendance",
           responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def regularize_attendance(emp_code: str, date: Annotated[CalendarDate, Path()], body: RegularizeIn):
    # The contract uses a plain string code here; unknown codes are 404, not a new pattern rule.
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")
    final = {**record}
    final.setdefault("punch_in", None)
    final.setdefault("punch_out", None)
    for field in body.model_fields_set & {"status", "punch_in", "punch_out"}:
        value = getattr(body, field)
        final[field] = from_epoch_ms(value) if field in ("punch_in", "punch_out") else value
    if final["status"] in ("ABSENT", "LEAVE"):
        final.update(punch_in=None, punch_out=None)
    else:
        if final.get("punch_in") is None:
            attendance_validation_error("punch_in", "presence status requires punch_in")
        final["punch_in"] = truncate_instant(final["punch_in"])
        if final.get("punch_out") is not None:
            final["punch_out"] = truncate_instant(final["punch_out"])
        if attendance_date(final["punch_in"], employee["shift_start"], employee["shift_end"]) != date:
            attendance_validation_error("punch_in", "punch_in must remain on the attendance date")
        chronological_punches(final["punch_in"], final.get("punch_out"))
    final.update(attendance_calculations(final, employee))
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {field: {"from": record.get(field, defaults.get(field)), "to": final[field]}
               for field in fields if record.get(field, defaults.get(field)) != final[field]}
    if not changes:
        attendance_validation_error("body", "correction changes nothing")
    entry = {"at": truncate_instant(datetime.now(UTC)), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        attendance_snapshot_filter(record),
        {"$set": {field: change["to"] for field, change in changes.items()}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance changed; retry with current values")
    return serialize_attendance(updated)
