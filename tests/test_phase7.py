"""Deterministic hidden-case matrices; no Atlas access or production locks."""
import asyncio
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from bson import BSON
from pymongo.errors import ConfigurationError, OperationFailure, ServerSelectionTimeoutError

from test_phase3 import main, request, ROOT, MemoryDatabase, MemoryClient
from test_phase4 import AtomicCollection
from test_phase5 import AggregateCollection
from final_phase3_verification import validate_schema
from final_phase6_verification import inspect_plan
from phase5_fixtures import fixture_data, monthly_expected, summary_expected, leaderboard_expected, trend_expected


CONTRACT = yaml.safe_load((ROOT / 'openapi.yaml').read_text())


def contract_path(path):
    if path.startswith('/attendance/') and path not in ('/attendance/punch-in', '/attendance/punch-out'):
        return '/attendance/{emp_code}/{date}'
    if path.startswith('/analytics/employees/'):
        return '/analytics/employees/{emp_code}/monthly'
    if path.startswith('/analytics/departments/') and path.endswith('/trend'):
        return '/analytics/departments/{department}/trend'
    if path.startswith('/admin/explain/'):
        return '/admin/explain/{endpoint}'
    return path


def checked_http(method, path, body=None, query=None):
    result = asyncio.run(request(method, path, body=body, query=query))
    status, response = result
    schema = CONTRACT['paths'][contract_path(path)][method.lower()]['responses'].get(str(status))
    if schema is None:
        assert status == 503, 'Unexpected undocumented HTTP status'
        schema = CONTRACT['components']['responses']['Error']
    if '$ref' in schema:
        schema = CONTRACT['components']['responses'][schema['$ref'].split('/')[-1]]
    validate_schema(CONTRACT, schema['content']['application/json']['schema'], response)
    if not path.startswith('/admin/explain/'):
        assert '"_id"' not in json.dumps(response), 'Internal document id leaked'
    return result


def epoch(day='2026-07-06', clock='09:30:00'):
    return int(datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=main.IST).timestamp()) * 1000


class AttendanceHardeningTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()
        self.database.attendance_logs = AtomicCollection()
        main.ensure_indexes(self.database)
        self.patch = patch.object(main, 'db', self.database)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def punch(self, code='EMP0001', at=None, status='PRESENT'):
        return checked_http('POST', '/attendance/punch-in', {'emp_code': code,
            'punched_at': epoch() if at is None else at, 'status': status})

    def close(self, code='EMP0001', at=None):
        return checked_http('POST', '/attendance/punch-out', {'emp_code': code,
            'punched_at': epoch(clock='19:00:00') if at is None else at})

    def correct(self, code='EMP0001', day='2026-07-06', **changes):
        return checked_http('PATCH', f'/attendance/{code}/{day}',
            {'reason': 'Boundary correction', 'regularized_by': 'QA', **changes})

    def test_all_twelve_operation_ids_statuses_and_request_schema_fields(self):
        actual = main.app.openapi()
        count = 0
        for path, methods in CONTRACT['paths'].items():
            for method, operation in methods.items():
                count += 1
                generated = actual['paths'][path][method]
                self.assertEqual(generated['operationId'], operation['operationId'])
                self.assertEqual(set(generated['responses']), set(operation['responses']))
                if 'requestBody' in operation:
                    name = operation['requestBody']['content']['application/json']['schema']['$ref'].split('/')[-1]
                    generated_name = generated['requestBody']['content']['application/json']['schema']['$ref'].split('/')[-1]
                    expected, model = CONTRACT['components']['schemas'][name], actual['components']['schemas'][generated_name]
                    self.assertEqual(set(expected['properties']), set(model['properties']))
                    self.assertEqual(set(expected['required']), set(model['required']))
        self.assertEqual(count, 12)

    def test_wrong_json_container_and_field_types_no_writes(self):
        self.punch()
        before = BSON.encode({'records': self.database.attendance_logs.docs})
        for path, method in [('/employees', 'POST'), ('/attendance/punch-in', 'POST'),
                             ('/attendance/punch-out', 'POST'), ('/attendance/EMP0001/2026-07-06', 'PATCH')]:
            for value in ([], ['x'], 'x', 1, True):
                with self.subTest(path=path, value=value):
                    self.assertEqual(checked_http(method, path, value)[0], 422)
        self.assertEqual(BSON.encode({'records': self.database.attendance_logs.docs}), before)

    def test_missing_body_and_each_required_employee_field(self):
        body = {'emp_code': 'EMP7777', 'name': 'A', 'email': 'a@b.co', 'department': 'Q', 'joined_on': '2026-01-01'}
        for field in body:
            self.assertEqual(checked_http('POST', '/employees', {k:v for k,v in body.items() if k != field})[0], 422)
        for path in ('/employees', '/attendance/punch-in', '/attendance/punch-out'):
            self.assertEqual(checked_http('POST', path)[0], 422)
        self.assertEqual(checked_http('PATCH', '/attendance/EMP0001/2026-07-06', {})[0], 422)

    def test_employee_min_max_and_extra_fields_preserve_defaults(self):
        for code, length in [('EMP7001', 1), ('EMP700002', 100)]:
            response = checked_http('POST', '/employees', {'emp_code': code, 'name': 'N' * length,
                'email': 'a' * 115 + '@b.co', 'department': 'D' * min(length, 50),
                'joined_on': '2024-02-29', 'unknown': 'ignored', 'work_hours': 999})
            self.assertEqual(response[0], 201)
            self.assertEqual((response[1]['shift_start'], response[1]['shift_end']), ('09:30', '18:30'))
            self.assertNotIn('unknown', response[1])
        for field, value in [('name', ''), ('name', 'x'*101), ('department', ''), ('department', 'x'*51),
                             ('email', 'a'*116+'@b.co'), ('joined_on', '2023-02-29')]:
            body = {'emp_code': 'EMP8888', 'name': 'A', 'email': 'a@b.co', 'department': 'D', 'joined_on': '2026-01-01', field: value}
            self.assertEqual(checked_http('POST', '/employees', body)[0], 422)

    def test_grace_floor_generated_second_matrix(self):
        start = epoch()
        for seconds in (0, 599, 600, 601, 659, 660, 3599, 3600, 86399):
            self.database.attendance_logs.docs.clear()
            response = self.punch(at=start + seconds*1000 + 999)
            self.assertEqual(response[0], 201)
            elapsed = seconds if seconds < 52200 else seconds - 86400
            self.assertEqual(response[1]['late_minutes'], elapsed//60 if elapsed > 600 else 0)
            self.assertEqual(response[1]['punch_in'], start + seconds*1000)

    def test_cross_month_year_overnight_exact_shift_end_boundaries(self):
        self.database.employees.docs[0].update(shift_start='22:00', shift_end='06:00')
        for day in ('2026-02-01', '2027-01-01', '2024-03-01'):
            previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            for clock, expected_day in [('00:30:00', previous), ('05:59:59', previous), ('06:00:00', day)]:
                self.database.attendance_logs.docs.clear()
                self.assertEqual(self.punch(at=epoch(day, clock))[1]['date'], expected_day)
            self.database.attendance_logs.docs.clear()
            self.punch(at=epoch(previous, '22:00:00'))
            result = self.close(at=epoch(day, '06:30:01'))
            self.assertEqual((result[0], result[1]['date'], result[1]['work_hours'], result[1]['overtime_minutes']), (200, previous, 8.5, 30))

    def test_utc_ist_midnight_and_naive_bson_agree(self):
        for aware, expected_day in [(datetime(2026,12,31,18,29,59,tzinfo=main.UTC), '2026-12-31'),
                                    (datetime(2026,12,31,18,30,tzinfo=main.UTC), '2027-01-01'),
                                    (datetime(2027,1,1,0,0,tzinfo=main.UTC), '2027-01-01')]:
            self.assertEqual(main.attendance_date(aware, '09:30', '18:30'), expected_day)
            self.assertEqual(main.to_epoch_ms(aware), main.to_epoch_ms(aware.replace(tzinfo=None)))

    def test_duration_half_day_overtime_generated_matrix(self):
        start = epoch()
        for seconds in (1, 17, 18, 53, 54, 16164, 16181, 16182, 16200, 16217, 16218, 86399, 86400):
            self.database.attendance_logs.docs.clear()
            self.punch(at=start)
            response = self.close(at=start + seconds*1000)
            hours = float((Decimal(seconds)/3600).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            self.assertEqual(response[0], 200)
            self.assertEqual((response[1]['work_hours'], response[1]['half_day']), (hours, hours < 4.5))
        for seconds in (1799, 1800, 1801):
            self.database.attendance_logs.docs.clear(); self.punch()
            self.assertEqual(self.close(at=epoch(clock='18:30:00') + seconds*1000)[1]['overtime_minutes'], 0 if seconds < 1800 else 30)

    def test_epoch_min_max_type_and_bounds_matrix(self):
        for value in (100000000000, 4102444800000):
            self.database.attendance_logs.docs.clear()
            self.assertEqual(self.punch(at=value)[1]['punch_in'], value//1000*1000)
        for value in (99999999999, 4102444800001, 1.0, '1780000000000', False, None):
            self.assertEqual(checked_http('POST', '/attendance/punch-in', {'emp_code':'EMP0001','punched_at':value})[0], 422)

    def test_five_way_duplicate_employee_creation(self):
        self.database.employees.barrier = threading.Barrier(5)
        body = {'emp_code':'EMP7777','name':'Parallel','email':'p@q.co','department':'QA','joined_on':'2026-01-01'}
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: checked_http('POST','/employees',body), range(5)))
        self.assertEqual(sorted(r[0] for r in results), [201,409,409,409,409])
        self.assertEqual(self.database.employees.count_documents({'emp_code':'EMP7777'}), 1)

    def test_five_way_punch_in_and_punch_out(self):
        self.database.attendance_logs.barrier = threading.Barrier(5)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.punch(), range(5)))
        self.assertEqual(sorted(r[0] for r in results), [201,409,409,409,409])
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.close(), range(5)))
        self.assertEqual(sorted(r[0] for r in results), [200,409,409,409,409])
        self.assertEqual(len(self.database.attendance_logs.docs), 1)
        self.assertEqual(self.database.attendance_logs.docs[0]['history'], [])

    def test_five_way_corrections_one_history_and_retry_chain(self):
        self.punch()
        self.database.attendance_logs.barrier = threading.Barrier(5)
        times = [epoch(clock=f'09:{minute}:00') for minute in (31,32,33,34,35)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda instant: self.correct(punch_in=instant), times))
        self.assertEqual(sorted(r[0] for r in results), [200,409,409,409,409])
        stored = self.database.attendance_logs.docs[0]
        self.assertEqual(len(stored['history']), 1)
        winner = next(index for index,r in enumerate(results) if r[0]==200)
        self.database.attendance_logs.barrier = None
        prior = copy.deepcopy(stored['history'])
        self.assertEqual(self.correct(punch_in=times[(winner+1)%5])[0], 200)
        self.assertEqual(stored['history'][:1], prior)
        self.assertEqual(stored['history'][1]['changes']['punch_in']['from'], stored['history'][0]['changes']['punch_in']['to'])

    def test_patch_punch_out_race_retry_preserves_both_changes(self):
        self.punch(); self.database.attendance_logs.barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b=pool.submit(self.close),pool.submit(self.correct,status='WFH')
            results=[a.result(),b.result()]
        self.assertEqual(sorted(r[0] for r in results), [200,409])
        self.database.attendance_logs.barrier=None
        self.assertEqual((self.close() if results[0][0]==409 else self.correct(status='WFH'))[0],200)
        stored=self.database.attendance_logs.docs[0]
        self.assertEqual((stored['status'],len(stored['history']),stored['work_hours']),('WFH',1,9.5))

    def test_five_independent_employees_no_interference(self):
        codes=[employee['emp_code'] for employee in self.database.employees.docs[:5]]
        for employee in self.database.employees.docs: employee.update(shift_start='09:30',shift_end='18:30')
        self.database.attendance_logs.barrier=threading.Barrier(5)
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual([r[0] for r in pool.map(lambda code:self.punch(code),codes)],[201]*5)
            self.assertEqual([r[0] for r in pool.map(lambda code:self.correct(code=code,status='WFH'),codes)],[200]*5)
        self.assertEqual({doc['emp_code'] for doc in self.database.attendance_logs.docs},set(codes))
        self.assertTrue(all(len(doc['history'])==1 and doc['status']=='WFH' for doc in self.database.attendance_logs.docs))

    def test_all_status_transition_matrix_and_rejected_audits_unchanged(self):
        for initial in ('PRESENT','WFH','ON_DUTY'):
            for final in ('PRESENT','WFH','ON_DUTY','ABSENT','LEAVE'):
                self.database.attendance_logs.docs.clear(); self.punch(status=initial)
                result=self.correct(status=final)
                self.assertEqual(result[0],422 if final==initial else 200)
                self.assertEqual(len(self.database.attendance_logs.docs[0]['history']),int(final!=initial))
        for initial in ('ABSENT','LEAVE'):
            self.database.attendance_logs.docs.clear();self.punch();self.correct(status=initial)
            before=copy.deepcopy(self.database.attendance_logs.docs)
            self.assertEqual(self.correct(status='WFH')[0],422)
            self.assertEqual(self.database.attendance_logs.docs,before)
            self.assertEqual(self.correct(status='WFH',punch_in=epoch())[0],200)

    def test_rejected_corrections_no_partial_storage_or_history(self):
        self.punch();self.correct(status='WFH')
        before=BSON.encode({'docs':self.database.attendance_logs.docs})
        for changes in ({'status':'WFH'},{'work_hours':1},{'punch_out':epoch()},
                        {'punch_in':epoch('2026-07-07')},{'status':'LEAVE','punch_out':epoch()},
                        {'reason':'bad'},{'regularized_by':''},{'punch_in':None}):
            self.assertEqual(self.correct(**changes)[0],422)
            self.assertEqual(BSON.encode({'docs':self.database.attendance_logs.docs}),before)

    def test_driver_failure_categories_all_controlled_no_mutation(self):
        self.punch();before=copy.deepcopy(self.database.attendance_logs.docs)
        errors=[ServerSelectionTimeoutError('private DNS/TLS timeout'), ConfigurationError('private DNS'),
                OperationFailure('private authorization',code=13),OperationFailure('private driver failure',code=8)]
        for error in errors:
            with patch.object(self.database.attendance_logs,'find_one_and_update',side_effect=error):
                self.assertEqual(self.correct(status='WFH'),(503,{'detail':'MongoDB unavailable'}))
            self.assertEqual(self.database.attendance_logs.docs,before)

    def test_all_twelve_routes_driver_failures_match_error_schema(self):
        employee={'emp_code':'EMP7777','name':'A','email':'a@b.co','department':'D','joined_on':'2026-01-01'}
        cases=[('POST','/employees',employee,None,self.database.employees,'insert_one'),
               ('GET','/employees',None,None,self.database.employees,'count_documents'),
               ('POST','/attendance/punch-in',{'emp_code':'EMP0001'},None,self.database.employees,'find_one'),
               ('POST','/attendance/punch-out',{'emp_code':'EMP0001'},None,self.database.employees,'find_one'),
               ('PATCH','/attendance/EMP0001/2026-07-06',{'reason':'Valid reason','regularized_by':'QA','status':'WFH'},None,self.database.employees,'find_one'),
               ('GET','/attendance',None,None,self.database.attendance_logs,'count_documents'),
               ('GET','/analytics/employees/EMP0001/monthly',None,{'month':'2026-07'},self.database.employees,'aggregate'),
               ('GET','/analytics/departments/summary',None,{'month':'2026-07'},self.database.employees,'aggregate'),
               ('GET','/analytics/leaderboard/late',None,{'month':'2026-07'},self.database.attendance_logs,'aggregate'),
               ('GET','/analytics/departments/QA/trend',None,{'from':'2026-07-01','to':'2026-07-01'},self.database.employees,'aggregate'),
               ('GET','/admin/explain/attendance_list',None,None,self.database,'command')]
        before=BSON.encode({'employees':self.database.employees.docs,'logs':self.database.attendance_logs.docs})
        for method,path,body,query,target,attribute in cases:
            with patch.object(target,attribute,create=True,side_effect=OperationFailure('private connection details',code=13)):
                self.assertEqual(checked_http(method,path,body,query),(503,{'detail':'MongoDB unavailable'}))
            self.assertEqual(BSON.encode({'employees':self.database.employees.docs,'logs':self.database.attendance_logs.docs}),before)
        client=MemoryClient();client.error=ServerSelectionTimeoutError('private TLS')
        with patch.object(main,'client',client):
            self.assertEqual(checked_http('GET','/health'),(503,{'detail':'MongoDB unavailable'}))

    def test_wrong_scalar_types_for_each_employee_and_correction_field(self):
        body={'emp_code':'EMP7777','name':'A','email':'a@b.co','department':'D','joined_on':'2026-01-01'}
        for field in (*body,'shift_start','shift_end'):
            for value in (1,True,[],{}):
                self.assertEqual(checked_http('POST','/employees',{**body,field:value})[0],422)
        self.punch()
        for field in ('status','reason','regularized_by','punch_in','punch_out'):
            for value in ([],{},True):
                self.assertEqual(self.correct(**{field:value})[0],422)

    def test_absence_records_not_punch_out_candidates_and_missing_filters(self):
        self.punch();self.correct(status='LEAVE')
        self.assertEqual(self.close()[0],404)
        self.assertEqual(checked_http('GET','/attendance',query={'emp_code':'unknown'})[1]['items'],[])
        self.assertEqual(checked_http('GET','/employees',query={'department':'unknown'})[1]['total'],0)


