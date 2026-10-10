"""Phase 6 isolated command, contract, redaction and winning-plan regression tests."""
import asyncio
import copy
import unittest
from unittest.mock import MagicMock, patch
import yaml
from bson import ObjectId, Decimal128, Timestamp
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError
from test_phase3 import main, request, ROOT, MemoryDatabase
from final_phase3_verification import validate_schema, generate_database_name, guard_database
from final_phase6_verification import inspect_plan, CASES


class Phase6Tests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.database.command.return_value = {
            'queryPlanner': {'winningPlan': {'stage': 'FETCH', 'inputStage': {'stage': 'IXSCAN', 'indexName': 'employee_code_unique'}},
                             'rejectedPlans': [{'stage': 'COLLSCAN'}]},
            'executionStats': {'nReturned': 0, 'totalKeysExamined': 0, 'totalDocsExamined': 0, 'executionTimeMillis': 0},
            'ok': 1}
        self.patch = patch.object(main, 'db', self.database)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.contract = yaml.safe_load((ROOT / 'openapi.yaml').read_text())

    def http(self, target='attendance_list', query=None):
        status, value = asyncio.run(request('GET', '/admin/explain/' + target, query))
        if status in (200, 422):
            schema = self.contract['paths']['/admin/explain/{endpoint}']['get']['responses'][str(status)]
            if '$ref' in schema: schema = self.contract['components']['responses'][schema['$ref'].split('/')[-1]]
            validate_schema(self.contract, schema['content']['application/json']['schema'], value)
        return status, value

    def test_route_id_enum_parameters_responses_exact_contract(self):
        actual = main.app.openapi()['paths']['/admin/explain/{endpoint}']['get']
        expected = self.contract['paths']['/admin/explain/{endpoint}']['get']
        self.assertEqual(actual['operationId'], 'explainEndpoint')
        self.assertEqual(set(actual['responses']), set(expected['responses']))
        parameters = [self.contract['components']['parameters'][p['$ref'].split('/')[-1]] if '$ref' in p else p for p in expected['parameters']]
        self.assertEqual({(p['name'], p['in']) for p in actual['parameters']}, {(p['name'], p['in']) for p in parameters})
        enum = next(p for p in actual['parameters'] if p['name'] == 'endpoint')['schema']['enum']
        self.assertEqual(enum, parameters[0]['schema']['enum'])
        self.assertEqual(len(main.app.openapi()['paths']), 11)  # employees has two methods -> 12 operations

    def test_five_commands_and_verbosity_reuse_production_builders(self):
        expected = [main.attendance_query('EMP9101', '2026-07-01', '2026-07-31', page=2, page_size=1),
                    main.monthly_pipeline('EMP9101', '2026-07'),
                    main.department_summary_pipeline('2026-07', 'Analytics QA'),
                    main.leaderboard_pipeline('2026-07', 1, 'Analytics QA'),
                    main.trend_pipeline('Analytics QA', '2026-07-01', '2026-07-10')]
        for (target, query), shape in zip(CASES, expected):
            with self.subTest(target=target):
                status, response = self.http(target, query)
                self.assertEqual(status, 200)
                outer = self.database.command.call_args.args[0]
                self.assertEqual(set(outer), {'explain', 'verbosity'})
                self.assertEqual(outer['verbosity'], 'executionStats')
                command = outer['explain']
                self.assertEqual(command if target == 'attendance_list' else command['pipeline'], shape)
                self.assertNotIn('hint', command)
                self.assertEqual(response['collection'], command.get('find', command.get('aggregate')))

    def test_attendance_defaults_and_full_filter_pagination(self):
        self.http()
        self.assertEqual(self.database.command.call_args.args[0]['explain'], main.attendance_query())
        query = dict(emp_code='EMP0001', date_from='2026-01-01', date_to='2026-02-01', status='WFH', page=3, page_size=7)
        self.http(query=query)
        command = self.database.command.call_args.args[0]['explain']
        self.assertEqual(command['filter'], {'emp_code': 'EMP0001', 'date': {'$gte': '2026-01-01', '$lte': '2026-02-01'}, 'status': 'WFH'})
        self.assertEqual((command['sort'], command['skip'], command['limit'], command['projection']), ({'date': -1, 'emp_code': 1}, 14, 7, {'_id': 0}))

    def test_unknown_targets_rejected_before_database(self):
        for target in ('unknown', 'employees', 'health', 'punch_out', '$out', 'attendance_listx'):
            self.assertEqual(self.http(target)[0], 422)
        self.database.command.assert_not_called()

    def test_conditional_required_parameters(self):
        for target, query in [('employee_monthly', {}), ('employee_monthly', {'month': '2026-07'}),
                              ('department_summary', {}), ('late_leaderboard', {}), ('department_trend', {}),
                              ('department_trend', {'department': 'QA', 'from': '2026-07-01'})]:
            self.assertEqual(self.http(target, query)[0], 422)
        self.database.command.assert_not_called()

    def test_invalid_dates_months_status_and_bounds(self):
        for query in ({'month': '0000-01'}, {'month': '2026-13'}, {'date_from': '2026-02-30'}, {'to': 'bad'},
                      {'status': 'OTHER'}, {'limit': 0}, {'limit': 51}, {'page': 0}, {'page_size': 101}):
            self.assertEqual(self.http(query=query)[0], 422)
        self.database.command.assert_not_called()

    def test_range_validation_shared_and_92_day_boundary(self):
        self.assertEqual(self.http(query={'date_from': '2026-07-02', 'date_to': '2026-07-01'})[0], 422)
        for end in ('2026-06-30', '2026-10-01'):
            self.assertEqual(self.http('department_trend', {'department': 'QA', 'from': '2026-07-01', 'to': end})[0], 422)
        self.database.command.assert_not_called()
        self.assertEqual(self.http('department_trend', {'department': 'QA', 'from': '2026-07-01', 'to': '2026-09-30'})[0], 200)

    def test_arbitrary_command_collection_and_pipeline_rejected(self):
        for key in ('query', 'pipeline', 'collection', 'verbosity', 'hint', 'command'):
            status, value = self.http(query={key: 'private-malicious-payload'})
            self.assertEqual(status, 422)
            self.assertNotIn('private-malicious-payload', str(value))
        self.database.command.assert_not_called()

    def test_string_values_remain_literals_not_query_operators(self):
        self.http(query={'emp_code': '{"$ne":null}'})
        self.assertEqual(self.database.command.call_args.args[0]['explain']['filter']['emp_code'], '{"$ne":null}')
        self.http('department_trend', {'department': '$out', 'from': '2026-07-01', 'to': '2026-07-01'})
        self.assertEqual(self.database.command.call_args.args[0]['explain']['pipeline'][0], {'$match': {'department': '$out'}})

    def test_empty_resource_still_explained_without_404_or_fetch(self):
        self.assertEqual(self.http('employee_monthly', {'emp_code': 'missing', 'month': '2026-07'})[0], 200)
        self.database.employees.find_one.assert_not_called()
        self.database.employees.aggregate.assert_not_called()

    def test_output_preserves_raw_plans_metrics_and_redacts_metadata(self):
        raw = self.database.command.return_value
        raw.update(serverInfo={'host': 'private-host', 'port': 27017}, serverParameters={'secret': 1},
                   operationTime=Timestamp(1, 2), **{'$clusterTime': {'private': 1}})
        raw['queryPlanner']['host'] = 'private-host'
        before = copy.deepcopy(raw)
        status, value = self.http()
        self.assertEqual(status, 200)
        self.assertNotIn('private-host', str(value))
        self.assertEqual(value['explain']['executionStats'], before['executionStats'])
        self.assertEqual(value['explain']['queryPlanner']['winningPlan'], before['queryPlanner']['winningPlan'])
        self.assertIn('rejectedPlans', value['explain']['queryPlanner'])
        self.assertEqual(raw, before)

    def test_bson_json_compatible_conversion(self):
        raw = {'bounds': [ObjectId('0123456789abcdef01234567'), Decimal128('1.25')], 'nReturned': 9}
        result = main.explain_json(raw)
        self.assertEqual(result['nReturned'], 9)
        self.assertEqual(result['bounds'][1], {'$numberDecimal': '1.25'})

    def test_mongodb_failures_sanitized(self):
        for error in (OperationFailure('private-uri-and-password', code=13), ServerSelectionTimeoutError('private-host')):
            self.database.command.side_effect = error
            self.assertEqual(self.http(), (503, {'detail': 'MongoDB unavailable'}))

    def test_bad_server_response_sanitized(self):
        self.database.command.return_value = []
        self.assertEqual(self.http(), (503, {'detail': 'MongoDB unavailable'}))
        self.database.command.return_value = {'bad': object()}
        self.assertEqual(self.http(), (503, {'detail': 'MongoDB unavailable'}))

    def test_nested_winning_scan_and_rejected_scan_ignored(self):
        report = inspect_plan(self.database.command.return_value)
        self.assertTrue(report['has_ixscan']); self.assertFalse(report['has_collection_scan'])
        self.assertEqual(report['indexes'], ['employee_code_unique'])
        self.assertEqual(report['statistics'][0]['totalDocsExamined'], 0)

    def test_sharded_or_subplan_and_sbe_nested_detection(self):
        raw = {'queryPlanner': {'winningPlan': {'stage': 'SHARD_MERGE', 'shards': [
            {'winningPlan': {'queryPlan': {'stage': 'SUBPLAN', 'inputStage': {'stage': 'OR', 'inputStages': [
                {'stage': 'IXSCAN', 'indexName': 'one'}, {'stage': 'IXSCAN', 'indexName': 'two'}]}}}}]}}}
        report = inspect_plan(raw)
        self.assertEqual(report['indexes'], ['one', 'two'])
        self.assertTrue(report['has_ixscan'])

    def test_cursor_and_lookup_scans_metrics_not_fabricated(self):
        raw = {'stages': [{'$cursor': self.database.command.return_value}, {'$lookup': {'from': 'employees'},
                         'indexesUsed': ['employee_code_unique'], 'collectionScans': 2, 'totalDocsExamined': 17, 'nReturned': 4}]}
        report = inspect_plan(raw)
        self.assertTrue(report['has_collection_scan'])
        self.assertEqual(report['statistics'][-1]['totalDocsExamined'], 17)
        self.assertTrue(inspect_plan({'winningPlan': {'stage': 'COLLSCAN'}})['has_collection_scan'])
        self.assertEqual(inspect_plan({})['statistics'], [])

    def test_query_literals_not_misread_as_plan_stages(self):
        self.assertFalse(inspect_plan({'command': {'filter': {'stage': 'COLLSCAN', 'indexName': 'fake'}}})['has_collection_scan'])
        self.assertEqual(inspect_plan({'command': {'stage': 'IXSCAN'}})['stages'], [])

    def test_existing_seven_indexes_preserved_and_idempotent(self):
        database = MemoryDatabase()
        main.ensure_indexes(database); before = copy.deepcopy((database.employees.indexes, database.attendance_logs.indexes))
        main.ensure_indexes(database)
        self.assertEqual(before, (database.employees.indexes, database.attendance_logs.indexes))
        self.assertEqual({spec[2] for spec in main.INDEXES}, {'employee_code_unique', 'employee_department_code',
            'employee_joined_department', 'employee_department_joined', 'attendance_employee_date_unique',
            'attendance_date_code', 'attendance_latest_punch'})

    def test_phase6_names_and_ownership_refusal(self):
        from final_phase5_verification import seed_owned_fixtures
        name = generate_database_name('a' * 32, 6)
        self.assertEqual(len(name.encode()), 35); guard_database(name, name)
        for actual in ('attendance_db', name[:-1] + 'b', 'hrone_p7v_20261010_' + 'a' * 16):
            with self.assertRaises(RuntimeError): guard_database(actual, name)
        self.database.name = name
        self.database.__getitem__.return_value.find_one.return_value = None
        with self.assertRaises(AssertionError): seed_owned_fixtures(self.database, phase=6)
        self.database.employees.insert_many.assert_not_called()

    def test_complete_live_callback_with_isolated_owned_storage(self):
        from types import SimpleNamespace
        from test_phase5 import AggregateCollection
        from test_phase3 import MemoryCollection
        from final_phase6_verification import phase6_checks
        collections = {'employees': [], 'attendance_logs': []}
        name = generate_database_name('a' * 32, 6)
        class Owned(SimpleNamespace):
            def __getitem__(self, key): return getattr(self, key)
        owned = Owned(name=name,
            employees=AggregateCollection([], collections), attendance_logs=AggregateCollection([], collections),
            _verification_owner=MemoryCollection([{'_id': 'owner', 'database': name, 'run_token': 'a' * 32}]))
        for key in collections:
            collections[key] = owned[key].docs
            owned[key].list_indexes = lambda: [{'name': spec[2]} for spec in main.INDEXES]
        def command(value):
            # Server response double: actual production command is retained and inspected.
            result = copy.deepcopy(self.database.command.return_value)
            result['command'] = copy.deepcopy(value['explain'])
            return result
        self.database.command.side_effect = command
        owned.command = self.database.command
        def http(method, path, expected, query=None):
            result = self.http(path.rsplit('/', 1)[1], query)
            self.assertIn(result[0], expected)
            return result
        labels = []
        phase6_checks(http, owned, [], None, lambda label, **details: labels.append((label, details)))
        self.assertEqual([label for label, _ in labels], ['phase6_real_explain_commands_and_statistics',
                         'phase6_validation_contract', 'phase6_explain_read_only'])
        self.assertEqual(len(labels[0][1]['targets']), 5)


if __name__ == '__main__':
    unittest.main()
