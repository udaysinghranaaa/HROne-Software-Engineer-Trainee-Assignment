"""Offline fixture, safety and reporting tests. Never connect to MongoDB."""
import copy
from datetime import datetime, timedelta, timezone
import io
import itertools
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bson import BSON
from pymongo.errors import OperationFailure
import final_phase8_verification as benchmark
import phase8_fixtures as fixtures
from final_phase6_verification import inspect_plan


class Phase8Tests(unittest.TestCase):
    def setUp(self):
        self.token = 'a'*32
        self.name = benchmark.database_name(self.token)
        self.marker = {'_id': 'owner', 'database': self.name, 'run_token': self.token}
        self.now = datetime.now(timezone.utc)
        self.capacity = {'tier': 'M0', 'available_bytes': 400*1024*1024,
            'verified_at': self.now.isoformat(), 'same_deployment': True, 'source': 'Atlas UI'}

    def test_name_length_and_identity(self):
        self.assertEqual(len(self.name.encode()), 35)
        benchmark.harness.guard_database(self.name, self.name)
        with self.assertRaises(RuntimeError): benchmark.harness.guard_database(self.name, self.name+'a')

    def test_invalid_tokens(self):
        for token in ('', 'a'*31, 'A'*32, 'x'*32, 'a'*33):
            with self.subTest(token=token), self.assertRaises(RuntimeError): benchmark.database_name(token)

    def test_unknown_database_cleanup_guard(self):
        for name in ('attendance_db','hrone_p8v_20261010_abc','other', 'x'*39, '\u00e9'*20):
            with self.subTest(name=name), self.assertRaises(RuntimeError): benchmark.harness.guard_database(name, name)

    def owned(self, marker=None):
        class Database:
            name = self.name
            def __getitem__(inner, collection):
                if collection == '_verification_owner':
                    return SimpleNamespace(find_one=lambda query: copy.deepcopy(self.marker if marker is None else marker))
                raise AssertionError('Unexpected collection access before safety gate')
        return Database()

    def test_owner_exact(self): benchmark.verify_owner(self.owned(), self.name, self.token)

    def test_owner_token_mismatch(self):
        with self.assertRaises(RuntimeError): benchmark.verify_owner(self.owned({'run_token': 'b'*32}), self.name, self.token)

    def test_owner_extra_fields_rejected(self):
        with self.assertRaises(RuntimeError): benchmark.verify_owner(self.owned({**self.marker, 'extra': True}), self.name, self.token)

    def test_owner_development_rejected(self):
        db = self.owned(); db.name = 'attendance_db'
        with self.assertRaises(RuntimeError): benchmark.verify_owner(db, self.name, self.token)

    def test_capacity_valid(self): self.assertEqual(benchmark.capacity_gate(self.capacity, 100, self.now)['tier'], 'M0')

    def test_capacity_insufficient(self):
        with self.assertRaises(RuntimeError): benchmark.capacity_gate(self.capacity, self.capacity['available_bytes']+1, self.now)

    def test_capacity_stale_and_future(self):
        for offset in (-86401, 1):
            with self.assertRaises(RuntimeError): benchmark.capacity_gate({**self.capacity, 'verified_at': (self.now+timedelta(seconds=offset)).isoformat()},100,self.now)

    def test_capacity_unknown_tier(self):
        with self.assertRaises(RuntimeError): benchmark.capacity_gate({**self.capacity,'tier':'M10'},100,self.now)

    def test_capacity_must_identify_same_deployment(self):
        with self.assertRaises(RuntimeError): benchmark.capacity_gate({**self.capacity,'same_deployment':False},100,self.now)

    def test_capacity_no_secret_fields(self):
        with self.assertRaises(RuntimeError): benchmark.capacity_gate({**self.capacity,'uri':'redacted'},100,self.now)

    def test_capacity_naive_timestamp(self):
        with self.assertRaises(RuntimeError): benchmark.capacity_gate({**self.capacity,'verified_at':'2026-10-10T00:00:00'},100,self.now)

    def test_batch_boundaries(self):
        self.assertEqual([len(batch) for batch in fixtures.batches(range(201))],[100,100,1])
        self.assertEqual(list(fixtures.batches([])),[])

    def test_batch_sizes_rejected(self):
        for size in (0,101,True,1.5):
            with self.assertRaises(ValueError): list(fixtures.batches([],size))

    def test_exact_100k_unique_keys_and_employee_count(self):
        # Keep just keys, never 100k full documents. Production generator is streaming.
        keys = set(); count = 0
        for batch in fixtures.batches(fixtures.records()):
            self.assertLessEqual(len(batch),100)
            for row in batch:
                key = (row['emp_code'],row['date'])
                self.assertNotIn(key,keys); keys.add(key); count += 1
        self.assertEqual(count,100000)
        staff = list(fixtures.employees())
        self.assertEqual(len(staff),1050)
        self.assertEqual(len({row['emp_code'] for row in staff}),1050)

    def test_determinism(self):
        self.assertEqual([BSON.encode(row) for row in itertools.islice(fixtures.records(),300)],
                         [BSON.encode(row) for row in itertools.islice(fixtures.records(),300)])

    def test_zero_log_employees(self):
        staff = [employee for employee in fixtures.employees() if employee['department']=='P8Zero']
        self.assertEqual(len(staff),50)
        self.assertEqual(sum(len(list(fixtures.attendance(employee))) for employee in staff),0)

    def test_representative_schema_and_derived_values(self):
        staff = list(fixtures.employees())[:10]
        logs = [row for employee in staff for row in fixtures.attendance(employee)]
        self.assertEqual({row['status'] for row in logs},{'PRESENT','WFH','ON_DUTY','ABSENT','LEAVE'})
        self.assertTrue(any(row['history'] for row in logs));self.assertTrue(any(row['half_day'] for row in logs))
        self.assertTrue(any(row['overtime_minutes'] for row in logs))
        for row in logs:
            BSON.encode(row)
            if row['status'] in ('ABSENT','LEAVE'):
                self.assertIsNone(row['punch_in']);self.assertIsNone(row['work_hours'])
            else:
                self.assertEqual(row['punch_in'].utcoffset(),timedelta(0))
                self.assertGreater(row['punch_out'],row['punch_in'])
                self.assertLessEqual((row['punch_out']-row['punch_in']).total_seconds(),86400)
                self.assertEqual(row['half_day'],row['work_hours']<4.5)

    def test_estimated_storage_is_explicitly_advisory(self):
        report = fixtures.estimate()
        self.assertGreater(report['advisory_capacity_budget_bytes'],report['estimated_logical_bson_bytes'])
        self.assertIn('not measured',report['estimate_method'])

    def test_partial_insert_sanitized_and_stops(self):
        db = self.owned(); writes=[]
        original = db.__class__.__getitem__
        def get(inner, collection):
            if collection == '_verification_owner': return original(inner,collection)
            def insert(rows, ordered):
                writes.append(collection)
                raise OperationFailure('SECRET mongodb://private',code=13)
            return SimpleNamespace(insert_many=insert)
        with patch.object(db.__class__,'__getitem__',get), patch.object(benchmark.time,'monotonic',return_value=1):
            with self.assertRaises(RuntimeError) as caught: benchmark.load_fixtures(db,self.name,self.token,10,sleep=lambda _:None)
        self.assertEqual(writes,['employees'])
        self.assertNotIn('SECRET',str(caught.exception)); self.assertIn('13',str(caught.exception))
        self.assertIn('employees.insert_many',str(caught.exception))

    def test_timeout_before_any_write(self):
        with patch.object(benchmark.time,'monotonic',return_value=10), self.assertRaises(RuntimeError):
            benchmark.load_fixtures(self.owned(),self.name,self.token,10,sleep=lambda _:None)

    def test_owner_failure_before_any_write(self):
        with self.assertRaises(RuntimeError): benchmark.load_fixtures(self.owned({}),self.name,self.token,float('inf'),sleep=lambda _:None)

    def test_percentiles_and_failures(self):
        report = benchmark.latency_summary([{'seconds':i/1000,'status':200 if i<20 else 503,'failed':i==20} for i in range(1,21)])
        self.assertEqual((report['median_ms'],report['p95_ms'],report['max_ms']),(10.5,19,20))
        self.assertEqual(report['failure_count'],1)
        self.assertEqual(report['http_statuses'],{'200':19,'503':1})

    def test_empty_metrics_not_measured(self): self.assertEqual(benchmark.latency_summary([])['status'],'NOT MEASURED')

    def test_small_sample_qualification(self):
        self.assertIn('not statistically',benchmark.latency_summary([{'seconds':.1,'status':200,'failed':False}])['qualification'])

    def test_express_nested_no_rejected_scan(self):
        report=inspect_plan({'winningPlan':{'stage':'FETCH','inputStage':{'stage':'EXPRESS_IXSCAN'}},'rejectedPlans':[{'stage':'COLLSCAN'}]})
        self.assertTrue(report['has_ixscan']);self.assertFalse(report['has_collection_scan'])

    def test_metrics_not_summed(self):
        report=inspect_plan({'executionStats':{'totalDocsExamined':100,'inputStage':{'totalDocsExamined':100}}})
        self.assertEqual(report['statistics'],[{'totalDocsExamined':100},{'totalDocsExamined':100}])

    def test_nested_lookup_collection_scan(self): self.assertTrue(inspect_plan({'lookup':{'collectionScans':1}})['has_collection_scan'])

    def test_default_plan_no_connection(self):
        with patch.object(benchmark.harness,'run_verification',side_effect=AssertionError('No live call')),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(benchmark.main([]),0)
        self.assertEqual(json.loads(output.getvalue())['live_records_inserted'],0)

    def test_approval_without_capacity_blocked(self):
        with patch.object(benchmark.harness,'run_verification',side_effect=AssertionError('No live call')),patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(benchmark.main(['--approve-100k']),1)

    def test_memory_unavailable_not_estimated(self): self.assertEqual(benchmark.memory_sample(None)['status'],'NOT MEASURED')

    def test_fresh_startup_rejects_owner_before_spawn(self):
        with patch.object(benchmark.subprocess,'Popen') as spawn, self.assertRaises(RuntimeError):
            benchmark.fresh_startup(self.owned({}),self.name,self.token)
        spawn.assert_not_called()

    def test_fresh_startup_exited_child_sanitized(self):
        process = SimpleNamespace(poll=lambda:1)
        with patch.object(benchmark.harness,'child_process_configuration',return_value=('python',{})), \
             patch.object(benchmark.subprocess,'Popen',return_value=process), self.assertRaisesRegex(RuntimeError,'child exited'):
            benchmark.fresh_startup(self.owned(),self.name,self.token)

    def test_fresh_startup_timeout_stops_child(self):
        from unittest.mock import MagicMock
        process=MagicMock(); process.poll.return_value=None
        with patch.object(benchmark.harness,'child_process_configuration',return_value=('python',{})), \
             patch.object(benchmark.subprocess,'Popen',return_value=process), \
             patch.object(benchmark.time,'perf_counter',side_effect=[0,21]), self.assertRaises(AssertionError):
            benchmark.fresh_startup(self.owned(),self.name,self.token)
        process.terminate.assert_called_once(); process.wait.assert_called_once_with(timeout=10)

    def test_independent_write_probe_changes_status(self):
        staff={row['emp_code']:row for row in fixtures.employees() if row['emp_code'] in ('EMP800000','EMP800003')}
        for employee in staff.values():
            first=next(fixtures.attendance(employee))
            self.assertEqual(first['date'],'2024-01-01')
            self.assertNotEqual(first['status'],'ON_DUTY')


if __name__=='__main__': unittest.main()
