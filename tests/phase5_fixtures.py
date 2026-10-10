"""Deterministic fixtures and independent Python oracles, for tests only."""
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def rounded(value, places):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def fixture_data():
    employees = []
    for code, name, department, joined in (
        ("EMP9101", "Alpha", "Analytics QA", "2026-07-01"),
        ("EMP9102", "Beta", "Analytics QA", "2026-07-06"),
        ("EMP9103", "Gamma", "Analytics QA", "2026-07-01"),
        ("EMP9104", "Zero", "Analytics Quiet", "2026-07-01"),
        ("EMP9105", "Future", "Analytics Future", "2026-08-01"),
        ("EMP9106", "Mid Zero", "Analytics QA", "2026-07-20"),
        ("EMP9107", "Next", "Analytics Quiet", "2026-07-08")):
        employees.append({"emp_code": code, "name": name, "department": department,
                          "joined_on": joined, "shift_start": "09:30", "shift_end": "18:30",
                          "email": code.lower() + "@example.com", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    records = []
    for code, day, status, hours, late, overtime, half in (
        ("EMP9101", "2026-06-30", "PRESENT", 9, 100, 0, False),
        ("EMP9101", "2026-07-01", "PRESENT", 2.345, 20, 30, False),
        ("EMP9101", "2026-07-02", "LEAVE", None, 0, 0, False),
        ("EMP9101", "2026-07-03", "ABSENT", None, 0, 0, False),
        ("EMP9101", "2026-07-04", "WFH", 8, 30, 40, False),
        ("EMP9101", "2026-07-31", "ON_DUTY", 4.5, 0, 0, False),
        ("EMP9102", "2026-07-01", "PRESENT", 1, 10, 0, True),
        ("EMP9102", "2026-07-06", "WFH", 2, 40, 0, True),
        ("EMP9102", "2026-07-07", "ON_DUTY", None, 0, 0, False),
        ("EMP9103", "2026-07-06", "PRESENT", 7, 30, 0, False),
        ("EMP9105", "2026-07-07", "PRESENT", 9, 5, 0, False),
        ("EMP9998", "2026-07-06", "PRESENT", 9, 999, 0, False)):
        instant = datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc)
        record = {"emp_code": code, "date": day, "status": status,
                  "punch_in": instant if status in PRESENT else None,
                  "punch_out": instant + timedelta(hours=9) if hours is not None else None,
                  "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
                  "half_day": half, "history": []}
        records.append(record)
    records[9].pop("half_day")
    records[9].pop("history")
    return employees, records


def monthly_expected(employees, records, code, month):
    employee = next(employee for employee in employees if employee["emp_code"] == code)
    year, number = map(int, month.split("-"))
    days = [date(year, number, day) for day in range(1, calendar.monthrange(year, number)[1] + 1)]
    working = sum(day.weekday() < 5 and day.isoformat() >= employee["joined_on"] for day in days)
    logs = [record for record in records if record["emp_code"] == code and record["date"].startswith(month + "-")]
    present = sum(0.5 if record.get("half_day", False) else 1 for record in logs
                  if record["status"] in PRESENT and date.fromisoformat(record["date"]).weekday() < 5
                  and record["date"] >= employee["joined_on"])
    return {"emp_code": code, "month": month, "working_days": working, "present_days": present,
            "leave_days": sum(record["status"] == "LEAVE" for record in logs),
            "late_count": sum(record.get("late_minutes", 0) > 0 for record in logs),
            "total_late_minutes": sum(record.get("late_minutes", 0) for record in logs),
            "total_overtime_minutes": sum(record.get("overtime_minutes", 0) for record in logs),
            "attendance_pct": rounded(Decimal(str(present)) / working * 100, 2) if working else None}


def summary_expected(employees, records, month, department=None):
    year, number = map(int, month.split("-"))
    last = date(year, number, calendar.monthrange(year, number)[1]).isoformat()
    eligible = [employee for employee in employees if employee["joined_on"] <= last
                and (department is None or employee["department"] == department)]
    items = []
    for name in sorted({employee["department"] for employee in eligible}):
        staff = [employee for employee in eligible if employee["department"] == name]
        codes = {employee["emp_code"] for employee in staff}
        logs = [record for record in records if record["emp_code"] in codes and record["date"].startswith(month + "-")]
        hours = [Decimal(str(record["work_hours"])) for record in logs if record["status"] in PRESENT and record.get("work_hours") is not None]
        items.append({"department": name, "headcount": len(staff),
            "present_days": sum(monthly_expected(employees, records, employee["emp_code"], month)["present_days"] for employee in staff),
            "avg_work_hours": rounded(sum(hours) / len(hours), 2) if hours else None,
            "late_count": sum(record.get("late_minutes", 0) > 0 for record in logs),
            "total_late_minutes": sum(record.get("late_minutes", 0) for record in logs),
            "leave_count": sum(record["status"] == "LEAVE" for record in logs),
            "on_duty_count": sum(record["status"] == "ON_DUTY" for record in logs)})
    return {"month": month, "items": items}


def leaderboard_expected(employees, records, month, limit=10, department=None):
    items = []
    for employee in employees:
        if department is not None and employee["department"] != department: continue
        logs = [record for record in records if record["emp_code"] == employee["emp_code"]
                and record["date"].startswith(month + "-") and record.get("late_minutes", 0) > 0]
        if logs:
            items.append({"emp_code": employee["emp_code"], "name": employee["name"], "department": employee["department"],
                          "total_late_minutes": sum(record["late_minutes"] for record in logs), "late_count": len(logs)})
    items.sort(key=lambda item: (-item["total_late_minutes"], item["emp_code"]))
    previous = None
    for position, item in enumerate(items, 1):
        if item["total_late_minutes"] != previous:
            rank = position
            previous = item["total_late_minutes"]
        item["rank"] = rank
    return {"month": month, "items": [item for item in items if item["rank"] <= limit]}


def trend_expected(employees, records, department, first, last):
    staff = [employee for employee in employees if employee["department"] == department]
    codes = {employee["emp_code"] for employee in staff}
    day, end = date.fromisoformat(first), date.fromisoformat(last)
    items = []
    while day <= end:
        iso = day.isoformat()
        logs = [record for record in records if record["date"] == iso and record["emp_code"] in codes]
        headcount = sum(employee["joined_on"] <= iso for employee in staff)
        present = sum(0.5 if record.get("half_day", False) else 1 for record in logs if record["status"] in PRESENT)
        rate = rounded(Decimal(str(present)) / headcount, 4) if headcount and day.weekday() < 5 else None
        rates = [item["attendance_rate"] for item in items[-6:] if item["attendance_rate"] is not None]
        if rate is not None: rates.append(rate)
        moving = rounded(sum(Decimal(str(value)) for value in rates) / len(rates), 4) if rates else None
        items.append({"date": iso, "is_working_day": day.weekday() < 5, "headcount": headcount,
                      "present_count": present, "late_count": sum(record.get("late_minutes", 0) > 0 for record in logs),
                      "attendance_rate": rate, "moving_avg_7d": moving})
        day += timedelta(days=1)
    return {"department": department, "items": items}
