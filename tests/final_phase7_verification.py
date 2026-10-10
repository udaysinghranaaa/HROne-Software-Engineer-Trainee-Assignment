"""Manual-only Phase 7 HTTP/Atlas verification in a strictly owned temporary DB."""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from bson import BSON
import final_phase3_verification as harness
from final_phase6_verification import phase6_checks
from phase5_fixtures import monthly_expected, summary_expected, leaderboard_expected, trend_expected


def simultaneous(calls):
    """Dispatch real HTTP clients together; do not synchronize server DB snapshots."""
    barrier = threading.Barrier(len(calls))
    def execute(call):
        barrier.wait(timeout=10)
        return call()
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(execute, calls))


def epoch(day='2026-07-06', clock='09:30:00'):
    return int(datetime.fromisoformat(day+'T'+clock).replace(tzinfo=harness.IST).timestamp())*1000


def phase7_checks(http, database, codes, inherited_epoch, passed):
    # Parent checks its exact ownership token immediately before this callback.
    harness.guard_database(database.name, database.name)
    assert database.name.startswith('hrone_p7v_'), 'Phase 7 database required'
    phase6_checks(http, database, codes, inherited_epoch, passed, fixture_phase=7)
    def create(code, overnight=False):
        return http('POST','/employees',[201,409],body={'emp_code':code,'name':'Phase Seven',
            'email':'phase7@example.com','department':'Phase7 QA','joined_on':'2024-01-01',
            'shift_start':'22:00' if overnight else '09:30','shift_end':'06:00' if overnight else '18:30'})
    def punch(code, at=None):
        return http('POST','/attendance/punch-in',[201,409],body={'emp_code':code,'punched_at':epoch() if at is None else at})
    def close(code, at=None):
        return http('POST','/attendance/punch-out',[200,409],body={'emp_code':code,'punched_at':epoch(clock='19:00:00') if at is None else at})
    def correct(code, day='2026-07-06', **changes):
        return http('PATCH',f'/attendance/{code}/{day}',[200,409],body={
            'reason':'Phase seven correction','regularized_by':'QA',**changes})

    assert database.employees.find_one({'emp_code':'EMP9701'}) is None, 'Phase 7 employee collision'
    results=simultaneous([lambda:create('EMP9701') for _ in range(5)])
    assert sorted(r[0] for r in results)==[201,409,409,409,409], 'Employee five-way race failed'
    results=simultaneous([lambda:punch('EMP9701') for _ in range(5)])
    assert sorted(r[0] for r in results)==[201,409,409,409,409], 'Punch-in five-way race failed'
    results=simultaneous([lambda:close('EMP9701') for _ in range(5)])
    assert sorted(r[0] for r in results)==[200,409,409,409,409], 'Punch-out five-way race failed'
    assert database.attendance_logs.count_documents({'emp_code':'EMP9701','date':'2026-07-06'})==1
    passed('phase7_five_way_create_punch_in_punch_out')

    assert create('EMP9702')[0]==201 and punch('EMP9702')[0]==201
    times=[epoch(clock=f'09:{minute}:00') for minute in (31,32,33,34,35)]
    results=simultaneous([lambda at=at:correct('EMP9702',punch_in=at) for at in times])
    winners=sum(r[0]==200 for r in results)
    record=database.attendance_logs.find_one({'emp_code':'EMP9702','date':'2026-07-06'})
    assert 1<=winners<=5 and len(record['history'])==winners, 'Correction history/winner mismatch'
    changes=[entry['changes']['punch_in'] for entry in record['history']]
    assert all(previous['to']==following['from'] for previous,following in zip(changes,changes[1:])), 'Lost correction chain'
    previous=[BSON.encode(entry) for entry in record['history']]
    assert correct('EMP9702',status='WFH')[0]==200
    record=database.attendance_logs.find_one({'emp_code':'EMP9702','date':'2026-07-06'})
    assert [BSON.encode(entry) for entry in record['history'][:-1]]==previous
    passed('phase7_correction_race_history_and_retry',successes=winners,statuses=[r[0] for r in results])

    assert create('EMP9703')[0]==201 and punch('EMP9703')[0]==201
    results=simultaneous([lambda:close('EMP9703'),lambda:correct('EMP9703',status='WFH')])
    assert any(r[0]==200 for r in results)
    if results[0][0]==409: assert close('EMP9703')[0]==200
    if results[1][0]==409: assert correct('EMP9703',status='WFH')[0]==200
    record=database.attendance_logs.find_one({'emp_code':'EMP9703','date':'2026-07-06'})
    assert record['status']=='WFH' and record['punch_out'] is not None and len(record['history'])==1
    passed('phase7_patch_punch_out_race_recovery',statuses=[r[0] for r in results])

    independent=[f'EMP{9710+i}' for i in range(5)]
    for code in independent: assert create(code)[0]==201
    assert all(r[0]==201 for r in simultaneous([lambda code=code:punch(code) for code in independent]))
    assert all(r[0]==200 for r in simultaneous([lambda code=code:correct(code,status='ON_DUTY') for code in independent]))
    for code in independent:
        record=database.attendance_logs.find_one({'emp_code':code,'date':'2026-07-06'})
        assert record['status']=='ON_DUTY' and len(record['history'])==1
    passed('phase7_independent_employee_concurrency')

    assert create('EMP9704',overnight=True)[0]==201
    assert punch('EMP9704',epoch('2026-12-31','22:00:00'))[0]==201
    closed=close('EMP9704',epoch('2027-01-01','06:30:01'))[1]
    assert (closed['date'],closed['work_hours'],closed['overtime_minutes'])==('2026-12-31',8.5,30)
    assert create('EMP9705')[0]==201
    for day,seconds,expected,half in [('2026-07-07',16181,4.49,True),('2026-07-08',16182,4.50,False)]:
        assert punch('EMP9705',epoch(day))[0]==201
        closed=close('EMP9705',epoch(day)+seconds*1000)[1]
        assert (closed['work_hours'],closed['half_day'])==(expected,half)
    passed('phase7_cross_year_overnight_half_up_boundaries')

    assert database.employees.count_documents({})<1000 and database.attendance_logs.count_documents({})<1000
    employees=list(database.employees.find({}).limit(1000));records=list(database.attendance_logs.find({}).limit(1000))
    before=([BSON.encode(item) for item in employees],[BSON.encode(item) for item in records])
    for month in ('2024-02','2026-07','2026-12','2027-01'):
        actual=http('GET','/analytics/employees/EMP9704/monthly',[200],query={'month':month})[1]
        assert actual==monthly_expected(employees,records,'EMP9704',month), 'Monthly oracle mismatch'
    actual=http('GET','/analytics/departments/summary',[200],query={'month':'2026-07','department':'Phase7 QA'})[1]
    assert actual==summary_expected(employees,records,'2026-07','Phase7 QA'), 'Summary oracle mismatch'
    actual=http('GET','/analytics/leaderboard/late',[200],query={'month':'2026-07','department':'Phase7 QA','limit':1})[1]
    assert actual==leaderboard_expected(employees,records,'2026-07',1,'Phase7 QA'), 'Leaderboard oracle mismatch'
    actual=http('GET','/analytics/departments/Phase7%20QA/trend',[200],query={'from':'2026-12-29','to':'2027-01-05'})[1]
    assert actual==trend_expected(employees,records,'Phase7 QA','2026-12-29','2027-01-05'), 'Trend oracle mismatch'
    assert before==([BSON.encode(item) for item in database.employees.find({}).limit(1000)],
                    [BSON.encode(item) for item in database.attendance_logs.find({}).limit(1000)])
    passed('phase7_extended_analytics_read_only_oracles')


if __name__=='__main__':
    harness.RESULT_FILE=harness.ROOT/'tests/phase7_final_results.json'
    sys.exit(harness.run_verification(extra_checks=phase7_checks,phase=7))