class AnalyticsHardeningTests(unittest.TestCase):
    def setUp(self):
        employees,logs=fixture_data()
        self.collections={'employees':employees,'attendance_logs':logs}
        self.database=SimpleNamespace(employees=AggregateCollection(employees,self.collections),
                                     attendance_logs=AggregateCollection(logs,self.collections))
        self.collections.update(employees=self.database.employees.docs, attendance_logs=self.database.attendance_logs.docs)
        self.patch=patch.object(main,'db',self.database);self.patch.start();self.addCleanup(self.patch.stop)

    def test_join_date_calendar_generated_month_matrix(self):
        employee=self.collections['employees'][0]
        for month in ('2024-02','2026-02','2026-07','2026-12'):
            last=main.month_bounds(month)[1]
            for joined in (month+'-01',month+'-15',last,'2027-01-01'):
                employee['joined_on']=joined
                result=checked_http('GET','/analytics/employees/EMP9101/monthly',query={'month':month})
                self.assertEqual(result[1],monthly_expected(self.collections['employees'],self.collections['attendance_logs'],'EMP9101',month))

    def test_trend_cross_month_year_and_leap_day_oracles(self):
        for first,last in [('2026-12-29','2027-01-05'),('2024-02-27','2024-03-02'),
                           ('2026-07-31','2026-08-01'),('2026-07-06','2026-07-06')]:
            actual=checked_http('GET','/analytics/departments/Analytics QA/trend',query={'from':first,'to':last})[1]
            self.assertEqual(actual,trend_expected(self.collections['employees'],self.collections['attendance_logs'],'Analytics QA',first,last))

    def test_all_tied_rank_cutoffs_more_rows_than_limit(self):
        employees=self.collections['employees'];logs=self.collections['attendance_logs']
        first=copy.deepcopy(employees[0]);employees[:]=[{**first,'emp_code':f'EMP{8000+i}'} for i in range(5)]
        logs[:]=[{'emp_code':employee['emp_code'],'date':'2026-07-06','late_minutes':30,'status':'PRESENT'} for employee in reversed(employees)]
        for limit in (1,2,5,50):
            result=checked_http('GET','/analytics/leaderboard/late',query={'month':'2026-07','limit':limit})[1]
            self.assertEqual(result,leaderboard_expected(employees,logs,'2026-07',limit))
            self.assertEqual([item['rank'] for item in result['items']],[1]*5)

    def test_summary_zero_log_legacy_defaults_and_response_order(self):
        for record in self.collections['attendance_logs']:
            for field in ('late_minutes','overtime_minutes','half_day','history'):record.pop(field,None)
        result=checked_http('GET','/analytics/departments/summary',query={'month':'2026-07'})[1]
        self.assertEqual(result,summary_expected(self.collections['employees'],self.collections['attendance_logs'],'2026-07'))
        self.collections['attendance_logs'].clear()
        empty=checked_http('GET','/analytics/departments/summary',query={'month':'2026-07'})[1]
        self.assertTrue(all(item['avg_work_hours'] is None and item['present_days']==0 for item in empty['items']))

    def test_every_read_endpoint_unknown_filter_empty_or_missing_behavior(self):
        self.assertEqual(checked_http('GET','/analytics/employees/unknown/monthly',query={'month':'2026-07'})[0],404)
        self.assertEqual(checked_http('GET','/analytics/departments/unknown/trend',query={'from':'2026-07-01','to':'2026-07-01'})[0],404)
        for path in ('/analytics/departments/summary','/analytics/leaderboard/late'):
            self.assertEqual(checked_http('GET',path,query={'month':'2026-07','department':'unknown'})[1]['items'],[])


