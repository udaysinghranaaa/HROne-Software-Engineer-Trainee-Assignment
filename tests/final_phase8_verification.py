"""Manual-only 100k benchmark. Default execution is an offline plan, never live I/O."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
import pathlib
import re
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import final_phase3_verification as harness
from final_phase6_verification import inspect_plan
from final_phase7_verification import simultaneous, epoch
from phase5_fixtures import monthly_expected, summary_expected, leaderboard_expected, trend_expected
import phase8_fixtures as fixtures


def database_name(token):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise RuntimeError('Invalid ownership token')
    name = 'hrone_p8v_'+datetime.now(harness.IST).strftime('%Y%m%d')+'_'+token[:16]
    harness.guard_database(name, name)
    return name


def verify_owner(database, name, token):
    harness.guard_database(database.name, name)
    if not name.startswith('hrone_p8v_') or not re.fullmatch('[0-9a-f]{32}', token):
        raise RuntimeError('Phase 8 ownership identity required')
    if database['_verification_owner'].find_one({'_id': 'owner'}) != {
            '_id': 'owner', 'database': name, 'run_token': token}:
        raise RuntimeError('Ownership mismatch; writes refused')


def capacity_gate(document, budget, now=None):
    """Operator verifies Atlas UI: dbStats cannot establish deployment-wide headroom."""
    if not isinstance(document, dict) or set(document) != {'tier', 'available_bytes', 'verified_at', 'same_deployment', 'source'}:
        raise RuntimeError('Invalid capacity attestation fields')
    limits = {'M0': 512*1024*1024, 'FLEX': 5*1024*1024*1024}
    available = document['available_bytes']
    if (document['tier'] not in limits or type(available) is not int or
            not budget <= available <= limits[document['tier']] or
            document['same_deployment'] is not True or document['source'] != 'Atlas UI'):
        raise RuntimeError('Deployment capacity not approved; insertion blocked')
    try:
        instant = datetime.fromisoformat(document['verified_at'].replace('Z', '+00:00'))
        age = ((now or datetime.now(timezone.utc))-instant).total_seconds()
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError('Invalid capacity verification timestamp') from None
    if not 0 <= age <= 86400:
        raise RuntimeError('Capacity attestation must be within 24 hours (advisory safety gate)')
    return {'tier': document['tier'], 'available_bytes': available, 'source': 'operator Atlas UI attestation'}


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise RuntimeError('Benchmark execution budget exhausted')


def load_fixtures(database, name, token, deadline, sleep=time.sleep):
    """One paced writer; exact owner checked before every acknowledged batch."""
    inserted = Counter()
    for collection, source in [('employees', fixtures.employees()), ('attendance_logs', fixtures.records())]:
        for batch in fixtures.batches(source):
            check_deadline(deadline)
            verify_owner(database, name, token)
            started = time.monotonic()
            try:
                result = database[collection].insert_many(batch, ordered=True)
            except Exception as error:
                from pymongo.errors import PyMongoError
                if isinstance(error, PyMongoError):
                    # Keep raw server messages out of shared parent diagnostics.
                    raise RuntimeError(json.dumps(harness.mongo_failure(error, collection+'.insert_many'))) from None
                raise
            if not result.acknowledged:
                raise RuntimeError('Fixture insertion not acknowledged')
            inserted[collection] += len(result.inserted_ids)
            sleep(max(0, len(batch)/50-(time.monotonic()-started)))
    assert inserted == {'employees': 1050, 'attendance_logs': 100000}, 'Fixture insertion count mismatch'
    assert database.attendance_logs.count_documents({'date': {'$lt': '2025-01-01'}}) == 100000
    assert database.employees.count_documents({'emp_code': {'$gte': 'EMP800000', '$lte': 'EMP801049'}}) == 1050
    return dict(inserted)


def latency_summary(observations):
    if not observations:
        return {'status': 'NOT MEASURED', 'samples': 0}
    values = sorted(row['seconds']*1000 for row in observations)
    return {'status': 'MEASURED', 'samples': len(values), 'median_ms': statistics.median(values),
            'p95_ms': values[math.ceil(.95*len(values))-1], 'max_ms': values[-1],
            'failure_count': sum(row['failed'] for row in observations),
            'http_statuses': dict(Counter(str(row['status']) for row in observations)),
            'qualification': 'client/network latency; descriptive small sample p95, not statistically representative'}


def memory_sample(pid):
    """Read one verified child's RSS. A snapshot is never described as peak memory."""
    try:
        if type(pid) is not int or pid <= 0:
            raise ValueError
        if os.name == 'nt':
            answer = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                f'(Get-Process -Id {pid} -ErrorAction Stop).WorkingSet64'],
                capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            if answer.returncode:
                raise ValueError
            rss = int(answer.stdout.strip())
        else:
            text = pathlib.Path(f'/proc/{pid}/status').read_text()
            rss = int(re.search(r'^VmRSS:\s+(\d+)', text, re.MULTILINE).group(1))*1024
        return {'status': 'MEASURED', 'rss_bytes': rss, 'qualification': 'process RSS snapshot; peak NOT MEASURED'}
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return {'status': 'NOT MEASURED', 'reason': 'Verified child RSS unavailable; peak not inferred'}


