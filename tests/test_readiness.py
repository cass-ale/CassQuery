import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import preflight
import readiness
import release_evidence
import test_preflight


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        base = Path(cls.scratch.name)
        cls.root = base / 'repo'; cls.root.mkdir()
        test_preflight.make_repo(cls.root)
        cls.output = base / 'evidence/preflight.json'
        result = test_preflight.run_preflight(cls.root, cls.output)
        if result.returncode != 0:
            cls.scratch.cleanup()
            raise AssertionError(result.stderr + result.stdout)
        cls.report = release_evidence.read_json(cls.output)
        cls.snapshot = release_evidence.source_snapshot(cls.root)
        cls.logs = {file.name: file.read_bytes() for file in cls.output.parent.iterdir()
                    if file.is_file() and file.name != 'preflight.json'}

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def setUp(self):
        self.value = copy.deepcopy(self.report)
        for name, data in self.logs.items():
            (self.output.parent / name).write_bytes(data)

    def validate(self, value=None, phase='pre-push', extra=()):
        self.output.write_text(json.dumps(self.value if value is None else value))
        argv = ['readiness', phase, '--root', str(self.root), '--preflight', str(self.output), *extra]
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()) as stream:
            readiness.main()
        return json.loads(stream.getvalue())

    def test_real_produced_preflight_is_ready_for_source_only(self):
        result = self.validate()
        self.assertTrue(result['pass'])
        self.assertEqual(result['tests'], self.report['tests'])
        self.assertIn('native/artifact distribution approval', result['not_checked'])

    def test_counts_schema_missing_duplicate_logs_and_source_are_rejected(self):
        mutations = [lambda r: r.update(tests=1), lambda r: r.update(tests=True),
                     lambda r: r.update(schema=True), lambda r: r.update(schema=2),
                     lambda r: r.update(source_sha='c' * 40), lambda r: r.update(version='9.0.0'),
                     lambda r: r.update(after_fingerprint='c' * 64), lambda r: r.update(stages=[]),
                     lambda r: r['stages'][1].update(tests=0), lambda r: r['stages'][2].update(tests=99),
                     lambda r: r['stages'][1].update(exit=False),
                     lambda r: r['stages'].__setitem__(1, r['stages'][2]),
                     lambda r: r['stages'].reverse(), lambda r: r['stages'][2].pop('counts')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = copy.deepcopy(self.report); mutate(value)
                with self.assertRaises(ValueError):
                    self.validate(value)
        node = self.output.parent / 'node.log'
        node.write_text('changed')
        with self.assertRaises(ValueError):
            self.validate()
        node.write_bytes(self.logs['node.log'])
        node.unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.validate()

    def test_missing_nonfinite_backwards_and_inconsistent_times_fail(self):
        for target in ('report', 'stage'):
            for field, value in (('started_at', None), ('started_at', True), ('started_at', 0),
                                 ('started_at', float('inf')), ('completed_at', float('nan')),
                                 ('completed_at', 1)):
                with self.subTest(target=target, field=field, value=value):
                    report = copy.deepcopy(self.report)
                    record = report if target == 'report' else report['stages'][0]
                    record[field] = value
                    with self.assertRaises(ValueError):
                        self.validate(report)
        for value in (0, -1, True, float('inf'), 99):
            report = copy.deepcopy(self.report); report['stages'][0]['duration_s'] = value
            with self.subTest(duration=value), self.assertRaises(ValueError):
                self.validate(report)
        report = copy.deepcopy(self.report)
        report['stages'][1]['started_at'] = report['stages'][0]['started_at']
        with self.assertRaises(ValueError):
            self.validate(report)

    def test_rehashed_outcome_logs_and_sidecar_contradictions_fail(self):
        for name, replacement in (('node.log', 'TAP version 13\n1..0\n# tests 0\n# pass 0\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n'),
                                  ('node.log', self.logs['node.log'].decode() + '# tests 3\n'),
                                  ('python.log', 'Ran 0 tests in 0.000s\n\nNO TESTS RAN\n'),
                                  ('python.log', self.logs['python.log'].decode() + 'Ran 1 test in 0.001s\n'),
                                  ('python.log', 'Ran 1 test in 0.001s\n\nOK (skipped=1)\n')):
            with self.subTest(log=name):
                path = self.output.parent / name; path.write_text(replacement)
                report = copy.deepcopy(self.report)
                stage = next(stage for stage in report['stages'] if stage['path'] == name)
                stage['sha256'] = release_evidence.digest(path)
                with self.assertRaises(ValueError):
                    self.validate(report)
                path.write_bytes(self.logs[name])
        for field, value in (('tests', 0), ('collected', 2), ('failures', 1), ('skipped', 1), ('schema', True),
                             ('files', ['tests/not-pinned.py'])):
            with self.subTest(sidecar=field):
                counts = json.loads(self.logs['python-counts.json']); counts[field] = value
                path = self.output.parent / 'python-counts.json'; path.write_text(json.dumps(counts))
                report = copy.deepcopy(self.report); report['stages'][2]['counts']['sha256'] = release_evidence.digest(path)
                with self.assertRaises(ValueError):
                    self.validate(report)
        (self.output.parent / 'python-counts.json').write_bytes(self.logs['python-counts.json'])

    def test_pre_release_requires_integration_and_rejects_unrelated_ci_identity(self):
        with self.assertRaisesRegex(ValueError, 'candidate and source integration'):
            self.validate(phase='pre-release')
        # These are complete actual-field protocol fixtures, not executed CI/native proof.
        sha = self.snapshot['source_sha']
        run = {'id': 100, 'run_attempt': 1, 'repository': {'full_name': release_evidence.REPOSITORY},
               'head_repository': {'full_name': release_evidence.REPOSITORY}, 'head_sha': sha,
               'event': 'push', 'head_branch': 'main', 'path': '.github/workflows/macos-candidate.yml',
               'status': 'completed', 'conclusion': 'success'}
        jobs = {'total_count': 2, 'jobs': [{'id': 200 + i, 'run_id': 100, 'head_sha': sha,
                 'name': name, 'status': 'completed', 'conclusion': 'success',
                 'steps': [{'name': step, 'status': 'completed', 'conclusion': 'success'} for step in release_evidence.REQUIRED_STEPS]}
                for i, name in enumerate(('candidate (macos-15, arm64)', 'candidate (macos-15-intel, x64)'))]}
        integration = {**run, 'id': 101, 'path': '.github/workflows/integration.yml'}
        integration_jobs = {'total_count': 1, 'jobs': [{'id': 300, 'run_id': 101, 'head_sha': sha, 'name': 'integration',
                             'status': 'completed', 'conclusion': 'success', 'steps': [
                             {'name': step, 'status': 'completed', 'conclusion': 'success'} for step in release_evidence.INTEGRATION_STEPS]}]}
        evidence = {'run': run, 'jobs': jobs, 'integration-run': integration, 'integration-jobs': integration_jobs}
        extra = []
        for name, value in evidence.items():
            file = self.output.parent / (name + '.json'); file.write_text(json.dumps(value))
            flag = {'run': '--run-json', 'jobs': '--jobs-json', 'integration-run': '--integration-run-json',
                    'integration-jobs': '--integration-jobs-json'}[name]
            extra.extend((flag, str(file)))
        self.assertTrue(self.validate(phase='pre-release', extra=extra)['pass'])
        for target, change in (('jobs', lambda v: v.update(total_count=999)),
                               ('jobs', lambda v: v['jobs'][0].update(run_id=999)),
                               ('jobs', lambda v: v['jobs'][0].update(head_sha='c' * 40)),
                               ('integration-run', lambda v: v.update(conclusion='failure')),
                               ('integration-jobs', lambda v: v['jobs'][0].update(run_id=999)),
                               ('integration-jobs', lambda v: v.update(total_count=2))):
            with self.subTest(target=target):
                bad = copy.deepcopy(evidence[target]); change(bad)
                file = self.output.parent / (target + '.json'); file.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    self.validate(phase='pre-release', extra=extra)
                file.write_text(json.dumps(evidence[target]))
