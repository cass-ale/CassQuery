"""v0.2 packaging closure models; no native launch, signing or installation claim."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import check_release
import macos_smoke
import release_evidence


class PackagingClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cq-v02-packaging-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = json.loads((ROOT / 'package.json').read_text())
        self.builder = json.loads((ROOT / 'packaging/electron-builder.json').read_text())
        self.sources = self.package['cassquerySources']

    def fixture(self):
        import test_release as contracts
        contracts.ReleaseContractTests.copy_inputs(self, self.root)
        return self.root

    def test_canonical_source_version_and_complete_declared_closure(self):
        lock = json.loads((ROOT / 'package-lock.json').read_text())
        self.assertEqual(self.package['version'], '0.2.0')
        self.assertEqual(lock['version'], self.package['version'])
        self.assertEqual(lock['packages']['']['version'], self.package['version'])
        self.assertEqual(self.builder['files'], self.sources['shell'] + ['package.json'])
        self.assertEqual(tuple(self.sources['shell']), release_evidence.NATIVE_SOURCE_PATHS)
        self.assertEqual(tuple(self.sources['python']), release_evidence.PYTHON_SOURCE_PATHS)
        self.assertEqual(macos_smoke.SHELL_FILES, release_evidence.NATIVE_SOURCE_PATHS)
        self.assertEqual(self.builder['electronFuses']['runAsNode'], True)

    def test_source_manifest_rejects_missing_duplicate_unsafe_and_unordered_paths(self):
        changes = [lambda p: p.pop('cassquerySources'),
                   lambda p: p['cassquerySources']['shell'].append(p['cassquerySources']['shell'][0]),
                   lambda p: p['cassquerySources']['shell'].__setitem__(0, '../outside.js'),
                   lambda p: p['cassquerySources']['shell'].reverse(),
                   lambda p: p['cassquerySources']['python'].__setitem__(0, 'outside/worker.py'),
                   lambda p: p.update(main='other.js')]
        for change in changes:
            with self.subTest(change=change):
                package = copy.deepcopy(self.package)
                change(package)
                with self.assertRaises(ValueError):
                    release_evidence.source_manifest(package)

    def test_preflight_rejects_missing_undeclared_or_redirected_executables(self):
        root = self.fixture()
        source = root / 'shell/preload.cjs'
        original = source.read_bytes()
        source.unlink()
        with self.assertRaises(AssertionError):
            check_release.check(root)
        source.write_bytes(original)
        undeclared = root / 'shell/undeclared.cjs'
        undeclared.write_text('module.exports = {};')
        with self.assertRaisesRegex(AssertionError, 'undeclared executable'):
            check_release.check(root)
        undeclared.unlink()
        source.unlink(); source.symlink_to(ROOT / 'shell/preload.cjs')
        with self.assertRaises(AssertionError):
            check_release.check(root)

    def test_broad_allowlist_missing_python_copy_and_disabled_node_mode_fail(self):
        root = self.fixture()
        config = root / 'packaging/electron-builder.json'
        changes = [lambda b: b.update(files=['shell/**', 'package.json']),
                   lambda b: b['extraResources'].pop(2),
                   lambda b: b['electronFuses'].update(runAsNode=False)]
        for change in changes:
            with self.subTest(change=change):
                builder = copy.deepcopy(self.builder)
                change(builder)
                config.write_text(json.dumps(builder))
                with self.assertRaises(AssertionError):
                    check_release.check(root)
        config.write_text(json.dumps(self.builder))
        self.assertTrue(check_release.check(root)['pass'])

    def test_leading_zero_version_fails_even_when_both_lock_roots_agree(self):
        root = self.fixture()
        package = copy.deepcopy(self.package)
        lock = json.loads((ROOT / 'package-lock.json').read_text())
        package['version'] = lock['version'] = lock['packages']['']['version'] = '00.2.0'
        (root / 'package.json').write_text(json.dumps(package))
        (root / 'package-lock.json').write_text(json.dumps(lock))
        with self.assertRaisesRegex(AssertionError, 'canonical release version'):
            check_release.check(root)

    def test_actual_spec_program_passes_complete_hidden_module_and_raw_source_closure(self):
        root = self.fixture()
        captured = {}
        hooks = types.ModuleType('PyInstaller.utils.hooks')
        hooks.collect_all = lambda name: ([], [], ['dependency.' + name])
        def analysis(scripts, **kwargs):
            captured.update(scripts=scripts, **kwargs)
            return types.SimpleNamespace(pure=[], scripts=scripts, binaries=kwargs['binaries'], datas=kwargs['datas'])
        with patch.dict(sys.modules, {'PyInstaller.utils.hooks': hooks}):
            runpy.run_path(str(root / 'packaging/worker.spec'), init_globals={
                'SPECPATH': str(root / 'packaging'), 'Analysis': analysis,
                'PYZ': lambda *args, **kwargs: None, 'EXE': lambda *args, **kwargs: None,
                'COLLECT': lambda *args, **kwargs: None})
        self.assertEqual(captured['scripts'], [str(root / 'worker.py')])
        for name in self.sources['python']:
            self.assertIn((str(root / name), 'source'), captured['datas'])
            if name != 'worker.py':
                self.assertIn(Path(name).stem, captured['hiddenimports'])
        self.assertIn('scipy._external.array_api_compat.numpy.fft', captured['hiddenimports'])

    def test_native_source_record_cannot_omit_new_module_even_when_rehashed(self):
        files = [{'path': name, 'bytes': (ROOT / name).stat().st_size,
                  'sha256': release_evidence.digest(ROOT / name)} for name in self.sources['shell']]
        source = {'files': files, 'fingerprint': hashlib.sha256(json.dumps({'files': files}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        release_evidence.validate_native_source(source)
        source['files'] = source['files'][:-1]
        source['fingerprint'] = hashlib.sha256(json.dumps({'files': source['files']}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.assertRaises(ValueError):
            release_evidence.validate_native_source(source)

    def test_workbench_integration_steps_cannot_be_skipped_or_replaced_with_noops(self):
        path = self.root / '.github/workflows/integration.yml'
        path.parent.mkdir(parents=True)
        text = (ROOT / '.github/workflows/integration.yml').read_text()
        path.write_text(text)
        self.assertTrue(check_release.check_integration_workflow(self.root))
        mutations = [
            ('run: node tools/workbench_suite.cjs\n', 'run: echo green\n'),
            ('run: node tools/workbench_suite.cjs --validate-report evidence/workbench-integration/workbench-results.json', 'run: true'),
            ('      - name: Real workbench editor and CAD roundtrip\n', '      - name: Real workbench editor and CAD roundtrip\n        if: false\n'),
            ('      - name: Validate workbench report bytes\n', '      - name: Validate workbench report bytes\n        continue-on-error: true\n'),
        ]
        for old, new in mutations:
            with self.subTest(old=old):
                self.assertIn(old, text)
                path.write_text(text.replace(old, new, 1))
                with self.assertRaises(AssertionError):
                    check_release.check_integration_workflow(self.root)

    def test_actual_integration_run_contract_requires_both_new_successful_steps(self):
        run = {'id': 100, 'run_attempt': 1, 'repository': {'full_name': release_evidence.REPOSITORY},
               'head_repository': {'full_name': release_evidence.REPOSITORY}, 'head_sha': 'a' * 40,
               'head_branch': 'main', 'event': 'push', 'path': '.github/workflows/integration.yml',
               'status': 'completed', 'conclusion': 'success'}
        job = {'id': 31, 'run_id': 100, 'run_attempt': 1, 'head_sha': run['head_sha'], 'head_branch': 'main',
               'name': 'integration', 'status': 'completed', 'conclusion': 'success',
               'steps': [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in release_evidence.INTEGRATION_STEPS]}
        release_evidence.verify_integration_run(run, {'total_count': 1, 'jobs': [job]}, run['head_sha'])
        for name in ('Real workbench editor and CAD roundtrip', 'Validate workbench report bytes'):
            for state in ('skipped', 'failure', 'cancelled'):
                bad = copy.deepcopy(job)
                next(step for step in bad['steps'] if step['name'] == name)['conclusion'] = state
                with self.subTest(step=name, state=state), self.assertRaises(ValueError):
                    release_evidence.verify_integration_run(run, {'total_count': 1, 'jobs': [bad]}, run['head_sha'])

    def test_three_new_candidate_steps_and_evidence_gates_are_mandatory(self):
        self.assertTrue({'Verify GitHub update channel transport', 'Native workbench editor and file dialogs',
                         'Native update installation and rollback'}.issubset(release_evidence.REQUIRED_STEPS))
        self.assertEqual(release_evidence.GATES['updatecheck'], 'evidence/updatecheck-{arch}.json')
        self.assertEqual(release_evidence.GATES['workbench'], 'evidence/workbench-{arch}/workbench-qualification.json')
        self.assertEqual(release_evidence.GATES['update'], 'evidence/update-{arch}/update.json')
        import test_release_publish as fixtures
        _, files = fixtures.candidate_bytes('arm64')
        folder = self.root / 'PROTOCOL'; folder.mkdir()
        for name, data in files.items():
            file = folder / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(data)
        manifest = json.loads((folder / 'candidate.json').read_text())
        kwargs = {'expected_sha': fixtures.SHA, 'expected_run_id': fixtures.RUN_ID, 'expected_arch': 'arm64', 'expected_attempt': 1}
        release_evidence.verify_candidate(folder, manifest, **kwargs)
        for gate, field, value in (('updatecheck', 'CI_qualification', False),
                                   ('workbench', 'qualifies_workbench', False),
                                   ('update', 'native_execution', False)):
            path = release_evidence.GATES[gate].format(arch='arm64')
            file = folder / path; original = file.read_bytes()
            report = json.loads(original); report[field] = value
            file.write_bytes(json.dumps(report).encode())
            changed = copy.deepcopy(manifest)
            record = release_evidence.file_record(folder, path)
            changed['files'] = [record if item['path'] == path else item for item in changed['files']]
            changed['gates'][gate] = record
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                release_evidence.verify_candidate(folder, changed, **kwargs)
            file.write_bytes(original)
        for gate in ('updatecheck', 'workbench', 'update'):
            changed = copy.deepcopy(manifest); changed['gates'].pop(gate)
            with self.subTest(missing_gate=gate), self.assertRaises(ValueError):
                release_evidence.verify_candidate(folder, changed, **kwargs)
        # The actual updater transport observes successful manual redirect legs too.
        path = release_evidence.GATES['updatecheck'].format(arch='arm64')
        file = folder / path; report = json.loads(file.read_bytes())
        report['statuses'].insert(1, {'endpoint': 'https://github.com/cass-ale/CassQuery/releases/download/v0.1.0/SHA256SUMS.txt',
                                     'status': 302, 'accept': 'application/octet-stream'})
        file.write_bytes(json.dumps(report).encode())
        record = release_evidence.file_record(folder, path)
        redirected = copy.deepcopy(manifest)
        redirected['files'] = [record if item['path'] == path else item for item in redirected['files']]
        redirected['gates']['updatecheck'] = record
        release_evidence.verify_candidate(folder, redirected, **kwargs)

    def test_compiled_helper_proof_rejects_missing_development_loader_and_wrong_resource_path(self):
        proof = {'resource_root': '/protocol-only/worker/_internal', 'modules': {
            Path(name).stem: {'path': '/protocol-only/worker/_internal/' + name, 'loader': 'PyiFrozenLoader'}
            for name in self.sources['python'] if name != 'worker.py'}}
        release_evidence.validate_python_modules(proof)
        changes = [lambda p: p['modules'].pop('cad_io'),
                   lambda p: p['modules']['workbench'].update(loader='SourceFileLoader'),
                   lambda p: p['modules']['cad_io'].update(path='/development/cad_io.py')]
        for change in changes:
            with self.subTest(change=change):
                bad = copy.deepcopy(proof)
                change(bad)
                with self.assertRaises(ValueError):
                    release_evidence.validate_python_modules(bad)


if __name__ == '__main__':
    unittest.main()
