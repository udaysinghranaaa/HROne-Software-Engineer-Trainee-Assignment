"""Streaming, deterministic benchmark data. No application imports or database I/O."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from itertools import islice
from bson import BSON

ATTENDANCE_COUNT = 100_000
EMPLOYEE_COUNT = 1050
IST = timezone(timedelta(hours=5, minutes=30))


def employees():
    for i in range(EMPLOYEE_COUNT):
        yield {'emp_code': f'EMP{800000+i}', 'name': f'Benchmark {i}',
               'email': f'benchmark{i}@example.com',
               'department': 'P8Zero' if i >= 1000 else 'P8Probe' if i < 5 else f'P8D{i%5}',
               'joined_on': ('2023-12-01', '2024-01-15', '2024-02-01')[i % 3],
               'shift_start': '22:00' if i % 7 == 0 else '09:30',
               'shift_end': '06:00' if i % 7 == 0 else '18:30',
               'created_at': datetime(2023, 12, 1, tzinfo=timezone.utc)}


def attendance(employee):
    """100 unique days per logged employee, with gaps and unequal monthly density."""
    i = int(employee['emp_code'][3:]) - 800000
    if i >= 1000:
        return
    first = max(date(2024, 1, 1), date.fromisoformat(employee['joined_on']))
    for n in range(100):
        day = first + timedelta(days=n + n//9)
        # Identical schedules for pairs create reproducible leaderboard ties.
        selector = (i//2 + n) % 5
        status = ('PRESENT', 'WFH', 'ON_DUTY', 'ABSENT', 'LEAVE')[selector]
        start = datetime.fromisoformat(day.isoformat()+'T'+employee['shift_start']).replace(tzinfo=IST)
        end = datetime.fromisoformat(day.isoformat()+'T'+employee['shift_end']).replace(tzinfo=IST)
        if end <= start:
            end += timedelta(days=1)
        late = 1200 if n % 4 == 0 else 0
        pin = start + timedelta(seconds=late)
        pout = pin + timedelta(seconds=16181) if n % 11 == 0 else end + timedelta(minutes=45 if n % 6 == 0 else 0)
        present = selector < 3
        hours = float((Decimal(int((pout-pin).total_seconds()))/3600).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)) if present else None
        overtime = max(0, int((pout-end).total_seconds())//60) if present else 0
        record = {'emp_code': employee['emp_code'], 'date': day.isoformat(), 'status': status,
                  'punch_in': pin.astimezone(timezone.utc) if present else None,
                  'punch_out': pout.astimezone(timezone.utc) if present else None,
                  'work_hours': hours, 'late_minutes': late//60 if present else 0,
                  'overtime_minutes': overtime if overtime >= 30 else 0,
                  'half_day': hours < 4.5 if present else False, 'history': []}
        if status == 'WFH' and n % 13 == 0:
            record['history'] = [{'at': pout.astimezone(timezone.utc)+timedelta(minutes=1),
                'by': 'Benchmark QA', 'reason': 'Approved work from home',
                'changes': {'status': {'from': 'PRESENT', 'to': 'WFH'}}}]
        yield record


def records():
    for employee in employees():
        yield from attendance(employee)


def batches(source, size=100):
    if type(size) is not int or not 1 <= size <= 100:
        raise ValueError('Batch size must be 1..100')
    iterator = iter(source)
    while batch := list(islice(iterator, size)):
        yield batch


def estimate():
    # Add 17 BSON bytes per omitted ObjectId. Storage compression is not assumed.
    logical = sum(len(BSON.encode(doc))+17 for doc in employees())
    logical += sum(len(BSON.encode(doc))+17 for doc in records())
    return {'planned_employees': EMPLOYEE_COUNT, 'planned_attendance': ATTENDANCE_COUNT,
            'estimated_logical_bson_bytes': logical,
            'advisory_capacity_budget_bytes': logical*3 + 32*1024*1024,
            'estimate_method': 'BSON plus ObjectId; 3x plus 32MiB advisory index/headroom budget; not measured storage'}