class ExplainHardeningTests(unittest.TestCase):
    def test_saved_express_stage_regression_without_changing_report(self):
        # Captured sanitized stage/statistics; later live report reruns cannot alter this regression.
        statistics={'nReturned':1,'executionTimeMillisEstimate':1}
        reconstructed={'queryPlanner':{'winningPlan':{'stage':'EXPRESS_IXSCAN'}},
                       'executionStats':{'executionStages':{'stage':'EXPRESS_IXSCAN'},**statistics}}
        report=inspect_plan(reconstructed)
        self.assertTrue(report['has_ixscan'])
        self.assertFalse(report['has_collection_scan'])
        self.assertEqual(report['statistics'][0],statistics)

    def test_modern_indexed_allowlist_and_unknown_stages_not_indexed(self):
        for stage in ('IXSCAN','EXPRESS_IXSCAN','CLUSTERED_IXSCAN','EXPRESS_CLUSTERED_IXSCAN','DISTINCT_SCAN'):
            self.assertTrue(inspect_plan({'winningPlan':{'stage':stage}})['has_ixscan'])
        for stage in ('UNKNOWN_IXSCAN','EXPRESS_UPDATE','EXPRESS_DELETE','UNKNOWN','EOF','FETCH','COLLSCAN'):
            self.assertFalse(inspect_plan({'winningPlan':{'stage':stage}})['has_ixscan'])

    def test_nested_express_rejected_candidates_and_lookup_scans(self):
        raw={'stages':[{'$cursor':{'queryPlanner':{'winningPlan':{'queryPlan':{'stage':'FETCH','inputStage':{'stage':'EXPRESS_IXSCAN'}}},
                                                'rejectedPlans':[{'stage':'COLLSCAN'}]}}},
                       {'$lookup':{'from':'employees'},'collectionScans':1,'totalKeysExamined':5}]}
        report=inspect_plan(raw)
        self.assertTrue(report['has_ixscan']);self.assertTrue(report['has_collection_scan'])
        self.assertEqual(report['statistics'],[{'totalKeysExamined':5,'collectionScans':1}])
        self.assertFalse(inspect_plan({'winningPlan':{'stage':'COLLSCAN'},'rejectedPlans':[{'stage':'EXPRESS_IXSCAN'}]})['has_ixscan'])


