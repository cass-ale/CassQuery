"""v0.2 proof-consumer regressions: physical PROTOCOL fixtures, never native install proof.

No Electron launch, OS dialog, updater fetch, signing, project execution or GUI is
mocked into a passing native run. These fixtures exercise only the pure validator.
The real update_smoke runner has no simulated-success path.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import release_evidence
spec = importlib.util.spec_from_file_location('update_evidence', ROOT / 'tools/update_smoke.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def png(width=320, height=240):
    rng = random.Random(90)
    raw = b''.join(b'\0' + rng.randbytes(width * 3) for _ in range(height))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


class UpdateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cq-update-protocol-')
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.sha = 'a' * 40
        self.challenge = 'b' * 64
        self.app = '/private/var/owned-protocol-only/CassQuery.app'
        self.start = {'monotonic_ms': 100, 'wall_time_ms': 1000}
        self.finish = {'monotonic_ms': 900, 'wall_time_ms': 9000}
        source_files = [{'path': name, 'bytes': (ROOT / name).stat().st_size,
            'sha256': hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), 'executable': False}
            for name in release_evidence.NATIVE_SOURCE_PATHS]
        source = {'schema': 1, 'source_sha': self.sha, 'clean': True, 'files': source_files}
        source['fingerprint'] = hashlib.sha256(json.dumps({'source_sha': self.sha, 'files': source_files}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.report = {'schema': 1, 'kind': update.KIND, 'pass': True, 'native_execution': True, 'fixture_only': True,
            'source_sha': self.sha, 'source_fingerprint': source['fingerprint'], 'after_fingerprint': source['fingerprint'],
            'source_before': self.put('source-before.json', source), 'version': '0.2.0', 'fixture_version': '0.2.1',
            'architecture': 'arm64', 'challenge': self.challenge, 'cleanup_complete': True, 'input_app_unchanged': True, 'cases': []}
        native_files = [{key: value for key, value in r.items() if key != 'executable'} for r in source_files]
        self.native_source = {'files': native_files, 'fingerprint': hashlib.sha256(json.dumps({'files': native_files}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        for index, name in enumerate(('install', 'rollback')):
            token = hashlib.sha256(('protocol-only-' + name).encode()).hexdigest()
            case = {'name': name, 'status': 'checked', 'helper_default_operations': True, 'helper_exit': 0 if name == 'install' else 1,
                'app': self.app, 'started': self.start, 'finished': self.finish, 'token_fingerprint': token, 'helper_pid': 50 + index,
                'expected_app_hash': 'd' * 64, 'actual_app_hash': 'd' * 64, 'codesign_verified': True,
                'failed_group_members_after_restore': [], 'protected_before': [], 'protected_after': []}
            for key in sorted(update.PROTECTED_KEYS):
                payload = ('public PROTOCOL-ONLY ' + key).encode()
                case['protected_before'].append({'key': key, **self.put(name + '/before/' + key, payload)})
                case['protected_after'].append({'key': key, **self.put(name + '/after/' + key, payload)})
            initial = self.ready(name, 100 + index * 10, '0.2.0')
            current = self.ready(name, 200 + index * 20, '0.2.1' if name == 'install' else '0.2.0')
            case['initial_ready'] = self.put(name + '/initial.json', initial)
            case['ready'] = self.put(name + '/ready.json', current)
            case['images'] = [self.image_records[r['images'][0]['path']] for r in (initial, current)]
            ack = {'pid': current['app_pid'], 'version': current['version'], 'arch': 'arm64', 'app': self.app,
                'token_fingerprint': token, 'rollback': name == 'rollback'}
            result = {'token_fingerprint': token, 'pass': name == 'install', 'version': '0.2.1',
                'updatedPid': current['app_pid'] if name == 'install' else 210, 'pgid': current['app_pid'],
                'updatedGroup': 210, 'updatedExit': 1, 'updatedSignal': None, 'updatedReaped': True, 'updatedGroupGone': True,
                'restoredPid': current['app_pid'], 'rollbackRestored': True, 'rollbackAcknowledged': True, 'rollbackError': None}
            case['request'] = self.put(name + '/request.json', {'schema': 1, 'token_fingerprint': token, 'oldPid': initial['app_pid'],
                'app': self.app, 'arch': 'arm64', 'currentVersion': '0.2.0', 'version': '0.2.1', 'newHash': 'd' * 64, 'oldHash': 'd' * 64})
            case['inspection'] = self.put(name + '/inspection.json', {'version': current['version'], 'arch': 'arm64',
                'app': self.app, 'inventory': {'sha256': 'd' * 64}})
            case['ack'] = self.put(name + '/ack.json', ack)
            case['result'] = self.put(name + '/result.json', result)
            self.report['cases'].append(case)
        self.validate()
        self.baseline_files = {p.relative_to(self.output).as_posix(): p.read_bytes() for p in self.output.rglob('*') if p.is_file()}

    def reset(self):
        for name, data in self.baseline_files.items():
            (self.output / name).write_bytes(data)
        self.validate()

    def put(self, name, value):
        file = self.output / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value if isinstance(value, bytes) else (json.dumps(value, sort_keys=True) + '\n').encode())
        return update.record(file, self.output)

    def ready(self, name, pid, version):
        if not hasattr(self, 'image_records'):
            self.image_records = {}
        file = self.output / f'ready-{pid}.png'
        item = self.put(file.relative_to(self.output).as_posix(), png())
        self.image_records[str(file)] = item
        return {'schema': 1, 'pass': True, 'source_sha': self.sha, 'version': version, 'architecture': 'arm64',
            'app_pid': pid, 'worker_pid': pid + 1, 'challenge': self.challenge,
            'resources_path': self.app + '/Contents/Resources', 'worker_executable': self.app + '/Contents/Resources/worker/cassquery-worker',
            'packaged': True, 'bundled_worker': True, 'errors': [], 'started': {'monotonic_ms': 200, 'wall_time_ms': 2000},
            'checkpoints': [{'name': 'desktop-ready', 'monotonic_ms': 300, 'wall_time_ms': 3000}], 'native_source': self.native_source,
            'worker_status': {'api_version': 2, 'load_errors': [], 'projects': [{'name': 'TESTPLATE', 'state': 'certified',
                'gates_pass': True, 'hashes': {'certified': 'a' * 16}}]},
            'model': {'tier': 'certified', 'hash': 'a' * 16, 'gates': {'pass': True,
                'checks': [{'id': key, 'status': 'pass'} for key in sorted(update.GATE_IDS)]}},
            'render_backend_requested': 'swiftshader-requested',
            'graphics': {'runtime': {'electron': '45.0.0', 'chromium': 'protocol-only', 'node': '22.23.4', 'platform': 'darwin', 'architecture': 'arm64'},
                'display': {'scale_factor': 1, 'bounds': {'x': 0, 'y': 0, 'width': 320, 'height': 240},
                    'work_area': {'x': 0, 'y': 0, 'width': 320, 'height': 240}, 'window_bounds': {'x': 0, 'y': 0, 'width': 320, 'height': 240}},
                'gpu': {'protocol_fixture': True}, 'webgl': {'context': 'webgl2', 'vendor': 'protocol', 'renderer': 'protocol', 'version': 'WebGL 2.0',
                    'shading_language': 'GLSL ES 3.0', 'dpr': 1, 'is_context_lost': False, 'unmasked_renderer': 'SwiftShader PROTOCOL fixture',
                    'drawing_buffer': [320, 240], 'viewport': [320, 240]}},
            'images': [{'path': str(file), 'sha256': item['sha256'], 'width': 320, 'height': 240}]}

    def validate(self, report=None):
        return update.validate_report(report or self.report, self.output, expected_sha=self.sha, expected_arch='arm64', expected_version='0.2.0')

    def rewrite(self, case, field, mutation):
        item = case[field]
        file = self.output / item['path']
        data = update.load_json(file)
        mutation(data)
        case[field] = self.put(item['path'], data)

    def test_complete_protocol_consumer_passes_but_is_not_native_execution(self):
        self.validate()
        with patch.object(update.platform, 'system', return_value='Linux'):
            with self.assertRaisesRegex(ValueError, 'real macOS'):
                update.run(Path('/not-an-app'), self.output, 'arm64', 'node', '0.2.1')

    def test_missing_failed_or_mocked_scenario_rejects(self):
        for change in (lambda r: r.update(pass_=False), lambda r: r.update(native_execution=False),
                       lambda r: r.update(fixture_only=False), lambda r: r.update(cleanup_complete=False),
                       lambda r: r['cases'].pop(), lambda r: r['cases'][0].update(helper_default_operations=False),
                       lambda r: r['cases'][1].update(status='skipped'), lambda r: r['cases'][0].update(expected_app_hash='')):
            report = copy.deepcopy(self.report)
            change(report)
            if 'pass_' in report:
                report['pass'] = report.pop('pass_')
            with self.assertRaises(ValueError):
                self.validate(report)

    def test_actual_ready_context_cannot_be_replaced_by_label(self):
        changes = [lambda r: r.update(app_pid=True), lambda r: r.update(worker_pid=r['app_pid']),
            lambda r: r.update(source_sha='c' * 40), lambda r: r.update(version='9.9.9'),
            lambda r: r.update(architecture='x64'), lambda r: r.update(challenge='c' * 64),
            lambda r: r.update(resources_path='/unrelated'), lambda r: r.update(bundled_worker=False),
            lambda r: r.update(errors=['renderer crashed']), lambda r: r['model'].update(tier='preview'),
            lambda r: r['model']['gates']['checks'].pop(), lambda r: r['checkpoints'][0].update(monotonic_ms=0),
            lambda r: r['graphics']['webgl'].update(is_context_lost=True),
            lambda r: r['graphics']['webgl'].update(unmasked_renderer='hardware driver'),
            lambda r: r.pop('native_source'), lambda r: r.pop('images'),
            lambda r: r['worker_status']['projects'][0]['hashes'].update(certified='c' * 16)]
        for change in changes:
            with self.subTest(change=change):
                self.reset()
                report = copy.deepcopy(self.report)
                self.rewrite(report['cases'][0], 'ready', change)
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    self.validate(report)

    def test_rehashed_ack_and_result_cannot_forge_restart_or_rollback(self):
        cases = [('request', 0, lambda r: r.update(oldPid=999)), ('inspection', 0, lambda r: r.update(version='9.9.9')),
                 ('request', 1, lambda r: r.update(oldHash='e' * 64)),
                 ('ack', 0, lambda r: r.update(pid=999)), ('ack', 0, lambda r: r.update(version='0.2.0')),
                 ('ack', 1, lambda r: r.update(rollback=False)), ('result', 0, lambda r: r.update(updatedPid=999)),
                 ('result', 1, lambda r: r.update(updatedReaped=False)), ('result', 1, lambda r: r.update(updatedGroupGone=False)),
                 ('result', 1, lambda r: r.update(rollbackError='child still running')), ('result', 1, lambda r: r.update(rollbackAcknowledged=False))]
        for field, index, change in cases:
            with self.subTest(field=field, index=index):
                self.reset()
                report = copy.deepcopy(self.report)
                self.rewrite(report['cases'][index], field, change)
                with self.assertRaises(ValueError):
                    self.validate(report)

    def test_source_state_and_protected_files_are_physical_not_counts(self):
        report = copy.deepcopy(self.report)
        after = report['cases'][0]['protected_after'][0]
        report['cases'][0]['protected_after'][0] = {'key': after['key'], **self.put(after['path'], b'changed physical bytes')}
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.validate(report)
        report = copy.deepcopy(self.report)
        report['cases'][0]['protected_before'] = report['cases'][0]['protected_before'][:1]
        report['cases'][0]['protected_after'] = report['cases'][0]['protected_after'][:1]
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.validate(report)

    def test_full_decoded_ready_frame_must_cover_realized_viewport(self):
        report = copy.deepcopy(self.report)
        case = report['cases'][0]
        ready = update.load_json(self.output / case['ready']['path'])
        target = Path(ready['images'][0]['path'])
        item = self.put(target.relative_to(self.output).as_posix(), png(100, 100))
        ready['images'][0].update(sha256=item['sha256'], width=100, height=100)
        case['ready'] = self.put(case['ready']['path'], ready)
        case['images'] = [item if r['path'] == item['path'] else r for r in case['images']]
        with self.assertRaisesRegex(ValueError, 'viewport'):
            self.validate(report)

    def test_initial_source_frames_and_attempts_are_independent_physical_proof(self):
        def wrong_initial_source(value):
            value['native_source']['files'][0]['sha256'] = 'e' * 64
            value['native_source']['fingerprint'] = hashlib.sha256(json.dumps({'files': value['native_source']['files']}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        for field, change in (('initial_ready', lambda value: value.pop('images')), ('initial_ready', wrong_initial_source),
                              ('ready', lambda value: value.pop('images'))):
            self.reset()
            report = copy.deepcopy(self.report)
            self.rewrite(report['cases'][0], field, change)
            with self.assertRaises(ValueError):
                self.validate(report)
        self.reset()
        report = copy.deepcopy(self.report)
        reused = report['cases'][0]['token_fingerprint']
        report['cases'][1]['token_fingerprint'] = reused
        for field in ('request', 'ack', 'result'):
            self.rewrite(report['cases'][1], field, lambda value: value.update(token_fingerprint=reused))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            self.validate(report)

    def test_each_ready_frame_uses_its_own_viewport(self):
        report = copy.deepcopy(self.report)
        self.rewrite(report['cases'][0], 'ready', lambda value: value['graphics']['webgl'].update(viewport=[200, 180]))
        self.validate(report)
        self.rewrite(report['cases'][0], 'initial_ready', lambda value: value['graphics']['webgl'].update(viewport=[400, 300]))
        with self.assertRaisesRegex(ValueError, 'viewport'):
            self.validate(report)

    def test_explicit_model_facade_supports_both_architectures(self):
        source = update.load_json(self.output / 'source-before.json')
        for arch in ('arm64', 'x64'):
            folder = self.output / ('MODEL-' + arch)
            report = update_protocol_fixture(folder, source, arch=arch, version='0.2.0')
            self.assertTrue(report['fixture_only'])
            update.validate_report(report, folder, expected_sha=self.sha, expected_arch=arch, expected_version='0.2.0')

    def test_native_copy_budget_is_explicit_and_bounded(self):
        from unittest.mock import patch
        with patch.object(update, 'command', return_value=b'{}') as run:
            update.node_json('node', 'fixed script', [], timeout=600)
            self.assertEqual(run.call_args.kwargs['timeout'], 600)
        source = (ROOT / 'tools/update_smoke.py').read_text()
        workflow = (ROOT / '.github/workflows/macos-candidate.yml').read_text()
        self.assertLess(workflow.index('npx --no-install install-electron'), workflow.index('Cheap fail-fast preflight'))
        self.assertIn('npm ci\n          npx --no-install install-electron', workflow)
        self.assertIn('require(selector.select(300)', source)
        self.assertIn('controller.wait(timeout=600)', source)
        self.assertIn('fixture_version], timeout=600)', source)

    def test_private_control_tokens_are_not_published_in_nested_receipts(self):
        file = self.output / 'private-control.json'
        token = 'c' * 64
        file.write_text(json.dumps({'token': token, 'readyAcknowledgement': {'token': token, 'pid': 200}}))
        value, item = update.redact_control(file, self.output, 'public-receipt.json')
        self.assertNotIn(token, (self.output / item['path']).read_text())
        self.assertEqual(value['token_fingerprint'], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(value['readyAcknowledgement']['token_fingerprint'], value['token_fingerprint'])


class NativeWorkbenchConformanceTests(unittest.TestCase):
    """Independent full-qualifier MODELS, never real chooser/IPC/native execution."""

    @staticmethod
    def refresh(directory, report, full):
        proof = directory / 'proof'
        def file_record(file):
            data = file.read_bytes()
            return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        full_file = proof / 'workbench-qualification.json'
        full_file.write_text(json.dumps(full, sort_keys=True) + '\n')
        main_file = proof / 'native-workbench-main.json'
        main = update.load_json(main_file)
        main['result']['qualification'].update(file_record(full_file))
        main_file.write_text(json.dumps(main, sort_keys=True) + '\n')
        editor_file = directory / 'workbench-native.json'
        editor = update.load_json(editor_file)
        editor['native_main'].update(file_record(main_file))
        editor_file.write_text(json.dumps(editor, sort_keys=True) + '\n')
        report['native_qualification'].update(file_record(full_file))
        report['editor_component'].update(file_record(editor_file))

    def test_full_source_model_does_not_qualify_editor_only_or_blocked(self):
        import workbench_native_smoke as native
        from test_workbench_contract import native_qualification_model
        with tempfile.TemporaryDirectory(prefix='cq-wb-admission-model-') as temporary:
            directory = Path(temporary)
            report = native_qualification_model(directory)
            native.validate_qualification(report, directory, expected_sha='a' * 40, expected_arch='arm64')
            report['qualifies_workbench'] = False
            with self.assertRaises((RuntimeError, ValueError)):
                native.validate_qualification(report, directory, expected_sha='a' * 40, expected_arch='arm64')

    def test_independent_original_four_rehashed_full_proof_false_greens_reject(self):
        import workbench_native_smoke as native
        from test_workbench_contract import native_qualification_model
        for label in ('images', 'source', 'action-id', 'open-path'):
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix='cq-wb-negative-model-') as temporary:
                directory = Path(temporary)
                report = native_qualification_model(directory)
                native.validate_qualification(report, directory, expected_sha='a' * 40, expected_arch='arm64')
                full = update.load_json(directory / 'proof/workbench-qualification.json')
                if label == 'images':
                    full['images'] = []
                elif label == 'source':
                    report.pop('source_fingerprint')
                    report.pop('after_fingerprint')
                elif label == 'action-id':
                    full['native_dialog_selection']['records'][0]['os_input']['id'] = 9999
                else:
                    next(c for c in full['checks'] if c['id'] == 'native-open-file')['evidence']['path'] = '/parser-only/unrelated-selected.py'
                self.refresh(directory, report, full)  # Exact full -> main -> editor -> outer physical rehash.
                with self.assertRaises((RuntimeError, ValueError)):
                    native.validate_qualification(report, directory, expected_sha='a' * 40, expected_arch='arm64')


def update_protocol_fixture(directory, source_before, *, arch, version):
    """Write explicit MODEL records for consumer tests; NEVER native execution proof.

    Packaging tests may reuse this exact schema. The native runner never calls it.
    Returns a report plus physical files at directory, with all links rehashed.
    """
    require = update.require
    require(arch in ('arm64', 'x64'), 'invalid fixture architecture')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    model = UpdateEvidenceTests('test_complete_protocol_consumer_passes_but_is_not_native_execution')
    model.setUp()
    try:
        source_by_path = {item['path']: item for item in source_before['files']}
        native_files = [{key: source_by_path[path][key] for key in ('bytes', 'path', 'sha256')}
                        for path in release_evidence.NATIVE_SOURCE_PATHS]
        native = {'files': native_files, 'fingerprint': hashlib.sha256(json.dumps({'files': native_files}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        pieces = list(map(int, version.split('.')))
        fixture_version = f'{pieces[0]}.{pieces[1]}.{pieces[2] + 1}'
        def adapt(value):
            if isinstance(value, dict):
                result = {key: adapt(item) for key, item in value.items()}
                if 'native_source' in result:
                    result['native_source'] = native
                return result
            if isinstance(value, list):
                return [adapt(item) for item in value]
            if value == model.sha:
                return source_before['source_sha']
            if value == 'arm64':
                return arch
            if value == '0.2.0':
                return version
            if value == '0.2.1':
                return fixture_version
            if isinstance(value, str) and value.startswith(str(model.output)):
                return '/protocol-artifacts/' + Path(value).name
            return value
        for name, data in model.baseline_files.items():
            file = directory / name
            file.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith('.json'):
                try:
                    value = json.loads(data)
                except ValueError:
                    pass  # Protected opaque fixture bytes are not runner JSON records.
                else:
                    data = (json.dumps(adapt(value), sort_keys=True) + '\n').encode()
            file.write_bytes(data)
        (directory / 'source-before.json').write_text(json.dumps(source_before, sort_keys=True) + '\n')
        report = adapt(model.report)
        report['source_fingerprint'] = report['after_fingerprint'] = source_before['fingerprint']
        def rehash(value):
            if isinstance(value, dict):
                if set(('path', 'sha256', 'bytes')).issubset(value) and isinstance(value['path'], str):
                    file = directory / value['path']
                    if file.is_file() and file.resolve().is_relative_to(directory.resolve()):
                        value.update(update.record(file, directory))
                for item in value.values():
                    rehash(item)
            elif isinstance(value, list):
                for item in value:
                    rehash(item)
        rehash(report)
        update.validate_report(report, directory, expected_sha=source_before['source_sha'], expected_arch=arch, expected_version=version)
        (directory / 'update.json').write_text(json.dumps(report, sort_keys=True) + '\n')
        return report
    finally:
        model.doCleanups()


if __name__ == '__main__':
    unittest.main()
