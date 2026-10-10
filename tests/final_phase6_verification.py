"""Manual-only Phase 6 server explain verification; import performs no live operations."""
import sys
from bson import BSON
import final_phase3_verification as harness
from final_phase5_verification import seed_owned_fixtures


def inspect_plan(document):
    """Inspect winning/executed trees, including cursor/shard/SBE and lookup statistics.

    Never treat rejected candidates or literal query/pipeline contents as executed stages.
    Metrics remain separate per node to avoid double-counting parent/child/shard totals.
    """
    stages, indexes, statistics = set(), set(), []
    ignored = {'rejectedPlans', 'allPlansExecution', 'command', 'parsedQuery', 'filter', 'indexBounds'}
    def walk(value):
        if isinstance(value, list):
            for item in value: walk(item)
        elif isinstance(value, dict):
            if isinstance(value.get('stage'), str): stages.add(value['stage'])
            if isinstance(value.get('indexName'), str): indexes.add(value['indexName'])
            indexes.update(name for name in value.get('indexesUsed', []) if isinstance(name, str))
            metrics = {key: value[key] for key in ('totalKeysExamined', 'totalDocsExamined', 'nReturned',
                         'executionTimeMillis', 'executionTimeMillisEstimate', 'collectionScans') if key in value}
            if metrics: statistics.append(metrics)
            for key, item in value.items():
                if key not in ignored: walk(item)
    walk(document)
    scans = 'COLLSCAN' in stages or any(node.get('collectionScans', 0) > 0 for node in statistics)
    return {'stages': sorted(stages), 'indexes': sorted(indexes), 'statistics': statistics,
            'has_ixscan': 'IXSCAN' in stages, 'has_collection_scan': scans}


CASES = (
    ('attendance_list', {'emp_code': 'EMP9101', 'date_from': '2026-07-01', 'date_to': '2026-07-31', 'page': 2, 'page_size': 1}),
    ('employee_monthly', {'emp_code': 'EMP9101', 'month': '2026-07'}),
    ('department_summary', {'month': '2026-07', 'department': 'Analytics QA'}),
    ('late_leaderboard', {'month': '2026-07', 'department': 'Analytics QA', 'limit': 1}),
    ('department_trend', {'department': 'Analytics QA', 'from': '2026-07-01', 'to': '2026-07-10'}),
)

SUPPORTING_INDEXES = {
    'attendance_list': {'attendance_employee_date_unique', 'attendance_date_code'},
    'employee_monthly': {'employee_code_unique'},
    'department_summary': {'employee_department_joined', 'employee_department_code', 'employee_joined_department'},
    'late_leaderboard': {'attendance_date_code'},
    'department_trend': {'employee_department_joined', 'employee_department_code'},
}


def phase6_checks(http, database, codes, epoch, passed):
    seed_owned_fixtures(database, phase=6)
    def snapshot():
        return {name: [BSON.encode(doc) for doc in database[name].find({}).sort('_id', 1).limit(1000)]
                for name in ('employees', 'attendance_logs')}
    before = snapshot()
    findings = {}
    for target, query in CASES:
        command = http('GET', '/admin/explain/' + target, [200], query=query)[1]
        expected_collection = 'attendance_logs' if target in ('attendance_list', 'late_leaderboard') else 'employees'
        assert command['endpoint'] == target and command['collection'] == expected_collection
        report = inspect_plan(command['explain'])
        assert report['statistics'], 'Server execution statistics missing'
        assert report['stages'], 'Server winning/executed plan missing'
        # Independent direct server explain of the exact returned command proves real metrics.
        raw_command = command['explain'].get('command')
        assert isinstance(raw_command, dict), 'Explained command missing'
        raw_command = {key: item for key, item in raw_command.items() if key not in ('$db', 'lsid', '$clusterTime')}
        direct = inspect_plan(database.command({'explain': raw_command, 'verbosity': 'executionStats'}))
        assert report['stages'] == direct['stages'] and report['indexes'] == direct['indexes'], 'HTTP/server plan mismatch'
        declared = {item['name'] for name in ('employees', 'attendance_logs') for item in database[name].list_indexes()}
        assert set(report['indexes']) <= declared, 'Plan refers to an undeclared index'
        report['meets_scan_requirement_on_fixture'] = report['has_ixscan'] and not report['has_collection_scan']
        report['supporting_index_observed'] = bool(set(report['indexes']) & SUPPORTING_INDEXES[target])
        findings[target] = report
    passed('phase6_real_explain_commands_and_statistics', targets=findings,
           large_dataset_acceptance='NOT_MEASURED: tiny fixtures do not establish the 100k scan requirement')
    for target in ('unknown', 'employees', 'punch_in', 'find', '$out'):
        http('GET', '/admin/explain/' + target, [422])
    for target in ('employee_monthly', 'department_summary', 'late_leaderboard', 'department_trend'):
        http('GET', '/admin/explain/' + target, [422])
    http('GET', '/admin/explain/attendance_list', [422], query={'collection': 'attendance_db'})
    http('GET', '/admin/explain/department_trend', [422], query={'department': 'Analytics QA', 'from': '2026-07-02', 'to': '2026-07-01'})
    passed('phase6_validation_contract')
    assert snapshot() == before, 'Explain changed records'
    passed('phase6_explain_read_only')


if __name__ == '__main__':
    harness.RESULT_FILE = harness.ROOT / 'tests/phase6_final_results.json'
    sys.exit(harness.run_verification(extra_checks=phase6_checks, phase=6))