class HarnessHardeningTests(unittest.TestCase):
    def test_phase7_name_and_unowned_fixture_writes_refused(self):
        from unittest.mock import MagicMock
        from final_phase3_verification import generate_database_name, guard_database
        from final_phase5_verification import seed_owned_fixtures
        name=generate_database_name('a'*32,phase=7)
        self.assertEqual(len(name.encode()),35);guard_database(name,name)
        database=MagicMock();database.name=name
        database.__getitem__.return_value.find_one.return_value=None
        with self.assertRaises(AssertionError):seed_owned_fixtures(database,phase=7)
        database.employees.insert_many.assert_not_called()
        with self.assertRaises(RuntimeError):guard_database('attendance_db',name)
        with self.assertRaises(RuntimeError):generate_database_name('a'*32,phase=8)

    def test_complete_phase7_callback_in_isolated_storage(self):
        from aggregation_memory import pipeline
        from test_phase3 import MemoryCollection
        from final_phase7_verification import phase7_checks
        class Hybrid(AtomicCollection):
            def aggregate(self,stages):return iter(pipeline(self.docs,stages,collections))
            def insert_many(self,docs):
                for doc in docs:self.insert_one(doc)
            def list_indexes(self):return [{'name':name} for name in self.indexes]
        class Owned(SimpleNamespace):
            def __getitem__(self,key):return getattr(self,key)
        name='hrone_p7v_20261010_0123456789abcdef'
        database=Owned(name=name,employees=Hybrid(),attendance_logs=Hybrid(),
            _verification_owner=MemoryCollection([{'_id':'owner','database':name,'run_token':'a'*32}]))
        collections={'employees':database.employees.docs,'attendance_logs':database.attendance_logs.docs}
        main.ensure_indexes(database)
        def command(outer):
            inner=outer['explain'];index='attendance_date_code' if inner.get('find',inner.get('aggregate'))=='attendance_logs' else 'employee_code_unique'
            return {'command':copy.deepcopy(inner),'queryPlanner':{'winningPlan':{'stage':'EXPRESS_IXSCAN','indexName':index}},
                    'executionStats':{'totalKeysExamined':1,'totalDocsExamined':1,'nReturned':1,'executionTimeMillis':0}}
        database.command=command
        def http(method,path,expected,body=None,query=None):
            from urllib.parse import unquote
            result=checked_http(method,unquote(path),body,query)
            self.assertIn(result[0],expected)
            return result
        labels=[]
        with patch.object(main,'db',database):
            phase7_checks(http,database,[],None,lambda label,**details:labels.append(label))
        self.assertEqual(len(labels),9)
        self.assertEqual(labels[-1],'phase7_extended_analytics_read_only_oracles')


if __name__=='__main__':
    unittest.main()