def fresh_startup(database, name, token):
    """Fresh process, imports, MongoDB/index setup, dynamic bind and health ping."""
    verify_owner(database, name, token)
    process = None
    with tempfile.TemporaryDirectory(prefix='p8_startup_') as folder:
        ready = pathlib.Path(folder)/'ready.json'
        executable, env = harness.child_process_configuration(name)
        started = time.perf_counter()
        try:
            process = subprocess.Popen([executable, '-B', str(pathlib.Path(harness.__file__).resolve()),
                '--serve', '--database', name, '--token', token, '--ready-file', str(ready)],
                cwd=harness.ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while not ready.exists():
                if process.poll() is not None:
                    diagnostic = ready.with_suffix('.error.json')
                    safe = json.loads(diagnostic.read_text()) if diagnostic.exists() else {'category': 'unavailable'}
                    raise RuntimeError('Fresh benchmark child exited: '+json.dumps(safe))
                if time.perf_counter()-started >= 20:
                    raise AssertionError('Full-dataset startup exceeded 20 seconds')
                time.sleep(.025)
            port = harness.validate_child_identity(json.loads(ready.read_text()), process, name, token)
            with urlopen(f'http://127.0.0.1:{port}/health', timeout=8) as response:
                assert response.status == 200 and json.load(response) == {'status': 'ok'}
            elapsed = time.perf_counter()-started
            assert elapsed < 20, 'Full-dataset startup exceeded 20 seconds'
            return round(elapsed, 3)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)


