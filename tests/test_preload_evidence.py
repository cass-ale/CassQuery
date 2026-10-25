"""Constructor/IPC evidence contract models, not native preload execution proof."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import release_evidence as evidence


def preload_model():
    """Typed protocol-only model of the agreed main/probe record, with real source bytes."""
    resources = '/PROTOCOL-ONLY/CassQuery.app/Contents/Resources'
    source = {'files': [{'path': path, 'bytes': (ROOT / path).stat().st_size,
                         'sha256': evidence.digest(ROOT / path)} for path in evidence.NATIVE_SOURCE_PATHS]}
    source['fingerprint'] = hashlib.sha256(json.dumps({'files': source['files']}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    original = {item['path']: item for item in source['files']}
    preload = original['shell/preload.cjs']
    identity = {'schema': 1, 'method': 'owned BrowserWindow constructor',
                'path': resources + '/app.asar/shell/preload.cjs', 'bytes': preload['bytes'],
                'sha256': preload['sha256'], 'web_contents_id': 20, 'resources_path': resources}
    settings = {'schema': 1, 'theme': 'system', 'editorFont': 'system-mono', 'editorFontSize': 14, 'tabSize': 4,
                'lineWrap': False, 'editorVisible': True, 'autoBuild': False, 'reducedMotion': 'system',
                'updateCheck': 'enabled', 'updateAutoDownload': False, 'updateChannel': 'stable', 'recentProjects': []}
    root = '/PROTOCOL-ONLY/projects/WORKBENCHPLATE'
    listing = {'project': 'WORKBENCHPLATE', 'root': root, 'entry': 'part_entry.py', 'source': 'part_entry.py',
               'read_only': False, 'files': [{'path': 'part_entry.py', 'bytes': 100, 'kind': 'python'},
                                           {'path': 'viewer.json', 'bytes': 100, 'kind': 'json'}]}
    shared = {'resources_path': resources, 'worker_origin': 'http://127.0.0.1:3939', 'window_id': 10,
              'web_contents_id': 20, 'project_root': root, 'preload_identity': identity, 'native_source': source}
    check = {'id': 'preload-ipc', 'pass': True, 'evidence': {
        'node_integration': False, 'context_isolation': True, 'sandbox': True,
        'bridge_present': True, 'project_root_matches': True, 'preload_identity': copy.deepcopy(identity),
        'bridge': {'keys': ['onCommand', 'recovery', 'setDirty', 'settings', 'update', 'workbench'],
                   'function_types': ['function'] * 6, 'node_require_type': 'undefined', 'node_process_type': 'undefined'},
        'ipc': {'method': 'actual contextBridge invoke', 'window_id': 10, 'web_contents_id': 20,
                'origin': shared['worker_origin'], 'settings_result': settings, 'workbench_result': listing,
                'workbench_http_status': 200, 'workbench_http_response': copy.deepcopy(listing)}}}
    main = copy.deepcopy(shared)
    result = {**copy.deepcopy(shared), 'checks': [check], 'worker_requests': [
        {'action': 'list', 'project': 'WORKBENCHPLATE', 'status': 200, 'response': copy.deepcopy(listing)}]}
    return main, result, original


class PreloadEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.main, self.result, self.original = preload_model()
        evidence.validate_preload_proof(self.main, self.result, self.original)

    def validate(self, main=None, result=None):
        return evidence.validate_preload_proof(main or self.main, result or self.result, self.original)

    def test_absent_getter_preload_fields_do_not_hide_actual_constructor_evidence(self):
        self.assertNotIn('preload', self.result)
        self.assertNotIn('preloadURL', self.result)
        self.validate()

    def test_missing_identity_wrong_method_path_or_source_bytes_reject(self):
        changes = [lambda m, r: m.pop('preload_identity'),
                   lambda m, r: m['preload_identity'].update(method='basename matched'),
                   lambda m, r: m['preload_identity'].update(path='/FOREIGN/app.asar/shell/preload.cjs'),
                   lambda m, r: m['preload_identity'].update(path='file:///wrong/preload.cjs'),
                   lambda m, r: m['preload_identity'].update(sha256='0' * 64),
                   lambda m, r: m['preload_identity'].update(bytes=True),
                   lambda m, r: m['preload_identity'].update(url='file:///extra/unapproved-alias')]
        for change in changes:
            main, result = copy.deepcopy(self.main), copy.deepcopy(self.result)
            change(main, result)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(main, result)

    def test_current_window_web_contents_and_resource_root_must_agree(self):
        for field, value in (('window_id', 999), ('web_contents_id', 999), ('resources_path', '/other/Contents/Resources'),
                             ('worker_origin', 'http://127.0.0.1:3940'), ('project_root', '/other/project')):
            result = copy.deepcopy(self.result); result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(result=result)
        result = copy.deepcopy(self.result); result['checks'][0]['evidence']['ipc']['web_contents_id'] = True
        with self.assertRaises(ValueError):
            self.validate(result=result)

    def test_bridge_methods_node_globals_and_security_flags_are_not_optional(self):
        changes = [lambda e: e['bridge']['keys'].pop(), lambda e: e['bridge']['function_types'].__setitem__(0, 'object'),
                   lambda e: e['bridge'].update(node_require_type='function'), lambda e: e['bridge'].update(node_process_type='object'),
                   lambda e: e.update(node_integration=True), lambda e: e.update(context_isolation=False),
                   lambda e: e.update(sandbox=False), lambda e: e.pop('preload_identity')]
        for change in changes:
            result = copy.deepcopy(self.result); change(result['checks'][0]['evidence'])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(result=result)

    def test_real_typed_settings_and_worker_http_evidence_are_required(self):
        changes = [lambda ipc: ipc.update(method='simulated provider'),
                   lambda ipc: ipc['settings_result'].update(editorFontSize=True),
                   lambda ipc: ipc['settings_result'].pop('autoBuild'),
                   lambda ipc: ipc.update(workbench_http_status=403),
                   lambda ipc: ipc['workbench_http_response'].update(project='OTHER'),
                   lambda ipc: ipc['workbench_result'].update(root='/wrong/root'),
                   lambda ipc: ipc['workbench_result'].update(read_only=True),
                   lambda ipc: ipc['workbench_result']['files'].clear()]
        for change in changes:
            result = copy.deepcopy(self.result); change(result['checks'][0]['evidence']['ipc'])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(result=result)

    def test_both_forged_nested_responses_cannot_replace_the_observed_http_trace(self):
        result = copy.deepcopy(self.result)
        ipc = result['checks'][0]['evidence']['ipc']
        ipc['workbench_result']['files'][0]['bytes'] += 1
        ipc['workbench_http_response'] = copy.deepcopy(ipc['workbench_result'])
        with self.assertRaisesRegex(ValueError, 'HTTP observations'):
            self.validate(result=result)
        result = copy.deepcopy(self.result); result['worker_requests'] = []
        with self.assertRaisesRegex(ValueError, 'HTTP observations'):
            self.validate(result=result)

    def test_entry_manifest_types_and_positive_bytes_bind_all_three_response_copies(self):
        # Neither self-equal IPC copies nor a matching trace can turn empty/asset
        # entry or manifest records into the required positive source files.
        for path, field, value in (('part_entry.py', 'bytes', 0), ('part_entry.py', 'kind', 'asset'),
                                   ('viewer.json', 'bytes', 0), ('viewer.json', 'kind', 'asset')):
            result = copy.deepcopy(self.result)
            ipc = result['checks'][0]['evidence']['ipc']
            next(item for item in ipc['workbench_result']['files'] if item['path'] == path)[field] = value
            ipc['workbench_http_response'] = copy.deepcopy(ipc['workbench_result'])
            result['worker_requests'][0]['response'] = copy.deepcopy(ipc['workbench_result'])
            with self.subTest(path=path, field=field), self.assertRaisesRegex(ValueError, 'positive typed source files'):
                self.validate(result=result)
        # Empty ordinary helpers are legitimate; the restriction is only the
        # selected public entry and viewer.json, not every project file.
        result = copy.deepcopy(self.result)
        ipc = result['checks'][0]['evidence']['ipc']
        ipc['workbench_result']['files'].append({'path': 'empty_helper.py', 'kind': 'python', 'bytes': 0})
        ipc['workbench_http_response'] = copy.deepcopy(ipc['workbench_result'])
        result['worker_requests'][0]['response'] = copy.deepcopy(ipc['workbench_result'])
        self.validate(result=result)

    def test_rehashed_full_candidate_cannot_hide_bad_constructor_bridge_or_http_evidence(self):
        import tempfile
        import test_release_publish as fixtures
        _, baseline = fixtures.candidate_bytes('arm64')
        def wrong_path(main):
            for identity in (main['preload_identity'], main['result']['preload_identity'],
                             main['result']['checks'][0]['evidence']['preload_identity']):
                identity['path'] = '/FOREIGN/app.asar/shell/preload.cjs'
        def forged_http(main):
            ipc = main['result']['checks'][0]['evidence']['ipc']
            ipc['workbench_result']['files'][0]['bytes'] += 1
            ipc['workbench_http_response'] = copy.deepcopy(ipc['workbench_result'])
        def invalid_entry(main):
            ipc = main['result']['checks'][0]['evidence']['ipc']
            entry = next(item for item in ipc['workbench_result']['files'] if item['path'] == 'part_entry.py')
            entry.update(bytes=0, kind='asset')
            ipc['workbench_http_response'] = copy.deepcopy(ipc['workbench_result'])
            main['result']['worker_requests'][0]['response'] = copy.deepcopy(ipc['workbench_result'])
        def invalid_manifest(main):
            ipc = main['result']['checks'][0]['evidence']['ipc']
            entry = next(item for item in ipc['workbench_result']['files'] if item['path'] == 'viewer.json')
            entry.update(bytes=0, kind='asset')
            ipc['workbench_http_response'] = copy.deepcopy(ipc['workbench_result'])
            main['result']['worker_requests'][0]['response'] = copy.deepcopy(ipc['workbench_result'])
        changes = [wrong_path,
                   lambda main: main['result']['checks'][0]['evidence']['bridge']['keys'].pop(),
                   lambda main: main['result'].update(web_contents_id=999), forged_http, invalid_entry, invalid_manifest]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory(prefix='preload-physical-PROTOCOL-') as directory:
                root = Path(directory).resolve()
                for name, data in baseline.items():
                    file = root / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(data)
                manifest = json.loads(baseline['candidate.json'])
                kwargs = dict(expected_sha=fixtures.SHA, expected_run_id=fixtures.RUN_ID,
                              expected_arch='arm64', expected_attempt=1, expected_version=fixtures.VERSION)
                evidence.verify_candidate(root, manifest, **kwargs)
                folder = root / 'evidence/workbench-arm64'
                main_file = folder / 'proof/native-workbench-main.json'
                main = json.loads(main_file.read_bytes()); change(main)
                main_file.write_bytes(json.dumps(main, sort_keys=True).encode())
                editor_file = folder / 'workbench-native.json'
                editor = json.loads(editor_file.read_bytes())
                editor['native_main'].update(bytes=main_file.stat().st_size, sha256=evidence.digest(main_file))
                editor_file.write_bytes(json.dumps(editor, sort_keys=True).encode())
                outer_file = folder / 'workbench-qualification.json'
                outer = json.loads(outer_file.read_bytes())
                outer['editor_component'].update(bytes=editor_file.stat().st_size, sha256=evidence.digest(editor_file))
                outer_file.write_bytes(json.dumps(outer, sort_keys=True).encode())
                updated = {file.relative_to(root).as_posix(): evidence.file_record(root, file.relative_to(root).as_posix())
                           for file in (main_file, editor_file, outer_file)}
                manifest['files'] = [updated.get(record['path'], record) for record in manifest['files']]
                manifest['gates']['workbench'] = updated[outer_file.relative_to(root).as_posix()]
                with self.assertRaises(ValueError):
                    evidence.verify_candidate(root, manifest, **kwargs)

    def test_rehashed_constructor_identity_cannot_replace_original_source(self):
        main, result = copy.deepcopy(self.main), copy.deepcopy(self.result)
        sha = hashlib.sha256(b'foreign preload').hexdigest()
        main['preload_identity']['sha256'] = sha
        result['preload_identity']['sha256'] = sha
        result['checks'][0]['evidence']['preload_identity']['sha256'] = sha
        next(item for item in main['native_source']['files'] if item['path'] == 'shell/preload.cjs')['sha256'] = sha
        with self.assertRaisesRegex(ValueError, 'original release source'):
            self.validate(main, result)


if __name__ == '__main__':
    unittest.main()