def benchmark_checks(http, database, codes, inherited_epoch, passed):
    name = http.database_name
    token = http.ownership_token
    verify_owner(database, name, token)
    deadline = time.monotonic()+3600  # Advisory safety budget, includes paced fixture loading.
    inserted = load_fixtures(database, name, token, deadline)
    passed('phase8_dataset', inserted=inserted, **fixtures.estimate())
    # Recreate only declared application indexes in this exactly-owned disposable DB.
    from app import main
    for collection, fields, index_name, unique in main.INDEXES:
        verify_owner(database, name, token)
        database[collection].drop_index(index_name)
    timings = [fresh_startup(database, name, token) for _ in range(2)]
    passed('phase8_full_dataset_startup', seconds=timings, limit_seconds=20,
           qualification='first fresh startup creates all seven indexes on full data; second verifies idempotence')
    for collection, fields, index_name, unique in main.INDEXES:
        actual = next(item for item in database[collection].list_indexes() if item['name'] == index_name)
        assert list(actual['key'].items()) == fields and bool(actual.get('unique', False)) == unique
    rss_before = memory_sample(getattr(http, 'server_pid', None))
    observations = {}
    def request(label, method, path, expected, body=None, query=None):
        check_deadline(deadline)
        started = time.perf_counter()
        row = {'status': 'NO_RESPONSE', 'failed': True}
        try:
            answer = http(method, path, expected, body=body, query=query)
            row.update(status=answer[0], failed=answer[0] not in expected)
            return answer
        except AssertionError as error:
            match = re.search(r'Unexpected HTTP status .*: (\d{3})$', str(error))
            if match:
                row['status'] = int(match.group(1))
            raise
        finally:
            row['seconds'] = time.perf_counter()-started
            observations.setdefault(label, []).append(row)
            if row['failed']:
                passed('phase8_latency_before_failure', endpoints={key: latency_summary(value) for key, value in observations.items()},
                       qualification='measurement evidence only; the failed request is re-raised')
            time.sleep(.2)

    cases = [('health', '/health', {}), ('employees', '/employees', {'page_size': 100}),
             ('attendance', '/attendance', {'date_from': '2024-01-01', 'date_to': '2024-12-31', 'page_size': 100}),
             ('monthly', '/analytics/employees/EMP800000/monthly', {'month': '2024-02'}),
             ('summary', '/analytics/departments/summary', {'month': '2024-02'}),
             ('leaderboard', '/analytics/leaderboard/late', {'month': '2024-02', 'limit': 10}),
             ('trend', '/analytics/departments/P8D0/trend', {'from': '2024-02-01', 'to': '2024-02-29'})]
    for label, path, query in cases:
        for _ in range(5):
            request(label, 'GET', path, [200], query=query)

    # Bounded independently generated probe (5 employees / 500 records), not a full DB fetch.
    staff = list(fixtures.employees())[:5]
    logs = [doc for employee in staff for doc in fixtures.attendance(employee)]
    expected = [('monthly_oracle', '/analytics/employees/EMP800000/monthly', {'month': '2024-02'}, monthly_expected(staff, logs, 'EMP800000', '2024-02')),
        ('summary_oracle', '/analytics/departments/summary', {'month': '2024-02', 'department': 'P8Probe'}, summary_expected(staff, logs, '2024-02', 'P8Probe')),
        ('leaderboard_oracle', '/analytics/leaderboard/late', {'month': '2024-02', 'department': 'P8Probe', 'limit': 1}, leaderboard_expected(staff, logs, '2024-02', 1, 'P8Probe')),
        ('trend_oracle', '/analytics/departments/P8Probe/trend', {'from': '2024-02-01', 'to': '2024-02-29'}, trend_expected(staff, logs, 'P8Probe', '2024-02-01', '2024-02-29'))]
    for label, path, query, oracle in expected:
        assert request(label, 'GET', path, [200], query=query)[1] == oracle, 'Independent analytics oracle mismatch'
    zero = [employee for employee in fixtures.employees() if employee['department'] == 'P8Zero']
    assert request('zero_logs', 'GET', '/analytics/departments/summary', [200], query={'month': '2024-02', 'department': 'P8Zero'})[1] == summary_expected(zero, [], '2024-02', 'P8Zero')
    passed('phase8_analytics_oracles', bounded_probe_records=len(logs))

    for path, query, collection, sort in [('/employees', {}, 'employees', [('emp_code', 1)]),
            ('/attendance', {'date_from': '2024-01-01', 'date_to': '2024-12-31'}, 'attendance_logs', [('date', -1), ('emp_code', 1)])]:
        mongo_filter = {} if collection == 'employees' else {'date': {'$gte': '2024-01-01', '$lte': '2024-12-31'}}
        total = database[collection].count_documents(mongo_filter)
        last = math.ceil(total/100)
        for page in (1, max(1, last//2), last, last+1):
            answer = request('pagination_'+collection, 'GET', path, [200], query={**query, 'page': page, 'page_size': 100})[1]
            stored = list(database[collection].find(mongo_filter, {'_id': 0}).sort(sort).skip((page-1)*100).limit(100))
            assert answer['total'] == total and len(answer['items']) <= 100
            assert [(row['emp_code'], row.get('date')) for row in answer['items']] == [(row['emp_code'], row.get('date')) for row in stored]
    for path, query in [('/employees', {'department': 'P8Probe'}), ('/attendance', {'status': 'LEAVE', 'emp_code': 'EMP800000', 'date_from': '2024-02-01', 'date_to': '2024-02-29'})]:
        answer = request('filtered_pagination', 'GET', path, [200], query={**query, 'page_size': 100})[1]
        assert all(row.get('department') == 'P8Probe' for row in answer['items']) if path == '/employees' else all(row['status'] == 'LEAVE' and row['emp_code'] == 'EMP800000' for row in answer['items'])
        mongo_filter = {'department': 'P8Probe'} if path == '/employees' else {'status': 'LEAVE', 'emp_code': 'EMP800000', 'date': {'$gte': '2024-02-01', '$lte': '2024-02-29'}}
        assert answer['total'] == database['employees' if path == '/employees' else 'attendance_logs'].count_documents(mongo_filter)
    passed('phase8_pagination', maximum_transferred_items=100)

    pagination_plans = {}
    for collection, sort, bounds in [('employees', {'emp_code': 1}, {}),
            ('attendance_logs', {'date': -1, 'emp_code': 1}, {'date': {'$gte': '2024-01-01', '$lte': '2024-12-31'}})]:
        total = database[collection].count_documents(bounds)
        for offset in (0, total//2, max(0,total-100)):
            pagination_plans[f'{collection}_offset_{offset}'] = inspect_plan(database.command({
                'explain': {'find': collection, 'filter': bounds, 'projection': {'_id': 0},
                            'sort': sort, 'skip': offset, 'limit': 100, 'maxTimeMS': 5000},
                'verbosity': 'executionStats'}))
    passed('phase8_pagination_plans', plans=pagination_plans)

    findings = {}
    explain_cases = [('attendance_list', {'emp_code': 'EMP800000', 'date_from': '2024-02-01', 'date_to': '2024-02-29'}),
        ('employee_monthly', {'emp_code': 'EMP800000', 'month': '2024-02'}),
        ('department_summary', {'month': '2024-02', 'department': 'P8D0'}),
        ('late_leaderboard', {'month': '2024-02', 'limit': 10}),
        ('department_trend', {'department': 'P8D0', 'from': '2024-02-01', 'to': '2024-02-29'})]
    for target, query in explain_cases:
        answer = request('explain', 'GET', '/admin/explain/'+target, [200], query=query)[1]
        findings[target] = inspect_plan(answer['explain'])
    passed('phase8_explain_evidence', targets=findings, metrics='per node; overlapping totals are never added')
    assert all(plan['has_ixscan'] and not plan['has_collection_scan'] and plan['statistics'] for plan in findings.values()), '100k winning-plan scan acceptance failed; inspect saved evidence before optimization'

    code = 'EMP899999'
    assert database.employees.find_one({'emp_code': code}) is None, 'Write probe collision'
    body = {'emp_code': code, 'name': 'Scale race', 'email': 'scale@example.com', 'department': 'P8Writes', 'joined_on': '2024-01-01', 'shift_start': '09:30', 'shift_end': '18:30'}
    race = simultaneous([lambda: request('create', 'POST', '/employees', [201,409], body=body) for _ in range(2)])
    assert sorted(row[0] for row in race) == [201,409]
    race = simultaneous([lambda: request('punch_in', 'POST', '/attendance/punch-in', [201,409], body={'emp_code': code, 'punched_at': epoch()}) for _ in range(2)])
    assert sorted(row[0] for row in race) == [201,409]
    race = simultaneous([lambda: request('punch_out', 'POST', '/attendance/punch-out', [200,409], body={'emp_code': code, 'punched_at': epoch(clock='19:00:00')}) for _ in range(2)])
    assert sorted(row[0] for row in race) == [200,409]
    corrections = simultaneous([lambda minute=minute: request('correction', 'PATCH', f'/attendance/{code}/2026-07-06', [200,409], body={'punch_in': epoch(clock=f'09:{minute}:00'), 'reason': 'Scale correction', 'regularized_by': 'QA'}) for minute in (31,32)])
    history = database.attendance_logs.find_one({'emp_code': code, 'date': '2026-07-06'})['history']
    assert len(history) == sum(row[0] == 200 for row in corrections) >= 1
    if len(history) == 2:
        assert history[0]['changes']['punch_in']['to'] == history[1]['changes']['punch_in']['from']
    independent = simultaneous([lambda code=code: request('independent_correction', 'PATCH', f'/attendance/{code}/2024-01-01', [200], body={'status': 'ON_DUTY', 'reason': 'Independent scale update', 'regularized_by': 'QA'}) for code in ('EMP800000','EMP800003')])
    assert all(row[0] == 200 for row in independent)
    passed('phase8_two_client_concurrency', correction_statuses=[row[0] for row in corrections])
    passed('phase8_latency', endpoints={key: latency_summary(value) for key, value in observations.items()},
           memory={'before': rss_before, 'after': memory_sample(getattr(http, 'server_pid', None)), 'peak': 'NOT MEASURED'})
    passed('phase8_storage', database_stats={key: value for key, value in database.command('dbStats').items() if key in ('objects','dataSize','storageSize','indexSize','totalSize')})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approve-100k', action='store_true', help='Explicitly authorize owned test DB writes and cleanup')
    parser.add_argument('--capacity-file', type=pathlib.Path)
    args = parser.parse_args(argv)
    plan = fixtures.estimate()
    if not args.approve_100k:
        print(json.dumps({'status': 'NOT MEASURED', 'live_records_inserted': 0, 'cleanup': 'NOT_NEEDED', **plan}, indent=2))
        return 0
    if args.capacity_file is None:
        print('BLOCKED: --capacity-file with recent Atlas UI attestation is required')
        return 1
    try:
        capacity_gate(json.loads(args.capacity_file.read_text(encoding='utf-8')), plan['advisory_capacity_budget_bytes'])
    except (RuntimeError, ValueError, OSError):
        print('BLOCKED: capacity gate rejected; no MongoDB operations attempted')
        return 1
    harness.RESULT_FILE = harness.ROOT/'tests/phase8_final_results.json'
    return harness.run_verification(extra_checks=benchmark_checks, phase=8, name_factory=database_name)


if __name__ == '__main__':
    sys.exit(main())
